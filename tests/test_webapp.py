import io
import json
import zipfile

import invariants
import pytest
from PIL import Image, ImageDraw

from minibadge_designer import pcb as pcb_mod
from minibadge_designer.webapp import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def _logo_bytes() -> bytes:
    img = Image.new("RGB", (120, 120), "white")
    ImageDraw.Draw(img).ellipse((20, 20, 100, 100), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _params(**over):
    params = {
        "name": "Test Badge!",
        "mask_color": "purple",
        "art": [{"material": "silk", "threshold": 128, "invert": False, "cx": 10.16, "cy": 11.5, "w": 12}],
        "leds": [{"x": 7, "y": 6, "color": "red"}, {"x": 14, "y": 6, "color": "blue"}],
    }
    params.update(over)
    return params


def test_index_serves_ui(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"minibadge" in resp.data


def test_generate_returns_kicad_zip(client):
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(_params()),
            "art0": (io.BytesIO(_logo_bytes()), "logo.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    names = zf.namelist()
    assert "Test_Badge/Test_Badge.kicad_pcb" in names
    assert "Test_Badge/Test_Badge.kicad_pro" in names
    assert "Test_Badge/BOM.csv" in names
    assert "Test_Badge/README.txt" in names
    board = zf.read("Test_Badge/Test_Badge.kicad_pcb").decode()
    assert board.startswith("(kicad_pcb")
    assert "gr_poly" in board  # logo made it onto the silkscreen
    assert board.count("(via ") == 2


def test_generate_without_logo(client):
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(name="", leds=[{"x": 10, "y": 10, "color": "green"}]))},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("minibadge/minibadge.kicad_pcb").decode()
    assert "gr_poly" not in board
    assert board.count("(via ") == 1


def test_generate_rejects_bad_params(client):
    resp = client.post("/generate", data={"params": "not json"})
    assert resp.status_code == 400


def test_generate_rejects_bad_image(client):
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(_params()),
            "art0": (io.BytesIO(b"definitely not an image"), "logo.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_art_materials(client):
    params = _params(
        name="materials",
        leds=[],
        art=[
            {"material": "copper", "cx": 10.16, "cy": 5.0, "w": 8},
            {"material": "glow", "cx": 10.16, "cy": 10.16, "w": 8},
            {"material": "plutonium", "cx": 10.16, "cy": 15.0, "w": 8},  # -> silk
        ],
    )
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(params),
            "art0": (io.BytesIO(_logo_bytes()), "a.png"),
            "art1": (io.BytesIO(_logo_bytes()), "b.png"),
            "art2": (io.BytesIO(_logo_bytes()), "c.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("materials/materials.kicad_pcb").decode()
    assert '(layer "F.Mask")' in board       # copper exposure
    assert '(layer "F.SilkS")' in board      # sanitized fallback material
    # glow window really cut the pours: fills have far fewer than full coverage
    assert board.count("filled_polygon") >= 2


def test_led_sides_pass_through_and_sanitize(client):
    leds = [
        {"x": 7, "y": 6, "color": "red", "side": "back"},
        {"x": 14, "y": 12, "color": "blue", "side": "sideways"},
    ]
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(leds=leds, name="sides"))},
        content_type="multipart/form-data",
    )
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    bom = zf.read("sides/BOM.csv").decode()
    # The Side column is this test's subject: "back" must survive the round
    # trip and "sideways" must fall back to front. Read the column rather than
    # matching a whole row, so a change to how a part is named does not read
    # as a side-handling regression.
    sides = {row.split(",")[0]: row.split(",")[3]
             for row in bom.strip().splitlines()[1:]}
    assert sides["D1"] == "back" and sides["R1"] == "back"
    assert sides["D2"] == "front" and sides["R2"] == "front"
    assert "red" in bom and "blue" in bom


def test_overlapping_leds_get_nudged_apart(client):
    leds = [
        {"x": 8, "y": 8, "color": "red"},
        {"x": 8.4, "y": 8, "color": "blue", "side": "back"},
    ]
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(leds=leds, name="nudge"))},
        content_type="multipart/form-data",
    )
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("nudge/nudge.kicad_pcb").decode()
    # D2 (back) was pushed right of D1: its footprint is no longer at x=108.4.
    assert "(at 108.4 108)" not in board


def test_led_count_capped_and_colors_sanitized(client):
    leds = [{"x": 4 + (i % 4) * 4, "y": 4 + (i // 4) * 5, "color": "plaid"} for i in range(70)]
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(leds=leds, name="caps"))},
        content_type="multipart/form-data",
    )
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("caps/caps.kicad_pcb").decode()
    assert board.count("(via ") == 64  # MAX_LEDS
    assert "LED_PLAID" not in board and "LED_RED" in board


def test_many_leds_pass_through(client):
    leds = [
        {"x": 4.5, "y": 5, "color": "red"},
        {"x": 10.5, "y": 5, "color": "green"},
        {"x": 16.5, "y": 5, "color": "blue"},
        {"x": 10.5, "y": 12, "color": "white", "side": "back"},
    ]
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(leds=leds, name="four"))},
        content_type="multipart/form-data",
    )
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("four/four.kicad_pcb").decode()
    assert board.count("(via ") == 4
    bom = zf.read("four/BOM.csv").decode()
    assert "D4,LED white" in bom


def test_front_text_carves_logo(client):
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(
                _params(
                    name="carve",
                    leds=[],
                    texts=[{"x": 10.16, "y": 11.5, "text": "WIDE TEXT", "size": 2.0}],
                )
            ),
            "art0": (io.BytesIO(_logo_bytes()), "logo.png"),
        },
        content_type="multipart/form-data",
    )
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("carve/carve.kicad_pcb").decode()
    # The 9-char size-2 text keepout spans y 9.8..13.2 across the whole logo
    # width; no logo rect may overlap the inner band of that clearing.
    import re

    rects = re.findall(
        r"\(gr_poly \(pts \(xy [\d.]+ ([\d.]+)\) \(xy [\d.]+ [\d.]+\) \(xy [\d.]+ ([\d.]+)\)",
        board,
    )
    assert rects, "logo produced no silkscreen at all"
    for y0s, y1s in rects:
        y0, y1 = float(y0s) - 100, float(y1s) - 100
        assert not (y0 < 13.0 and y1 > 10.0), f"logo rect y {y0}..{y1} inside text keepout"


def _tricolor_bytes() -> bytes:
    img = Image.new("RGB", (90, 90), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 29, 89), fill=(180, 160, 110))
    d.rectangle((30, 0, 59, 89), fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_palette_mode_layer(client):
    params = _params(
        name="crown",
        leds=[],
        art=[{
            "mode": "palette",
            "cx": 10.16, "cy": 10.16, "w": 10,
            "palette": [
                {"rgb": [180, 160, 110], "material": "copper"},
                {"rgb": [255, 255, 255], "material": "silk"},
                {"rgb": [0, 0, 0], "material": "ignore"},
            ],
        }],
    )
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(params),
            "art0": (io.BytesIO(_tricolor_bytes()), "crown.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("crown/crown.kicad_pcb").decode()
    import re

    assert re.search(r'\(gr_poly [^\n]+\(layer "F\.Mask"\)', board)   # copper part
    assert re.search(r'\(gr_poly [^\n]+\(layer "F\.SilkS"\)', board)  # silk part
    assert not re.search(r'\(gr_poly [^\n]+\(layer "B\.Mask"\)', board)


def _skull_bytes() -> bytes:
    img = Image.new("RGB", (120, 120), (255, 220, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 100, 100), fill=(0, 0, 0))
    d.ellipse((35, 45, 55, 65), fill=(255, 220, 0))
    d.ellipse((65, 45, 85, 65), fill=(255, 220, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_small_circle_gets_bridged_not_shattered(client):
    # A circle too small to reach the connector strips must be bridged into
    # one connected board (it used to silently become two floating bars).
    resp = client.post(
        "/outline",
        data={"params": json.dumps({"rows": ["top", "bottom"],
                                    "shape": {"mode": "circle", "d": 12}})},
        content_type="multipart/form-data",
    )
    data = resp.get_json()
    assert data["bridged"] is True
    assert data["rings"] is not None
    ys = [p[1] for p in data["rings"][0]]
    xs = [p[0] for p in data["rings"][0]]
    # One outline covering strips (y 0.16..20.16) AND the circle body.
    assert min(ys) < 1 and max(ys) > 19
    assert min(xs) < 1 and max(xs) > 19


def test_stranded_led_rejected_with_message(client):
    # Three chunky units cannot fit on a bridged Ø12 circle: clear 400.
    params = _params(
        name="tiny",
        art=[],
        leds=[{"x": 6, "y": 10, "color": "red"},
              {"x": 10, "y": 10, "color": "red"},
              {"x": 14, "y": 10, "color": "red"}],
        shape={"mode": "circle", "d": 12},
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "does not fit" in resp.get_json()["error"]


# A via-less unit gives up its via and instead runs a trace across its own
# layer to a connector pad carrying the net it needs. Some placements have no
# such run, and `_generate_impl` answers those with one of three hand-written
# 400s. These are the designs that reach them -- one per branch, so a repair
# of only one of the messages still goes red.
#
# Both are deliberately off the defaults. Every other LED payload in this file
# is a front-side, stacked, 0805 unit with the via left on, and that is exactly
# the gap parameter-space defects hide in.
_UNROUTABLE_DESIGNS = {
    # (a) The user dragged the trace's bend nodes until the run skims other
    #     copper -- a through-hole part on the back with its resistor nudged
    #     and turned. Branch: "a trace bend runs too close to other copper".
    "dragged-bend": [{
        "x": 10, "y": 10, "color": "red", "novia": True,
        "size": "1.8mm", "side": "back",
        "nodes": [[10.2, 10.2], [10.3, 10.25]],
        "adv": {"rx": 0.4, "ry": -0.3, "rrot": 90},
    }],
    # (b) A via-less unit walled off from the only kept pads: pins 15/16
    #     alone survive (bottom right), and two tall units stacked into a
    #     wall at x 13.5 fence every channel to them. This row used to box
    #     itself in with overlapping units instead, until the placement
    #     settle loop (resolve_placement) learnt to relocate conflicted
    #     units around their neighbours and quietly rescued the fixture;
    #     these three don't conflict, so the pipeline leaves them where the
    #     refusal needs them. Branch: "cannot reach its power without a via".
    "boxed-in-row": [
        {"x": 3.5, "y": 10.16, "color": "red", "novia": True,
         "size": "1206", "layout": "inline"},
        {"x": 13.5, "y": 6.0, "color": "red", "size": "1206",
         "layout": "inline", "rot": 90},
        {"x": 13.5, "y": 14.3, "color": "red", "size": "1206",
         "layout": "inline", "rot": 90},
    ],
    # (c) The same wall, but the first unit's trace end was hand-picked.
    #     Branch: "no clear path to the chosen trace end"; the refusal has
    #     to blame the choice, not the via setting the user turned off on
    #     purpose.
    "chosen-end-blocked": [
        {"x": 3.5, "y": 10.16, "color": "red", "novia": True,
         "size": "1206", "layout": "inline", "term": {"pad": "16"}},
        {"x": 13.5, "y": 6.0, "color": "red", "size": "1206",
         "layout": "inline", "rot": 90},
        {"x": 13.5, "y": 14.3, "color": "red", "size": "1206",
         "layout": "inline", "rot": 90},
    ],
}

#: The wall fixtures only fence anything with most pads dropped; the full set
#: leaves pad 2 reachable on the open left side.
_UNROUTABLE_PINS = {"boxed-in-row": ["15", "16"],
                    "chosen-end-blocked": ["15", "16"]}


@pytest.mark.webapp
# Defect #1 is fixed (`rows` -> `pins` at the novia_route call), so this is no
# longer an xfail: it is the regression guard that keeps the two hand-written
# 400s below reachable. Both branches were dead code for the whole life of the
# keep-any-pin refactor because nothing drove them over HTTP.
@pytest.mark.parametrize("route", ["/generate", "/model3d"])
@pytest.mark.parametrize("design", [
    "dragged-bend",
    # ~0.3 s a call: the pour has to be filled before the router can be told
    # the channel is fenced off.
    pytest.param("boxed-in-row", marks=pytest.mark.slow),
    pytest.param("chosen-end-blocked", marks=pytest.mark.slow),
])
def test_a_via_less_led_that_cannot_route_is_refused_and_not_crashed(
        client, route, design):
    """An LED whose via is off and whose power trace has nowhere to go is
    refused with an error the UI can display, on both download routes.

    A design the router cannot serve is a user error, not a server error. The
    client does `throw new Error((await resp.json()).error || resp.statusText)`
    for both routes, so a 4xx carrying an `error` string becomes a sentence
    naming the LED to move and how to free it. A 5xx instead hands the user
    Flask's HTML error page, `resp.json()` throws inside that expression, and
    the badge owner is left with a bare failure and nothing to act on -- with
    the fix written and waiting ten lines below the crash.

    `/model3d` is here without a kicad-cli guard on purpose: the refusal is
    returned before `_model_glb` is ever called, so this route answers 400
    whether or not the tool is installed (verified with the locator forced to
    None). If it ever starts needing kicad-cli, that is the regression.
    """
    params = {"leds": _UNROUTABLE_DESIGNS[design]}
    if design in _UNROUTABLE_PINS:
        params["pins"] = _UNROUTABLE_PINS[design]
    payload = {"params": json.dumps(params)}
    try:
        resp = client.post(route, data=payload,
                           content_type="multipart/form-data")
    except Exception as exc:
        # TESTING=True propagates rather than 500ing; in production this same
        # escape is the 500 the user's browser receives.
        raise AssertionError(
            f"{route} crashed on the {design!r} design instead of refusing "
            f"it: {type(exc).__name__}: {exc}") from exc
    invariants.assert_rejected(resp)


@pytest.mark.webapp
@pytest.mark.parametrize("design", sorted(_UNROUTABLE_DESIGNS))
def test_switching_the_via_back_on_makes_the_same_design_downloadable(
        client, design):
    """The remedy the refusal above recommends actually produces a project.

    This is also what keeps that test honest. Were these placements to become
    unacceptable for some unrelated reason -- off the board, "does not fit" --
    the refusal test would stay green while no longer exercising via-less
    routing at all. Here the only edit is the via setting, so a rejection
    above plus a project here isolates the cause to `novia`.
    """
    leds = [dict(led, novia=False) for led in _UNROUTABLE_DESIGNS[design]]
    params = {"name": "viaon", "leds": leds}
    if design in _UNROUTABLE_PINS:
        params["pins"] = _UNROUTABLE_PINS[design]
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    invariants.assert_project_zip(resp, "viaon")


@pytest.mark.webapp
def test_a_chosen_trace_end_rides_the_request_and_garbage_ones_are_sanitized(
        client):
    """The term field crosses HTTP intact (the board's run really ends on
    the chosen pad), while malformed or forbidden choices degrade to the
    automatic route: a hostile payload gets a sane board, never a 500 and
    never a trace landed on copper that cannot power it."""
    from minibadge_designer import pcb as _pcb

    led = {"x": 6, "y": 6, "color": "red", "novia": True,
           "size": "0603", "layout": "inline", "term": {"pad": "16"}}
    resp = client.post(
        "/generate",
        data={"params": json.dumps({"name": "term", "leds": [led]})},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data()[:200]
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "term/term.kicad_pcb").decode()
    pads = {num: (x, y) for num, x, y, _n, _r in _pcb.CONNECTOR_PADS}
    px, py = pads["16"]
    assert f"{_pcb._n(_pcb.ORIGIN + px)} {_pcb._n(_pcb.ORIGIN + py)}" in board, \
        "the emitted run does not end on the chosen pad"
    for garbage in ({"pad": [1]}, {"unit": "x"}, 5, {"pad": "1"},
                    {"unit": -3}, {"unit": 99}):
        r = client.post(
            "/generate",
            data={"params": json.dumps(
                {"name": "g", "leds": [dict(led, term=garbage)]})},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200, (garbage, r.get_data()[:200])


@pytest.mark.webapp
def test_internal_trace_bends_ride_the_request_into_the_shipped_copper(client):
    """anodes/vnodes cross HTTP intact: the board's internal traces really
    pass through every hand-placed bend, while garbage bend payloads degrade
    to the straight trace with a sane board, never a 500.

    A dropped bend is silent intent loss: the user dragged the trace clear
    of something, the preview shows the detour, and the fab would get the
    straight line back through it. Off the defaults: a back-side rotated
    1206 in free placement, whose stub trace only free placement exposes.
    """
    from minibadge_designer import pcb as _pcb

    bends = {"anodes": [[6.0, 6.0]], "vnodes": [[7.0, 9.5]]}
    led = {"x": 10, "y": 8, "color": "red", "side": "back", "rot": 90,
           "size": "1206",
           "adv": {"rx": -6.0, "ry": -3.0, "rrot": 0, "lrot": 0,
                   "vx": 3.0, "vy": 7.0}, **bends}
    resp = client.post(
        "/generate",
        data={"params": json.dumps({"name": "bends", "leds": [led]})},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data()[:200]
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "bends/bends.kicad_pcb").decode()
    flat = [(key, bx, by) for key, pts in bends.items() for bx, by in pts]
    assert len(flat) == 2, "the fixture lost its bends; this proves nothing"
    for key, bx, by in flat:
        at = f"{_pcb._n(_pcb.ORIGIN + bx)} {_pcb._n(_pcb.ORIGIN + by)}"
        assert at in board, \
            f"the emitted copper does not pass through the {key} bend ({bx}, {by})"
    for garbage in ({"anodes": "x"}, {"anodes": [[1]]}, {"vnodes": 5},
                    {"vnodes": [["a", "b"]]},
                    {"anodes": [[float("nan")]] * 40}):
        r = client.post(
            "/generate",
            data={"params": json.dumps(
                {"name": "g", "leds": [dict(led, **garbage)]})},
            content_type="multipart/form-data",
        )
        assert r.status_code in (200, 400), (garbage, r.get_data()[:200])
        assert r.status_code == 200 or b"invalid led" in r.data, garbage


def test_smoothing_strength_and_off(client):
    # Full-bleed disc (Ø = width = 20 mm) so it reaches all four pad plates:
    # no bridges, whose round caps would add diagonal edges of their own.
    img = Image.new("RGB", (120, 120), "white")
    ImageDraw.Draw(img).ellipse((0, 0, 119, 119), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    rings = {}
    for smooth in (0, 0.4):
        resp = client.post(
            "/outline",
            data={
                "params": json.dumps({
                    "rows": ["top", "bottom"],
                    "shape": {"mode": "image", "w": 20, "cx": 10.16, "cy": 10.16,
                              "threshold": 128, "smooth": smooth},
                }),
                "shape": (io.BytesIO(buf.getvalue()), "disc.png"),
            },
            content_type="multipart/form-data",
        )
        rings[smooth] = resp.get_json()["rings"][0]

    def diagonal_edges(ring):
        n = 0
        for i in range(len(ring)):
            (x0, y0), (x1, y1) = ring[i - 1], ring[i]
            if abs(x1 - x0) > 1e-6 and abs(y1 - y0) > 1e-6:
                n += 1
        return n

    # smooth=0 is the raw pixel staircase: essentially axis-parallel edges
    # only (simplify may fuse a couple of sub-pixel steps into diagonals).
    assert diagonal_edges(rings[0]) <= 4
    # smooth=0.4 rounds the staircase into curves: mostly diagonal edges.
    assert diagonal_edges(rings[0.4]) > len(rings[0.4]) / 2


def test_readme_describes_custom_outline_and_slug(client):
    params = _params(name="My Rad Badge!!", art=[], leds=[],
                     shape={"mode": "circle", "d": 18})
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    readme = zf.read("My_Rad_Badge/README.txt").decode()
    assert "custom outline" in readme
    assert "20 x 20 mm, minibadge v2 standard" not in readme
    assert "Open My_Rad_Badge.kicad_pro" in readme


def test_circle_shape_and_top_row_only(client):
    params = _params(
        name="round",
        leds=[{"x": 10, "y": 10, "color": "red"}],
        art=[],
        pins=["1", "2", "7", "8"],
        shape={"mode": "circle", "d": 18},
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("round/round.kicad_pcb").decode()
    import re

    assert re.search(r'\(gr_poly [^\n]*\(layer "Edge\.Cuts"\)', board)
    assert not re.search(r'\(pad "9" thru_hole', board)   # bottom row dropped
    assert re.search(r'\(pad "7" thru_hole', board)       # top 3V3 kept
    assert "CLK" not in board


def test_image_shape_outline(client):
    # Silhouette: a full square plus a blob extending above it.
    img = Image.new("RGB", (200, 260), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 60, 199, 259), fill="black")     # main square body
    d.ellipse((60, 0, 140, 120), fill="black")       # bump sticking out the top
    buf = io.BytesIO()
    img.save(buf, "PNG")
    params = _params(
        name="tabbed",
        leds=[],
        art=[],
        pins=["1", "2", "7", "8", "9", "10", "15", "16"],
        shape={"mode": "image", "threshold": 128, "invert": False,
               "w": 19, "cx": 10.16, "cy": 8.0},
    )
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(params),
            "shape": (io.BytesIO(buf.getvalue()), "shape.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("tabbed/tabbed.kicad_pcb").decode()
    import re

    m = re.search(r'\(gr_poly \(pts (.*?)\) \(stroke [^\n]*\(layer "Edge\.Cuts"\)', board)
    assert m
    ys = [float(v) - 100 for v in re.findall(r"\(xy [\d.-]+ ([\d.-]+)\)", m.group(1))]
    # One connected outline covering the bump above the square AND the full
    # body down to the bottom edge (a rounding bug once sliced it apart).
    assert min(ys) < 0
    assert max(ys) > 19
    assert board.count('(layer "Edge.Cuts")') == 1


def test_custom_outline_composes_elements(client):
    # Compose a board from parts: a big circle, a star poking out the right,
    # a hole cut through the middle, and an image silhouette part: the
    # outline must be one polygon containing all adds minus the cut.
    img = Image.new("RGB", (100, 100), "white")
    ImageDraw.Draw(img).rectangle((0, 0, 99, 99), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    shape = {
        "mode": "custom",
        "smooth": 0.12,
        "elements": [
            {"kind": "circle", "op": "add", "cx": 10.16, "cy": 10.16, "w": 22},
            {"kind": "star", "op": "add", "cx": 26.0, "cy": 10.16, "w": 16, "rot": 15},
            {"kind": "circle", "op": "cut", "cx": 10.16, "cy": 10.16, "w": 5},
            {"kind": "image", "op": "add", "cx": 10.16, "cy": 24.0, "w": 10,
             "threshold": 128, "invert": False},
        ],
    }
    resp = client.post(
        "/outline",
        data={
            "params": json.dumps({"rows": ["top", "bottom"], "shape": shape}),
            "shape3": (io.BytesIO(buf.getvalue()), "block.png"),
        },
        content_type="multipart/form-data",
    )
    rings = resp.get_json()["rings"]
    assert rings is not None
    assert len(rings) >= 2  # outer boundary + the cut hole
    xs = [p[0] for p in rings[0]]
    ys = [p[1] for p in rings[0]]
    assert max(xs) > 30    # star reaches past the circle
    assert max(ys) > 27    # image block hangs below
    # One of the interior rings is the ~5 mm hole cut around board center.
    assert any(
        7 < min(p[0] for p in hole) and max(p[0] for p in hole) < 13
        and 7 < min(p[1] for p in hole) and max(p[1] for p in hole) < 13
        for hole in rings[1:]
    )

    # The same composition must generate a valid board.
    params = _params(name="composed", leds=[], art=[], shape=shape)
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(params),
            "shape3": (io.BytesIO(buf.getvalue()), "block.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("composed/composed.kicad_pcb").decode()
    assert board.count('(layer "Edge.Cuts")') >= 2  # outline + at least the hole


def test_cut_only_carves_standard_square(client):
    # No add parts: cuts apply to the standard square base.
    resp = client.post(
        "/outline",
        data={"params": json.dumps({
            "rows": ["top", "bottom"],
            "shape": {"mode": "custom", "elements": [
                {"kind": "circle", "op": "cut", "cx": 10.16, "cy": 10.16, "w": 6},
            ]},
        })},
        content_type="multipart/form-data",
    )
    rings = resp.get_json()["rings"]
    assert rings is not None and len(rings) == 2
    xs = [p[0] for p in rings[0]]
    ys = [p[1] for p in rings[0]]
    assert min(xs) == 0.16 and max(xs) == 20.16  # outer ring is still the square
    assert min(ys) == 0.16 and max(ys) == 20.16
    hx = [p[0] for p in rings[1]]
    assert 7.0 < min(hx) and max(hx) < 13.3  # the Ø6 hole


# ---------------------------------------------------------------------------
# Cut-material artwork.
#
# "cut" is the one art material that is not ink on a layer: it is the absence
# of board.  Those regions are subtracted from the outline *before* the
# connector-pad tabs are unioned in, so a fab routs a real hole through
# copper, mask and laminate while the pads keep the material that holds them.
# ---------------------------------------------------------------------------
def _routed_board(board_text):
    """The shape a fab would rout: outer Edge.Cuts contour minus its holes.

    Contours are read through the public ``Board.graphics``; walking the
    s-expression children by name here (rather than borrowing invariants'
    private ``_kid``/``_edge_polygon``) keeps this working when those get
    renamed.
    """
    from shapely.geometry import Polygon

    from minibadge_designer import pcb

    board = invariants.assert_parses(board_text)
    rings = []
    for g in board.graphics("Edge.Cuts"):
        if g[0] != "gr_poly":
            continue
        pts = next(c for c in g if isinstance(c, list) and c and c[0] == "pts")
        rings.append(Polygon([(float(p[1]) - pcb.ORIGIN, float(p[2]) - pcb.ORIGIN)
                              for p in pts[1:]]))
    assert rings, "no gr_poly contour on Edge.Cuts"
    outer = max(rings, key=lambda p: p.area)
    for ring in rings:
        if ring is not outer:
            outer = outer.difference(ring)
    return outer


def _generated_board(client, params, slug, files=None):
    data = {"params": json.dumps(params), **(files or {})}
    resp = client.post("/generate", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    return zf.read(f"{slug}/{slug}.kicad_pcb").decode()


_HEX30 = {"mode": "custom", "elements": [
    {"kind": "hex", "op": "add", "cx": 10.16, "cy": 10.16, "w": 30,
     "rot": 0, "sides": 6}]}


@pytest.mark.parametrize(
    "shape, layer, upload, hole_probe, solid_probe",
    [
        # The default square, a front circle in the middle.
        (None,
         {"kind": "circle", "material": "cut", "side": "front",
          "cx": 10.16, "cy": 10.16, "w": 7, "h": 7, "rot": 0},
         False, (10.16, 10.16), (10.16, 5.66)),
        # Nothing about the rule is special to a circle, to the front face, to
        # an unrotated layer, or to the standard square: a rotated polygon on
        # the BACK of a custom hex outline cuts exactly the same way.
        (_HEX30,
         {"kind": "hex", "sides": 6, "material": "cut", "side": "back",
          "cx": 6.5, "cy": 13.0, "w": 6, "h": 6, "rot": 25},
         False, (6.5, 13.0), (6.5, 17.4)),
        # An UPLOADED bitmap reaches the cut through a different pipeline
        # (threshold -> pixel grid -> merged rects) than the vector shapes
        # above.  Both cases passed while that pipeline read the wrong
        # material key, which is what put this case here.
        (None,
         {"kind": "image", "material": "cut", "side": "front", "mode": "threshold",
          "threshold": 128, "invert": False, "cx": 10.16, "cy": 10.16, "w": 8},
         True, (10.16, 10.16), (10.16, 6.0)),
    ],
    ids=["square-circle-front", "hex-outline-polygon-back-rotated",
         "uploaded-bitmap-threshold"],
)
def test_art_assigned_the_cut_material_is_routed_out_of_the_board(
        client, shape, layer, upload, hole_probe, solid_probe):
    # If this breaks the fab ships a solid badge where the user drew a
    # cut-out (or, worse, routs one somewhere else).
    from shapely.geometry import Point

    params = _params(name="cut", leds=[], art=[layer])
    if shape is not None:
        params["shape"] = shape
    files = {"art0": (io.BytesIO(_logo_bytes()), "cut.png")} if upload else None
    board = _routed_board(_generated_board(client, params, "cut", files))
    assert not board.contains(Point(*hole_probe)), (
        f"{hole_probe} is under the cut layer but is still solid board; "
        "the cut-out was not routed")
    assert board.contains(Point(*solid_probe)), (
        f"{solid_probe} is clear of the cut layer but is not board; the cut "
        "removed more than the user drew")


def test_the_previewed_outline_carries_the_cut_the_board_is_routed_with(client):
    # The preview is the only thing the user sees before ordering.  A hole in
    # the canvas that the fab does not rout (or the reverse) is a lie that
    # costs a board run, and /outline and /generate reach it by different
    # call paths.
    from shapely.geometry import Polygon

    layer = {"kind": "star", "material": "cut", "side": "front",
             "cx": 10.16, "cy": 10.16, "w": 9, "h": 9, "rot": 15}
    params = _params(name="cut", leds=[], art=[layer], shape=_HEX30)

    resp = client.post(
        "/outline",
        data={"params": json.dumps({"shape": _HEX30, "art": [layer]})},
        content_type="multipart/form-data",
    )
    data = resp.get_json()
    assert data["rings"], "preview returned no outline at all"
    assert data["cutIgnored"] is False, (
        "the preview reported this cut as having no effect, but it lands "
        "in the middle of the board")
    previewed = Polygon(data["rings"][0], data["rings"][1:])
    routed = _routed_board(_generated_board(client, params, "cut"))

    # Agreement, not coordinates: whatever the shapes are, they must be the
    # same shape.
    disagreement = previewed.symmetric_difference(routed).area
    assert disagreement < 1e-3 * routed.area, (
        f"preview and routed board disagree over {disagreement:.4f} mm^2; "
        "the canvas is not showing the board that would be made")


def test_a_cut_never_removes_the_board_under_a_kept_connector_pad(client):
    # The pads are how the badge mounts to its host.  A cut is allowed to eat
    # the middle of the board, but the tab a kept pad pair stands on is
    # unioned back in afterwards and must survive a cut aimed straight at it.
    from shapely.geometry import Point

    from minibadge_designer import pcb

    kept = ["1", "2", "9", "10"]  # a subset: dropped pairs claim no tab
    aimed = [pcb.PAD_PAIRS[pair]["plate"] for pair in pcb.active_pairs(kept)]
    art = [
        # one cut that must work, so a board where cuts do nothing at all
        # cannot pass this test...
        {"kind": "circle", "material": "cut", "side": "front",
         "cx": 10.16, "cy": 10.16, "w": 8, "h": 8, "rot": 0},
        # ...and one deliberately centred on every kept pad tab, which must
        # not.
        *[{"kind": "circle", "material": "cut", "side": "front",
           "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2, "w": 4, "h": 4, "rot": 0}
          for x0, y0, x1, y1 in aimed],
    ]
    params = _params(name="cut", leds=[], pins=kept, art=art)
    board = _routed_board(_generated_board(client, params, "cut"))

    assert not board.contains(Point(10.16, 10.16)), (
        "the middle cut was not routed, so this test would pass even if cuts "
        "were ignored entirely")
    pairs = pcb.active_pairs(kept)
    assert len(pairs) >= 2, (
        f"pins {kept} were expected to keep several pad pairs, got {pairs}; "
        "with none kept the loop below would assert nothing at all")
    for pair in pairs:
        x0, y0, x1, y1 = pcb.PAD_PAIRS[pair]["plate"]
        centre = Point((x0 + x1) / 2, (y0 + y1) / 2)
        assert board.contains(centre), (
            f"a cut removed the board under kept pad pair {pair}; the badge "
            "cannot be soldered to its host")


@pytest.mark.parametrize(
    "shape, cut_w",
    [
        # Room left over: the unit is expected to be relocated onto board.
        (_HEX30, 8.0),
        # A Ø8 hole in the 20 mm square leaves only ~6 mm bands, too narrow
        # for a unit anywhere: the only honest answer left is a refusal.
        (None, 8.0),
    ],
    ids=["board-has-room-elsewhere", "board-has-no-room-left"],
)
def test_a_unit_is_never_shipped_standing_on_a_cut(client, shape, cut_w):
    # Two outcomes are acceptable — move the unit onto solid board, or refuse
    # the download naming it.  The third, shipping a board whose pads hang
    # over a routed hole, passes checkout and fails at the fab.
    from shapely.geometry import Point

    cut = {"kind": "circle", "material": "cut", "side": "front",
           "cx": 10.16, "cy": 10.16, "w": cut_w, "h": cut_w, "rot": 0}
    params = _params(name="cut", art=[cut],
                     leds=[{"x": 10.16, "y": 10.16, "side": "back", "color": "red"}])
    if shape is not None:
        params["shape"] = shape

    resp = client.post("/generate", data={"params": json.dumps(params)},
                       content_type="multipart/form-data")
    if resp.status_code != 200:
        assert "LED 1" in resp.get_json()["error"], (
            "the refusal must name the unit so the UI can point at it")
        return

    text = zipfile.ZipFile(io.BytesIO(resp.data)).read("cut/cut.kicad_pcb").decode()
    board = _routed_board(text)
    parsed = invariants.assert_parses(text)
    unit_pads = [p for p in parsed.pads if p.ref and p.ref[0] in ("D", "R")]
    assert unit_pads, "no unit pads on a board that was accepted with an LED"
    for pad in unit_pads:
        assert board.contains(Point(pad.x, pad.y)), (
            f"pad {pad.ref}.{pad.num} sits over the routed cut-out; the unit "
            "was shipped standing on a hole")


@pytest.mark.parametrize(
    "cx, cy, w",
    [
        (17.7, 18.6, 2.0),   # wholly inside a kept pad pair's tab
        (60.0, 60.0, 4.0),   # nowhere near the board
    ],
    ids=["under-a-pad-tab", "off-the-board"],
)
def test_the_preview_says_so_when_a_cut_removes_nothing(client, cx, cy, w):
    # Otherwise the hole the canvas drew while dragging simply closes itself
    # again on the server's answer, with no reason given, and the user is left
    # dragging a layer that cannot work where they are putting it.
    layer = {"kind": "circle", "material": "cut", "side": "front",
             "cx": cx, "cy": cy, "w": w, "h": w, "rot": 0}
    resp = client.post(
        "/outline",
        data={"params": json.dumps({"shape": _HEX30, "art": [layer]})},
        content_type="multipart/form-data",
    )
    assert resp.get_json()["cutIgnored"] is True, (
        "a cut that changes nothing about the outline was reported as if it "
        "had worked")


def test_the_readme_tells_the_fab_about_routed_cutouts(client):
    # The README is what a fab customer reads. A cut-through art layer is a
    # routing operation, not artwork -- a board that quietly arrives with a
    # hole in it, undocumented, is a surprise at quoting time.
    layer = {"kind": "circle", "material": "cut", "side": "front",
             "cx": 10.16, "cy": 10.16, "w": 6, "h": 6, "rot": 0}
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(name="cut", leds=[], art=[layer]))},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    readme = zipfile.ZipFile(io.BytesIO(resp.data)).read("cut/README.txt").decode()

    summary = next(line for line in readme.splitlines() if line.startswith("Generated by"))
    assert "cutout" in summary, (
        f"the board summary never mentions the hole that was routed: {summary!r}")
    assert "Cut through board" in readme, (
        "the artwork-materials section documents every material except the one "
        "that removes the board")

    # A board with no cut must not claim one.
    plain = client.post(
        "/generate",
        data={"params": json.dumps(_params(name="cut", leds=[], art=[]))},
        content_type="multipart/form-data",
    )
    plain_readme = zipfile.ZipFile(io.BytesIO(plain.data)).read("cut/README.txt").decode()
    plain_summary = next(line for line in plain_readme.splitlines()
                         if line.startswith("Generated by"))
    assert "cutout" not in plain_summary, (
        f"a board with no cut advertises one: {plain_summary!r}")


def test_font_file_served_and_unknown_404(client):
    resp = client.get("/fonts/archivo.ttf")
    assert resp.status_code == 200
    assert resp.data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true")
    # The metrics ride beside the outlines and the editor needs BOTH: the
    # outlines to draw with, the metrics to decide whether the text fits. A
    # missing metrics file is not a missing decoration -- the fit check falls
    # back to measuring the browser's own rasteriser and pads 8% per side.
    met = client.get("/fonts/archivo.metrics.json")
    assert met.status_code == 200
    assert met.get_json()["chars"]["M"][0] > 0, (
        "the served table has no advance for a capital M, so every string "
        "measured from it collapses to nothing")
    assert client.get("/fonts/nope.metrics.json").status_code == 404
    # send_file hands back a lazily-consumed FileWrapper. A PEP 3333 server
    # closes the WSGI iterable for you; the werkzeug test client does not, so
    # without this the TTF descriptor is finalised at some later GC point and
    # the ResourceWarning is raised against whichever test is running then.
    resp.close()
    assert client.get("/fonts/../secrets.ttf").status_code in (308, 404)
    assert client.get("/fonts/nope.ttf").status_code == 404


def test_index_lists_fonts(client):
    page = client.get("/").data.decode()
    assert "Archivo Black" in page and "Creepster" in page


#: How far the metrics table's box may exceed the ink text_geometry actually
#: produces, mm.  Stated here, not imported: text_geometry ends on
#: `simplify(0.005)`, which can drop the very vertex that reached furthest, and
#: char_metrics rounds each extent outward by up to 0.01 font unit (1.7 um at
#: the largest text the UI offers).  That bounds it at ~0.0085 mm and the worst
#: case measured over the sweep below is 0.0057 mm.
_INK_BOX_SLOP_MM = 0.015
#: How far the ink may reach OUTSIDE that box, mm -- the direction that ships a
#: board nobody previewed.  Structurally zero (every extent is rounded outward
#: and the walk uses the same advances), measured 0.000000, so this is float
#: noise money and nothing else.
_INK_ESCAPE_MM = 0.001
#: Strings that stress the layout rules the table has to reproduce: descenders,
#: a face-raised digit run, blanks at both ends (the ink is centred on the INK,
#: not the advance), a pair a rasteriser would gladly turn into one ligature
#: glyph, and characters no bundled face carries -- text_geometry skips those
#: with a half-em gap, which is a rule the table has to ship rather than a
#: rule the reader of the table can guess.
_INK_STRINGS = ["418", "gjpqy", " g ", "fi ffl", "MINIBADGE 2026!",
                "\u0416\u0416", "A\u0416B"]


@pytest.mark.slow  # ~2.3 s: flattens every glyph of all 12 bundled faces once
def test_shipped_font_metrics_predict_the_ink_the_board_prints():
    """The per-character metrics the editor is handed describe the same ink
    text_geometry puts on the board, in every bundled face.

    This is the editor's whole basis for deciding whether a text fits: it lays
    the string out from this table and refuses the download when the box lands
    off the board.  If the table and text_geometry disagree, the user is either
    refused a text that had room (the 8%-of-the-width pad this replaced cost a
    17 mm string 1.36 mm of phantom margin per side) or handed a board whose
    silk the fab clips off while the preview looked perfect.

    Bounds, not coordinates: the table's box must CONTAIN the ink and must not
    be meaningfully bigger than it.  Both directions are checked because they
    are different failures, and the tolerances are stated above independently of
    anything the production code reads.
    """
    from minibadge_designer import textpoly

    faces = sorted(textpoly.FONTS)
    assert len(faces) > 1, (
        "one face cannot show a per-face disagreement: the defect this guards "
        "against was invisible in the faces whose cap ratio happens to be 0.7")
    # Both ends of the size range the UI offers plus the ordinary middle.  Every
    # metric here scales linearly with size, so a case that fails at one size
    # and passes at another is the finding, not the noise.
    cases = [(f, t, size) for f in faces for t in _INK_STRINGS
             for size in (0.6, 1.5, 119.0)]
    assert len(cases) > 100, (
        f"only {len(cases)} face/string/size combinations to check; the sweep "
        "below is the whole test, and a short one covers whichever face the "
        "next disagreement hides in")
    generous, escaped, inked = [], [], 0
    for face, text, size in cases:
        m = textpoly.char_metrics(face)
        geom = textpoly.text_geometry(text, face, size)
        box = _predict_ink_box(m, text, size)
        if geom is None:
            assert box is None, (
                f"{face} prints nothing at all for {text!r}, yet its metrics "
                f"claim ink at {box}; the editor would reserve board for a "
                "glyph that never arrives")
            continue
        assert box is not None, (
            f"{face} puts ink on the board for {text!r} but its metrics claim "
            "none, so the editor sizes that text as empty and lets it sit "
            "anywhere, including off the edge")
        inked += 1
        for edge, gap in zip(("left", "top", "right", "bottom"),
                             (box[0] - geom.bounds[0], box[1] - geom.bounds[1],
                              geom.bounds[2] - box[2], geom.bounds[3] - box[3])):
            # gap > 0: ink outside the box.  gap < 0: box outside the ink.
            if gap > _INK_ESCAPE_MM:
                escaped.append((face, text, size, edge, round(gap, 6)))
            elif -gap > _INK_BOX_SLOP_MM:
                generous.append((face, text, size, edge, round(gap, 6)))

    assert inked > 30, (
        f"only {inked} of {len(cases)} cases put ink on the board, so the edge "
        "comparisons above covered almost nothing")
    assert not escaped, (
        f"{len(escaped)} edges have ink OUTSIDE the box the editor is given, so "
        f"that much silk is clipped away from a board whose preview looked "
        f"right: {sorted(escaped, key=lambda r: -r[4])[:4]}")
    assert not generous, (
        f"{len(generous)} edges reserve board the ink never fills, which is how "
        f"a text with room to spare gets refused: "
        f"{sorted(generous, key=lambda r: r[4])[:4]}")


def _predict_ink_box(metrics: dict, text: str, size_mm: float):
    """The ink box the editor computes from a face's metrics, in mm.

    A deliberate second implementation of index.html's inkRun100, kept in the
    test so the two cannot drift silently in the same edit: it walks the string
    one character at a time by that glyph's own advance, gives a character the
    face cannot draw the half-em gap text_geometry gives it, and centres the
    result on the INK.  Returns None when nothing in the string leaves ink.
    """
    x, x0, x1, up, down = 0.0, None, None, None, None
    for ch in text:
        entry = metrics["chars"].get(ch)
        if entry is None:
            x += metrics["miss"]
            continue
        if len(entry) > 1:
            _adv, gx0, gx1, gup, gdown = entry
            x0 = x + gx0 if x0 is None else min(x0, x + gx0)
            x1 = x + gx1 if x1 is None else max(x1, x + gx1)
            up = gup if up is None else max(up, gup)
            down = gdown if down is None else max(down, gdown)
        x += entry[0]
    if x0 is None or not x1 > x0:
        return None
    k = size_mm / metrics["cap"]
    half = (x1 - x0) * k / 2
    return (-half, size_mm / 2 - up * k, half, size_mm / 2 + down * k)


def test_ttf_texts_all_materials(client):
    # One text per material, front and back. TTF texts become gr_poly art:
    # silk on F/B.SilkS, copper opens that face's mask, bare opens both,
    # glow only cuts the pours. The KiCad-font text stays a gr_text.
    params = _params(
        name="fonty",
        art=[],
        leds=[],
        texts=[
            {"x": 10, "y": 4, "text": "SILK", "size": 2, "side": "front",
             "font": "archivo", "material": "silk"},
            {"x": 10, "y": 8, "text": "CU", "size": 2, "side": "front",
             "font": "blackops", "material": "copper"},
            {"x": 10, "y": 12, "text": "GLOW", "size": 2, "side": "back",
             "font": "vt323", "material": "glow"},
            {"x": 10, "y": 16, "text": "BARE", "size": 2, "side": "back",
             "font": "creepster", "material": "bare"},
            {"x": 10, "y": 18.5, "text": "back ink", "size": 1.2, "side": "back",
             "font": "pacifico", "material": "silk"},
            {"x": 10, "y": 6, "text": "stroke", "size": 1.2, "side": "front",
             "font": "kicad", "material": "copper"},  # coerced back to silk
        ],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("fonty/fonty.kicad_pcb").decode()
    import re

    def layers_of(kind):
        return set(re.findall(r"\(gr_poly [^\n]*?\(layer \"([^\"]+)\"\)", board))

    poly_layers = layers_of("gr_poly")
    assert "F.SilkS" in poly_layers   # archivo silk text
    assert "B.SilkS" in poly_layers   # pacifico back silk text
    assert "F.Mask" in poly_layers    # copper text opens the front mask
    assert "B.Mask" in poly_layers    # bare text opens the back mask too
    assert board.count("gr_text") == 1  # only the KiCad stroke text
    assert '"stroke"' in board
    # glow text must cut both copper pours: the fills avoid its area
    from minibadge_designer import textpoly

    g = textpoly.text_geometry("GLOW", "vt323", 2.0)
    assert g is not None


#: The silk-to-edge budget the app holds hand-placed text to, mm, stated here
#: independently of pcb.TEXT_EDGE_CLEAR so an edit to that constant is visible
#: as a failure rather than absorbed silently.  It is measured, not borrowed:
#: KiCad's own silk_edge_clearance only fires on CONTACT with Edge.Cuts (a
#: filled silk polygon 0.001 mm inside the outline is DRC-clean under this
#: project's rules, the same polygon touching it is not), so the number that
#: matters is the fab's published silk-to-edge capability, 0.2-0.25 mm -- which
#: is also the edge budget generate_project() already gives copper, the more
#: critical of the two layers.
_SILK_EDGE_MM = 0.2
#: The wider clip this rule must never regress to. It was the artwork frame --
#: uploaded images kept 0.5 mm where text kept 0.2, and text clipped to that
#: frame lost 0.3 mm of the user's string from the download while the editor
#: told them 0.2 was fine. Artwork keeps the same 0.2 as text now
#: (logo.EDGE_MARGIN), so nothing on the board is clipped here any more; the
#: number stays as the sentinel that says so.
_ART_FRAME_MM = 0.5


@pytest.mark.parametrize("material,layer,font", [
    ("silk", "F.SilkS", "pressstart"),
    ("copper", "F.Mask", "pacifico"),
])
@pytest.mark.parametrize("side", ["front", "back"])
def test_text_pushed_at_the_edge_keeps_the_ink_the_silk_limit_allows(
        client, material, layer, font, side):
    """A text placed hard against the board edge arrives clipped to the
    silk-to-edge limit -- not to the artwork frame, and not overhanging.

    Two failures live at this edge and they pull opposite ways.  Clip further in
    than the editor's own rule and the user's string loses a slice they were
    told would print, silently, in the file they send to the fab.  Clip further
    out and the ink reaches the routed edge, which is the one thing KiCad's
    silk_edge_clearance does fail the board for.

    So the test brackets it: ink strictly inside the outline, and reaching the
    stated silk-to-edge budget rather than stopping at the artwork frame.
    """
    import re

    if side == "back":
        layer = layer.replace("F.", "B.")
    # 19 mm of Press Start 2P shoved 6 mm off-centre: whichever direction the
    # face reads, this string's ink runs well past the left edge of the board.
    params = _params(
        name="edgy", art=[], leds=[],
        texts=[{"x": 4.0, "y": 10.16, "text": "MINIBADGE", "size": 2.7,
                "side": side, "font": font, "material": material}])
    resp = client.post("/generate", data={"params": json.dumps(params)},
                       content_type="multipart/form-data")
    assert resp.status_code == 200, resp.data[:200]
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read("edgy/edgy.kicad_pcb").decode()

    xs = [float(m) - pcb_mod.ORIGIN
          for blk in re.findall(
              r"\(gr_poly \(pts (.*?)\) \(stroke[^\n]*?\(layer \"" + re.escape(layer)
              + r"\"\)", board)
          for m in re.findall(r"\(xy ([-\d.]+) [-\d.]+\)", blk)]
    assert len(xs) > 20, (
        f"only {len(xs)} vertices of {material} text landed on {layer}; there is "
        "no clipped edge here to measure, so everything below passes vacuously")

    # Bracket, not a coordinate: the surviving ink has to start inside the
    # silk-to-edge budget (the editor refuses the user at exactly that line, so
    # anything closer is ink the editor never promised and DRC may fail), and it
    # has to start OUTSIDE the artwork frame (ink between the two is the slice
    # the old clip removed without telling anyone).  Where in that 0.3 mm band a
    # particular glyph's leftmost surviving vertex lands is up to the glyph:
    # clipping a letter to a hairline leaves slivers the emitter drops.
    gap = min(xs) - pcb_mod.OUTLINE[0]
    assert gap >= _SILK_EDGE_MM - 0.001, (
        f"{material} text ink comes within {gap:.4f} mm of the board edge on "
        f"{layer}, inside the {_SILK_EDGE_MM} mm the editor refuses the user "
        "at; the download is closer to the routed edge than the preview said, "
        "and silk that touches the outline fails DRC outright")
    # Midway between the two, so the comparison cannot be won by float noise:
    # 0.66 - 0.16 lands a hair BELOW 0.5 in binary, which let a clip at the
    # artwork frame pass a `gap < 0.5` written the obvious way.
    assert gap < (_SILK_EDGE_MM + _ART_FRAME_MM) / 2, (
        f"{material} text ink stops {gap:.4f} mm from the board edge, nearer "
        f"the {_ART_FRAME_MM} mm artwork frame than the {_SILK_EDGE_MM} mm silk "
        "limit the editor holds the user to: every text they push outward loses "
        "that slice from the file they send to the fab, silently")


def test_unknown_font_key_falls_back_to_stroke(client):
    params = _params(art=[], leds=[],
                     texts=[{"x": 10, "y": 10, "text": "hi", "size": 2,
                             "side": "front", "font": "comic-sans", "material": "copper"}])
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("Test_Badge/Test_Badge.kicad_pcb").decode()
    assert board.count("gr_text") == 1  # rendered with the stroke font, silk


def test_copper_islands_inside_windows_survive(client):
    # Copper art enclosed by a bare window (a skull's gold eyes) becomes an
    # isolated pour island; it must stay in the fill, and the zone must tell
    # KiCad's refill to keep it (island_removal_mode 1).
    img = Image.new("RGB", (120, 120), (255, 220, 0))       # copper background
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 100, 100), fill=(0, 0, 0))         # bare face
    d.ellipse((40, 40, 80, 80), fill=(255, 220, 0))         # copper eye island
    buf = io.BytesIO()
    img.save(buf, "PNG")
    params = _params(
        name="eyes",
        leds=[{"x": 16.5, "y": 6, "color": "red", "side": "back"}],
        art=[{"mode": "palette", "cx": 10.16, "cy": 10.16, "w": 14,
              "palette": [{"rgb": [255, 220, 0], "material": "copper"},
                          {"rgb": [0, 0, 0], "material": "bare"}]}],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params),
              "art0": (io.BytesIO(buf.getvalue()), "eyes.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read("eyes/eyes.kicad_pcb").decode()
    assert board.count("island_removal_mode 1") == 2  # both pours keep islands
    import re

    from shapely.geometry import Point, Polygon

    zone = board[board.index('(net_name "3V3")'):]
    eye = Point(10.16, 10.16)  # eye center == art center
    hit = False
    for fp in re.finditer(r'\(filled_polygon \(layer "F\.Cu"\) \(pts (.*?)\)\)', zone):
        pts = [(float(a) - 100, float(b) - 100)
               for a, b in re.findall(r"\(xy ([\d.-]+) ([\d.-]+)\)", fp.group(1))]
        if len(pts) >= 3 and Polygon(pts).buffer(0).contains(eye):
            hit = True
    assert hit, "copper eye island missing from the F.Cu fill"


def test_surface_finish_in_readme(client):
    # The finish picker is an ordering note: silver (HASL) vs gold (ENIG).
    for finish, phrase in (("hasl", "SILVER"), ("enig", "GOLD"), (None, "GOLD")):
        params = _params(name="fin", art=[], leds=[])
        if finish:
            params["finish"] = finish
        resp = client.post(
            "/generate",
            data={"params": json.dumps(params)},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        readme = zipfile.ZipFile(io.BytesIO(resp.data)).read("fin/README.txt").decode()
        assert phrase in readme, (finish, phrase)


def test_shape_art_layer_and_one_sided_bare(client):
    # A basic-shape art layer needs no upload; a bare layer with
    # bare_side=back opens ONLY the back mask and only cuts the back pour;
    # the front keeps its copper, so the window may sit over a front-side part.
    params = _params(
        name="winback",
        leds=[],
        art=[
            {"kind": "circle", "material": "bare", "bare_side": "back",
             "cx": 10.16, "cy": 10.16, "w": 10, "h": 10, "rot": 0},
            {"kind": "rect", "material": "copper", "bare_side": "through",
             "cx": 5.0, "cy": 16.5, "w": 5, "h": 3, "rot": 20},
        ],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read("winback/winback.kicad_pcb").decode()
    import re

    def polys_on(layer):
        return len(re.findall(rf'\(gr_poly [^\n]*?\(layer "{layer}"\)', board))

    assert polys_on("B.Mask") >= 1          # the back-only window opens B.Mask
    assert polys_on("F.Mask") == 1          # only the copper rect opens the front
    # the bare circle voids copper on the BACK pour only; the front pour, and
    # anything mounted on the front, is untouched
    from shapely.geometry import Point, Polygon

    def pour_covers(net, layer):
        zone = board[board.index(f'(net_name "{net}")'):]
        zone = zone[:zone.index("\n  )")]
        for fp in re.finditer(
                rf"\(filled_polygon \(layer \"{layer}\"\) \(pts (.*?)\)\)", zone):
            pts = [(float(a) - 100, float(b) - 100)
                   for a, b in re.findall(r"\(xy ([\d.-]+) ([\d.-]+)\)", fp.group(1))]
            if len(pts) >= 3 and Polygon(pts).buffer(0).contains(Point(10.16, 10.16)):
                return True
        return False

    assert not pour_covers("GND", "B.Cu"), "back pour should be cut by a back window"
    assert pour_covers("3V3", "F.Cu"), "front pour should survive a back-only window"


def test_back_side_art_mirrors_and_lands_on_back_layers(client):
    # An off-center dark square, placed on the BACK: silk goes to B.SilkS and
    # the geometry mirrors about the layer center so it reads correctly from
    # the back face.
    img = Image.new("RGB", (100, 100), "white")
    ImageDraw.Draw(img).rectangle((0, 40, 30, 60), fill="black")  # left edge
    buf = io.BytesIO()
    img.save(buf, "PNG")
    params = _params(
        name="backart", leds=[],
        art=[{"mode": "threshold", "material": "silk", "threshold": 128,
              "cx": 10.16, "cy": 10.16, "w": 10, "side": "back"}],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params),
              "art0": (io.BytesIO(buf.getvalue()), "bar.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read("backart/backart.kicad_pcb").decode()
    import re

    assert re.search(r'\(gr_poly [^\n]+\(layer "B\.SilkS"\)', board)
    # no ART on the front silk (the connector's pin captions print on both
    # silks by design, so count art polygons rather than layer mentions)
    assert not re.search(r'\(gr_poly [^\n]+\(layer "F\.SilkS"\)', board)
    # image feature was on the LEFT of the image; mirrored, its silk must sit
    # RIGHT of the layer center (board x > 110.16 in page coords)
    xs = []
    for line in board.splitlines():
        if "gr_poly" in line and '(layer "B.SilkS")' in line:
            xs += [float(a) for a, _b in re.findall(r"\(xy ([\d.-]+) ([\d.-]+)\)", line)]
    assert xs and min(xs) > 110.16, f"min x {min(xs) if xs else None}"


def test_rejects_no_rows(client):
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(rows=[]))},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_wand_overrides_pass_through(client):
    params = _params(
        name="skull",
        leds=[],
        art=[{
            "mode": "palette",
            "cx": 10.16, "cy": 10.16, "w": 12,
            "palette": [
                {"rgb": [255, 220, 0], "material": "copper"},
                {"rgb": [0, 0, 0], "material": "ignore"},
            ],
            "overrides": [
                {"u": 45 / 120, "v": 55 / 120, "material": "bare"},
                {"u": 75 / 120, "v": 55 / 120, "material": "bare"},
            ],
        }],
    )
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(params),
            "art0": (io.BytesIO(_skull_bytes()), "skull.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("skull/skull.kicad_pcb").decode()
    import re

    # Eyes became bare windows (mask openings on BOTH sides)...
    assert re.search(r'\(gr_poly [^\n]+\(layer "B\.Mask"\)', board)
    # ...while the same-colored background stays copper (front mask only).
    assert re.search(r'\(gr_poly [^\n]+\(layer "F\.Mask"\)', board)


def test_silk_carved_by_copper_openings(client):
    # Same image twice, same spot: copper wins, silk must vanish there.
    params = _params(
        name="carved",
        leds=[],
        art=[
            {"material": "silk", "cx": 10.16, "cy": 10.16, "w": 10},
            {"material": "copper", "cx": 10.16, "cy": 10.16, "w": 10},
        ],
    )
    resp = client.post(
        "/generate",
        data={
            "params": json.dumps(params),
            "art0": (io.BytesIO(_logo_bytes()), "a.png"),
            "art1": (io.BytesIO(_logo_bytes()), "b.png"),
        },
        content_type="multipart/form-data",
    )
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("carved/carved.kicad_pcb").decode()
    assert '(layer "F.Mask")' in board
    import re

    silk = len(re.findall(r'\(gr_poly [^\n]+\(layer "F\.SilkS"\)', board))
    assert silk == 0  # identical art fully covered by the copper opening


@pytest.mark.parametrize("material", ["glow", "bare"])
def test_a_window_leaves_only_board_crossing_copper_on_a_units_far_face(client, material):
    # A back-mounted inline unit under a full-coverage through window. The
    # window used to be carved around the WHOLE unit on both faces, so the
    # front pour survived as a dead slab shadowing the part, plainly visible
    # through a bare window or a translucent mask, and connected to nothing
    # the front layer needs. Only copper that actually crosses the board (the
    # via, fed by its thin perimeter bridge) has any business on that face,
    # so each face's window is now carved by that face's own keepouts.
    img = Image.new("L", (200, 200), 0)  # all black: full-coverage window art
    buf = io.BytesIO()
    img.save(buf, "PNG")
    params = _params(
        name="farface",
        leds=[{"x": 12, "y": 10, "color": "red", "side": "back",
               "layout": "inline", "size": "1206"}],
        art=[{"material": material, "threshold": 128,
              "cx": 10.16, "cy": 10.16, "w": 18}],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params),
              "art0": (io.BytesIO(buf.getvalue()), "win.png")},
        content_type="multipart/form-data",
    )
    board = invariants.assert_parses(
        zipfile.ZipFile(io.BytesIO(resp.data))
        .read("farface/farface.kicad_pcb").decode())
    from shapely.geometry import Point

    # Probe the res_in PAD center: the one spot where far-face shadow pour
    # could actually ship. Calibrated on a broken build (both faces carved by
    # the back keepouts): at 1206 the pad keepouts leave gaps, so the shadow
    # fractures and the floating-copper filter already drops every fragment
    # except the one touching the via anchor, the res_in/via piece. Probes
    # at the part bodies or the other pads read False on broken code too
    # (measured; the first two drafts of this test survived their mutant).
    # Unit anchor (12, 10), inline 1206: res_in pad center (4.7875, 10).
    front = list(board.emitted_fills("F.Cu"))
    assert front, ("the front pour vanished entirely: that is a missing "
                   "3V3 plane, not a well-carved window")
    spot = Point(4.7875, 10.0)
    assert not any(p.contains(spot) for p in front), (
        f"the {material} window left front pour at ({spot.x}, {spot.y}), "
        "shadowing a unit that is mounted on the back: dead copper the "
        "user sees straight through their window")
    # Contrast: the unit's own face keeps the pour hugging its cathode pad;
    # that copper is the LED's ground connection, not a shadow.
    pad = Point(13.5375, 10.0)
    assert any(p.contains(pad) for p in board.emitted_fills("B.Cu")), (
        "the window ate the back pour at the LED cathode pad; the unit's own "
        "face must keep the copper its pads connect through")


def test_a_far_side_led_leaves_no_ghost_pads_on_either_face(client):
    # "LED on the other side" splits the unit's copper across the board, but
    # the window keepout still claimed the WHOLE footprint on BOTH faces,
    # so a window left a pad-shaped slab of pour where the LED's pads used
    # to be on the resistor face (only two via barrels live there), and a
    # resistor-shaped one on the LED face where no resistor is. The keepout
    # is per-face now; each face's window reclaims the other half's room.
    from shapely.geometry import Point

    img = Image.new("L", (200, 200), 0)  # all black: full-coverage window
    buf = io.BytesIO()
    img.save(buf, "PNG")
    params = _params(
        name="ghost",
        leds=[{"x": 10, "y": 10, "color": "red", "side": "back",
               "layout": "inline", "farled": True}],
        art=[{"material": "bare", "threshold": 128,
              "cx": 10.16, "cy": 10.16, "w": 18}],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params),
              "art0": (io.BytesIO(buf.getvalue()), "win.png")},
        content_type="multipart/form-data",
    )
    board = invariants.assert_parses(
        zipfile.ZipFile(io.BytesIO(resp.data))
        .read("ghost/ghost.kicad_pcb").decode())
    # Back inline farled unit at (10, 10): LED pads cross to the front,
    # resistor stays on the back. Probe inside each half's OLD footprint on
    # the face it left, off the via octagons, so only ghost copper answers.
    back = list(board.emitted_fills("B.Cu"))
    front = list(board.emitted_fills("F.Cu"))
    assert back and front, "a pour vanished entirely: that is a missing plane"
    ghost_pad = Point(11.025, 10.95)   # led_k pad room, resistor face (B)
    assert not any(p.contains(ghost_pad) for p in back), (
        "the window left a pad-shaped slab on the resistor face where the "
        "LED's pads used to be; only their via barrels live there")
    ghost_res = Point(4.725, 10.0)     # res_in pad room, LED face (F)
    assert not any(p.contains(ghost_res) for p in front), (
        "the window left resistor-shaped pour on the LED face: the "
        "resistor never crossed the board")
    # Contrast: the GND collar around the cathode's via-in-pad on the
    # resistor face is that LED's ground connection and must survive. The
    # probe sits 0.38 mm out, just past the 0.35 mm barrel, inside the
    # collar the keepout leaves (via radius + the 0.2 mm netclass minimum,
    # less the window's 0.1 mm registration expansion).
    collar = Point(11.405, 10.0)
    assert any(p.contains(collar) for p in back), (
        "the window ate the pour collar around the cathode's via-in-pad: "
        "the LED ships wired to nothing")


@pytest.mark.slow  # two full fill computations (webapp accept + direct refuse)
def test_a_far_side_led_without_its_power_via_still_downloads(client):
    # "No power via" plus "LED on the other side", under a window: the GND
    # contact on the resistor face is just the via-in-pad collar, tied to the
    # plane by its perimeter bridge. resolve_novia's first pass judged the
    # fill WITHOUT bridges, saw an isolated collar, and refused the download:
    # 78 of 100 reasonable placements 400'd while the 2D preview showed a
    # working route. The pass now unions the unit's bridge, like pass two
    # always did.
    params = _params(
        name="nvf",
        leds=[{"x": 6, "y": 6, "color": "red", "side": "back",
               "layout": "inline", "novia": True, "farled": True}],
        art=[{"kind": "rect", "material": "bare",
              "cx": 9.2, "cy": 8.4, "w": 10.5, "h": 10.5}],
    )
    resp = client.post("/generate", data={"params": json.dumps(params)},
                       content_type="multipart/form-data")
    assert resp.status_code == 200, (
        f"a buildable no-power-via far-side board was refused: {resp.data[:200]}")
    invariants.assert_parses(
        zipfile.ZipFile(io.BytesIO(resp.data)).read("nvf/nvf.kicad_pcb").decode())
    # Contrast: a window drawn WITHOUT the webapp's pad halos really does
    # strand the collar's ring fragment (measured: the shipped board has 3
    # unconnected items), and that board must still be refused.
    from minibadge_designer import pcb

    broken = pcb.BadgeSpec(
        leds=[pcb.Led(x=10, y=10, side="back", novia=True, farled=True)],
        art=[pcb.ArtLayer("bare", rects=[(2.0, 2.0, 16.0, 16.0)])])
    _leds, bad = pcb.resolve_novia(broken)
    assert bad == [0], (
        "a genuinely stranded no-power-via unit slipped past resolve_novia: "
        "the user downloads a board whose LED is wired to nothing")


def _window_board(client, tenting):
    """A back inline unit under a full-coverage bare window, generated for
    the mask-cover tests: its 3V3 bridge and its via both cross the opening."""
    img = Image.new("L", (200, 200), 0)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    params = _params(
        name="masked",
        tenting=tenting,
        leds=[{"x": 12, "y": 10, "color": "red", "side": "back",
               "layout": "inline", "size": "1206"}],
        art=[{"material": "bare", "threshold": 128,
              "cx": 10.16, "cy": 10.16, "w": 18}],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params),
              "art0": (io.BytesIO(buf.getvalue()), "win.png")},
        content_type="multipart/form-data",
    )
    text = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "masked/masked.kicad_pcb").decode()
    return text, invariants.assert_parses(text)


def _mask_openings(board, layer):
    """The mask-opening gr_polys on one face, as shapely polygons (board mm)."""
    from shapely.geometry import Polygon

    from minibadge_designer import pcb

    out = []
    for g in board.graphics(layer):
        if g[0] != "gr_poly":
            continue
        pts = next(c for c in g[1:] if isinstance(c, list) and c[0] == "pts")
        out.append(Polygon([(float(p[1]) - pcb.ORIGIN, float(p[2]) - pcb.ORIGIN)
                            for p in pts[1:]]))
    return out


def test_a_bridge_crossing_a_window_stays_under_soldermask(client):
    # The perimeter bridge is a hairline of copper across the window, and the
    # mask used to open right over it: the trace shipped plated bare, a
    # corrosion and short hazard no fab leaves on purpose, and the exposed
    # gold streak the user reported in the 3D view. The window's mask opening
    # now keeps a dam of mask over every bridge; the copper cut underneath is
    # untouched, so the window still works.
    from shapely.geometry import Point

    _text, board = _window_board(client, tenting=True)
    openings = _mask_openings(board, "F.Mask")
    assert openings, "the bare window opened no front mask at all"
    # The unit at (12, 10) bridges its via (3.7875, 10) left to the ring;
    # probe the run's midpoint, and the same window 1.5 mm off the trace.
    dam = Point(2.37, 10.0)
    win = Point(2.37, 8.5)
    assert any(p.contains(win) for p in openings), (
        "the window fails to open the mask even beside the bridge: that is "
        "a missing window, not a mask dam")
    assert not any(p.contains(dam) for p in openings), (
        "the mask opens right over the perimeter bridge: its copper ships "
        "plated bare across the window")


@pytest.mark.parametrize("tenting", [True, False])
def test_the_via_keeps_its_mask_cap_only_while_the_board_is_tented(client, tenting):
    # Tenting is a board-wide fab option (it lives with mask color and finish
    # in the Shape panel): tented vias keep soldermask over their annulus
    # (including a cap where a window crosses them), while exposed vias plate
    # bare and say so in the file with KiCad 9's per-via `(tenting none)`.
    from shapely.geometry import Point

    text, board = _window_board(client, tenting=tenting)
    openings = _mask_openings(board, "F.Mask")
    assert openings, "the bare window opened no front mask at all"
    # A point on the via annulus (via at (3.7875, 10), barrel r 0.35),
    # far enough from the bridge dam's end cap not to be shadowed by it.
    annulus = Point(3.7875, 10.3)
    exposed = any(p.contains(annulus) for p in openings)
    assert exposed == (not tenting), (
        "a tented via must keep its mask cap inside a window"
        if tenting else
        "an exposed-vias board must open the window over the via annulus")
    assert ("(tenting none)" in text) == (not tenting), (
        "(tenting none) must be written exactly when the board opts out of "
        "tenting: KiCad's default is tented, so silence means covered")


def test_texts_pass_through_and_sanitize(client):
    texts = [
        {"x": 10, "y": 4, "text": "front text", "size": 2.0},
        {"x": 10, "y": 15, "text": "back\x00 text", "size": 5000, "side": "back"},
        {"x": 10, "y": 10, "text": "   "},
    ]
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(texts=texts, name="texty"))},
        content_type="multipart/form-data",
    )
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("texty/texty.kicad_pcb").decode()
    assert '(gr_text "front text"' in board
    assert '(gr_text "back text"' in board  # control char stripped
    assert "(size 119 119)" in board        # size clamped to TEXT_SIZE_MM's ceiling
    assert board.count("gr_text") == 2      # whitespace-only text dropped


def test_image_cuts_override_connector_strips(client):
    # A narrow vertical bar silhouette: the outline must follow the bar plus
    # small pad plates, NOT full-width connector strips. The area between a
    # pad plate and the bar (old strip territory) must be empty board-less
    # space, and the bar reaches the pad rows via bridges.
    img = Image.new("RGB", (100, 260), "white")
    ImageDraw.Draw(img).rectangle((30, 0, 70, 259), fill="black")  # bar
    buf = io.BytesIO()
    img.save(buf, "PNG")
    resp = client.post(
        "/outline",
        data={
            "params": json.dumps({
                "rows": ["top", "bottom"],
                "shape": {"mode": "image", "w": 20, "cx": 10.16, "cy": 10.16,
                          "threshold": 128, "smooth": 0.12},
            }),
            "shape": (io.BytesIO(buf.getvalue()), "bar.png"),
        },
        content_type="multipart/form-data",
    )
    data = resp.get_json()
    assert data["rings"] is not None
    from shapely.geometry import Point, Polygon

    poly = Polygon(data["rings"][0], data["rings"][1:])
    # Pad plates present at the corners...
    assert poly.contains(Point(2.5, 1.5)) and poly.contains(Point(17.7, 18.6))
    # ...the bar present in the middle...
    assert poly.contains(Point(10.16, 10.0))
    # ...but the gaps between the plates and the bar (old full-width strip
    # territory) are NOT solid board any more: at most a thin bridge
    # crosses them, never the whole band.
    from shapely.geometry import box as sbox

    left_gap = sbox(5.05, 16.92, 6.1, 20.16)
    right_gap = sbox(14.25, 0.16, 15.27, 3.4)
    assert not poly.contains(left_gap)
    assert not poly.contains(right_gap)


def test_polygon_sides_in_outline_and_art(client):
    # A "hex" element with sides=3 must produce a 3-cornered outline ring,
    # not a hexagon, and an art shape layer passes sides through too.
    params = _params(
        name="pgon",
        leds=[],
        art=[{"kind": "hex", "sides": 5, "material": "silk", "side": "front",
              "cx": 10.16, "cy": 10.16, "w": 8, "h": 8, "rot": 0}],
        shape={"mode": "custom", "elements": [
            {"kind": "hex", "op": "add", "cx": 10.16, "cy": 10.16,
             "w": 30, "sides": 3, "rot": 0},
        ]},
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("pgon/pgon.kicad_pcb").decode()
    import re

    edge = re.search(r'\(gr_poly \(pts ((?:\(xy [-\d. ]+\) ?)+)\)[^\n]*Edge\.Cuts', board)
    assert edge is not None
    xs = [float(m) for m in re.findall(r"\(xy ([-\d.]+) ", edge.group(1))]
    # A triangle outline (plus the connector pad plates): nothing near the
    # hexagon's mid-height leftmost/rightmost vertices at x = 10.16 +/- 15.
    assert min(xs) > 100 - 5.2  # ORIGIN + 10.16 - 15 would be ~95; triangle stays right of that
    silk = re.search(r'\(gr_poly \(pts ((?:\(xy [-\d. ]+\) ?)+)\)[^\n]*F\.SilkS', board)
    assert silk is not None
    pent = re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", silk.group(1))
    assert len(pent) == 5  # exact vector pentagon


def test_led_size_and_reverse_parse(client):
    params = _params(
        name="sizes",
        art=[],
        leds=[
            {"x": 5, "y": 6, "color": "red", "size": "0603"},
            # legacy spelling AND the new flag: reverse forces 1206 either way
            {"x": 14, "y": 14, "color": "blue", "layout": "reverse", "size": "0805"},
            {"x": 5, "y": 13, "color": "white", "layout": "inline",
             "reverse": True, "size": "0603"},
            {"x": 10, "y": 17, "color": "green", "size": "bogus"},
        ],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    board = zf.read("sizes/sizes.kicad_pcb").decode()
    assert '"minibadge-designer:LED_RED_0603"' in board
    # reverse forces 1206 regardless of the requested size, and routes a hole
    assert '"minibadge-designer:LED_BLUE_1206"' in board
    assert '"minibadge-designer:LED_WHITE_1206"' in board  # inline + reverse flag
    assert board.count("(gr_circle") == 2 and 'Edge.Cuts' in board
    assert '"minibadge-designer:LED_GREEN_0805"' in board  # bogus size falls back
    bom = zf.read("sizes/BOM.csv").decode()
    assert "reverse-mount" in bom


def test_art_hugs_unit_copper_not_bbox(client):
    # A silk shape inside the unit's old bounding rectangle but clear of its
    # actual copper (pads/via/traces + margins) now survives the carve.
    params = _params(
        name="hug",
        leds=[{"x": 10, "y": 10, "color": "red", "layout": "stacked"}],
        art=[{"kind": "rect", "material": "silk", "side": "front",
              "cx": 7.0, "cy": 6.5, "w": 0.5, "h": 0.5, "rot": 0}],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read("hug/hug.kicad_pcb").decode()
    import re

    assert re.search(r'\(gr_poly [^\n]+\(layer "F\.SilkS"\)', board)
    # But art overlapping a pad piece is still carved away entirely.
    params["art"][0]["cx"], params["art"][0]["cy"] = 9.05, 7.4  # on the res pad
    params["name"] = "carved"
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read("carved/carved.kicad_pcb").decode()
    assert not re.search(r'\(gr_poly [^\n]+\(layer "F\.SilkS"\)', board)


def test_advanced_placement_roundtrip(client):
    params = _params(
        name="advtest",
        art=[],
        leds=[{"x": 10, "y": 10, "color": "red", "layout": "stacked",
               "adv": {"rx": 5.0, "ry": 0.0, "rrot": 90, "vx": -2.0, "vy": 2.0}}],
    )
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "advtest/advtest.kicad_pcb").decode()
    import re

    # resistor at LED+(5,0), pads carrying the extra 90°; via at LED+(-2,2)
    assert re.search(r'"minibadge-designer:220R_0805" \(layer "F\.Cu"\) \(tstamp [0-9a-f-]+\)\n'
                     r"    \(at 115 110\)", board)
    assert "(via (at 108 112)" in board
    # oversized offsets are clamped to the ±20 mm reach
    params["leds"][0]["adv"] = {"rx": 999, "ry": 0, "vx": -999, "vy": 0}
    params["name"] = "advclamp"
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "advclamp/advclamp.kicad_pcb").decode()
    from minibadge_designer import pcb as _pcb

    led = _pcb.Led(10, 10, "red",
                   adv={"rx": 20.0, "ry": 0, "rrot": 0, "vx": -20.0, "vy": 0})
    assert _pcb.led_geometry(led)["res"] == (20.0, 0.0)
    # a non-numeric adv value is a 400, like any other bad led parameter
    params["leds"][0]["adv"] = {"rx": "junk"}
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_model3d_returns_glb(client):
    from minibadge_designer import webapp as _w

    params = _params(name="glb", art=[],
                     leds=[{"x": 7, "y": 6, "color": "red"}])
    resp = client.post(
        "/model3d",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    if _w._kicad_cli() is None:
        assert resp.status_code == 501
        return
    assert resp.status_code == 200
    assert resp.data[:4] == b"glTF"  # binary glTF magic
    assert len(resp.data) > 50000
    # bad design errors like /generate does
    params["leds"] = [{"x": 5, "y": 5, "color": "red", "rot": "x"}]
    resp = client.post(
        "/model3d",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_footprints_reference_3d_models(client):
    params = _params(name="mdl", art=[],
                     leds=[{"x": 7, "y": 6, "color": "red", "size": "1206"}])
    resp = client.post(
        "/generate",
        data={"params": json.dumps(params)},
        content_type="multipart/form-data",
    )
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read("mdl/mdl.kicad_pcb").decode()
    assert "LED_SMD.3dshapes/LED_1206_3216Metric.step" in board
    assert "Resistor_SMD.3dshapes/R_1206_3216Metric.step" in board


def test_glb_layers_tagged_and_opaque():
    """The 3D viewer's opacity sliders group materials by name, so the export
    must label each one with the board layer it paints, and start opaque."""
    import json
    import struct

    from minibadge_designer.webapp import _tag_glb_layers

    gltf = {
        "materials": [
            {"pbrMetallicRoughness": {"baseColorFactor": [0, 0, 0, 0.83]},
             "alphaMode": "BLEND"},                                    # mask
            {"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]}},  # silk
            {"pbrMetallicRoughness": {"baseColorFactor": [1, 0, 0, 1]}},  # a part
            {"pbrMetallicRoughness": {"baseColorFactor": [0, 1, 0, 1]}},  # shared
        ],
        "meshes": [
            {"name": "brd_soldermask", "primitives": [{"material": 0}]},
            {"name": "brd_silkscreen", "primitives": [{"material": 1}]},
            {"name": "LED_D3.0mm", "primitives": [{"material": 2}]},
            # used by a board layer AND a part: too ambiguous to attribute
            {"name": "brd_copper", "primitives": [{"material": 3}]},
            {"name": "R_0805_2012Metric", "primitives": [{"material": 3}]},
        ],
    }
    body = json.dumps(gltf).encode()
    body += b" " * (-len(body) % 4)
    blob = (struct.pack("<4sII", b"glTF", 2, 20 + len(body))
            + struct.pack("<I4s", len(body), b"JSON") + body)

    out = _tag_glb_layers(blob)
    ln, _ = struct.unpack_from("<I4s", out, 12)
    got = json.loads(out[20:20 + ln])
    names = [m["name"].split(":")[0] for m in got["materials"]]
    assert names == ["soldermask", "silkscreen", "components", "components"]
    # nothing ships translucent: the viewer dials transparency in on request
    assert all(m["pbrMetallicRoughness"]["baseColorFactor"][3] == 1.0
               for m in got["materials"])
    assert not any(m.get("alphaMode") == "BLEND" for m in got["materials"])


# ===========================================================================
# The hostile boundary: what a bad request gets back
#
# `tests/hostile.py` already proves these payloads do not crash the server, and
# `probe()` returns None on purpose so no test over that corpus can say more.
# What it cannot see is whether the user is left with anything to act on: the
# client does `throw new Error((await resp.json()).error || resp.statusText)`,
# so a rejection without a JSON `error` string reaches the badge owner as a
# bare failure. These tests assert the status *class* and the error *shape*,
# never a status number and never the prose, both of which are incidental.
# ===========================================================================

_MALFORMED_DESIGNS = {
    # defect #12: valid JSON that is not an object. json.loads succeeds, so
    # the JSONDecodeError guard never fires and the first params.get() used to
    # raise from outside every try.
    "params-json-null": "null",
    "params-json-list": "[]",
    "params-json-number": "42",
    "params-json-string": '"hello"',
    # defect #13: a scalar where the connector pin list belongs. _parse_pins
    # runs before any of the handler's own guards.
    "pins-scalar": '{"pins": 5}',
    "rows-scalar": '{"rows": 5}',
    # defect #6: NaN geometry. Three separate raise sites, one per parameter
    # group, all deliberately off the 0805/front/stacked defaults: a
    # through-hole back-side unit, an inline unit's advanced resistor angle,
    # and a rotated TTF-less text.
    "led-x-nan": '{"leds":[{"x":NaN,"y":10,"color":"red","size":"3mm","side":"back"}]}',
    "led-adv-rrot-nan": ('{"leds":[{"x":10,"y":10,"color":"red","layout":"inline",'
                         '"size":"0603","adv":{"rrot":NaN}}]}'),
    "text-rot-nan": '{"texts":[{"x":10,"y":10,"text":"hi","size":2,"rot":NaN}]}',
}


@pytest.mark.webapp
@pytest.mark.parametrize("route", ["/generate", "/model3d"])
@pytest.mark.parametrize("design", sorted(_MALFORMED_DESIGNS))
def test_a_malformed_design_is_refused_with_a_message_the_ui_can_show(
        client, route, design):
    """A request the server cannot build a board from comes back as a refusal
    carrying an error string, on both download routes.

    If this breaks the user clicks Download, waits, and receives Flask's HTML
    500 page: `resp.json()` throws inside the client's error path, so the UI
    shows nothing at all and there is no hint that the coordinate they typed,
    or the pin list a stale saved design carries, is the thing to change.

    `/model3d` is here without a kicad-cli guard on purpose: every one of
    these is rejected in the shared parse phase, long before `_model_glb` is
    reached, so the route answers 4xx whether or not the tool is installed.
    """
    try:
        resp = client.post(route, data={"params": _MALFORMED_DESIGNS[design]},
                           content_type="multipart/form-data")
    except Exception as exc:
        # TESTING=True propagates instead of 500ing; in production this same
        # escape is the 500 page the browser renders.
        raise AssertionError(
            f"{route} crashed on the {design!r} payload instead of refusing "
            f"it: {type(exc).__name__}: {exc}") from exc
    invariants.assert_rejected(resp)


@pytest.mark.webapp
@pytest.mark.parametrize("mask_color,reaches_the_fab", [
    # Payloads that unbalance the stackup's s-expression when interpolated
    # raw. Measured on the unfixed code: `green")` drives the paren depth
    # negative, `g" (x` leaves it one too deep, and a trailing backslash runs
    # the quoted string away. (`green)` and `green") (gr_text "P` happen to
    # stay balanced, so they are useless as probes.)
    ('green")', "green"),
    ('g" (x', "green"),
    ("g\\", "green"),
    # Not an attack, just a colour no fab stocks.
    ("plutonium", "green"),
    # Legitimate choices, off the "green" default in both value and case. The
    # whitelist must not flatten a real pick to the fallback.
    ("Purple", "purple"),
    ("white", "white"),
])
def test_the_soldermask_colour_on_the_board_is_always_one_a_fab_can_build(
        client, mask_color, reaches_the_fab):
    """Whatever `mask_color` a request carries, the stackup names a real colour
    and the board file stays openable.

    Two separate ways for the user to lose here, and the corpus sees neither
    because both ship a 200. Unbalanced: the zip downloads, looks normal, and
    KiCad refuses to open the .kicad_pcb with no error the app ever showed.
    Unknown-but-balanced: KiCad opens it and the 3D view plus the fab order
    carry a colour that does not exist.

    `mask_color` is the one user string that reaches the board file neither
    whitelisted nor escaped: `name` is whitelisted by `_slug`, text content
    is escaped by `pcb._esc`. So this is the only test that can see it.
    """
    import types

    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(name="mask", art=[], leds=[],
                                           mask_color=mask_color))},
        content_type="multipart/form-data",
    )
    # assert_project_zip checks the four-member zip and the paren balance.
    board = invariants.assert_project_zip(resp, "mask")
    invariants.assert_fab_choices_reach_the_stackup(
        invariants.assert_parses(board),
        types.SimpleNamespace(mask_color=reaches_the_fab, finish="enig"))


# ===========================================================================
# Upload complexity: a request that never comes back
#
# The exact SVG pipeline's cost is superlinear in the geometry it is handed and
# used to be uncapped. Measured on this repo before `MAX_SVG_COMPLEXITY`
# landed, all returning 200: a single <path> of 40 000 line vertices (272 KB)
# took 5.3 s, 80 000 took 25.7 s and 200 000 took 493 s, and Bezier segments
# cost ~4 ms each on top. Every one of those is far under the 24 MiB upload cap
# and lands in the range an auto-traced logo reaches routinely.
#
# A hang is invisible to a status check, so the wall clock IS the assertion
# here. The budget is `hostile.SVG_BUDGET_S`, the same number the corpus uses,
# and a product statement rather than a benchmark: a logo has to come back
# while the user is still looking at the preview. /outline runs on every edit.
# ===========================================================================

def _line_svg(points: int) -> bytes:
    """A single <path> of `points` straight-line vertices."""
    d = b" ".join(b"L%d %d" % (i % 100, (i * 7) % 100) for i in range(points))
    return (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            b'<path fill="#000" d="M0 0 ' + d + b' Z"/></svg>')


def _curve_svg(segments: int) -> bytes:
    """`segments` separate cubic-Bezier paths: the shape that costs most per
    byte, because each long curve flattens to up to 256 chords."""
    ps = b"".join(b'<path fill="#000" d="M%d %d C%d %d %d %d %d %d Z"/>'
                  % (i % 97, (i * 3) % 97, i % 97, (i * 13) % 97,
                     (i * 7) % 97, (i * 29) % 97, (i * 3) % 97, (i * 11) % 97)
                  for i in range(segments))
    return (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            + ps + b'</svg>')


#: Payloads whose exact geometry is past `MAX_SVG_COMPLEXITY`. Both are held
#: under 500 KB on purpose: over that, werkzeug's *test client* spools the
#: request body to a temp file it never closes, and the resulting
#: ResourceWarning lands on whichever unlucky test triggers the next GC.
#: 60 000 line vertices measured 12 s uncapped (200 000 measured 493 s, on the
#: same curve); 4 000 cubic segments measured 15.5 s uncapped.
_OVERSIZED_SVGS = {
    "60k-line-vertices": lambda: _line_svg(60_000),
    "4k-cubic-segments": lambda: _curve_svg(4_000),
}

#: (label, params, SVG form field, raster form field). The board outline and an
#: artwork layer are separate entry points into the same exact pipeline, and
#: /outline reaches only the first, so both have to be covered.
_SVG_UPLOAD_SITES = {
    "board-shape": ({"shape": {"mode": "image", "cx": 10.16, "cy": 10.16, "w": 18}},
                    "shape", "shape_raster"),
    "art-layer": ({"art": [{"material": "silk", "cx": 10.16, "cy": 10.16, "w": 12}]},
                  "art0", "art0_raster"),
}


@pytest.mark.webapp
@pytest.mark.parametrize("site", sorted(_SVG_UPLOAD_SITES))
@pytest.mark.parametrize("svg", sorted(_OVERSIZED_SVGS))
def test_an_svg_too_detailed_to_trace_exactly_falls_back_to_the_raster_render(
        client, site, svg):
    """An SVG past the detail cap still produces a project, using the browser's
    raster render of the same file, and comes back promptly.

    This is the ordinary case: the UI ships a `*_raster` alongside every SVG
    precisely so a file the exact pipeline cannot handle (a gradient fill, and
    now an unprintable amount of detail) degrades instead of failing. If this
    breaks, someone who dropped in an auto-traced logo watches the download
    spin for eight minutes and then gets a board anyway, or is refused work
    the app can perfectly well do at raster fidelity.
    """
    import time

    import hostile

    overrides, field, raster_field = _SVG_UPLOAD_SITES[site]
    data = {
        "params": json.dumps(_params(name="big", leds=[], **{"art": [], **overrides})),
        field: (io.BytesIO(_OVERSIZED_SVGS[svg]()), "x.svg"),
        raster_field: (io.BytesIO(_logo_bytes()), "x.png"),
    }
    started = time.monotonic()
    resp = client.post("/generate", data=data, content_type="multipart/form-data")
    elapsed = time.monotonic() - started
    invariants.assert_project_zip(resp, "big")
    assert elapsed <= hostile.SVG_BUDGET_S, (
        f"{site}/{svg} took {elapsed:.1f}s against a {hostile.SVG_BUDGET_S}s "
        "budget: the fallback is supposed to skip the expensive pipeline, not "
        "run it first")


@pytest.mark.webapp
@pytest.mark.parametrize("site", sorted(_SVG_UPLOAD_SITES))
@pytest.mark.parametrize("svg", sorted(_OVERSIZED_SVGS))
def test_an_svg_too_detailed_to_trace_with_no_raster_is_refused_promptly(
        client, site, svg):
    """With no raster to fall back on, the same file is refused quickly rather
    than processed for minutes.

    A client that sends no `*_raster` (a script, an old build of the UI) has
    nothing to degrade to, so the honest answer is a refusal, and the refusal
    has to arrive in a moment, because the whole point of the cap is that the
    expensive pipeline never starts. If this breaks the request occupies a
    worker for the better part of ten minutes and the user has no way to tell
    that simplifying the path is what would fix it.
    """
    import time

    import hostile

    overrides, field, _raster = _SVG_UPLOAD_SITES[site]
    data = {
        "params": json.dumps(_params(name="big", leds=[], **{"art": [], **overrides})),
        field: (io.BytesIO(_OVERSIZED_SVGS[svg]()), "x.svg"),
    }
    started = time.monotonic()
    resp = client.post("/generate", data=data, content_type="multipart/form-data")
    elapsed = time.monotonic() - started
    invariants.assert_rejected(resp)
    assert elapsed <= hostile.SVG_BUDGET_S, (
        f"{site}/{svg} took {elapsed:.1f}s against a {hostile.SVG_BUDGET_S}s "
        "budget: a hang is invisible to a status check, so the clock is the "
        "assertion")


@pytest.mark.webapp
@pytest.mark.slow  # ~1.3 s: half the cap is genuinely expensive to trace
def test_a_logo_with_far_more_detail_than_a_badge_can_print_is_still_traced_exactly(
        client):
    """A 20 000-vertex path (half the cap, 136 KB) still goes through the
    exact vector pipeline and lands on the board.

    This is the false-positive side of the cap, and the reason it is set where
    it is. A limit tuned low enough to feel safe would silently push ordinary
    auto-traced artwork onto the raster pipeline (0.18 mm pixel staircase) or
    refuse it outright. No raster is sent here, so a `gr_poly` on the board is
    proof the exact path ran: there was nothing else it could have used.
    """
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(
                  name="fine", leds=[],
                  art=[{"material": "silk", "cx": 10.16, "cy": 10.16, "w": 12}])),
              "art0": (io.BytesIO(_line_svg(20_000)), "x.svg")},
        content_type="multipart/form-data",
    )
    board = invariants.assert_project_zip(resp, "fine")
    assert "gr_poly" in board, (
        "a 20k-vertex logo produced no polygons: the detail cap is rejecting "
        "artwork the app is supposed to trace exactly")


def _cli_stub(tmp_path, real: str, body: str) -> str:
    """A stand-in kicad-cli. Only the *external tool* is substituted here;
    the app, its locator and the whole HTTP path stay real."""
    import os

    stub = tmp_path / "kicad-cli-stub"
    stub.write_text(f'#!/bin/sh\n{body}\n'.replace("@REAL@", real))
    os.chmod(stub, 0o755)
    return str(stub)


@pytest.mark.webapp
@pytest.mark.kicad
@pytest.mark.slow  # one real GLB export
@pytest.mark.needs("kicad")
@pytest.mark.parametrize("size,side", [("1206", "front"), ("3mm", "back")])
def test_a_3d_export_that_reports_problems_still_shows_the_board(
        client, kicad_cli, tmp_path, monkeypatch, size, side):
    """When kicad-cli exits non-zero but has written a complete model, the
    viewer gets that model, flagged, not a 500.

    `kicad-cli pcb export glb` exits 1 for things that are not fatal to the
    output: most commonly it cannot substitute one component's 3D shape, says
    so on stderr, and writes the whole board anyway. Trusting the exit code
    turns that into "3D preview unavailable" for a badge that is perfectly
    fine, and the user has no way to tell a missing LED model from a broken
    board. Degrading honestly means: hand over the model, and say it may be
    incomplete rather than presenting it as the finished article.

    Both LED packages are off the 0805 default and one sits on the back, so
    the design carries several substituted component models rather than the
    minimum.
    """
    monkeypatch.setenv("KICAD_CLI", _cli_stub(
        tmp_path, kicad_cli, '@REAL@ "$@"\nexit 1'))
    resp = client.post(
        "/model3d",
        data={"params": json.dumps(_params(
            name="glb", art=[],
            leds=[{"x": 7, "y": 6, "color": "red", "size": size, "side": side}]))},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, (
        "a non-zero exit that still produced a model came back as "
        f"{resp.status_code}: {resp.get_data()[:200]!r}")
    assert resp.data[:4] == b"glTF", "response is not a binary glTF"
    assert resp.headers.get("X-Minibadge-Export-Warning"), (
        "the export reported problems and the response said nothing: a model "
        "with a part silently missing is presented as complete")


@pytest.mark.webapp
@pytest.mark.kicad
@pytest.mark.needs("kicad")
def test_a_3d_export_that_produces_no_model_is_still_reported_as_a_failure(
        client, kicad_cli, tmp_path, monkeypatch):
    """The contrast case: tolerating a non-zero exit must not become tolerating
    an empty response.

    Without this, "degrade honestly" would quietly widen into serving whatever
    bytes happen to be on disk, and the viewer would be handed nothing with no
    explanation. A tool that produced no model is a real failure and has to
    read as one.
    """
    monkeypatch.setenv("KICAD_CLI", _cli_stub(tmp_path, kicad_cli, "exit 1"))
    resp = client.post(
        "/model3d",
        data={"params": json.dumps(_params(name="glb", art=[], leds=[]))},
        content_type="multipart/form-data",
    )
    assert resp.status_code >= 500, (
        f"an export that wrote no model answered {resp.status_code}")
    body = resp.get_json()
    assert isinstance(body, dict) and body.get("error"), (
        f"failure carried no error message: {body!r}")


# ---------------------------------------------------------------------------
# The fab Gerber package (POST /gerbers)
#
# DRC never sees a Gerber: everything in this section is invisible to both
# in-process invariants and `kicad-cli pcb drc`, because those read the
# .kicad_pcb.  The only oracle for "the fab receives the right board" is the
# plotted package itself.
# ---------------------------------------------------------------------------

def _fab_params():
    """A design deliberately off every default the plot path reads: TH back
    LED, rotated inline 0603, HASL, black mask, half the pins dropped."""
    return _params(
        name="Fab Test!", mask_color="black", finish="hasl",
        pins=["1", "2", "15", "16"], art=[],
        leds=[
            {"x": 6, "y": 13, "color": "green", "size": "3mm", "side": "back"},
            {"x": 13, "y": 7, "color": "red", "size": "0603",
             "layout": "inline", "rot": 90},
        ],
        texts=[{"text": "fab", "x": 10, "y": 17, "size": 1.2, "side": "back"}],
    )


@pytest.mark.webapp
@pytest.mark.kicad
@pytest.mark.needs("kicad")
def test_fab_gerber_zip_uploads_to_a_board_house_as_is(client, kicad_cli):
    """The /gerbers zip is the whole fab handshake: flat, one file per layer
    plus a merged drill file, in the one dialect all the popular fabs accept
    (Protel extensions, plain RS-274X, metric decimal Excellon).

    A missing member, a folder wrapper, or X2 attributes each break a real
    upload: JLCPCB maps layers by the Protel extension, PCBWay documents that
    its CAM mishandles X2, and a zip whose mask or outline never arrived gets
    fabbed as a wrong-but-real board.
    """
    import re as _re
    import time

    started = time.monotonic()
    resp = client.post(
        "/gerbers",
        data={"params": json.dumps(_fab_params())},
        content_type="multipart/form-data",
    )
    elapsed = time.monotonic() - started
    assert resp.status_code == 200, resp.get_data()[:300]
    assert elapsed <= 30, f"fab export took {elapsed:.1f}s: hang territory"
    assert resp.headers.get("Content-Disposition", "").endswith("-gerbers.zip")

    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    names = zf.namelist()
    assert names, "the fab zip arrived empty"
    assert all("/" not in n for n in names), (
        f"fab zip must be flat; board-house upload forms read the archive "
        f"root: {names}")
    # The board-house contract, stated independently of webapp._FAB_LAYERS /
    # _FAB_EXTENSIONS: if someone trims that constant, this goes red.
    required = {"gtl", "gbl",          # copper
                "gts", "gbs",          # soldermask
                "gto", "gbo",          # silkscreen
                "gtp", "gbp",          # paste
                "gm1",                 # board outline
                "drl"}                 # drill
    have = {n.rsplit(".", 1)[-1].lower() for n in names}
    assert required <= have, f"fab zip is missing layers: {required - have}"
    assert have <= required | {"gbrjob"}, (
        f"unexpected extras in the fab zip may confuse a CAM auto-loader: "
        f"{have - required - {'gbrjob'}}")

    drills = [n for n in names if n.endswith(".drl")]
    assert len(drills) == 1, (
        f"PTH and NPTH must ship merged in one Excellon file, got {drills}")
    drl = zf.read(drills[0]).decode()
    assert "METRIC" in drl, "drill file is not metric; every fab guide asks for mm"
    # Fab-critical hole sizes, stated literally (not read back from pcb.py):
    # the 0.30 mm via drill is the cheap-tier minimum this project targets,
    # and 0.95 mm is the minibadge connector's plated hole.
    tools = [l for l in drl.splitlines() if _re.match(r"T\d+C", l)]
    assert any("C0.300" in t for t in tools), f"via drill missing: {tools}"
    assert any("C0.950" in t for t in tools), f"connector drill missing: {tools}"

    for n in names:
        if n.endswith((".drl", ".gbrjob")):
            continue
        text = zf.read(n).decode()
        assert "%FSLAX" in text, f"{n} lacks an RS-274X format header"
        assert "%TF" not in text, (
            f"{n} carries X2 attributes: PCBWay's CAM is documented to "
            "mishandle them; the package must stay plain RS-274X")
    edge = zf.read(next(n for n in names if n.endswith(".gm1"))).decode()
    assert "D01*" in edge, "the board outline plotted empty"


@pytest.mark.webapp
@pytest.mark.kicad
@pytest.mark.needs("kicad")
def test_gerbers_are_refused_when_zones_cannot_be_refilled(
        client, kicad_cli, tmp_path, monkeypatch):
    """A server that cannot refill the pours must refuse the fab package, not
    ship it: the .kicad_pcb's fills carry fracture slits to the board edge (a
    file-format constraint), and plotting them puts hairline copper gaps
    across both power planes on the physical boards.

    Only the environment is substituted: a poisoned PYTHONPATH shadows pcbnew
    for every interpreter the refill could reach (the subprocess inherits it,
    and PYTHONPATH outranks site-packages), so no pcbnew exists anywhere as
    far as this request is concerned.  The locator, route, plotting tool and
    refusal logic all run for real, which is the point: if the refusal is
    ever dropped, the real kicad-cli plots the slit board successfully and
    this test sees the 200 it must never see.
    """
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "pcbnew.py").write_text(
        'raise ImportError("pcbnew unavailable (poisoned by '
        'test_gerbers_are_refused_when_zones_cannot_be_refilled)")\n')
    monkeypatch.setenv("PYTHONPATH", str(poison))

    resp = client.post(
        "/gerbers",
        data={"params": json.dumps(_fab_params())},
        content_type="multipart/form-data",
    )
    assert resp.status_code >= 500, (
        f"a fab package the server could not refill answered "
        f"{resp.status_code}; slit copper may have shipped")
    body = resp.get_json()
    assert isinstance(body, dict) and body.get("error"), (
        f"the refusal carried no error message: {body!r}")


@pytest.mark.webapp
@pytest.mark.kicad
@pytest.mark.slow  # two real plots and a pcbnew refill
@pytest.mark.needs("kicad")
def test_fab_copper_is_the_refilled_fill_not_the_slit_open_form(
        client, kicad_cli, tmp_path):
    """The copper in the fab package is what KiCad's own filler produces, not
    the slit-open form stored in the .kicad_pcb.

    Differential oracle: plot the very board /generate ships, without a
    refill, with the same plot flags. If /gerbers ever skips the refill its
    copper comes out byte-identical to that baseline (gerber plots are
    deterministic modulo G04 comment lines; measured: two plots of one board
    agree exactly, while slit vs refilled differ by hundreds of outline
    vertices).  Equality here therefore means slit copper shipped.

    Only the copper layers are plotted for the baseline, with the
    copper-relevant flags copied from the endpoint (no-x2, no-netlist).  If
    the endpoint's plot dialect ever drifts from these, the two plots become
    trivially different and this test silently loses power rather than going
    red; keep them in step when changing the export.
    """
    import subprocess

    params = _params(
        name="slitcheck", mask_color="purple", finish="enig", art=[
            # A glow window voids both pours (kind layers need no upload), so
            # the shipped fill is guaranteed to carry slits to refill away.
            {"kind": "circle", "material": "glow", "cx": 10.16, "cy": 10.16,
             "w": 6, "h": 6},
        ],
        leds=[{"x": 6, "y": 6, "color": "red", "size": "1206"},
              {"x": 14, "y": 14, "color": "blue", "side": "back"}])
    data = {"params": json.dumps(params)}

    gen = client.post("/generate", data=dict(data),
                      content_type="multipart/form-data")
    assert gen.status_code == 200, gen.get_data()[:300]
    board = zipfile.ZipFile(io.BytesIO(gen.data)).read(
        "slitcheck/slitcheck.kicad_pcb")
    src = tmp_path / "slitcheck.kicad_pcb"
    src.write_bytes(board)
    run = subprocess.run(
        [kicad_cli, "pcb", "export", "gerbers", "-o", str(tmp_path) + "/",
         "--layers", "F.Cu,B.Cu", "--no-x2", "--no-netlist", str(src)],
        capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr[-300:]

    fab = client.post("/gerbers", data=dict(data),
                      content_type="multipart/form-data")
    assert fab.status_code == 200, (
        f"the fab endpoint refused a board /generate accepted: "
        f"{fab.get_data()[:300]}")
    zf = zipfile.ZipFile(io.BytesIO(fab.data))

    def stripped(text: str) -> str:
        # G04 lines carry the plot timestamp; everything else is geometry.
        return "\n".join(l for l in text.splitlines()
                         if not l.startswith("G04"))

    for ext, layer in (("gtl", "F_Cu"), ("gbl", "B_Cu")):
        baseline = stripped(
            (tmp_path / f"slitcheck-{layer}.{ext}").read_text())
        shipped = stripped(zf.read(
            next(n for n in zf.namelist() if n.endswith(f".{ext}"))).decode())
        assert "%FSLAX" in shipped and "%FSLAX" in baseline
        assert shipped != baseline, (
            f"{layer}: the fab package's copper is identical to a no-refill "
            "plot; the zone refill was skipped and slit copper shipped")


def test_a_blinking_led_with_pin_9_dropped_is_refused_and_names_the_pin(client):
    """The preview shows a blinking LED; with pin 9 gone there is no clock to
    blink from. Shipping a silently-steady board would make the download lie
    about the design the user approved, so the app refuses and names the pin
    to put back. Off-default on purpose: the LED is back-side and the
    direct-trace hookup is selected, so the refusal cannot hinge on the
    jumper's existence.
    """
    resp = client.post("/generate", data={"params": json.dumps(_params(
        art=[],
        leds=[{"x": 14, "y": 12, "color": "blue", "side": "back", "clk": True}],
        clk={"jumper": False},
        pins=["1", "2", "7", "8", "10", "15", "16"],
    ))})
    invariants.assert_rejected(resp)
    assert "pin 9" in resp.get_json()["error"], (
        f"the refusal does not name pin 9: {resp.get_json()['error']!r}; the "
        "user cannot tell which checkbox to put back")


@pytest.mark.slow
def test_the_clk_jumper_yields_to_a_unit_parked_on_its_spot(client):
    """A unit was placed first; enabling blink drops the jumper onto it. The
    server must move the JUMPER (moving the unit would silently rearrange the
    board behind the user's back) and the shipped copper must still clear
    every unit pad by the fab's netclass minimum.

    0.2 mm is stated literally, not read from pcb.py (D2): it is the
    min_clearance the shipped .kicad_pro orders the fab's DRC to enforce.
    """
    jumper_home = (9.2, 18.45)  # pcb.JUMPER_AT, restated independently
    resp = client.post("/generate", data={"params": json.dumps(_params(
        art=[],
        leds=[{"x": jumper_home[0], "y": jumper_home[1] - 0.95, "color": "red"},
              {"x": 6, "y": 6, "color": "blue", "clk": True}],
        clk={"jumper": True, "x": jumper_home[0], "y": jumper_home[1]},
    ))})
    assert resp.status_code == 200, resp.data
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    text = zf.read(next(n for n in zf.namelist()
                        if n.endswith(".kicad_pcb"))).decode()
    b = invariants.assert_parses(text)
    jpads = [p for p in b.pads if "SolderJumper" in p.fp]
    assert jpads, "the jumper vanished from the board instead of moving over"
    unit_pads = [p for p in b.pads
                 if "SolderJumper" not in p.fp and "MiniBadge" not in p.fp]
    assert unit_pads, ("no unit pads on the board at all; the parked unit "
                       "vanished and the clearance sweep below proves nothing")
    for jp in jpads:
        for up in unit_pads:
            gap = jp.copper().distance(up.copper())
            assert gap >= 0.2 - 1e-9, (
                f"jumper pad {jp.num} sits {gap:.3f} mm from pad {up.num} of "
                f"{up.ref}, under the 0.2 mm netclass minimum; the download "
                "ships a short the preview never showed")


def test_the_jumper_via_choice_never_strands_a_far_face_blinker(client):
    """The "feed 3V3 through a via" switch only picks how the back jumper's
    STEADY pad is wired (via into the front pour, or a trace to a 3V3 pin);
    it must never take the rail via away from a blinking LED on the other
    face. Off-default on purpose: back jumper, via off, front blinker.
    """
    resp = client.post("/generate", data={"params": json.dumps(_params(
        art=[],
        leds=[{"x": 6, "y": 6, "color": "red", "clk": True}],
        clk={"jumper": True, "side": "back", "via": False},
    ))})
    assert resp.status_code == 200, resp.data
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    text = zf.read(next(n for n in zf.namelist()
                        if n.endswith(".kicad_pcb"))).decode()
    b = invariants.assert_parses(text)
    rail = b.net_of.get("CLK_LED")
    assert any(v.net == rail for v in b.vias), (
        "no rail via on the board: the front blinker has no plated hole to "
        "reach the back-side jumper through, and its LED ships dark")
    v3 = b.net_of.get("3V3")
    assert not any(v.net == v3 for v in b.vias), (
        "a 3V3 via shipped although the user chose the traced hookup; the "
        "board carries a drill the design turned off")


# ---------------------------------------------------------------------------
# Traced art: an uploaded picture reaches the board as polygons, not as a grid
# of pixel squares. The metrics below are calibrated (see the numbers quoted in
# each test) against the pixel-rect path this replaced.
# ---------------------------------------------------------------------------

def _disc_png(px: int = 600, margin: int = 20) -> bytes:
    """An opaque circle: the one shape whose true edge a test can recompute."""
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((margin, margin, px - margin, px - margin),
                                fill=(0, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _block_png(px: int = 400) -> bytes:
    img = Image.new("RGBA", (px, px), (0, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _art_rings(board: str, layer: str) -> list[list[tuple[float, float]]]:
    """Every ``gr_poly`` ring on one layer, in board mm."""
    import re

    out = []
    for pts, lay in re.findall(
            r"\(gr_poly \(pts ((?:\(xy [-\d. ]+\) ?)+)\)[^\n]*?\(layer \"([^\"]+)\"\)",
            board):
        if lay == layer:
            out.append([(float(x) - pcb_mod.ORIGIN, float(y) - pcb_mod.ORIGIN)
                        for x, y in re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", pts)])
    return out


def _axis_aligned_fraction(ring: list[tuple[float, float]]) -> float:
    """How much of a ring's perimeter runs along the sampling grid's axes.

    This is the staircase's signature and the reason the metric is a ratio
    rather than a length: a region tiled out of axis-aligned rectangles scores
    exactly 1.0 whatever its size, while a traced curve scores near zero
    because only the odd chord happens to be level or plumb.
    """
    import math

    total = axis = 0.0
    for i, (x0, y0) in enumerate(ring):
        x1, y1 = ring[(i + 1) % len(ring)]
        seg = math.hypot(x1 - x0, y1 - y0)
        if not seg:
            continue
        total += seg
        if abs(x1 - x0) < 1e-9 or abs(y1 - y0) < 1e-9:
            axis += seg
    return axis / total if total else 0.0


def _radial_spread(ring: list[tuple[float, float]], cx: float, cy: float) -> float:
    """Widest minus narrowest radius of a ring about a point.

    Zero for a true circle, whatever its radius -- so it measures roundness
    without the test having to know how big the placed artwork came out.
    """
    import math

    r = [math.hypot(x - cx, y - cy) for x, y in ring]
    return max(r) - min(r)


def _art_outline(rings) -> list[tuple[float, float]]:
    """The outline of the artwork itself, however the emitter split it up.

    A holed shape reaches the board as several rings that TILE it (they share
    their cut edges), so no single ring is the shape: the largest one carries
    an arbitrary share of the real outline plus two horizontal edges per cut,
    and a metric read off it moves when the emitter changes its mind about
    where to divide -- measured, at 0.93 against a 0.90 threshold, with the
    artwork itself untouched. Unioning first puts the shared edges back
    together and leaves exactly the shape the fab receives.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    merged = unary_union([Polygon(r) for r in rings if len(r) >= 3])
    biggest = max(getattr(merged, "geoms", [merged]), key=lambda g: g.area)
    return list(biggest.exterior.coords)[:-1]


def _rect_rings(rings) -> int:
    """Rings that are a bare axis-aligned rectangle -- one pixel-grid tile."""
    return sum(1 for r in rings
               if len(r) == 4 and _axis_aligned_fraction(r) == 1.0)


#: Roundness a ring must hold to print as a circle rather than as steps. A fab
#: registers silkscreen to about +/-0.15 mm and its ink spreads ~0.05 mm, so a
#: ring rounder than this cannot be told from a true circle on the finished
#: board. Stated here, not imported: the point is to fail if the tracer's own
#: tolerances are ever loosened past what the fab can hide. Measured: 0.083 mm
#: and 0.075 mm for the two cases below, against 0.174 mm and 0.168 mm on the
#: 0.18 mm pixel path, which is the regression this guards.
ROUND_ENOUGH_MM = 0.12


@pytest.mark.webapp
@pytest.mark.parametrize(
    "label,art,layer",
    [
        # Defaults on purpose only for the first case; the second moves off
        # every one of them: palette instead of threshold, exposed copper
        # instead of silkscreen, the back face instead of the front, and a
        # rotation that is not a multiple of 90 (which resamples the upload
        # through logo._transpose before it is ever classified).
        ("threshold-silk-front", {"mode": "threshold", "material": "silk",
                                  "rot": 0}, "F.SilkS"),
        ("palette-copper-back-rot45",
         {"mode": "palette", "rot": 45, "side": "back",
          "palette": [{"rgb": [0, 0, 0], "material": "copper"}]}, "B.Mask"),
    ],
)
def test_a_curved_raster_edge_reaches_the_board_as_a_curve_not_pixel_steps(
        client, label, art, layer):
    """A round logo prints round.

    An uploaded picture is classified on a sampling grid, but the cells are
    evidence for a boundary -- not squares of ink to print. When they are
    printed as squares the badge carries a visible staircase on every curve
    and every diagonal, which is what the user sees on the fabbed board and
    in the 3D preview, and no amount of DRC notices it.
    """
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(
                  name="round", leds=[], texts=[],
                  art=[{"cx": 10.16, "cy": 10.16, "w": 9.0, **art}])),
              "art0": (io.BytesIO(_disc_png()), "disc.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "round/round.kicad_pcb").decode()
    rings = _art_rings(board, layer)
    assert rings, f"{label}: no art polygon on {layer}"
    assert _rect_rings(rings) == 0, (
        f"{label}: {_rect_rings(rings)} of {len(rings)} rings are plain "
        "axis-aligned rectangles -- the circle was tiled out of pixel squares")
    edge = _art_outline(rings)
    assert _axis_aligned_fraction(edge) < 0.5, (
        f"{label}: {_axis_aligned_fraction(edge):.0%} of the outline runs "
        "along the sampling axes; a traced circle runs across them")
    assert _radial_spread(edge, 10.16, 10.16) <= ROUND_ENOUGH_MM, (
        f"{label}: the outline wanders {_radial_spread(edge, 10.16, 10.16):.3f} "
        f"mm in radius, past the {ROUND_ENOUGH_MM} mm a fab could hide")


@pytest.mark.webapp
def test_raster_art_carved_around_a_unit_keeps_the_keepouts_own_curve(client):
    """Art that has to dodge a blinker dodges it along the part's real shape.

    The carve used to drop whole grid cells whose centres a keepout covered,
    so the notch around a round part came out as steps and could leave ink up
    to half a cell inside the keepout -- silk creeping onto a pad. Off-default
    on purpose: a full-face silk layer (not a small logo) around a placed unit.
    """
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(
                  name="carve", texts=[],
                  leds=[{"x": 10.16, "y": 10.16, "color": "red"}],
                  art=[{"mode": "threshold", "material": "silk",
                        "cx": 10.16, "cy": 10.16, "w": 14.0}])),
              "art0": (io.BytesIO(_block_png()), "block.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "carve/carve.kicad_pcb").decode()
    rings = _art_rings(board, "F.SilkS")
    assert rings, "no silk art survived the carve"
    # Asked of the shape, not of each emitted ring: a holed layer is tiled
    # into pieces that share their cut edges, and a piece well away from the
    # carve is legitimately a rectangle -- what must not be a bare rectangle
    # is the artwork.
    assert _rect_rings([_art_outline(rings)]) == 0, (
        "the silk layer arrived as a bare axis-aligned rectangle: a pixel "
        "tile, not a carved shape")
    # The outer boundary of a square layer IS axis-aligned; the carve is what
    # has to run across the axes, so a mostly-but-not-wholly axial ring is the
    # signature of a real curve here. The pixel path measured 1.00.
    edge = _art_outline(rings)
    assert _axis_aligned_fraction(edge) < 0.9, (
        "the whole silk boundary runs along the sampling axes, so the notch "
        "around the unit is a staircase rather than the part's own outline")


@pytest.mark.webapp
def test_the_preview_resolves_artwork_at_the_pitch_the_tracer_samples_it_at():
    """What the editor draws for a picture is as fine as what gets fabbed.

    The canvas classifies an uploaded image itself, in JavaScript, and draws
    the result; the server classifies the same image and traces polygons from
    it. Two copies of one number, and if the browser's copy is coarser the
    editor shows a staircase the board will not have -- or, worse, is finer
    and promises detail the board drops. Both directions are the same defect:
    the user approves a badge they were not shown.
    """
    import re
    from pathlib import Path

    from minibadge_designer import logo
    from minibadge_designer import pcb as pcb_for_path

    html = (Path(pcb_for_path.__file__).parent / "templates" / "index.html").read_text()
    m = re.search(r"const TRACE_PIXEL_MM = ([\d.]+), MAX_TRACE_COLS = (\d+);", html)
    assert m, "the canvas no longer declares the tracer's sampling pitch at all"
    assert float(m.group(1)) == logo.TRACE_PIXEL_MM, (
        f"the preview classifies art at {m.group(1)} mm while the board is "
        f"traced from {logo.TRACE_PIXEL_MM} mm cells")
    assert int(m.group(2)) == logo.MAX_TRACE_COLS, (
        f"the preview caps art at {m.group(2)} cells across and the server at "
        f"{logo.MAX_TRACE_COLS}: on artwork wider than the cap the two "
        "disagree about how much detail survives")


#: How close to the board edge a light window may go, mm, and the copper cut
#: beneath it. Restated here rather than read from ``pcb`` so that moving
#: either one comes past this test: the editor refuses a window text at this
#: distance and the server clips every window to it, and the two have to be
#: the same number or the user is shown ink the download does not carry.
WINDOW_EDGE_CLEAR, WINDOW_COPPER_INSET = 1.2, 1.1


@pytest.mark.webapp
def test_the_window_edge_margin_leaves_the_pours_a_usable_perimeter_ring():
    """The editor, the server and the pour ring agree on one margin.

    A window cuts copper, so it is held off the board edge to leave the pours
    an unbroken ring right round the outside -- without it a full-width window
    saws a plane in half and the LEDs on the far piece are wired to nothing.
    Three copies of that decision have to line up: the number the canvas
    refuses a window text at, the number the server clips every window to, and
    the width of ring the copper cut actually leaves behind. The middle one is
    the one users feel: too large and it silently eats 0.4 mm of drawable board
    on all four sides, too small and the ring stops carrying what lives in it.

    The ring's tenants are asserted as relationships, not as a second literal,
    so a future loosening is measured against what it has to fit rather than
    against a number somebody typed.
    """
    import re
    from pathlib import Path

    from minibadge_designer import pcb

    html = (Path(pcb.__file__).parent / "templates" / "index.html").read_text()
    m = re.search(r"const WINDOW_EDGE_CLEAR = ([\d.]+);", html)
    assert m, "the canvas no longer declares how far a window keeps off the edge"
    assert float(m.group(1)) == WINDOW_EDGE_CLEAR == pcb.WINDOW_EDGE_CLEAR, (
        f"the editor holds windows {m.group(1)} mm off the board edge and the "
        f"server clips them at {pcb.WINDOW_EDGE_CLEAR} mm: one of them is "
        "lying to the user about where their window text may sit")
    assert pcb.WINDOW_COPPER_INSET == WINDOW_COPPER_INSET, (
        "the copper cut moved without this test: the mask opening has to sit "
        "inside it or the pour's edge is exposed through the window")
    assert pcb.WINDOW_EDGE_CLEAR - pcb.WINDOW_COPPER_INSET > 0, (
        "the copper cut must run wider than the mask opening, so fab "
        "registration slop cannot uncover the pour's cut edge")

    ring = pcb.WINDOW_COPPER_INSET - pcb.POUR_EDGE_INSET
    assert ring >= pcb.POUR_MIN_WIDTH, (
        f"the ring the window leaves is {ring:.2f} mm wide and the pours "
        f"declare a {pcb.POUR_MIN_WIDTH} mm minimum thickness: it is thinner "
        "than the copper KiCad will pour, so the ring is not there at all")
    bridge_reach = pcb.BRIDGE_INSET + pcb.TRACK_W / 2
    assert pcb.WINDOW_COPPER_INSET >= bridge_reach, (
        f"a unit's perimeter bridge reaches {bridge_reach:.2f} mm in from the "
        f"outline and the window now cuts copper from "
        f"{pcb.WINDOW_COPPER_INSET} mm: the bridge lands in cut-away copper "
        "and the unit it feeds is stranded")


@pytest.mark.webapp
@pytest.mark.parametrize(
    "name,expect",
    [
        # An accented name folds to the letters it is made of, rather than
        # losing them: this is the case that made "n_c_d" out of a real word.
        ("naïve café", "naive_cafe"),
        ("Grüße", "Gru_e"),
        # Nothing to fold: a script that does not decompose to ASCII leaves an
        # empty slug, and a generic folder beats a meaningless one.
        ("名前", "minibadge"),
        ("Ω", "minibadge"),
        # Off the defaults in the other direction: short names keep their own
        # title (a length floor would have eaten these), and the sanitiser's
        # existing duties still hold.
        ("v2", "v2"),
        ("../../etc/passwd", "etc_passwd"),
    ],
)
def test_a_project_name_reaches_the_zip_as_something_its_owner_can_recognise(
        client, project_files, name, expect):
    """The folder in the download is named after the badge, not after whatever
    survived an ASCII filter.

    The KiCad project's folder and file names have to be portable, so they are
    slugged; the question is what happens to the characters that cannot survive
    that. Dropping them turned "ünïcødé" into "n_c_d" — a name its owner cannot
    recognise on their own disk, which is the same failure as naming the folder
    at random.
    """
    resp = client.post(
        "/generate",
        data={"params": json.dumps(_params(name=name))},
        content_type="multipart/form-data",
    )
    files = project_files(resp)
    folders = {n.split("/")[0] for n in files if "/" in n}
    assert folders == {expect}, (
        f"{name!r} produced {folders}, not {expect!r}: the download is named "
        "something its owner would not recognise")
    assert f"{expect}/{expect}.kicad_pcb" in files, (
        f"the board file inside is not named {expect} either")


def _back_silk_polys(zf, name):
    """Every back-silk polygon on a generated board, as shapely shapes."""
    from shapely.geometry import Polygon

    root = invariants._parse_sexp(zf.read(f"{name}/{name}.kicad_pcb").decode())
    out = []
    for g in invariants._kids(root, "gr_poly"):
        if str(invariants._val(g, "layer")) != "B.SilkS":
            continue
        ring = [(float(q[1]) - pcb_mod.ORIGIN, float(q[2]) - pcb_mod.ORIGIN)
                for q in invariants._kids(invariants._kid(g, "pts"), "xy")]
        if len(ring) >= 3:
            out.append(Polygon(ring))
    return out


@pytest.mark.webapp
def test_switching_the_pin_captions_off_hands_the_strip_back_to_the_artwork(client):
    """With the captions gone, artwork prints where they would have been.

    The captions are a soldering aid, and they cost the drawing a band across
    every connector pair -- artwork is carved around them. Turning them off has
    to give that band back, or the switch only stops the ink printing while
    still reserving the room for it.
    """
    from shapely.geometry import Point

    png = _logo_bytes()
    covered = {}
    for on in (True, False):
        params = {
            "name": "strip", "pinlabels": on, "leds": [], "texts": [],
            "art": [{"mode": "threshold", "cx": 10.16, "cy": 10.16, "w": 22.0,
                     "material": "silk", "side": "back", "invert": True}],
        }
        resp = client.post("/generate", data={
            "params": json.dumps(params), "art0": (io.BytesIO(png), "ink.png"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200, resp.get_json()
        polys = _back_silk_polys(zipfile.ZipFile(io.BytesIO(resp.data)), "strip")
        assert polys, f"pinlabels={on}: the board carries no back silk at all"
        # The middle of a caption, which is where its ink would print.
        spots = [Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
                 for b in pcb_mod.caption_boxes(pcb_mod.ALL_PINS, True)]
        covered[on] = sum(any(p.contains(s) for p in polys) for s in spots)
        assert spots, "no captions to test the strip of"

    assert covered[False] > covered[True], (
        f"artwork covers {covered[False]} of the caption spots with the "
        f"captions off and {covered[True]} with them on: switching them off "
        "stops the ink printing but keeps the room reserved")
    assert covered[True] == 0, (
        f"artwork prints over {covered[True]} caption spots while the captions "
        "are on, and the fab will print them on top of each other")


@pytest.mark.webapp
def test_a_window_keeps_off_a_part_label_only_on_the_face_it_prints_on(client):
    """A back part's label costs the artwork nothing on the front.

    D1/R1 print on the face their part is mounted on, and a window may not
    open mask under them THERE -- the fab clips ink over an opening. On the
    other face there is no ink to protect, and the label's box used to be
    reserved on both cuts anyway: a through window wore a label-shaped hole
    on the face the label never touches, in the middle of the drawing.
    """
    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union

    params = {
        "name": "lab",
        "leds": [{"x": 10.16, "y": 10.5, "color": "red", "side": "back",
                  "layout": "inline", "size": "0805"}],
        "texts": [],
        "art": [{"kind": "rect", "material": "bare", "side": "through",
                 "cx": 10.16, "cy": 10.5, "w": 16.0, "h": 13.0}],
    }
    resp = client.post("/generate", data={"params": json.dumps(params)},
                       content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    root = invariants._parse_sexp(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "lab/lab.kicad_pcb").decode())

    def mask(layer):
        return unary_union([
            Polygon([(float(q[1]) - pcb_mod.ORIGIN, float(q[2]) - pcb_mod.ORIGIN)
                     for q in invariants._kids(invariants._kid(g, "pts"), "xy")])
            for g in invariants._kids(root, "gr_poly")
            if str(invariants._val(g, "layer")) == layer])

    front, back = mask("F.Mask"), mask("B.Mask")
    assert not front.is_empty and not back.is_empty, "the window opened nothing"
    labs = pcb_mod.refdes_layout(pcb_mod.BadgeSpec(leds=[
        pcb_mod.Led(10.16, 10.5, "red", side="back", layout="inline",
                    size="0805")]))
    assert len(labs) == 2, "the unit did not place both its labels"
    for lab in labs:
        assert lab["face"] == "back", "fixture drift: the labels moved face"
        cx = sum(q[0] for q in lab["quad"]) / 4
        cy = sum(q[1] for q in lab["quad"]) / 4
        assert front.contains(Point(cx, cy)), (
            f"{lab['ref']}: the FRONT window cut still avoids this back "
            "label's box, wearing a label-shaped hole in the artwork on a "
            "face the label never touches")
        assert not back.contains(Point(cx, cy)), (
            f"{lab['ref']}: the BACK window cut opens mask under the printed "
            "label, and the fab will clip the ink")


def _tab_slivers_mm2(rings, pins=("9", "10", "15", "16")) -> float:
    """Non-board wedges narrower than 0.6 mm within 1 mm of a kept pad tab.

    A closing (dilate then erode by 0.3 mm) of the outline near a tab fills
    exactly such wedges; whatever it fills that the outline itself lacks is
    the sliver. Calibrated: a flat triangle whose slanted sides cross the
    bottom tabs' top edges reads 0.213 mm^2 on the un-fixed union and the
    helmet silhouette reads 1.16 mm^2; both read 0.000 once the outline
    closes the gap.
    """
    from shapely.geometry import Polygon, box

    outline = Polygon(rings[0], rings[1:])
    total = 0.0
    for key in pcb_mod.active_pairs(pins):
        plate = box(*pcb_mod.PAD_PAIRS[key]["plate"])
        near = outline.intersection(plate.buffer(2.0))
        closed = near.buffer(0.3, join_style=2).buffer(-0.3, join_style=2)
        total += closed.difference(outline).intersection(plate.buffer(1.0)).area
    return total


def test_a_shape_meeting_a_pad_tab_at_a_shallow_angle_leaves_no_sliver():
    """A curved or slanted board edge crossing the straight top of a pad tab
    used to leave a wedge of non-board between them: a slit no fab can rout,
    and a notch in the mask right beside the pins on the badge. The outline
    must close that wedge -- and nothing else: a narrow slot the user cut on
    purpose away from the tabs is theirs to keep.

    Varied off the defaults: a triangle part (not the image or circle every
    other outline test uses), rotated 180 so it points down, wider than the
    board so both slanted edges cross the bottom tabs with a solid overlap
    (no bridge involved); the top pins dropped so only the bottom tabs exist.
    """
    from shapely.geometry import Polygon

    from minibadge_designer import webapp

    pins = ("9", "10", "15", "16")
    tri = {"mode": "custom", "smooth": 0, "elements": [
        {"kind": "triangle", "op": "add", "cx": 10.16, "cy": 15.5, "w": 30.0, "h": 11.0,
         "rot": 180, "sides": 6, "threshold": 128, "invert": False}]}
    rings, bridged = webapp._compute_outline(tri, {}, pins, {})
    assert rings is not None and not bridged, "the triangle overlaps both tabs solidly; no bridge expected"
    assert len(rings) == 1, f"closing the wedge must not open a hole: {len(rings)} rings"
    assert Polygon(rings[0]).is_valid, (
        "the closed outline must be a clean polygon: an invalid one broke every clip "
        "downstream and DRC saw silk run into the edge")
    assert _tab_slivers_mm2(rings, pins) < 0.02, (
        f"a sliver survives where the slanted edge meets a tab: {_tab_slivers_mm2(rings, pins):.3f} mm^2")

    # Contrast: a 0.4 mm slit the user drew into a silhouette, far from any
    # tab, is kept. (Shape parts clamp to 2 mm, so the slit comes from an
    # image: a 20 x 20 mm black square at 0.05 mm/px with an 8 px x 6 mm cut
    # from the top edge.) An unbounded closing fills it: area 401.6 vs 398.6.
    img = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(img).rectangle((196, 0, 203, 120), fill=255)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    slit = {"mode": "custom", "smooth": 0, "elements": [
        {"kind": "image", "op": "add", "cx": 10.16, "cy": 10.16, "w": 20, "h": 20, "rot": 0,
         "sides": 6, "threshold": 128, "invert": False, "fname": "slit.png"}]}
    rings, _ = webapp._compute_outline(slit, {0: buf.getvalue()}, pins, {})
    area = Polygon(rings[0], rings[1:]).area
    assert area < 399.0, (
        f"the user's own 0.4 x 6 mm slit was filled in (area {area:.2f}); closing must stay within 1 mm of a tab")
