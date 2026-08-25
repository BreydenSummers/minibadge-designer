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
    # Logo dragged mostly off-board: emitted pixels stay on it. Cell CENTERS
    # are kept the silk-to-edge budget inside the outline -- 0.16 mm board
    # edge + 0.2 mm, the same distance hand-placed text and copper keep, and
    # the bottom of every fab's published capability -- while an edge may
    # overhang it by half a pixel.
    keep_in = 0.16 + 0.2
    rects = logo_to_rects(_disc(), cx=1.0, cy=1.0, width_mm=18)
    assert rects
    for x, y, w, h in rects:
        assert x + w / 2 >= keep_in - 0.01 and y + h / 2 >= keep_in - 0.01, (
            f"a cell centred at ({x + w / 2:.2f}, {y + h / 2:.2f}) is inside "
            f"the {keep_in:.2f} mm the artwork keeps off the routed edge")


def _tricolor() -> bytes:
    # Left third tan, middle third white, right third black, like a badge
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
    # in the black; the eyes match the background color but are a separate
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


BOARD = (0.16, 0.16, 20.16, 20.16)


def test_artwork_may_hang_off_the_board_and_keeps_its_detail():
    """Art is placed at the size asked for, and sampled where it can print.

    Art used to be shrunk to fit inside the board, which made the one thing a
    big image is *for* impossible: lining a picture up with a board profile,
    where the part you care about sits on the board and the rest hangs off.
    Nothing downstream needed the shrinking -- everything outside the board is
    clipped anyway.

    Sampling has to follow the placement, though, or honouring the width would
    quietly cost detail: the grid has a fixed cell budget, and spreading it
    over 87 mm of drawing to print 20 mm of board is a coarser board than the
    same art gets when it fits. The window that can print is cropped out first,
    so the pitch stays put.
    """
    # Black in the RIGHT half only, so where the ink lands says which part of
    # the image was sampled.
    png = _png(lambda d: d.rectangle((100, 0, 199, 199), fill="black"))

    fits = classify_image(png, cx=10.16, cy=10.16, width_mm=14.0,
                          mode="threshold", material="silk", board=BOARD)
    wide = classify_image(png, cx=10.16, cy=10.16, width_mm=87.0,
                          mode="threshold", material="silk", board=BOARD)

    assert wide.pw < fits.pw * 1.15, (
        f"87 mm of art samples at {wide.pw:.3f} mm cells where 14 mm samples "
        f"at {fits.pw:.3f}: honouring the width cost detail on the board")
    assert wide.x_org <= BOARD[0] and wide.x_org + wide.cols * wide.pw >= BOARD[2], (
        f"the sampled window ({wide.x_org:.2f} .. "
        f"{wide.x_org + wide.cols * wide.pw:.2f}) does not cover the board")

    # The ink is the half of the image that lands on the board, not a shrunken
    # copy of the whole thing: at 87 mm centred, the black half starts at the
    # board's own centre.
    ink = [wide.x_org + (c + 0.5) * wide.pw
           for r in range(wide.rows) for c in range(wide.cols)
           if wide.grids["silk"][r * wide.cols + c]]
    assert ink, "87 mm of art put no ink on the board at all"
    assert abs(min(ink) - 10.16) < 0.3, (
        f"the black half starts at x={min(ink):.2f}; centred art 87 mm wide "
        "puts that boundary on the board's centre line")

    # Pushed right off the left edge: the centre is nowhere near the board and
    # the placement still holds.
    off = classify_image(png, cx=-8.0, cy=10.16, width_mm=40.0,
                         mode="threshold", material="silk", board=BOARD)
    rects = grid_to_rects(off, "silk", [], board=BOARD)
    assert rects, "art whose centre is off the board printed nothing"
    right = max(x + w for x, _y, w, _h in rects)
    assert right <= 12.1, (
        f"the ink runs to x={right:.2f}; the image's black half ends at 12 mm "
        "when its centre sits 8 mm off the left edge")
