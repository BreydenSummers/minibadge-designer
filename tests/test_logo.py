import io

from PIL import Image, ImageDraw

from minibadge_designer.logo import (
    CircleKeepout,
    RectKeepout,
    classify_image,
    grid_to_rects,
    logo_to_rects,
)


def _png(draw_fn, size=(200, 200)) -> bytes:
    img = Image.new("RGB", size, "white")
    draw_fn(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _disc() -> bytes:
    return _png(lambda d: d.ellipse((40, 40, 160, 160), fill="black"))


def test_disc_produces_rects_within_box():
    rects = logo_to_rects(_disc(), cx=10.16, cy=10.16, width_mm=12)
    assert rects
    for x, y, w, h in rects:
        assert w > 0 and h > 0
        assert 10.16 - 6.01 <= x and x + w <= 10.16 + 6.01
        assert 10.16 - 6.01 <= y and y + h <= 10.16 + 6.01


def test_all_white_produces_nothing():
    rects = logo_to_rects(_png(lambda d: None), cx=10.16, cy=10.16, width_mm=12)
    assert rects == []


def test_invert_flips_coverage():
    normal = logo_to_rects(_disc(), cx=10.16, cy=10.16, width_mm=12)
    inverted = logo_to_rects(_disc(), cx=10.16, cy=10.16, width_mm=12, invert=True)
    area = lambda rs: sum(w * h for _, _, w, h in rs)
    assert area(normal) > 0 and area(inverted) > 0
    # Disc covers less than half its bounding square, so inverting grows the area.
    assert area(inverted) > area(normal)


def test_keepouts_exclude_pixels():
    keepouts = [CircleKeepout(10.16, 10.16, 3.0), RectKeepout(4, 4, 7, 16.5)]
    rects = logo_to_rects(_disc(), cx=10.16, cy=10.16, width_mm=14, keepouts=keepouts)
    for x, y, w, h in rects:
        cx, cy = x + w / 2, y + h / 2
        assert (cx - 10.16) ** 2 + (cy - 10.16) ** 2 >= 2.5**2
        assert not (4.1 < cx < 6.9 and 4.1 < cy < 16.4)


def test_clipped_to_board_edges():
    # Logo dragged mostly off-board: emitted pixels stay on it (centers are
    # kept 0.5 mm inside the outline; edges may overhang by half a pixel).
    rects = logo_to_rects(_disc(), cx=1.0, cy=1.0, width_mm=18)
    assert rects
    for x, y, w, h in rects:
        assert x + w / 2 >= 0.66 - 0.01 and y + h / 2 >= 0.66 - 0.01


def _tricolor() -> bytes:
    # Left third tan, middle third white, right third black — like a badge
    # with lettering, filigree, and background.
    img = Image.new("RGBA", (90, 90), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 29, 89), fill=(180, 160, 110, 255))
    d.rectangle((30, 0, 59, 89), fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_palette_mode_splits_materials():
    palette = [
        ((180, 160, 110), "copper"),
        ((255, 255, 255), "silk"),
        ((0, 0, 0), "ignore"),
    ]
    ci = classify_image(_tricolor(), cx=10.16, cy=10.16, width_mm=9, mode="palette", palette=palette)
    assert set(ci.grids) == {"copper", "silk"}
    copper = grid_to_rects(ci, "copper")
    silk = grid_to_rects(ci, "silk")
    assert copper and silk
    # Copper occupies the left third, silk the middle third.
    assert max(x + w for x, _, w, _ in copper) < 10.16 - 1.0
    for x, _, w, _ in silk:
        assert 10.16 - 2.0 < x and x + w < 10.16 + 2.0
    # Roughly equal areas (each one third of a 9 mm square).
    a_c = sum(w * h for _, _, w, h in copper)
    a_s = sum(w * h for _, _, w, h in silk)
    assert abs(a_c - a_s) < 3.0


def test_palette_mode_skips_transparent_pixels():
    img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((15, 15, 45, 45), fill=(255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    ci = classify_image(
        buf.getvalue(), cx=10.16, cy=10.16, width_mm=8,
        mode="palette", palette=[((255, 0, 0), "glow")],
    )
    rects = grid_to_rects(ci, "glow")
    area = sum(w * h for _, _, w, h in rects)
    assert 0 < area < 8 * 8 * 0.5  # only the opaque square, not the canvas


def _skull() -> bytes:
    # Helldivers-style: yellow background, black skull, yellow eyes enclosed
    # in the black — the eyes match the background color but are a separate
    # connected region.
    img = Image.new("RGB", (120, 120), (255, 220, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 100, 100), fill=(0, 0, 0))
    d.ellipse((35, 45, 55, 65), fill=(255, 220, 0))   # left eye
    d.ellipse((65, 45, 85, 65), fill=(255, 220, 0))   # right eye
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


SKULL_PALETTE = [((255, 220, 0), "copper"), ((0, 0, 0), "ignore")]


def test_wand_override_retargets_one_region():
    # Without overrides both eyes are copper like the background.
    plain = classify_image(_skull(), cx=10.16, cy=10.16, width_mm=12,
                           mode="palette", palette=SKULL_PALETTE)
    assert set(plain.grids) == {"copper"}
    # Wand the left eye (center ~ (45/120, 55/120)) to bare.
    ci = classify_image(
        _skull(), cx=10.16, cy=10.16, width_mm=12,
        mode="palette", palette=SKULL_PALETTE,
        overrides=[(45 / 120, 55 / 120, "bare")],
    )
    bare = grid_to_rects(ci, "bare")
    copper = grid_to_rects(ci, "copper")
    assert bare and copper
    # The bare region is only the left eye: small, and left of center.
    bare_area = sum(w * h for _, _, w, h in bare)
    assert bare_area < 4.0
    assert all(x + w < 10.16 for x, _, w, _ in bare)
    # The background (same color, different region) is still copper and the
    # right eye stayed copper too.
    copper_area = sum(w * h for _, _, w, h in copper)
    assert copper_area > bare_area * 5


def test_wand_override_on_threshold_mode():
    # Threshold mode: skull is "on", eyes are enclosed "off" regions.
    # Wanding an eye adds material where there was none.
    ci = classify_image(
        _skull(), cx=10.16, cy=10.16, width_mm=12,
        mode="threshold", threshold=128, material="silk",
        overrides=[(45 / 120, 55 / 120, "glow")],
    )
    assert "glow" in ci.grids and "silk" in ci.grids
    glow_area = sum(w * h for _, _, w, h in grid_to_rects(ci, "glow"))
    assert 0 < glow_area < 4.0


def test_rotate_and_flip():
    # Asymmetric: dark square in the top-left quadrant only.
    img = Image.new("RGB", (100, 100), "white")
    ImageDraw.Draw(img).rectangle((5, 5, 40, 40), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()

    def center(rot=0, flip=False):
        ci = classify_image(data, cx=10, cy=10, width_mm=10, rot=rot, flip=flip)
        rects = grid_to_rects(ci, "silk")
        xs = [x + w / 2 for x, _, w, _ in rects]
        ys = [y + h / 2 for _, y, _, h in rects]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    x0, y0 = center()
    assert x0 < 10 and y0 < 10                       # top-left
    x90, y90 = center(rot=90)
    assert x90 > 10 and y90 < 10                     # CW: -> top-right
    xf, yf = center(flip=True)
    assert xf > 10 and yf < 10                       # mirrored: -> top-right
    x180, y180 = center(rot=180)
    assert x180 > 10 and y180 > 10                   # -> bottom-right


def test_transparency_flattens_to_white():
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((25, 25, 75, 75), fill=(0, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    rects = logo_to_rects(buf.getvalue(), cx=10.16, cy=10.16, width_mm=10)
    assert rects
    # Transparent border must not become silkscreen: total area ~ quarter of box.
    assert sum(w * h for _, _, w, h in rects) < 10 * 10 * 0.5
