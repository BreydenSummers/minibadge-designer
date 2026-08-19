"""Text rendered to exact vector polygons via bundled TTF fonts.

The KiCad stroke font (gr_text) can only live on the silkscreen. Turning a
string into real polygons lets text use every art material (silk, exposed
copper, glow window, bare board) and any bundled typeface, through the same
pipeline that places SVG artwork.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).parent / "fonts"

# key -> (UI label, filename). Keys are what the web UI sends; labels show
# in the font <select>. Keep keys stable: they end up in saved designs.
FONTS: dict[str, tuple[str, str]] = {
    "archivo": ("Archivo Black", "ArchivoBlack-Regular.ttf"),
    "dmserif": ("DM Serif Display", "DMSerifDisplay-Regular.ttf"),
    "righteous": ("Righteous", "Righteous-Regular.ttf"),
    "audiowide": ("Audiowide", "Audiowide-Regular.ttf"),
    "vt323": ("VT323 (terminal)", "VT323-Regular.ttf"),
    "pressstart": ("Press Start 2P (arcade)", "PressStart2P-Regular.ttf"),
    "blackops": ("Black Ops One (stencil)", "BlackOpsOne-Regular.ttf"),
    "bangers": ("Bangers (comic)", "Bangers-Regular.ttf"),
    "creepster": ("Creepster (horror)", "Creepster-Regular.ttf"),
    "pirata": ("Pirata One (blackletter)", "PirataOne-Regular.ttf"),
    "pacifico": ("Pacifico (script)", "Pacifico-Regular.ttf"),
    "specialelite": ("Special Elite (typewriter)", "SpecialElite-Regular.ttf"),
}


def _flatten_pen():
    """A fontTools pen class that records contours as flattened point lists."""
    from fontTools.pens.basePen import BasePen

    class PolyPen(BasePen):
        STEPS = 12  # curve flattening segments (~0.01 mm error at badge sizes)

        def __init__(self, glyph_set):
            super().__init__(glyph_set)
            self.contours: list[list[tuple[float, float]]] = []
            self._cur: list[tuple[float, float]] = []

        def _moveTo(self, p):
            self._cur = [p]

        def _lineTo(self, p):
            self._cur.append(p)

        def _qCurveToOne(self, p1, p2):
            x0, y0 = self._cur[-1]
            for i in range(1, self.STEPS + 1):
                t = i / self.STEPS
                m = 1 - t
                self._cur.append((
                    m * m * x0 + 2 * m * t * p1[0] + t * t * p2[0],
                    m * m * y0 + 2 * m * t * p1[1] + t * t * p2[1],
                ))

        def _curveToOne(self, p1, p2, p3):
            x0, y0 = self._cur[-1]
            for i in range(1, self.STEPS + 1):
                t = i / self.STEPS
                m = 1 - t
                self._cur.append((
                    m**3 * x0 + 3 * m * m * t * p1[0] + 3 * m * t * t * p2[0] + t**3 * p3[0],
                    m**3 * y0 + 3 * m * m * t * p1[1] + 3 * m * t * t * p2[1] + t**3 * p3[1],
                ))

        def _closePath(self):
            if len(self._cur) >= 3:
                self.contours.append(self._cur)
            self._cur = []

    return PolyPen


def _contours_to_geom(contours):
    """Assemble glyph contours into filled geometry (nonzero winding).

    TrueType outer contours wind one way and holes the other, but script
    fonts also OVERLAP outer contours (joined strokes), so an even-odd XOR
    would punch false holes. Union all outers, then subtract the holes.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    outers, holes = [], []
    for c in contours:
        area2 = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(c, c[1:] + c[:1]))
        poly = Polygon(c).buffer(0)
        if poly.is_empty:
            continue
        # glyf convention (y-up font units): clockwise = filled outline.
        (outers if area2 < 0 else holes).append(poly)
    if not outers:  # tolerate fonts with flipped winding
        outers, holes = holes, outers
    if not outers:
        return None
    g = unary_union(outers)
    if holes:
        g = g.difference(unary_union(holes))
    return g


@lru_cache(maxsize=None)
def _load_font(key: str):
    """Parse one bundled face. Owns no OS file descriptor once it returns.

    The `with` is load-bearing, not tidiness. This cache is unbounded and
    never evicted, so anything the returned TTFont still holds is held for
    the life of the process -- and a Flask worker that has rendered text in
    every bundled face would then be sitting on one descriptor per face that
    nothing can ever reclaim, until the worker runs out and starts refusing
    requests. Handing TTFont a path instead lets *it* open the file, and
    whether it closes that handle is an internal detail of the non-lazy
    read path (ttFont.py: `if not self.lazy: ... if closeStream: file.close()`);
    a `lazy=True` added later for speed silently keeps it open forever. Open
    it here and the guarantee is ours to keep.
    """
    from fontTools.ttLib import TTFont

    _label, fname = FONTS[key]
    with (FONT_DIR / fname).open("rb") as fh:
        font = TTFont(fh)  # non-lazy: the whole file is in memory before we exit
    upm = font["head"].unitsPerEm
    cap = getattr(font.get("OS/2"), "sCapHeight", 0) or 0
    if cap <= 0:
        cap = int(upm * 0.7)
    return font, font.getGlyphSet(), font.getBestCmap(), upm, cap


def cap_ratio(key: str) -> float:
    """Cap height as a fraction of the em, for one bundled face.

    The web preview draws text with the browser's own rasteriser, which sizes a
    face by its em, while everything on the board is sized by its CAP HEIGHT
    (see :func:`text_geometry`). Converting between the two needs this ratio,
    and it is a property of the individual font: the bundled faces run from
    0.348 (Special Elite) to 1.000 (Press Start 2P). A preview that assumes one
    value for all of them draws text at the wrong size and then decides whether
    it fits the board from that wrong size.
    """
    _font, _gs, _cmap, upm, cap = _load_font(key)
    return cap / upm


@lru_cache(maxsize=256)
def _glyph_geom(key: str, ch: str):
    """(geometry in font units | None, advance width) for one character."""
    font, glyph_set, cmap, _upm, _cap = _load_font(key)
    name = cmap.get(ord(ch))
    if name is None:
        return None, None  # not in this font
    glyph = glyph_set[name]
    pen = _flatten_pen()(glyph_set)
    glyph.draw(pen)
    return _contours_to_geom(pen.contours), glyph.width


def text_geometry(text: str, key: str, size_mm: float):
    """A string as one shapely geometry in mm, anchored at (0, 0) center.

    `size_mm` is the capital-letter height (matching KiCad's text size), the
    y axis points down (board coordinates), and the ink bounding box is
    centered on the origin horizontally; vertically the cap-height band is
    centered, so mixed-case strings sit where the stroke font would.
    """
    from shapely.affinity import affine_transform, translate
    from shapely.ops import unary_union

    if key not in FONTS:
        raise ValueError(f"unknown font {key!r}")
    _font, _gs, _cmap, upm, cap = _load_font(key)
    scale = size_mm / cap
    parts = []
    x = 0.0
    for ch in text:
        geom, advance = _glyph_geom(key, ch)
        if advance is None:
            x += upm * 0.5  # unknown character: leave a gap
            continue
        if geom is not None and not geom.is_empty:
            parts.append(translate(geom, xoff=x))
        x += advance
    if not parts:
        return None
    g = unary_union(parts)
    # Font units (y-up) -> board mm (y-down); baseline at cap/2 so the
    # capital band straddles y=0.
    g = affine_transform(g, [scale, 0, 0, -scale, 0, cap / 2 * scale])
    minx, _y0, maxx, _y1 = g.bounds
    g = translate(g, xoff=-(minx + maxx) / 2)
    return g.simplify(0.005)


#: Ink extents are rounded OUTWARD to this many font units before they are
#: shipped, so the table always describes a box that contains the real ink
#: (never one inside it) while keeping the payload small. 0.01 font unit is
#: 1e-5 em, which at the largest text the UI offers (119 mm cap) is 1.7 um --
#: four orders of magnitude below the 0.2 mm silk-to-edge budget it feeds.
_INK_ROUND = 2


def _out_lo(v: float) -> float:
    from math import floor

    return floor(v * 10**_INK_ROUND) / 10**_INK_ROUND


def _out_hi(v: float) -> float:
    from math import ceil

    return ceil(v * 10**_INK_ROUND) / 10**_INK_ROUND


@lru_cache(maxsize=None)
def char_metrics(key: str) -> dict:
    """Every character one bundled face can draw, measured as the board draws it.

    The editor has to know how wide a string will print BEFORE it prints, to
    decide whether it clears the board edge and the holes. It used to ask the
    browser, whose rasteriser is not the thing that lays out the board: the
    browser kerns, forms ligatures and swaps in a face's default alternates,
    while :func:`text_geometry` walks the string one cmap glyph at a time. The
    disagreement reached 6.1% of a string's width (Pacifico "gjpqy", whose "g"
    and "o" the browser draws from that face's alternates), and the editor
    covered it by padding every measurement of every face by 8% -- which on a
    17 mm string is 1.36 mm of phantom margin per side, enough to refuse a
    text that had millimetres of real clearance.

    Shipping these numbers instead lets the editor compute the server's own ink
    box exactly, for any string, with no padding and no round trip. The values
    are this face's FONT UNITS (divide by `cap` and multiply by the text's
    cap-height size to get mm), because that is the frame the glyph outlines
    are already in and the advances are exact integers there:

    * ``upm`` / ``cap``   -- em size and cap height, the two scales involved
    * ``miss``            -- advance text_geometry gives a character this face
                             has no glyph for (it draws nothing and moves on)
    * ``chars[ch]``       -- ``[advance]`` for a blank glyph, else
                             ``[advance, x0, x1, up, down]``: ink extents left
                             and right of the pen and above and below the
                             baseline, y measured upward like the font's own
                             outlines.

    Ink comes from the same flattened, unioned geometry :func:`text_geometry`
    places, so a face whose curves the 12-step flattening undershoots is
    undershot identically here; the editor's box is the ink the board gets,
    not the ink the outline describes.
    """
    _font, _gs, cmap, upm, cap = _load_font(key)
    chars: dict[str, list[float]] = {}
    for cp in sorted(cmap):
        if cp < 0x20:  # the UI strips control characters before they get here
            continue
        ch = chr(cp)
        geom, advance = _glyph_geom(key, ch)
        if advance is None:  # cmap named a glyph the glyf table does not have
            continue
        if geom is None or geom.is_empty:
            chars[ch] = [advance]
            continue
        x0, y0, x1, y1 = geom.bounds
        chars[ch] = [advance, _out_lo(x0), _out_hi(x1), _out_hi(y1), _out_hi(-y0)]
    return {"upm": upm, "cap": cap, "miss": upm * 0.5, "chars": chars}
