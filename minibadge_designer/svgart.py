"""Exact vector geometry from SVG uploads.

Raster images go through logo.py's pixel-grid classifier; SVGs skip the
grid entirely. Every filled shape is flattened to polygons with fine
chords, assembled under its fill rule (holes stay holes), occluded in
paint order (later shapes cover earlier ones, like a renderer would), and
grouped by the color that actually ends up visible. The result is the
artwork's true printed geometry, with no 0.18 mm pixel staircase.

Approximations versus a real renderer (fine for flat-color logo SVGs,
which is what the palette pipeline targets):
- curves become chords ~1/1500 of the document diagonal long;
- paint with alpha < 50% is treated as fully transparent, >= 50% as
  fully opaque (the raster path applies the same alpha-128 cut);
- strokes get round caps/joins regardless of the SVG's cap/join style;
- gradients/patterns aren't solid colors; parsing raises ValueError and
  the caller falls back to the raster pipeline.
"""

from __future__ import annotations

import io

from .logo import EDGE_MARGIN

# A parsed document is normalized so its frame is (0, 0, width, height):
# svgelements applies the viewBox transform, matching what a browser
# rasterizes. Geometry may spill outside the frame; like the raster path
# (which clips to the canvas), we clip to the frame.


def is_svg(data: bytes | None) -> bool:
    """Cheap content sniff: XML that opens an <svg> element."""
    if not data:
        return False
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if not head.startswith((b"<?xml", b"<!doctype svg", b"<svg", b"<!--")):
        return False
    return b"<svg" in head


def _ring_points(subpath, chord: float) -> list[tuple[float, float]]:
    """Flatten one subpath into a closed ring of points."""
    from svgelements import Arc, Close, CubicBezier, Line, Move, QuadraticBezier

    pts: list[tuple[float, float]] = []

    def add(p) -> None:
        xy = (float(p.x), float(p.y))
        if not pts or pts[-1] != xy:
            pts.append(xy)

    for seg in subpath:
        if isinstance(seg, (Move, Line, Close)):
            if seg.end is not None:
                add(seg.end)
        elif isinstance(seg, (Arc, CubicBezier, QuadraticBezier)):
            try:
                length = seg.length(error=1e-4)
            except ZeroDivisionError:  # degenerate arc
                length = 0.0
            n = min(256, max(4, int(length / chord) + 1))
            for i in range(1, n + 1):
                add(seg.point(i / n))
    return pts


def _rings_to_geometry(rings: list[list[tuple[float, float]]], fill_rule: str):
    """Assemble subpath rings into a filled area under the SVG fill rule.

    Rings are sorted by area (outermost first) and folded: a ring whose
    interior band is painted unions in, one whose band is unpainted cuts
    out. Winding numbers decide "painted" (nonzero) or containment parity
    (evenodd), so opposite-wound holes AND same-wound nested subpaths
    both come out the way a renderer draws them.
    """
    from shapely.geometry import LinearRing, Polygon

    entries = []
    for ring in rings:
        if len(ring) < 3:
            continue
        try:
            sign = 1 if LinearRing(ring).is_ccw else -1
        except Exception:  # noqa: BLE001, S112 (degenerate ring: skip it)
            continue
        poly = Polygon(ring).buffer(0)  # heal self-intersections
        if poly.is_empty:
            continue
        entries.append((poly, sign))
    if not entries:
        return None
    entries.sort(key=lambda e: e[0].area, reverse=True)

    geom = None
    for i, (poly, _sign) in enumerate(entries):
        # A point in ring i's own band: inside i, outside the rings i holds.
        band = poly
        for other, _s in entries[i + 1:]:
            if band.contains(other.representative_point()):
                band = band.difference(other)
        if band.is_empty:
            continue
        rep = band.representative_point()
        covering = [(p, s) for p, s in entries if p.covers(rep)]
        if fill_rule == "evenodd":
            painted = len(covering) % 2 == 1
        else:  # nonzero (the SVG default)
            painted = sum(s for _p, s in covering) != 0
        if painted:
            geom = poly if geom is None else geom.union(poly)
        elif geom is not None:
            geom = geom.difference(poly)
    return None if geom is None or geom.is_empty else geom


def _paint_alpha_ok(color, opacity: float) -> bool:
    return (
        color is not None
        and color.value is not None
        and (color.alpha or 0) * opacity >= 128
    )


def svg_color_regions(data: bytes):
    """Parse an SVG into painter-flattened per-color regions.

    Returns (regions, frame): regions is an ordered list of
    ((r, g, b), geometry) pairs (mutually disjoint, each the part of that
    color actually visible after later shapes covered earlier ones), and
    frame is (width, height) in SVG user units. Raises ValueError for
    SVGs this parser can't do exactly (gradients, patterns, no fillable
    shapes); callers then fall back to the raster pipeline.
    """
    from shapely.ops import unary_union
    from svgelements import SVG, Color, Path, Shape

    doc = SVG.parse(io.BytesIO(data), reify=True)
    fw, fh = float(doc.width), float(doc.height)
    if not (fw > 0 and fh > 0):
        raise ValueError("SVG has no usable dimensions")
    chord = (fw**2 + fh**2) ** 0.5 / 1500

    painted: list[tuple[tuple[int, int, int], object]] = []  # document order
    for el in doc.elements():
        if not isinstance(el, Shape):
            continue
        if el.values.get("visibility") == "hidden" or el.values.get("display") == "none":
            continue
        # svgelements silently resolves url(#gradient) paint to a stop color;
        # a gradient is not one flat color, so refuse and let the caller use
        # the browser's raster render instead.
        for attr in ("fill", "stroke"):
            if str(el.values.get(attr, "")).strip().startswith("url("):
                raise ValueError("non-solid paint (gradient/pattern)")
        fill, stroke = el.fill, el.stroke
        for paint in (fill, stroke):
            if paint is not None and paint.value is not None and not isinstance(paint, Color):
                raise ValueError("non-solid paint (gradient/pattern)")
        try:
            opacity = float(el.values.get("opacity", 1.0))
        except (TypeError, ValueError):
            opacity = 1.0
        path = Path(el)
        if len(path) == 0:
            continue
        rings = [r for sp in path.as_subpaths() if (r := _ring_points(sp, chord))]
        if _paint_alpha_ok(fill, opacity):
            rule = el.values.get("fill-rule", "nonzero")
            geom = _rings_to_geometry(rings, rule)
            if geom is not None:
                painted.append(((fill.red, fill.green, fill.blue), geom))
        if _paint_alpha_ok(stroke, opacity) and float(el.stroke_width or 0) > 0:
            from shapely.geometry import LineString

            lines = [LineString(r) for r in rings if len(r) >= 2]
            if lines:
                geom = unary_union(lines).buffer(float(el.stroke_width) / 2)
                if not geom.is_empty:
                    painted.append(((stroke.red, stroke.green, stroke.blue), geom))
    if not painted:
        raise ValueError("SVG contains no fillable shapes")

    # Painter's algorithm: walk back-to-front so each shape keeps only what
    # nothing later covers, then merge the survivors per color.
    covered = None
    visible: list[tuple[tuple[int, int, int], object]] = []
    for rgb, geom in reversed(painted):
        vis = geom if covered is None else geom.difference(covered)
        if not vis.is_empty:
            visible.append((rgb, vis))
        covered = geom if covered is None else covered.union(geom)
    visible.reverse()

    by_color: dict[tuple[int, int, int], object] = {}
    order: list[tuple[int, int, int]] = []
    for rgb, geom in visible:
        if rgb in by_color:
            by_color[rgb] = by_color[rgb].union(geom)
        else:
            by_color[rgb] = geom
            order.append(rgb)
    return [(rgb, by_color[rgb]) for rgb in order], (fw, fh)


def fit_transform(
    frame: tuple[float, float],
    cx: float,
    cy: float,
    width_mm: float,
    board: tuple[float, float, float, float],
    rot: float = 0,
    flip: bool = False,
) -> tuple[list[float], float, float]:
    """Affine (shapely 2D matrix) mapping SVG units onto board mm.

    Mirrors logo._fit + logo._transpose exactly: rotate clockwise by any
    angle FIRST, then mirror horizontally; the width slider applies to the
    rotated artwork's bounding box, clamped to the board with the same
    margins and height backpressure. Returns (matrix, placed_w, placed_h).
    """
    import math

    fw, fh = frame
    t = math.radians(float(rot) % 360)
    ct, st = math.cos(t), math.sin(t)
    # bounding box of the rotated frame
    rw = fw * abs(ct) + fh * abs(st)
    rh = fw * abs(st) + fh * abs(ct)

    width_mm = max(2.0, min(width_mm, board[2] - board[0] - 2 * EDGE_MARGIN))
    aspect = rh / rw
    height_mm = width_mm * aspect
    max_h = board[3] - board[1] - 2 * EDGE_MARGIN
    if height_mm > max_h:
        height_mm = max_h
        width_mm = height_mm / aspect
    s = width_mm / rw

    # (x, y) -> rotate cw about the frame center -> mirror x -> scale
    # -> translate to (cx, cy). Same axes as pcb._r (y grows downward,
    # so positive angles appear clockwise).
    a, b, d, e = ct, -st, st, ct
    if flip:
        a, b = -a, -b
    hx, hy = fw / 2, fh / 2  # frame center
    return (
        [s * a, s * b, s * d, s * e, cx - s * (a * hx + b * hy), cy - s * (d * hx + e * hy)],
        width_mm,
        height_mm,
    )


def geom_polygons(geom) -> list:
    """The Polygon components of any shapely geometry (skips slivers)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        out = []
        for g in geom.geoms:
            out.extend(geom_polygons(g))
        return out
    return []


def polygon_rings(poly) -> list[list[tuple[float, float]]]:
    """A polygon as rounded coordinate rings, exterior first."""
    rings = [[(round(x, 4), round(y, 4)) for x, y in poly.exterior.coords[:-1]]]
    for hole in poly.interiors:
        rings.append([(round(x, 4), round(y, 4)) for x, y in hole.coords[:-1]])
    return rings
