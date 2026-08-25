"""Exact SVG pipeline: vector parsing, webapp integration, board output."""

import io
import json
import math
import re
import zipfile

import pytest

from minibadge_designer import pcb, svgart
from minibadge_designer.webapp import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def _svg(body: str, w: int = 100, h: int = 100) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'
    ).encode()


# ---- svgart geometry -------------------------------------------------------

def test_is_svg_sniffs_content():
    assert svgart.is_svg(_svg(""))
    assert svgart.is_svg(b'<?xml version="1.0"?>\n<svg xmlns="x"/>')
    assert not svgart.is_svg(b"\x89PNG\r\n\x1a\n")
    assert not svgart.is_svg(None)
    assert not svgart.is_svg(b"<html><svg>")  # html, not an svg document


def test_circle_is_exact():
    regions, frame = svgart.svg_color_regions(
        _svg('<circle cx="50" cy="50" r="40" fill="#000"/>')
    )
    assert frame == (100.0, 100.0)
    [(rgb, geom)] = regions
    assert rgb == (0, 0, 0)
    assert abs(geom.area - math.pi * 40**2) / (math.pi * 40**2) < 0.001


def test_nonzero_and_evenodd_holes():
    # Same-direction nested rings: nonzero fills them, evenodd cuts a hole.
    ring2 = "M10 50 a40 40 0 1 0 80 0 a40 40 0 1 0 -80 0 M30 50 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0"
    for rule, expect in (("nonzero", math.pi * 1600), ("evenodd", math.pi * 1200)):
        regions, _ = svgart.svg_color_regions(
            _svg(f'<path d="{ring2}" fill="red" fill-rule="{rule}"/>')
        )
        [(_rgb, geom)] = regions
        assert abs(geom.area - expect) / expect < 0.002, rule


def test_painter_occlusion_and_color_merge():
    svg = _svg(
        '<rect x="0" y="0" width="60" height="60" fill="#112233"/>'
        '<rect x="30" y="30" width="60" height="60" fill="#ffffff"/>'
        '<rect x="0" y="70" width="10" height="10" fill="#112233"/>'
    )
    regions, _ = svgart.svg_color_regions(svg)
    areas = {rgb: g.area for rgb, g in regions}
    assert abs(areas[(17, 34, 51)] - (3600 - 900 + 100)) < 1e-6  # occluded + merged
    assert abs(areas[(255, 255, 255)] - 3600) < 1e-6


def test_strokes_become_geometry():
    regions, _ = svgart.svg_color_regions(
        _svg('<path d="M10 50 L90 50" stroke="#000" stroke-width="4" fill="none"/>')
    )
    [(_rgb, geom)] = regions
    # 80 x 4 band plus two round caps.
    assert abs(geom.area - (320 + math.pi * 4)) < 1.0


def test_transparent_fill_ignored_and_gradient_raises():
    with pytest.raises(ValueError):
        svgart.svg_color_regions(_svg('<rect width="50" height="50" fill="#000" opacity="0.2"/>'))
    with pytest.raises(ValueError):
        svgart.svg_color_regions(_svg(
            '<defs><linearGradient id="g"><stop offset="0" stop-color="red"/></linearGradient></defs>'
            '<rect width="50" height="50" fill="url(#g)"/>'
        ))


def test_fit_transform_matches_raster_fit():
    from shapely.affinity import affine_transform
    from shapely.geometry import box

    board = (0.16, 0.16, 20.16, 20.16)
    # 200x100 frame at width 12 -> 12 x 6 mm centered at (10, 10).
    m, w, h = svgart.fit_transform((200, 100), 10.0, 10.0, 12.0, board)
    assert (w, h) == (12.0, 6.0)
    g = affine_transform(box(0, 0, 200, 100), m)
    assert [round(v, 6) for v in g.bounds] == [4.0, 7.0, 16.0, 13.0]
    # rot 90: the width slider now applies to the rotated (narrow) axis.
    m, w, h = svgart.fit_transform((200, 100), 10.0, 10.0, 8.0, board, rot=90)
    g = affine_transform(box(0, 0, 200, 100), m)
    assert [round(v, 6) for v in g.bounds] == [6.0, 2.0, 14.0, 18.0]
    # flip mirrors horizontally: a left-half box lands on the right.
    m, w, h = svgart.fit_transform((200, 100), 10.0, 10.0, 12.0, board, flip=True)
    g = affine_transform(box(0, 0, 100, 100), m)
    assert [round(v, 6) for v in g.bounds] == [10.0, 7.0, 16.0, 13.0]


# ---- webapp integration ----------------------------------------------------

def _params(**over):
    params = {"name": "svg-badge", "leds": [], "art": []}
    params.update(over)
    return params


def _post(client, params, files):
    data = {"params": json.dumps(params), **files}
    return client.post("/generate", data=data, content_type="multipart/form-data")


def _board(resp, name="svg-badge"):
    assert resp.status_code == 200, resp.get_json()
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    return zf.read(f"{name}/{name}.kicad_pcb").decode()


def test_svg_art_emits_exact_polygons(client):
    svg = _svg('<circle cx="50" cy="50" r="45" fill="#000"/>')
    params = _params(art=[{"mode": "threshold", "material": "silk",
                           "cx": 10.16, "cy": 10.16, "w": 10}])
    board = _board(_post(client, params, {"art0": (io.BytesIO(svg), "logo.svg")}))
    polys = re.findall(r"\(gr_poly \(pts ((?:\(xy [-\d. ]+\) ?)+)\)", board)
    assert polys, "no art polygons emitted"
    # An exact circle flattens to many vertices, nothing like a 4-point rect
    # grid, and every raster rect signature (axis-aligned 4-pointers) is gone.
    counts = [p.count("(xy") for p in polys]
    assert max(counts) > 40
    # Vertex radii match the placed circle (r = 4.5 mm at width 10 of a 100-frame).
    pts = re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", polys[0])
    for x, y in pts[:20]:
        r = math.hypot(float(x) - 110.16, float(y) - 110.16)
        assert abs(r - 4.5) < 0.02


def test_svg_palette_and_wand_override(client):
    svg = _svg(
        '<rect x="0" y="0" width="100" height="100" fill="#0000ff"/>'
        '<circle cx="30" cy="50" r="15" fill="#ff0000"/>'
        '<circle cx="70" cy="50" r="15" fill="#ff0000"/>'
    )
    palette = [{"rgb": [0, 0, 255], "material": "silk"},
               {"rgb": [255, 0, 0], "material": "copper"}]
    art = {"mode": "palette", "palette": palette, "cx": 10.16, "cy": 10.16, "w": 14,
           "overrides": [{"u": 0.3, "v": 0.5, "material": "bare"}]}
    board = _board(_post(client, _params(art=[art]),
                         {"art0": (io.BytesIO(svg), "two-dots.svg")}))
    # The left red dot became bare (front+back mask), the right stayed copper.
    assert '(layer "B.Mask")' in board   # bare emits on both mask layers
    assert '(layer "F.Mask")' in board
    assert '(layer "F.SilkS")' in board  # blue background is silk


def test_svg_board_outline_is_exact(client):
    # A silhouette circle Ø matches exactly: the outline ring's points sit on
    # the true circle, not on smoothed 0.25 mm pixel steps.
    svg = _svg('<circle cx="50" cy="50" r="50" fill="#000"/>')
    resp = client.post(
        "/outline",
        data={
            "params": json.dumps({
                "rows": ["top", "bottom"],
                "shape": {"mode": "image", "w": 30, "cx": 10.16, "cy": 10.16},
            }),
            "shape": (io.BytesIO(svg), "round.svg"),
        },
        content_type="multipart/form-data",
    )
    # The Ø30 circle fully contains the pad plates, so the board outline IS
    # the exact circle.
    rings = resp.get_json()["rings"]
    assert rings
    for x, y in rings[0]:
        r = math.hypot(x - 10.16, y - 10.16)
        assert abs(r - 15.0) < 0.03, f"point ({x},{y}) off the exact circle"


def test_svg_gradient_falls_back_to_raster(client):
    svg = _svg(
        '<defs><linearGradient id="g"><stop offset="0" stop-color="#000"/>'
        '<stop offset="1" stop-color="#000"/></linearGradient></defs>'
        '<rect x="10" y="10" width="80" height="80" fill="url(#g)"/>'
    )
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (100, 100), "white")
    ImageDraw.Draw(img).rectangle((10, 10, 90, 90), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    params = _params(art=[{"mode": "threshold", "material": "silk",
                           "cx": 10.16, "cy": 10.16, "w": 10}])
    board = _board(_post(client, params, {
        "art0": (io.BytesIO(svg), "grad.svg"),
        "art0_raster": (io.BytesIO(buf.getvalue()), "grad.png"),
    }))
    assert "gr_poly" in board  # raster fallback still produced art
    # Without the fallback the same SVG is a clean 400, not a 500.
    resp = _post(client, params, {"art0": (io.BytesIO(svg), "grad.svg")})
    assert resp.status_code == 400


def test_svg_silk_carved_around_text_exactly(client):
    svg = _svg('<rect x="0" y="0" width="100" height="100" fill="#000"/>')
    params = _params(
        art=[{"mode": "threshold", "material": "silk", "cx": 10.16, "cy": 10.16, "w": 14}],
        texts=[{"x": 10.16, "y": 10.16, "text": "HI", "size": 2.0}],
    )
    board = _board(_post(client, params, {"art0": (io.BytesIO(svg), "block.svg")}))
    polys = re.findall(r"\(gr_poly \(pts ((?:\(xy [-\d. ]+\) ?)+)\)", board)
    assert polys
    # No art vertex may sit inside the text keepout's inner half
    # (webapp._text_keepout: w = 2*2*1.05 + 0.6, h = 3.4, centered 10.16).
    for p in polys:
        for xs, ys in re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", p):
            x, y = float(xs) - 100, float(ys) - 100
            assert not (8.5 < x < 11.8 and 9.3 < y < 11.0), f"vertex ({x},{y}) in text keepout"


def test_svg_glow_window_cuts_pours_exactly():
    spec = pcb.BadgeSpec(
        name="vec-glow",
        leds=[pcb.Led(10.0, 15.5, "red")],
        art=[pcb.ArtLayer("glow", polys=[[[(6.0, 6.0), (14.0, 6.0), (10.0, 12.0)]]])],
    )
    from shapely.geometry import Point

    for layer in ("F.Cu", "B.Cu"):
        for poly in pcb._fill_geometry("3V3" if layer == "F.Cu" else "GND", layer, spec):
            assert not poly.contains(Point(10.0, 8.0)), f"{layer} pour crosses the window"
    out = pcb.generate_pcb(spec)
    assert "gr_poly" not in out  # glow draws nothing


def test_a_ring_of_artwork_prints_as_a_ring():
    """Ink on the whole band, none in the middle, and no hairline across it.

    A ``gr_poly`` cannot carry a hole, so a holed shape has to be broken up --
    and *how* is not the guarantee. This asserted the old mechanism instead
    (one polygon, more than eight vertices, its ring dipping through the void),
    which said nothing about what reached the board and went red when the
    mechanism changed with the artwork unaffected.

    What reaches the board is the guarantee: the band inked whole, the void
    left clear. The old answer scored a 0.02 mm slit from the void out through
    the band -- no fab prints a dam that thin, but KiCad plots and renders one,
    which is how it was noticed, as a line drawn across the "4" of a badge.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    outer = [(5.0, 5.0), (15.0, 5.0), (15.0, 15.0), (5.0, 15.0)]
    void = [(8.0, 8.0), (12.0, 8.0), (12.0, 12.0), (8.0, 12.0)]
    slack = 0.005  # emitted coordinates are rounded to four decimals

    out = pcb._art_vector_items([[outer, void]], "F.SilkS", "t")
    assert out, "a ring of artwork emitted no ink at all"
    rings = [[(float(x) - pcb.ORIGIN, float(y) - pcb.ORIGIN)
              for x, y in re.findall(r"\(xy ([\d.-]+) ([\d.-]+)\)", item)]
             for item in out]
    inked = unary_union([Polygon(r) for r in rings if len(r) >= 3])

    band = Polygon(outer, [void]).buffer(-slack)
    assert inked.contains(band), (
        f"{band.difference(inked).area:.4f} mm^2 of the ring is missing ink "
        f"around {tuple(round(v, 2) for v in band.difference(inked).bounds)}: "
        "a gap scored across artwork the user drew solid")
    assert not inked.intersects(Polygon(void).buffer(-slack)), (
        "ink covers the middle of the ring; the hole the artwork asked for "
        "was flooded shut")


#: A drawing whose middle tenth is a stripe: at a known placed width the
#: stripe has a known width on the board, so a layer that was quietly resized
#: says so in millimetres.
_STRIPE_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    b'<rect x="45" y="0" width="10" height="100" fill="#101010"/></svg>'
)


def _stripe_png() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 400), "white")
    ImageDraw.Draw(img).rectangle((180, 0, 219, 399), fill=(16, 16, 16))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _inked_width(resp, name="over", layer="F.SilkS"):
    """Width of the printed ink, in mm, off a generated board."""
    import invariants

    root = invariants._parse_sexp(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        f"{name}/{name}.kicad_pcb").decode())
    xs = [float(q[1]) - pcb.ORIGIN
          for g in invariants._kids(root, "gr_poly")
          if str(invariants._val(g, "layer")) == layer
          for q in invariants._kids(invariants._kid(g, "pts"), "xy")]
    return (max(xs) - min(xs)) if xs else 0.0


@pytest.mark.parametrize("kind", ["svg", "png"], ids=["svg", "png"])
def test_artwork_wider_than_the_board_keeps_its_size_whatever_it_was_drawn_in(
        client, kind):
    """A vector drawing hangs off the board like a raster one does.

    Overhang is how a picture is lined up with a silhouette board: the layer
    stays the size the user set and the part off the board is clipped. The SVG
    path used to shrink the drawing to fit instead, so the same artwork placed
    the same way came out at a different scale depending on which file the user
    happened to upload -- and the editor, which previews both by overhanging
    them, showed the scale it did not get.
    """
    placed, view = 35.0, 0.10   # the stripe is a tenth of the drawing
    upload = _STRIPE_SVG if kind == "svg" else _stripe_png()
    resp = client.post("/generate", data={
        "params": json.dumps({
            "name": "over", "leds": [], "texts": [],
            "art": [{"mode": "threshold", "cx": 10.16, "cy": 10.16,
                     "w": placed, "material": "silk", "side": "front"}]}),
        "art0": (io.BytesIO(upload), f"stripe.{kind}"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    width = _inked_width(resp)
    want = placed * view
    assert width > 0, f"{kind}: the stripe printed nothing at all"
    # A tenth of 35 mm is 3.5; shrunk to fit a 20.32 mm board it would be 2.0,
    # which is what this is really telling apart. Quarter-millimetre of slack
    # for the tracer's rounding of the two edges.
    assert abs(width - want) < 0.25, (
        f"{kind}: the stripe prints {width:.2f} mm wide where the layer's own "
        f"width puts it at {want:.2f} mm -- the drawing was resized to fit the "
        "board instead of hanging off it")
