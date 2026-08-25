"""End-to-end validation against a real KiCad installation (skipped if absent)."""

import io
import json
import shutil
import subprocess
import zipfile

import pytest

from minibadge_designer import pcb

KICAD_CLI = shutil.which("kicad-cli") or (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    if shutil.which("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    else None
)

pytestmark = pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")


SPECS = {
    "front-back": pcb.BadgeSpec(
        name="drc-check",
        leds=[pcb.Led(6.5, 13.8, "red"), pcb.Led(14.5, 13.8, "blue", side="back")],
        art=[pcb.ArtLayer("silk", [(8.0, 6.0, 4.0, 0.2), (8.0, 6.2, 3.0, 0.2)])],
    ),
    # PCB-art materials: exposed copper, a glow window with a back LED next
    # to it, and a bare-laminate window.
    "pcb-art": pcb.BadgeSpec(
        name="drc-art",
        leds=[pcb.Led(15.5, 10.0, "white", side="back", rot=90)],
        art=[
            pcb.ArtLayer("copper", [(4.0, 4.0, 5.0, 0.2), (4.0, 4.4, 4.0, 0.2)]),
            pcb.ArtLayer("glow", [(4.0, 9.0, 6.0, 3.0)]),
            pcb.ArtLayer("bare", [(4.0, 14.0, 5.0, 2.0)]),
        ],
    ),
    "rotated": pcb.BadgeSpec(
        name="drc-rot",
        leds=[pcb.Led(6.0, 10.0, "red", rot=90), pcb.Led(14.5, 10.0, "green", side="back", rot=270)],
    ),
    # Units clamped hard into the corners of the safe region land on the
    # connector pad pairs; the pad backstop (the same one the webapp runs)
    # must slide them clear, into the strip between the pairs.
    "extremes": pcb.BadgeSpec(
        name="drc-extreme",
        leds=[
            pcb.resolve_pad_overlap(
                pcb.Led(*pcb.clamp_led(0.0, 0.0, 0), color="red", rot=0),
                pcb.ALL_PINS),
            pcb.resolve_pad_overlap(
                pcb.Led(*pcb.clamp_led(99.0, 99.0, 180), color="blue", rot=180),
                pcb.ALL_PINS),
        ],
    ),
    # Inline rows: a front one along the bottom, a rotated back one up the
    # side (clear of the top-left pad pair; direct specs place responsibly).
    "inline": pcb.BadgeSpec(
        name="drc-inline",
        leds=[
            pcb.Led(12.0, 16.0, "red", layout="inline"),
            pcb.Led(4.0, 9.3, "green", side="back", rot=90, layout="inline"),
        ],
    ),
    # A glow band spanning the full interior: the perimeter copper ring must
    # keep both pours in one piece (this once split the GND plane in half).
    "band": pcb.BadgeSpec(
        name="drc-band",
        leds=[pcb.Led(10.0, 15.5, "red")],
        art=[pcb.ArtLayer("glow", [(0.5, 8.0, 19.3, 4.0)])],
    ),
    # Custom outline: top-row-only badge with a tab sticking 4 mm out the
    # top, art in the tab, and an LED; pours must follow the shape.
    "tab": pcb.BadgeSpec(
        name="drc-tab",
        pins=("1", "2", "7", "8"),
        outline=[[
            (0.16, 0.16), (6.0, 0.16), (6.0, -4.0), (14.0, -4.0), (14.0, 0.16),
            (20.16, 0.16), (20.16, 20.16), (0.16, 20.16),
        ]],
        leds=[pcb.Led(10.0, 12.0, "red")],
        art=[pcb.ArtLayer("silk", [(7.0, -3.0, 6.0, 0.2), (7.0, -2.6, 5.0, 0.2)])],
    ),
    # An oversized board well past the old 40 x 44 mm cap (85 x 95 mm),
    # standard connector strips in the middle, art, windows, and LED units
    # far outside the original square, plus a unit tucked into the top
    # connector strip between the two pad pairs.
    "big": pcb.BadgeSpec(
        name="drc-big",
        pins=pcb.ALL_PINS,
        outline=[[
            (-30.0, -35.0), (55.0, -35.0), (55.0, 60.0), (-30.0, 60.0),
        ]],
        leds=[
            pcb.Led(10.0, 12.0, "red"),
            pcb.Led(-20.0, -25.0, "green"),
            pcb.Led(45.0, 50.0, "blue", side="back"),
            pcb.Led(10.16, 1.5, "yellow"),  # between the top mounting holes
        ],
        art=[
            pcb.ArtLayer("silk", [(-7.0, -9.0, 10.0, 0.3), (-7.0, -8.4, 8.0, 0.3)]),
            pcb.ArtLayer("glow", [(22.0, 24.0, 5.0, 4.0)]),
        ],
        texts=[pcb.Text(10.0, 28.0, "big badge", size=2.0)],
    ),
    # Every SMD package on one board. 1206 is what catches silk positioned
    # from the pad centre rather than the pad edge: its hand-solder pads are
    # the widest, so a fixed offset lands on the mask opening and gets clipped.
    "smd-sizes": pcb.BadgeSpec(
        name="drc-smd",
        leds=[
            pcb.Led(5.5, 7.0, "red", size="0603"),
            pcb.Led(14.5, 7.0, "green", size="0805"),
            pcb.Led(10.5, 14.5, "blue", size="1206", side="back", rot=90),
            pcb.Led(5.0, 14.0, "white", size="1206"),
        ],
    ),
    # Through-hole LEDs: all three TH packages (3 mm stacked front, 1.8 mm
    # inline back at an odd angle, 5x2 mm bar) mixed with an SMD unit. TH
    # pad barrels shape BOTH pours.
    "through-hole": pcb.BadgeSpec(
        name="drc-th",
        leds=[
            pcb.Led(6.5, 8.0, "red", size="3mm"),
            pcb.Led(14.5, 13.5, "blue", size="1.8mm", side="back", rot=30,
                    layout="inline"),
            pcb.Led(11.5, 6.0, "white", size="5x2mm", rot=90),
            pcb.Led(5.5, 15.0, "green", size="0805"),
        ],
        texts=[pcb.Text(10.0, 17.5, "TH", size=1.2, side="front")],
    ),
    # A crowd: five LEDs plus user text on both sides. The green unit sits
    # in the top connector strip, between the two pad pairs.
    "crowd": pcb.BadgeSpec(
        name="drc-crowd",
        leds=[
            pcb.Led(4.5, 6.5, "red"),
            pcb.Led(10.5, 4.5, "green"),
            pcb.Led(16.5, 6.5, "blue"),
            pcb.Led(6.0, 11.0, "yellow", side="back"),
            pcb.Led(12.0, 14.0, "white", layout="inline"),
        ],
        texts=[
            pcb.Text(10.16, 9.5, "hax", size=2.5, side="front"),
            pcb.Text(13.0, 8.0, "back", size=1.0, side="back"),
        ],
    ),
    # Parts placed by hand, tucked as close to the header as the placement
    # rule allows: its resistor and via are 12 mm from the LED, so the box
    # around the unit covers a pad pair while its copper stops 0.02 mm short
    # (pcb.unit_footprint). That is the placement the rule now permits and
    # nothing else in this corpus builds, so DRC is the oracle that says the
    # permission is safe rather than merely intended.
    "free-placed": pcb.BadgeSpec(
        name="drc-free",
        leds=[pcb.Led(8.3, 4.1, "red",
                      adv={"rx": 5.0, "ry": 12.0, "vx": 2.0, "vy": 10.0})],
    ),
}


@pytest.mark.parametrize("name", SPECS)
def test_generated_board_passes_kicad_drc(tmp_path, name):
    spec = SPECS[name]
    board = tmp_path / f"{name}.kicad_pcb"
    board.write_text(pcb.generate_pcb(spec))
    (tmp_path / f"{name}.kicad_pro").write_text(pcb.generate_project(name))

    report = tmp_path / "drc.txt"
    result = subprocess.run(
        [
            KICAD_CLI,
            "pcb",
            "drc",
            "--severity-all",
            "--exit-code-violations",
            "-o",
            str(report),
            str(board),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,  # exit code asserted below with the report attached
    )
    assert result.returncode == 0, f"DRC violations:\n{report.read_text()}"


def test_wand_fringe_design_passes_drc(tmp_path):
    """Silk pixels adjacent to wand-assigned bare pixels once produced dozens
    of silk-clipped-by-mask warnings; the carve margin must prevent that."""
    from PIL import Image, ImageDraw

    from minibadge_designer.webapp import app

    img = Image.new("RGB", (240, 240), (250, 210, 40))
    d = ImageDraw.Draw(img)
    d.ellipse((40, 40, 200, 200), fill=(20, 20, 20))
    d.ellipse((85, 90, 125, 125), fill=(250, 210, 40))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    params = {
        "name": "fringe",
        "shape": {"mode": "circle", "d": 18},
        "art": [{
            "mode": "palette", "cx": 10.16, "cy": 10.16, "w": 11,
            "palette": [
                {"rgb": [250, 210, 40], "material": "copper"},
                {"rgb": [20, 20, 20], "material": "silk"},
            ],
            "overrides": [{"u": 0.437, "v": 0.447, "material": "bare"}],
        }],
        "leds": [{"x": 10, "y": 15.3, "color": "red"}],
        "texts": [],
    }
    client = app.test_client()
    resp = client.post("/generate", data={
        "params": json.dumps(params),
        "art0": (io.BytesIO(buf.getvalue()), "fringe.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = tmp_path / "fringe.kicad_pcb"
    board.write_bytes(zf.read("fringe/fringe.kicad_pcb"))
    (tmp_path / "fringe.kicad_pro").write_bytes(zf.read("fringe/fringe.kicad_pro"))
    result = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--severity-all", "--exit-code-violations",
         "-o", str(tmp_path / "drc.txt"), str(board)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, f"DRC violations:\n{(tmp_path / 'drc.txt').read_text()}"


def test_exact_svg_badge_passes_drc(tmp_path):
    """A badge built entirely from SVG vectors (traced board outline plus
    multi-material exact art with a glow window) must be DRC-clean."""
    from minibadge_designer.webapp import app

    shape_svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="110">'
        b'<path d="M50 0 L100 40 L82 110 L18 110 L0 40 Z" fill="#000"/></svg>'
    )
    art_svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        b'<circle cx="50" cy="50" r="46" fill="#0000ff"/>'
        b'<path d="M50 14 a36 36 0 1 0 0 72 a36 36 0 1 0 0 -72 '
        b'M50 30 a20 20 0 1 1 0 40 a20 20 0 1 1 0 -40" fill="#ff0000"/>'
        b'<rect x="44" y="44" width="12" height="12" fill="#00ff00"/></svg>'
    )
    params = {
        "name": "vector",
        "shape": {"mode": "image", "w": 34, "cx": 10.16, "cy": 8.0, "threshold": 128},
        "art": [{
            "mode": "palette", "cx": 10.16, "cy": 8.0, "w": 20,
            "palette": [
                {"rgb": [0, 0, 255], "material": "silk"},
                {"rgb": [255, 0, 0], "material": "copper"},
                {"rgb": [0, 255, 0], "material": "glow"},
            ],
        }],
        "leds": [{"x": 10.16, "y": 22.0, "color": "red"},
                 {"x": 10.16, "y": 4.5, "color": "white", "side": "back"}],
        "texts": [{"x": 10.16, "y": 26.5, "text": "exact", "size": 1.5}],
    }
    client = app.test_client()
    resp = client.post("/generate", data={
        "params": json.dumps(params),
        "shape": (io.BytesIO(shape_svg), "shield.svg"),
        "art0": (io.BytesIO(art_svg), "rings.svg"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = tmp_path / "vector.kicad_pcb"
    board.write_bytes(zf.read("vector/vector.kicad_pcb"))
    (tmp_path / "vector.kicad_pro").write_bytes(zf.read("vector/vector.kicad_pro"))
    text = board.read_text()
    assert "gr_poly" in text
    result = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--severity-all", "--exit-code-violations",
         "-o", str(tmp_path / "drc.txt"), str(board)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, f"DRC violations:\n{(tmp_path / 'drc.txt').read_text()}"


def test_artwork_around_through_hole_led_passes_drc(tmp_path):
    """A logo plus a through-hole LED is the most ordinary design there is,
    and it used to ship silk-on-silk overlaps that KiCad flags."""
    import io
    import json

    from PIL import Image, ImageDraw

    from minibadge_designer.webapp import app

    img = Image.new("RGB", (300, 300), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((20, 20, 280, 280), fill=(20, 20, 20))
    d.ellipse((110, 110, 190, 190), fill=(250, 210, 40))
    buf = io.BytesIO()
    img.save(buf, "PNG")

    for size in ("0805", "1.8mm", "3mm", "5x2mm"):
        params = {
            "name": "thart", "mask_color": "green", "finish": "enig",
            "leds": [{"x": 10.16, "y": 10.16, "color": "red", "side": "front",
                      "rot": 0, "layout": "stacked", "size": size}],
            "texts": [{"x": 10.16, "y": 17.4, "text": "BADGE", "size": 1.4,
                       "side": "front", "font": "kicad", "material": "silk"}],
            "art": [{"mode": "palette", "cx": 10.16, "cy": 10.16, "w": 16,
                     "rot": 0, "side": "front", "palette": [
                         {"rgb": [20, 20, 20], "material": "silk"},
                         {"rgb": [250, 210, 40], "material": "copper"},
                         {"rgb": [255, 255, 255], "material": "ignore"}]}],
        }
        resp = app.test_client().post("/generate", data={
            "params": json.dumps(params),
            "art0": (io.BytesIO(buf.getvalue()), "logo.png"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200, size
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        board = tmp_path / f"{size}.kicad_pcb"
        board.write_text(zf.read(next(n for n in zf.namelist()
                                      if n.endswith(".kicad_pcb"))).decode())
        (tmp_path / f"{size}.kicad_pro").write_text(pcb.generate_project(size))
        report = tmp_path / f"{size}-drc.txt"
        r = subprocess.run(
            [KICAD_CLI, "pcb", "drc", "--severity-all", "--exit-code-violations",
             "-o", str(report), str(board)],
            capture_output=True, text=True, timeout=120, check=False)
        assert r.returncode == 0, f"{size} DRC:\n{report.read_text()}"


@pytest.mark.parametrize("hookup",
                         ["jumper", "jumper-back", "jumper-back-traced",
                          "trace"])
def test_a_blinking_badge_passes_drc_in_both_hookup_styles(tmp_path, hookup):
    """A design running LEDs off the badge clock ships DRC-clean, whichever
    hookup the user picked.

    CLK introduces a whole class of copper no other case contains: a second
    supply net routed as traces across the 3V3 pour, the 3-pad solder jumper,
    a rail via, and a routed link to pin 9. Any clearance, short, or
    unconnected-item defect in that class is exactly what the external oracle
    exists to catch. Built through the webapp path (never hand-fed to
    generate_pcb): de-confliction lives there. Off-default on purpose: a
    rotated 0603 front blinker, a back-side blinker (no 3V3 via), and a
    via-less bystander sharing the board.
    """
    from minibadge_designer.webapp import app

    params = {
        "name": f"clk-{hookup}",
        "leds": [
            {"x": 5.5, "y": 8, "color": "red", "clk": True, "rot": 90,
             "size": "0603"},
            {"x": 14.5, "y": 12, "color": "blue", "side": "back", "clk": True},
            {"x": 15, "y": 5.5, "color": "green", "novia": True},
        ],
        "texts": [],
        "clk": {"jumper": hookup != "trace",
                "side": "back" if hookup.startswith("jumper-back") else "front",
                "via": hookup != "jumper-back-traced"},
    }
    client = app.test_client()
    resp = client.post("/generate", data={"params": json.dumps(params)})
    assert resp.status_code == 200, resp.data
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    slug = f"clk-{hookup}"
    board = tmp_path / f"{slug}.kicad_pcb"
    board.write_bytes(zf.read(f"{slug}/{slug}.kicad_pcb"))
    (tmp_path / f"{slug}.kicad_pro").write_bytes(zf.read(f"{slug}/{slug}.kicad_pro"))
    result = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--severity-all", "--exit-code-violations",
         "-o", str(tmp_path / "drc.txt"), str(board)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, f"DRC violations:\n{(tmp_path / 'drc.txt').read_text()}"


def test_a_refilled_window_still_has_no_copper_under_its_open_mask(tmp_path):
    """A refill does not put copper back inside a light window.

    Every other window check in this suite reads the fill the generator
    *precomputed*, and that fill was never the problem. A zone fill is not a
    fixed artifact: KiCad recomputes it from the keepout rule areas alone the
    moment anyone refills, and this project refills on three paths the user
    cannot see around -- the 3D preview does it before exporting the GLB, the
    Gerber plot does it before plotting (it cannot ship the slits), and the
    README tells people to press B. So the only honest oracle for "is there
    copper in the light window" is a board that has been through KiCad's own
    filler, which is what this runs.

    The board is the one a user reported: a bare "418" in a pixel face, whose
    counters make the window a shape with holes. Emitting the keepout for such
    a shape means breaking it into hole-free pieces, and keeping only the
    largest of them left a 1.6 x 0.7 mm block of the 8 with no rule area:
    kicad-cli DRC passed with 0 violations, the zip and the preview looked
    right, and the refill quietly poured the 3V3 plane into the middle of the
    window under an OPEN mask opening -- bare live copper where light was
    meant to pass, on both faces.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    import invariants
    from minibadge_designer import webapp

    params = {
        "name": "refill-window",
        # Both defaults moved off: a back-mounted inline unit (so the window
        # is carved around real copper from the far face) and a window text
        # rather than the rectangles every other window case here draws.
        "leds": [{"x": 11.9, "y": 15.33, "color": "red", "side": "back",
                  "size": "0805", "layout": "inline"}],
        "texts": [{"x": 9.99, "y": 10.42, "text": "418", "size": 5.887,
                   "font": "pressstart", "material": "bare", "side": "front"}],
    }
    client = webapp.app.test_client()
    resp = client.post("/generate", data={"params": json.dumps(params)})
    assert resp.status_code == 200, resp.data
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    slug = "refill-window"
    board = tmp_path / f"{slug}.kicad_pcb"
    board.write_bytes(zf.read(f"{slug}/{slug}.kicad_pcb"))

    # The app's own refill, not a hand-rolled one: this is the code path the
    # 3D view and the Gerber export take, and it locates its interpreter
    # itself. It returns False when pcbnew is not importable anywhere, which
    # is a skip and not a pass -- a green here on an unrefilled board is
    # exactly the vacuous result this test exists to avoid.
    if not webapp._refill_zones(str(board)):
        pytest.skip("pcbnew not importable; cannot refill (KiCad's python?)")

    text = board.read_text()
    root = invariants._parse_sexp(text)

    def _polys(nodes):
        out = []
        for node in nodes:
            pts = invariants._kid(node, "pts")
            ring = [(float(p[1]) - pcb.ORIGIN, float(p[2]) - pcb.ORIGIN)
                    for p in invariants._kids(pts, "xy")]
            if len(ring) >= 3:
                poly = Polygon(ring)
                out.append(poly if poly.is_valid else poly.buffer(0))
        return out

    for mask_layer, copper_layer in (("F.Mask", "F.Cu"), ("B.Mask", "B.Cu")):
        opening = unary_union(_polys(
            g for g in invariants._kids(root, "gr_poly")
            if invariants._val(g, "layer") == mask_layer))
        assert not opening.is_empty, (
            f"no {mask_layer} opening survived the refill, so this check has "
            "nothing to measure and would pass on any board")
        # Parsed with the s-expression reader on purpose: KiCad 9 rewrites a
        # refilled fill as multi-line blocks carrying `(island)`, which the
        # single-line regex in invariants.Board.emitted_fills does not match.
        # It returns [] on a refilled board, and every assertion built on it
        # then passes vacuously -- measured, on this very board.
        fills = _polys(
            fp for z in invariants._kids(root, "zone")
            for fp in invariants._kids(z, "filled_polygon")
            if invariants._val(fp, "layer") == copper_layer)
        assert fills, f"the refill left no {copper_layer} fill at all"
        exposed = opening.intersection(unary_union(fills))
        assert exposed.is_empty, (
            f"KiCad's refill put {exposed.area:.4f} mm^2 of {copper_layer} "
            f"copper inside the light window (around "
            f"{tuple(round(v, 2) for v in exposed.bounds)}), where "
            f"{mask_layer} is open: the badge ships with bare plated copper "
            "in the middle of the window and DRC will not say a word")


def test_artwork_pushed_past_every_edge_still_passes_drc(tmp_path):
    """Art that hangs off the board is clipped, not shipped over the edge.

    Placing a picture over a board profile means pushing it past the outline on
    purpose, so the clip at the edge is what keeps the board legal: ink or
    copper running into the routed edge is a fab reject and DRC flags it. Four
    layers, one over each edge, plus one bigger than the whole board.
    """
    from PIL import Image, ImageDraw

    from minibadge_designer.webapp import app

    img = Image.new("RGB", (300, 300), (20, 20, 20))
    ImageDraw.Draw(img).ellipse((40, 40, 260, 260), fill=(230, 180, 40))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    png = buf.getvalue()

    # cx/cy well outside the board on each side, and one layer swallowing it.
    places = [(-6.0, 10.16, 24.0), (26.0, 10.16, 24.0),
              (10.16, -6.0, 24.0), (10.16, 26.0, 24.0),
              (10.16, 10.16, 90.0)]
    art = [{"mode": "palette", "cx": cx, "cy": cy, "w": w,
            "palette": [{"rgb": [20, 20, 20], "material": "silk"},
                        {"rgb": [230, 180, 40], "material": "copper"}]}
           for cx, cy, w in places]
    params = {"name": "overhang", "leds": [{"x": 10.16, "y": 10.16, "color": "red"}],
              "texts": [], "art": art}
    data = {"params": json.dumps(params)}
    for i in range(len(art)):
        data[f"art{i}"] = (io.BytesIO(png), f"blob{i}.png")
    client = app.test_client()
    resp = client.post("/generate", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()

    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = tmp_path / "overhang.kicad_pcb"
    board.write_bytes(zf.read("overhang/overhang.kicad_pcb"))
    (tmp_path / "overhang.kicad_pro").write_bytes(
        zf.read("overhang/overhang.kicad_pro"))
    report = tmp_path / "drc.txt"
    result = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--severity-all", "--exit-code-violations",
         "-o", str(report), str(board)],
        capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, f"DRC violations:\n{report.read_text()}"

    # And the ink really is inside: art hanging over the edge has to be gone,
    # not merely legal. Board-level polygons only -- a footprint's own shapes
    # are in ITS frame, and reading those as page coordinates says -99 mm.
    import invariants
    from minibadge_designer import pcb as pcb_mod

    root = invariants._parse_sexp(board.read_text())
    pts = [(float(q[1]) - pcb_mod.ORIGIN, float(q[2]) - pcb_mod.ORIGIN)
           for g in invariants._kids(root, "gr_poly")
           if str(invariants._val(g, "layer")) != "Edge.Cuts"
           for q in invariants._kids(invariants._kid(g, "pts"), "xy")]
    assert pts, "the board carries no artwork polygons at all"
    xs = [x for x, _y in pts]
    ys = [y for _x, y in pts]
    assert min(xs) >= -0.01 and max(xs) <= 20.33, (
        f"artwork x runs {min(xs):.2f}..{max(xs):.2f}, outside the board")
    assert min(ys) >= -0.01 and max(ys) <= 20.33, (
        f"artwork y runs {min(ys):.2f}..{max(ys):.2f}, outside the board")


#: (id, art material, pin captions, how close to a pad centre the artwork is
#: allowed to reach, mm). The floors are stated here rather than read from
#: pcb.PAD_ART_GAP: they are the fab rule this test exists to hold. Silk is
#: 0.875 pad copper + the 0.15 ink gap; "copper" artwork is a mask opening
#: over the pour, so it keeps a 0.5 mm solder-mask dam instead.
_PAD_ART = [
    ("silk-captions-off", "silk", False, 1.02),
    ("silk-captions-on", "silk", True, 1.02),
    ("copper-captions-on", "copper", True, 1.37),
]


@pytest.mark.kicad
@pytest.mark.needs("kicad")
@pytest.mark.parametrize("label,art_material,labels,floor", _PAD_ART,
                         ids=[c[0] for c in _PAD_ART])
def test_artwork_may_print_close_beside_the_connector_pads(
        tmp_path, label, art_material, labels, floor):
    """Art runs up near the pads, and the fab still takes the board.

    Artwork used to stop 0.775 mm short of every connector pad's copper,
    whatever it was made of, which on a 20 mm board is a visible bite out of
    all four corners. What actually limits it is narrower and different per
    material -- ink is clipped where it crosses a pad's mask opening, and an
    exposed-copper drawing has to leave a mask dam so solder cannot walk from
    the pad onto it -- so each is allowed in as far as its own rule permits,
    with DRC as the oracle that the fab still accepts the result.
    """
    import io

    from PIL import Image

    from minibadge_designer import pcb as pcb_mod
    from minibadge_designer.webapp import app

    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (20, 20, 20)).save(buf, "PNG")   # all ink
    png = buf.getvalue()
    params = {
        "name": "beside", "pinlabels": labels,
        "leds": [{"x": 10.16, "y": 10.16, "color": "red", "side": "back"}],
        "texts": [],
        "art": [{"mode": "threshold", "cx": 10.16, "cy": 10.16, "w": 22.0,
                 "material": art_material, "side": side}
                for side in ("front", "back")],
    }
    data = {"params": json.dumps(params)}
    for i in range(2):
        data[f"art{i}"] = (io.BytesIO(png), f"ink{i}.png")
    resp = app.test_client().post("/generate", data=data,
                                  content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = tmp_path / "beside.kicad_pcb"
    board.write_bytes(zf.read("beside/beside.kicad_pcb"))
    (tmp_path / "beside.kicad_pro").write_bytes(zf.read("beside/beside.kicad_pro"))
    report = tmp_path / "drc.txt"
    result = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--severity-all", "--exit-code-violations",
         "-o", str(report), str(board)],
        capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, f"DRC violations:\n{report.read_text()}"

    # And it really did come closer: legal-but-unchanged would pass the check
    # above while the board still wore the old bite out of every corner.
    import invariants

    root = invariants._parse_sexp(board.read_text())
    # Ink prints on the silk layers; an exposed-copper drawing is an opening
    # in the mask, so that is where its outline lives.
    face = "SilkS" if art_material == "silk" else "Mask"
    silk = [[(float(q[1]) - pcb_mod.ORIGIN, float(q[2]) - pcb_mod.ORIGIN)
             for q in invariants._kids(invariants._kid(g, "pts"), "xy")]
            for g in invariants._kids(root, "gr_poly")
            if str(invariants._val(g, "layer")).endswith(face)]
    assert silk, f"the board carries no {art_material} artwork at all"
    pads = [(x, y) for num, x, y, _net, _row in pcb_mod.CONNECTOR_PADS
            if num in pcb_mod.ALL_PINS]
    near = min(((px - x) ** 2 + (py - y) ** 2) ** 0.5
               for ring in silk for px, py in ring for x, y in pads)
    assert near >= floor, (
        f"{label}: artwork reaches {near:.3f} mm from a connector pad centre, "
        f"inside the {floor:.2f} mm its material has to keep from the pad")
    assert near < 1.6, (
        f"{label}: artwork stops {near:.3f} mm from the nearest pad centre: it "
        "is still keeping the old 0.775 mm distance from the connector copper")


@pytest.mark.kicad
@pytest.mark.needs("kicad")
def test_a_part_parked_in_the_reclaimed_band_still_passes_drc(tmp_path):
    """The room the captions gave back is room the fab accepts.

    A part may now sit half a millimetre closer to the connector than it could
    while the pin captions reserved that band. Closer to the pads is where
    clearance and silk-overlap violations live, so the board that puts a part
    there is the one worth handing to DRC.
    """
    import json as _json

    from minibadge_designer import pcb as pcb_mod
    from minibadge_designer.webapp import app

    # As close to the top pads as the keepout allows, found the way the test
    # above does: walk in until the placement stops conflicting.
    y = 2.0
    while pcb_mod.pad_conflict(pcb_mod.Led(2.54, y, "red", size="0805"),
                               pcb_mod.ALL_PINS, None, False):
        y += 0.01
        assert y < 12.0, "nothing fits beside the pads at all"
    params = {"name": "band", "pinlabels": False, "texts": [], "art": [],
              "leds": [{"x": 2.54, "y": round(y, 2), "color": "red",
                        "size": "0805", "side": "front"},
                       {"x": 17.78, "y": round(y, 2), "color": "green",
                        "size": "0805", "side": "back"}]}
    resp = app.test_client().post(
        "/generate", data={"params": _json.dumps(params)},
        content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = tmp_path / "band.kicad_pcb"
    board.write_bytes(zf.read("band/band.kicad_pcb"))
    (tmp_path / "band.kicad_pro").write_bytes(zf.read("band/band.kicad_pro"))

    # The part has to still BE there: the pipeline slides units it dislikes,
    # and a board whose part was pushed back to the middle would pass DRC
    # while proving nothing about the band.
    import invariants

    root = invariants._parse_sexp(board.read_text())
    placed = [float(invariants._val(f, "at", 2)) - pcb_mod.ORIGIN
              for f in invariants._kids(root, "footprint")
              if invariants._val(f, "at", 2) is not None]
    assert any(abs(fy - y) < 1.0 for fy in placed), (
        f"no footprint sits near y={y:.2f}; the pipeline moved the part out of "
        "the band instead of building it there")
    report = tmp_path / "drc.txt"
    result = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--severity-all", "--exit-code-violations",
         "-o", str(report), str(board)],
        capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, f"DRC violations:\n{report.read_text()}"
