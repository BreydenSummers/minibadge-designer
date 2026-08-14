import io
import json
import zipfile

import pytest
from PIL import Image, ImageDraw

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
    assert "D1,LED red,LED 0805 (2012 metric),back" in bom
    assert "D2,LED blue,LED 0805 (2012 metric),front" in bom


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


def test_smoothing_strength_and_off(client):
    # Full-bleed disc (Ø = width = 20 mm) so it reaches all four pad plates —
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
    # a hole cut through the middle, and an image silhouette part — the
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


def test_font_file_served_and_unknown_404(client):
    resp = client.get("/fonts/archivo.ttf")
    assert resp.status_code == 200
    assert resp.data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true")
    assert client.get("/fonts/../secrets.ttf").status_code in (308, 404)
    assert client.get("/fonts/nope.ttf").status_code == 404


def test_index_lists_fonts(client):
    page = client.get("/").data.decode()
    assert "Archivo Black" in page and "Creepster" in page


def test_ttf_texts_all_materials(client):
    # One text per material, front and back — TTF texts become gr_poly art:
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
    # isolated pour island — it must stay in the fill, and the zone must tell
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
    # bare_side=back opens ONLY the back mask and only cuts the back pour —
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


def test_texts_pass_through_and_sanitize(client):
    texts = [
        {"x": 10, "y": 4, "text": "front text", "size": 2.0},
        {"x": 10, "y": 15, "text": "back\x00 text", "size": 99, "side": "back"},
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
    assert "(size 6 6)" in board            # size clamped to 6 mm
    assert board.count("gr_text") == 2      # whitespace-only text dropped


def test_image_cuts_override_connector_strips(client):
    # A narrow vertical bar silhouette: the outline must follow the bar plus
    # small pad plates — NOT full-width connector strips. The area between a
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
    # territory) are NOT solid board any more — at most a thin bridge
    # crosses them, never the whole band.
    from shapely.geometry import box as sbox

    left_gap = sbox(5.05, 16.92, 6.1, 20.16)
    right_gap = sbox(14.25, 0.16, 15.27, 3.4)
    assert not poly.contains(left_gap)
    assert not poly.contains(right_gap)


def test_polygon_sides_in_outline_and_art(client):
    # A "hex" element with sides=3 must produce a 3-cornered outline ring,
    # not a hexagon — and an art shape layer passes sides through too.
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
    # A triangle outline (plus the connector pad plates) — nothing near the
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
            # legacy spelling AND the new flag — reverse forces 1206 either way
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
    must label each one with the board layer it paints — and start opaque."""
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
