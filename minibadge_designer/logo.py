"""Convert artwork images into per-material rectangles.

An image is downsampled to ~0.18 mm pixels and classified per pixel in one
of two modes:

- threshold: flatten onto white, grayscale, threshold — every "on" pixel
  belongs to a single material (the classic one-color logo path).
- palette: each opaque pixel snaps to its nearest palette color, and each
  palette color carries a material assignment ("silk", "copper", "glow",
  "bare", or "ignore"). This is how one multi-color image (e.g. an SVG
  rasterized by the browser) becomes several PCB-art materials at once.

Classification is separate from rect generation because material rects
need different keepouts (windows avoid all parts; silk avoids mask
openings), which the caller resolves across layers.

The web UI runs identical math client-side for the live preview, so what
you see is what gets fabbed.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image

PIXEL_MM = 0.18          # target artwork "pixel" size; >= typical 0.15 mm min feature
MAX_COLS = 192           # grid cap; binds only for artwork wider than ~34 mm
EDGE_MARGIN = 0.5        # keep artwork off the board edge
BOARD = (0.16, 0.16, 20.16, 20.16)
MAX_PALETTE = 6


@dataclass
class CircleKeepout:
    x: float
    y: float
    r: float

    def hits(self, px: float, py: float) -> bool:
        return (px - self.x) ** 2 + (py - self.y) ** 2 < self.r**2


@dataclass
class RectKeepout:
    x0: float
    y0: float
    x1: float
    y1: float

    def hits(self, px: float, py: float) -> bool:
        return self.x0 <= px <= self.x1 and self.y0 <= py <= self.y1


@dataclass
class ClassifiedImage:
    """Per-material pixel grids on the board-mm pixel raster."""

    x_org: float
    y_org: float
    pw: float          # pixel width, mm
    ph: float          # pixel height, mm
    cols: int
    rows: int
    grids: dict[str, list[bool]] = field(default_factory=dict)  # material -> row-major


def _open_rgba(image_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    return img


def _fit(
    img: Image.Image, width_mm: float, board, max_cols: int = MAX_COLS
) -> tuple[float, float, int, int]:
    width_mm = max(2.0, min(width_mm, board[2] - board[0] - 2 * EDGE_MARGIN))
    aspect = img.height / img.width
    height_mm = width_mm * aspect
    max_h = board[3] - board[1] - 2 * EDGE_MARGIN
    if height_mm > max_h:
        height_mm = max_h
        width_mm = height_mm / aspect
    cols = min(max_cols, max(1, round(width_mm / PIXEL_MM)))
    rows = max(1, round(height_mm * cols / width_mm))
    return width_mm, height_mm, cols, rows


def _transpose(img: Image.Image, rot: float, flip: bool) -> Image.Image:
    """Rotate clockwise by any angle, then mirror horizontally.

    Multiples of 90 use exact transposes (no resampling); other angles
    rotate with NEAREST so flat-color art keeps exact colors for palette
    snapping, on an expanded transparent canvas.
    """
    rot = float(rot) % 360
    if rot % 90 == 0:
        k = int(rot // 90) % 4
        if k == 1:
            img = img.transpose(Image.ROTATE_270)  # PIL rotates CCW
        elif k == 2:
            img = img.transpose(Image.ROTATE_180)
        elif k == 3:
            img = img.transpose(Image.ROTATE_90)
    else:
        img = img.rotate(-rot, expand=True, resample=Image.NEAREST,
                         fillcolor=(0, 0, 0, 0))
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    return img


def _flood(classes: list[int], cols: int, rows: int, seed: int) -> list[int]:
    """4-connected region of the seed's class. Returns pixel indexes."""
    target = classes[seed]
    region = []
    seen = bytearray(cols * rows)
    stack = [seed]
    seen[seed] = 1
    while stack:
        idx = stack.pop()
        region.append(idx)
        r, c = divmod(idx, cols)
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if 0 <= nr < rows and 0 <= nc < cols:
                n = nr * cols + nc
                if not seen[n] and classes[n] == target:
                    seen[n] = 1
                    stack.append(n)
    return region


def classify_image(
    image_bytes: bytes,
    cx: float,
    cy: float,
    width_mm: float,
    mode: str = "threshold",
    threshold: int = 128,
    invert: bool = False,
    material: str = "silk",
    palette: list[tuple[tuple[int, int, int], str]] | None = None,
    rot: int = 0,
    flip: bool = False,
    overrides: list[tuple[float, float, str]] | None = None,
    board: tuple[float, float, float, float] = BOARD,
    max_cols: int = MAX_COLS,
) -> ClassifiedImage:
    img = _transpose(_open_rgba(image_bytes), rot, flip)
    width_mm, height_mm, cols, rows = _fit(img, width_mm, board, max_cols)
    # Palette mode uses NEAREST: flat-color art must keep exact colors, or
    # anti-aliased boundary pixels snap to the wrong palette entry and leave
    # a halo of the wrong material.
    resample = Image.NEAREST if mode == "palette" else Image.LANCZOS
    resized = img.resize((cols, rows), resample)
    px = resized.load()

    ci = ClassifiedImage(
        x_org=cx - width_mm / 2,
        y_org=cy - height_mm / 2,
        pw=width_mm / cols,
        ph=height_mm / rows,
        cols=cols,
        rows=rows,
    )

    # classes: connectivity map for the magic wand (-1 = transparent);
    # mats: material per pixel ("" = none).
    n = cols * rows
    classes = [-1] * n
    mats = [""] * n

    if mode == "palette" and palette:
        colors = [tuple(rgb) for rgb, _mat in palette]
        pal_mats = [mat for _rgb, mat in palette]
        for r in range(rows):
            for c in range(cols):
                pr, pg, pb, pa = px[c, r]
                if pa < 128:
                    continue
                best, best_d = 0, 1 << 30
                for j, (qr, qg, qb) in enumerate(colors):
                    d = (pr - qr) ** 2 + (pg - qg) ** 2 + (pb - qb) ** 2
                    if d < best_d:
                        best, best_d = j, d
                idx = r * cols + c
                classes[idx] = best
                mat = pal_mats[best]
                if mat != "ignore":
                    mats[idx] = mat
    else:
        # Flatten transparency onto white so alpha edges threshold sanely.
        bg = Image.new("RGBA", resized.size, (255, 255, 255, 255))
        gray = Image.alpha_composite(bg, resized).convert("L")
        gpx = gray.load()
        for r in range(rows):
            for c in range(cols):
                dark = gpx[c, r] < threshold
                if invert:
                    dark = not dark
                idx = r * cols + c
                classes[idx] = 1 if dark else 0
                if dark:
                    mats[idx] = material

    # Magic-wand overrides: each (u, v, material) reassigns the connected
    # same-class region under the seed. Later overrides win.
    for u, v, mat in overrides or []:
        c0 = min(cols - 1, max(0, int(u * cols)))
        r0 = min(rows - 1, max(0, int(v * rows)))
        seed = r0 * cols + c0
        if classes[seed] < 0:
            continue
        for idx in _flood(classes, cols, rows, seed):
            mats[idx] = "" if mat == "ignore" else mat

    for idx, mat in enumerate(mats):
        if mat:
            ci.grids.setdefault(mat, [False] * n)[idx] = True
    return ci


def grid_to_rects(
    ci: ClassifiedImage,
    material: str,
    keepouts: list | None = None,
    board: tuple[float, float, float, float] = BOARD,
) -> list[tuple[float, float, float, float]]:
    """RLE a material grid into merged rects (board mm), applying keepouts."""
    grid = ci.grids.get(material)
    if not grid:
        return []
    keepouts = keepouts or []
    lo_x, lo_y = board[0] + EDGE_MARGIN, board[1] + EDGE_MARGIN
    hi_x, hi_y = board[2] - EDGE_MARGIN, board[3] - EDGE_MARGIN

    def on(c: int, r: int) -> bool:
        if not grid[r * ci.cols + c]:
            return False
        mx, my = ci.x_org + (c + 0.5) * ci.pw, ci.y_org + (r + 0.5) * ci.ph
        if not (lo_x <= mx <= hi_x and lo_y <= my <= hi_y):
            return False
        return not any(k.hits(mx, my) for k in keepouts)

    rects: list[list[float]] = []
    open_runs: dict[tuple[int, int], list[float]] = {}
    for r in range(ci.rows):
        row_runs: list[tuple[int, int]] = []
        start = None
        for c in range(ci.cols + 1):
            if c < ci.cols and on(c, r):
                if start is None:
                    start = c
            elif start is not None:
                row_runs.append((start, c))
                start = None
        next_open: dict[tuple[int, int], list[float]] = {}
        for span in row_runs:
            prev = open_runs.get(span)
            if prev is not None:
                prev[3] += ci.ph  # extend the rect from the row above
                next_open[span] = prev
            else:
                rect = [
                    ci.x_org + span[0] * ci.pw,
                    ci.y_org + r * ci.ph,
                    (span[1] - span[0]) * ci.pw,
                    ci.ph,
                ]
                rects.append(rect)
                next_open[span] = rect
        open_runs = next_open

    return [tuple(round(v, 4) for v in rect) for rect in rects]


def logo_to_rects(
    image_bytes: bytes,
    cx: float,
    cy: float,
    width_mm: float,
    threshold: int = 128,
    invert: bool = False,
    keepouts: list | None = None,
    board: tuple[float, float, float, float] = BOARD,
) -> list[tuple[float, float, float, float]]:
    """Single-material threshold pipeline (classify + rects in one call)."""
    ci = classify_image(
        image_bytes, cx, cy, width_mm,
        mode="threshold", threshold=threshold, invert=invert,
        material="silk", board=board,
    )
    return grid_to_rects(ci, "silk", keepouts, board)
