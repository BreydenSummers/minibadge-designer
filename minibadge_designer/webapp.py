"""Flask web UI: design a minibadge in the browser, download a KiCad project."""

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile

from flask import Flask, render_template, request, send_file
from PIL import Image
from shapely.errors import ShapelyError
from werkzeug.middleware.proxy_fix import ProxyFix

from . import pcb, svgart, textpoly
from .logo import (
    EDGE_MARGIN,
    MAX_PALETTE,
    CircleKeepout,
    ImageTooLarge,
    RectKeepout,
    classify_image,
    grid_to_rects,
)

MAX_UPLOAD = 24 * 1024 * 1024
MAX_LEDS = 64   # generous: custom outlines can reach ~120 mm across
MAX_TEXTS = 24
MAX_ART = 8

#: Soldermask colors the fabs (and the UI's picker) actually offer. The value
#: is interpolated straight into the KiCad stackup, so anything outside this
#: list would ship a board file KiCad refuses to open: a `mask_color` of
#: `green")` used to close the stackup's s-expression early (defect #4).
#: Whitelisting at the boundary is the same treatment `finish` already gets.
MASK_COLORS = ("green", "purple", "black", "red", "blue", "white")

#: Exceptions that degenerate geometry raises. `shapely` reports a NaN or
#: otherwise unbuildable ring as `GEOSException`, which is a *sibling* of
#: `ValueError` (`GEOSException -> ShapelyError -> Exception`), so a tuple of
#: builtins never sees it. The user's numbers made the geometry impossible;
#: that is a 400, not a server fault.
_GEOMETRY_ERRORS = (OSError, ValueError, TypeError, AttributeError, KeyError,
                    IndexError, ZeroDivisionError, ShapelyError,
                    Image.DecompressionBombError)


#: How much vector detail one uploaded SVG may carry before the exact pipeline
#: gives up on it. The cost of the exact pipeline is superlinear and was
#: uncapped: measured on this repo, a single `<path>` of 40 000 line vertices
#: (272 KB) takes 5.3 s, 80 000 takes 26 s and 200 000 takes 493 s, all
#: returning 200. Bezier segments cost ~4 ms each, roughly linearly.
#:
#: The score below weights a curve/arc segment as `SVG_CURVE_COST` line
#: vertices precisely so that both shapes hit this ceiling at about the same
#: wall-clock cost, which is ~5 s for a line-heavy file and ~13 s for the
#: worst curve-heavy one. Raise this one number to buy more detail and more
#: seconds; nothing else needs editing.
#:
#: It is deliberately generous: 40 000 straight vertices, or ~2 500 Bezier
#: segments, is far more detail than a 20 mm badge can print (the average
#: chord would be ~0.01 mm against a ~0.15 mm minimum feature). And it is not
#: a refusal: an SVG over the cap falls back to the browser's raster render of
#: the same file, exactly like a gradient-filled SVG does today. Only a client
#: that sent no raster fallback is turned away, with a message saying so.
MAX_SVG_COMPLEXITY = 40_000
SVG_CURVE_COST = 16

#: Floor charged for every paintable shape, whatever its vertex count.
#:
#: Vertices were the only thing priced, and they are the wrong unit for a
#: document made of many simple shapes: `svg_color_regions`' painter pass
#: unions and differences a growing geometry once per *shape*, so 6 000
#: four-vertex `<rect>` scored 24 000 against the 40 000 cap -- comfortably
#: inside it -- and still took 12 s. Measured at ~2 ms of that pass per shape,
#: which against a cap tuned for a few seconds puts one shape at ~24 vertices.
#:
#: Applied as `max(vertex_score, this)` rather than added, so a single
#: 40 000-vertex path still scores exactly 40 000 and the documented ceiling
#: above keeps meaning what it says.
SVG_SHAPE_COST = 24


class _TooComplex(ValueError):
    """An SVG whose exact vector geometry would cost minutes to build."""


#: Pixel rectangles one board-outline computation may union into geometry,
#: summed over all of its (up to 12) image elements.
#:
#: The outline grid is 480 cells across, and `grid_to_rects` merges each row's
#: horizontal runs, so this number tracks how *broken up* the silhouette is
#: rather than how big it is -- which is also what the cost tracks. Measured
#: end to end on /outline at the full 119 mm width, one element:
#:
#:     helmet fixture       610 rects   0.11 s
#:     dot grid, 16 px    1 857 rects   0.42 s
#:     dot grid, 12 px    3 300 rects   0.85 s
#:     dot grid, 10 px    3 600 rects   1.37 s
#:     dot grid,  8 px    7 425 rects   2.92 s
#:     dot grid,  6 px   11 600 rects   7.79 s
#:     1-px checkerboard 64 620 rects   61 s (measured in production)
#:
#: Superlinear, and /outline runs on every edit of the design, so the ceiling
#: is set at the knee rather than as far out as it could go: 6 000 rects is
#: about 2 s in the worst accepted case, with ten times the headroom over a
#: real traced silhouette. An earlier 40 000 was picked to be generous and
#: measured afterwards at 13 s for a single accepted element, which is a
#: refusal the user would rather have had.
#:
#: What it refuses could not be routed anyway: tens of thousands of separate
#: 0.25 mm islands against a ~2 mm router bit is not a board outline, so
#: answering "too fine to cut" is the honest reply as well as the cheap one.
MAX_OUTLINE_RECTS = 6_000


class _TooFine(ValueError):
    """A silhouette broken into more pieces than the board could be cut in."""


class _RectBudget:
    """Per-outline allowance of pixel rectangles, shared across elements."""

    def __init__(self, total: int = MAX_OUTLINE_RECTS):
        self.left = total

    def spend(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise _TooFine(
                "this artwork carries more fine detail than the board can be "
                f"cut in (over {MAX_OUTLINE_RECTS:,} separate pieces at the "
                "outline's resolution). Raise the threshold, smooth the "
                "shape, or use a cleaner silhouette with fewer isolated "
                "specks -- a board router cannot cut features below about "
                "2 mm anyway")


#: Wall-clock a single export request may spend inside subprocesses.
#:
#: This number exists because the old per-subprocess budgets did not add up.
#: /gerbers allowed 120 s for the pcbnew zone refill, then 120 s for the plot,
#: then 120 s for the drill export -- 360 s against gunicorn's `timeout = 300`.
#: Each of those three steps has a carefully written 500 explaining what timed
#: out and what to do instead, and in the case they were written for the worker
#: was SIGKILLed before it could send one; the user got a dropped connection.
#:
#: One deadline for the request, shared out across its steps, means the handler
#: always wins that race. 240 s leaves 60 s of gunicorn budget for zipping the
#: result and streaming it back.
EXPORT_BUDGET_S = int(os.environ.get("EXPORT_BUDGET_S", "240"))


class _Deadline:
    """A wall-clock budget shared by every subprocess in one request."""

    def __init__(self, budget: float = EXPORT_BUDGET_S):
        self.end = time.monotonic() + budget

    def left(self) -> float:
        # Floored rather than raising: a step handed ~0 s times out at once and
        # the caller's own TimeoutExpired branch reports it, which is the
        # message the user should see. Overshoot is bounded by the floor.
        return max(1.0, self.end - time.monotonic())


#: Refusals that name something the user can actually change about their own
#: upload -- too much vector detail, too many pixels, a silhouette too broken
#: up to cut -- as opposed to geometry that merely came out unbuildable. Each
#: carries a message written for the person who hit it, so these are reported
#: verbatim while `_GEOMETRY_ERRORS` collapses to a generic 400.
_UPLOAD_REFUSALS = (_TooComplex, ImageTooLarge, _TooFine)


# Path-data commands and how many numbers one segment of each consumes. `t`
# and `s` are the smooth-curve forms, so they count as curves despite their
# short argument lists.
_SVG_ARITY = {"m": 2, "l": 2, "t": 2, "h": 1, "v": 1,
              "c": 6, "s": 4, "q": 4, "a": 7, "z": 0}
_SVG_CURVES = frozenset("csqta")
_SVG_PATH_D = re.compile(rb"""\bd\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.DOTALL)
_SVG_POINTS = re.compile(rb"""\bpoints\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.DOTALL)
_SVG_TOKEN = re.compile(rb"([MmZzLlHhVvCcSsQqTtAa])|([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")


def _path_complexity(d: bytes) -> int:
    """Weighted segment count of one `d` attribute.

    Counts *segments*, not command letters: SVG path data repeats a command
    implicitly (`L1 1 2 2 3 3` is three line-tos), and auto-tracers lean on
    that heavily, so counting letters would under-measure a traced logo by an
    order of magnitude.
    """
    cost = 0
    cmd, run = "l", 0

    def flush(cmd: str, run: int) -> int:
        arity = _SVG_ARITY.get(cmd, 2)
        if not arity or not run:
            return 0
        return max(1, run // arity) * (SVG_CURVE_COST if cmd in _SVG_CURVES else 1)

    for m in _SVG_TOKEN.finditer(d):
        letter = m.group(1)
        if letter is None:
            run += 1
            continue
        cost += flush(cmd, run)
        cmd, run = letter.decode().lower(), 0
    return cost + flush(cmd, run)


# What one of each non-path shape flattens to, in the same currency
# `_path_complexity` counts. A circle or ellipse becomes four arcs, and
# `_ring_points` gives each arc up to 256 chords, so they carry the curve
# weight; a plain rect is four straight sides, a rounded one four sides plus
# four corner arcs. `text` and `image` are absent on purpose: svgelements does
# not report them as `Shape`, so `svg_color_regions` never draws them.
_SVG_ELEMENT_COST = {
    "circle": 4 * SVG_CURVE_COST,
    "ellipse": 4 * SVG_CURVE_COST,
    "line": 1,
    "rect": 4,
}

# Tags svgelements reports as a `Shape`, i.e. the ones that get drawn and so
# the ones that pay `SVG_SHAPE_COST`. Containers and metadata score nothing of
# their own.
_SVG_SHAPES = frozenset({"path", "polygon", "polyline",
                         "circle", "ellipse", "line", "rect"})

# Subtrees that are definitions, not drawings: nothing inside them paints
# unless a `<use>` names it, at which point it is scored through that `<use>`.
_SVG_DEFS = frozenset({"defs", "symbol", "clipPath", "mask", "pattern",
                       "marker", "filter", "linearGradient", "radialGradient"})


def _svg_localname(tag) -> str:
    """`circle` from `{http://www.w3.org/2000/svg}circle`."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _svg_postorder(root) -> list:
    """Every element, children before parents, without recursion.

    Iterative on purpose: `tests/hostile.nested_svg` is 5 000 nested `<g>`
    around one rect -- a harmless file, and one a recursive walk crashes on
    long before Python's own limit becomes the user's problem.
    """
    out, stack = [], [(root, False)]
    while stack:
        el, done = stack.pop()
        if done:
            out.append(el)
            continue
        stack.append((el, True))
        for child in reversed(el):
            stack.append((child, False))
    return out


def _svg_tree_complexity(data: bytes) -> int | None:
    """Score an SVG's whole render tree, resolving `<use>`. None if unparseable.

    This exists because the byte-level scan below cannot see the two axes that
    actually cost the most.

    The first is element count. The scan only reads `d` and `points`
    attributes, so `<circle>`/`<rect>`/`<ellipse>`/`<line>` scored zero no
    matter how many there were -- and shape *count*, not vertex count, is what
    `svg_color_regions` pays for: its painter pass unions and differences a
    growing geometry once per shape. Measured on this repo, 8 000 `<circle>`
    (382 KB) took 36 s and 6 000 `<rect>` took 12 s, both returning 200.

    The second is `<use>`. A `<g>` holding two `<use>` of the previous `<g>`
    doubles the shape count per level, so payload size grows linearly while
    the work grows as 2^depth: a **1 253-byte** file (depth 17, 131 072
    shapes) held a worker past 120 s. That one cannot be caught after the
    fact, because the blow-up happens inside `SVG.parse` itself -- hence a
    guard that runs on the XML before svgelements ever sees it.

    `xml.etree` is the right parser for that job: it is cheap (microseconds on
    these files, against seconds for reification) and it resolves no external
    entities, so pointing it at hostile markup is safe. When it cannot parse
    at all -- an internal DTD with undefined entities, malformed markup --
    this returns None and the caller keeps the byte scan, which is what those
    files already got.

    `<use>` makes the reference graph a DAG rather than a tree, so the scores
    are relaxed to a fixed point instead of being computed in one pass: each
    round resolves one more level of `<use>` nesting, and every score
    saturates at the limit. That is what makes the exponential case cheap --
    doubling from one shape reaches the ceiling in about ten rounds, so it
    stops there instead of counting to 131 072 and then refusing.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        return None

    cap = MAX_SVG_COMPLEXITY + 1

    def own(el, tag: str) -> int:
        if tag not in _SVG_SHAPES:
            return 0
        if tag == "path":
            detail = _path_complexity(
                (el.get("d") or "").encode("utf-8", "replace"))
        elif tag in ("polygon", "polyline"):
            pts = (el.get("points") or "").encode("utf-8", "replace")
            detail = len(_SVG_TOKEN.findall(pts)) // 2
        elif tag == "rect" and (el.get("rx") or el.get("ry")):
            detail = 4 + 4 * SVG_CURVE_COST
        else:
            detail = _SVG_ELEMENT_COST.get(tag, 0)
        return max(detail, SVG_SHAPE_COST)

    order = _svg_postorder(root)
    tags = {id(el): _svg_localname(el.tag) for el in order}
    mine = {id(el): own(el, tags[id(el)]) for el in order}
    by_id, uses = {}, []
    for el in order:
        ref = el.get("id")
        if ref is not None and ref not in by_id:
            by_id[ref] = el
        if tags[id(el)] == "use":
            href = el.get("href") or el.get(
                "{http://www.w3.org/1999/xlink}href") or ""
            uses.append((el, href[1:] if href.startswith("#") else None))

    # `drawn` is what an element contributes where it sits: zero inside a
    # definition, which paints only through a <use>. `whole` ignores that rule,
    # so a <use> naming a <symbol> can still be charged for its contents.
    drawn: dict[int, int] = {}
    whole: dict[int, int] = {}
    use_score: dict[int, int] = dict.fromkeys((id(u) for u, _ in uses), 0)

    # Each round resolves one more level of <use>, so a settled answer needs as
    # many rounds as the reference graph is deep. Non-convergence is reported
    # as "over the limit" rather than as the partial total, because the partial
    # total is a LOWER bound: a 60-level chain left the root's <use> still
    # reading zero, which would have waved through the very file the guard is
    # here to stop. A <use> graph deeper than this is pathological by itself.
    settled = False
    for _round in range(64):
        for el in order:  # children first, so their totals are already in
            key, tag = id(el), tags[id(el)]
            if tag == "use":
                drawn[key] = whole[key] = use_score[key]
                continue
            total = min(mine[key] + sum(drawn.get(id(c), 0) for c in el), cap)
            whole[key] = total
            drawn[key] = 0 if tag in _SVG_DEFS else total
        if not uses or whole[id(root)] >= cap:
            settled = True
            break
        moved = False
        for use_el, ref in uses:
            target = by_id.get(ref) if ref else None
            if target is None or target is use_el:
                continue
            # `whole`, not `drawn`: a <use> paints its target wherever it lives.
            was, now = use_score[id(use_el)], whole.get(id(target), 0)
            if now > was:
                use_score[id(use_el)], moved = now, True
        if not moved:
            settled = True
            break

    return whole[id(root)] if settled else cap


def _check_svg_complexity(data: bytes) -> None:
    """Refuse the exact vector pipeline an SVG too intricate to build in time."""
    cost = _svg_tree_complexity(data)
    if cost is None:
        # Not parseable as XML: score what the bytes show, as before.
        cost = sum(_path_complexity(m.group(1) or m.group(2) or b"")
                   for m in _SVG_PATH_D.finditer(data))
        for m in _SVG_POINTS.finditer(data):  # <polygon>/<polyline>
            cost += len(_SVG_TOKEN.findall(m.group(1) or m.group(2) or b"")) // 2
    if cost > MAX_SVG_COMPLEXITY:
        raise _TooComplex(
            "this SVG carries too much vector detail to trace exactly "
            f"(about {cost:,} segments against a {MAX_SVG_COMPLEXITY:,} limit); "
            "simplify the path, raise the smoothing/tolerance in your tracing "
            "tool, or upload a PNG of the same artwork instead")


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

# The app is served behind a reverse proxy on the same host, which is itself
# behind Cloudflare, so every peer address the app sees is the proxy's loopback
# address. Without any of this the gunicorn access log recorded 127.0.0.1 for
# all traffic -- and when the site fell over there was no way to tell who had
# done it or what to block.
#
# ProxyFix alone does NOT solve it here, and counting hops is the wrong tool for
# the job. `X-Forwarded-For` positions depend on the local proxy's config, and
# both usual configs get it wrong: nginx's `$proxy_add_x_forwarded_for` appends
# the peer, so the chain is "<visitor>, <cloudflare-edge>" and the rightmost
# entry -- the only one it is safe to count from -- is Cloudflare's edge, not
# the visitor; nginx's `$remote_addr` overwrites the header outright and the
# visitor's address is not in it at all. Measured both ways: x_for=1 yields the
# Cloudflare edge IP, which varies enough to look plausible in a log and is
# useless for blocking anyone.
#
# `CF-Connecting-IP` is the header that answers the question. Cloudflare sets it
# on every request as a single address, *overwriting* whatever the client sent,
# and a local proxy passes it through untouched, so there is no chain to count.
#
# What makes trusting it sound is the topology, not the header: compose
# publishes only on 127.0.0.1, gunicorn accepts forwarded headers only from
# loopback, and nothing but the proxy can reach the app. If the origin is ever
# exposed directly -- a LAN bind, a port opened on the host -- this becomes
# forgeable and the check has to become "is the peer a Cloudflare address".
#
# ProxyFix still runs, for `x_proto`/`x_host` (the scheme and host the visitor
# actually used) and as the fallback when the header is absent, which is what
# happens when the app is reached without Cloudflare in front of it.
class _CloudflareClientIP:
    """Set REMOTE_ADDR from `CF-Connecting-IP`, when Cloudflare supplied one.

    The value is parsed as an IP address before it is used, and dropped if it
    is not one. Cloudflare would never send anything else, but REMOTE_ADDR is
    interpolated straight into the access log by gunicorn's `%(h)s`, and a
    header that reached that unchecked would be a log-injection primitive: one
    newline and an attacker writes their own log lines. Validating is cheaper
    than trusting the whole path.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        raw = environ.get("HTTP_CF_CONNECTING_IP")
        if raw:
            import ipaddress

            try:
                environ["REMOTE_ADDR"] = str(ipaddress.ip_address(raw.strip()))
            except ValueError:
                pass  # not an address: keep whatever ProxyFix worked out
        return self.wsgi_app(environ, start_response)


# Order matters and is easy to get backwards. WSGI middleware runs
# outermost-first, so ProxyFix has to be the OUTER wrapper and this the inner
# one: ProxyFix works out scheme, host and a fallback address from
# X-Forwarded-*, then this overrides the address with Cloudflare's answer. Wrap
# them the other way round and ProxyFix runs second and puts the Cloudflare edge
# IP back -- which is exactly what the first version of this did.
app.wsgi_app = ProxyFix(_CloudflareClientIP(app.wsgi_app),
                        x_for=1, x_proto=1, x_host=1)


#: Response headers added to everything this app serves.
#:
#: The site had none of these. Individually they are all cheap; the one worth
#: explaining is the CSP, because the whole UI is a single 7 000-line page with
#: its script and styles inline, and a policy is the difference between "a
#: future templating slip is a bug" and "a future templating slip is an
#: account-less stranger running JavaScript on the designer".
#:
#: `'unsafe-inline'` is in there because that inline block is the app. It still
#: buys the parts that matter: `default-src 'self'` means no third-party
#: origin can be reached at all (the typefaces and <model-viewer> are served
#: from here on purpose), `object-src 'none'` and `base-uri 'none'` close the
#: two classic injection escapes, and `frame-ancestors 'none'` replaces
#: X-Frame-Options. Moving the inline block into a static file would let
#: `'unsafe-inline'` go, and nothing else here would need to change.
#:
#: HSTS is sent from the origin so it survives a change of edge provider, but
#: it only takes effect over HTTPS -- the http:// -> https:// redirect itself
#: has to be turned on at the edge, which the app cannot do for itself.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self'; "
        # blob: is load-bearing, not belt-and-braces: /model3d is fetched,
        # turned into an object URL, and handed to <model-viewer>, which
        # fetches that URL itself. Without blob: here the 3D view dies with
        # "Failed to fetch" and an empty viewer -- measured in Chrome, which
        # is the only way this shows up at all.
        "connect-src 'self' blob:; "
        "worker-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


@app.after_request
def _add_security_headers(resp):
    # setdefault, not assignment: a handler that has deliberately set one of
    # these for its own response keeps it.
    for name, value in _SECURITY_HEADERS.items():
        resp.headers.setdefault(name, value)
    return resp


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._-")
    return slug or "minibadge"


def _parse_pins(params: dict) -> tuple[str, ...]:
    """Connector pins the design keeps, from either the new or old payload.

    Older saved designs (and the old UI) sent rows = ["top", "bottom"]; a
    row means both of its corner pairs, so they map straight onto pin lists.

    Raises ValueError when the payload holds something no pin list can be read
    out of (`pins: 5`, `rows: 5`). This runs before any of the handlers' own
    guards, so without the type check a scalar here escaped as a 500 rather
    than the 400 a malformed request deserves.
    """
    raw = params.get("pins")
    if raw is not None:
        try:
            wanted = set(map(str, raw))
        except TypeError:
            raise ValueError('pins must be a list of connector pin numbers, '
                             'like ["1", "2"]') from None
        return tuple(q for q in pcb.ALL_PINS if q in wanted)
    raw_rows = params.get("rows")
    if raw_rows is None:
        raw_rows = ["top", "bottom"]
    try:
        keep = {r for r in ("top", "bottom") if r in raw_rows}
    except TypeError:
        raise ValueError('rows must be a list of connector rows, '
                         'like ["top", "bottom"]') from None
    return tuple(q for q in pcb.ALL_PINS
                 if pcb.PAD_PAIRS[pcb.pair_of(q)]["row"] in keep)


def _led_keepout(led: pcb.Led, safe=None, pins=pcb.ALL_PINS, others=(),
                 outline=None, face: str | None = None,
                 clk=None) -> "_GeomKeepout":
    # The unit's actual copper (pads/via/traces/hole) plus clearance margins
    # (not the old bounding rectangle), so art wraps snugly around units.
    # pins/others matter for a via-less unit: its power trace runs to a
    # connector pad, and where it goes depends on both. `face` matters for a
    # far-side LED, whose copper is split across the board: each face's art
    # and windows dodge only the copper that is really there. `clk` (from
    # pcb.clk_info) matters for a CLK unit, whose supply trace runs to the
    # jumper or pin 9 and claims its own band of the face.
    return _GeomKeepout(pcb.unit_copper_poly(led, safe, pins, others, outline,
                                             face, clk))


def _reverse_hole_keepout(led: pcb.Led, safe=None) -> "CircleKeepout":
    # A reverse-mount unit's routed hole penetrates BOTH faces: decor on the
    # opposite side must stay clear of the hole (and its light spot).
    g = pcb._layout(led.layout, led.size, led.reverse)
    x, y = pcb.clamp_led(led.x, led.y, led.rot, led.layout, safe,
                         led.size, led.reverse)
    return CircleKeepout(x, y, g["hole"] / 2 + 0.5)


def _window_corridor(led: pcb.Led, safe=None) -> RectKeepout:
    """A 2 mm-wide band from the unit to the nearest board edge.

    FALLBACK ONLY: units normally stay tied to the perimeter ring by thin
    routed bridge traces (pcb.unit_bridges), so windows may hug them. When
    no straight bridge routes clear on some layer, this reserved band keeps
    that unit's copper island connected the old way. The web UI erases
    window pixels in the same band for exactly the same units.
    """
    b = pcb.led_unit_bbox(led, safe)
    lo, hi = 0.16, 20.16
    ex = pcb.OUTLINE_EXTENT  # bands run past any custom outline's extremes
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    dists = (b[0] - lo, hi - b[2], b[1] - lo, hi - b[3])  # left, right, top, bottom
    side = dists.index(min(dists))
    hw = 1.0
    if side == 0:
        return RectKeepout(ex[0] - 1.0, cy - hw, b[0] + 0.1, cy + hw)
    if side == 1:
        return RectKeepout(b[2] - 0.1, cy - hw, ex[2] + 1.0, cy + hw)
    if side == 2:
        return RectKeepout(cx - hw, ex[1] - 1.0, cx + hw, b[1] + 0.1)
    return RectKeepout(cx - hw, b[3] - 0.1, cx + hw, ex[3] + 1.0)


def _text_keepout(t: pcb.Text):
    # Conservative bounds for KiCad's stroke font so the logo is carved
    # clear of the glyphs. The web UI paints the same rectangle.
    w = len(t.text) * t.size * 1.05 + 0.6
    h = t.size * 1.7
    if not t.rot:
        return RectKeepout(t.x - w / 2, t.y - h / 2, t.x + w / 2, t.y + h / 2)
    # Rotated: carve the turned rectangle itself, not its envelope, so art
    # keeps the room the glyphs actually take.
    from shapely.affinity import rotate as _srotate
    from shapely.geometry import box as _sbox

    rect = _sbox(t.x - w / 2, t.y - h / 2, t.x + w / 2, t.y + h / 2)
    return _GeomKeepout(_srotate(rect, t.rot, origin=(t.x, t.y)))


class _GeomKeepout:
    """Keepout backed by an arbitrary shapely geometry (union of art rects)."""

    def __init__(self, geom):
        from shapely.prepared import prep

        self.geom = geom
        self._prep = prep(geom)

    def hits(self, px: float, py: float) -> bool:
        from shapely.geometry import Point

        return self._prep.contains(Point(px, py))


class _OutsideKeepout:
    """Keepout for everywhere OUTSIDE a geometry (clips art to the outline)."""

    def __init__(self, geom):
        from shapely.prepared import prep

        self.geom = geom
        self._prep = prep(geom)

    def hits(self, px: float, py: float) -> bool:
        from shapely.geometry import Point

        return not self._prep.contains(Point(px, py))


def _clip_art_geom(geom, keepouts: list, board: tuple[float, float, float, float]):
    """Apply the pixel keepouts to exact vector art as true set operations.

    The raster path drops pixels whose centers land in a keepout; here the
    same regions are subtracted geometrically, so SVG art is carved with
    exact edges instead of pixel bites.
    """
    from shapely.geometry import Point
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    g = geom.intersection(
        sbox(board[0] + EDGE_MARGIN, board[1] + EDGE_MARGIN,
             board[2] - EDGE_MARGIN, board[3] - EDGE_MARGIN)
    )
    cuts = []
    for k in keepouts:
        if isinstance(k, RectKeepout):
            cuts.append(sbox(k.x0, k.y0, k.x1, k.y1))
        elif isinstance(k, CircleKeepout):
            cuts.append(Point(k.x, k.y).buffer(k.r, quad_segs=24))
        elif isinstance(k, _OutsideKeepout):
            g = g.intersection(k.geom)
        else:  # _GeomKeepout
            cuts.append(k.geom)
    if cuts:
        g = g.difference(unary_union(cuts))
    return g.simplify(0.005)


def _svg_classify(
    data: bytes,
    *,
    mode: str,
    cx: float,
    cy: float,
    width_mm: float,
    rot: float,
    flip: bool,
    overrides: list[tuple[float, float, str]],
    board: tuple[float, float, float, float],
    threshold: int = 128,
    invert: bool = False,
    material: str = "silk",
    palette: list[tuple[tuple[int, int, int], str]] | None = None,
) -> dict[str, object]:
    """Vector twin of logo.classify_image: material -> exact geometry.

    Colors map to materials the same way (nearest palette entry, or the
    luma threshold), and each magic-wand override retargets the connected
    visible region under its seed point, here a polygon component instead
    of a flood-filled pixel patch. Raises for SVGs the exact parser can't
    represent; the caller then falls back to the raster pipeline.
    """
    from shapely.affinity import affine_transform
    from shapely.geometry import Point
    from shapely.geometry import box as sbox

    regions, frame = svgart.svg_color_regions(data)
    matrix, w, h = svgart.fit_transform(frame, cx, cy, width_mm, board, rot, flip)
    x0, y0 = cx - w / 2, cy - h / 2
    placed = sbox(x0, y0, x0 + w, y0 + h)

    components: list[list] = []  # [polygon, material] in paint order
    for rgb, geom in regions:
        if mode == "palette" and palette:
            best = min(
                range(len(palette)),
                key=lambda j: sum((rgb[c] - palette[j][0][c]) ** 2 for c in range(3)),
            )
            mat = palette[best][1]
        else:
            dark = (299 * rgb[0] + 587 * rgb[1] + 114 * rgb[2]) / 1000 < threshold
            if invert:
                dark = not dark
            mat = material if dark else "ignore"
        g = affine_transform(geom, matrix).intersection(placed)
        for poly in svgart.geom_polygons(g):
            components.append([poly, mat])

    for u, v, mat in overrides or []:
        p = Point(x0 + u * w, y0 + v * h)
        for comp in components:
            if comp[0].covers(p):
                comp[1] = mat
                break

    mats: dict[str, object] = {}
    for poly, mat in components:
        if mat == "ignore":
            continue
        mats[mat] = poly if mat not in mats else mats[mat].union(poly)
    return mats


OUTLINE_COLS = 480  # outline classification grid (~0.25 mm pixels at 119 mm)


def _geom_rings(geom) -> list[list[tuple[float, float]]]:
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    rings = []
    for poly in polys:
        rings.append([(round(x, 3), round(y, 3)) for x, y in poly.exterior.coords[:-1]])
        for hole in poly.interiors:
            rings.append([(round(x, 3), round(y, 3)) for x, y in hole.coords[:-1]])
    return rings


def _smooth_radius(shape_meta: dict) -> float:
    """User-adjustable edge smoothing, mm (0 = off)."""
    try:
        r = float(shape_meta.get("smooth", 0.12))
    except (TypeError, ValueError):
        r = 0.12
    return min(max(r, 0.0), 0.5)


def _svg_shape_geometry(data: bytes, el: dict):
    """A board silhouette taken straight from SVG vector paths.

    Shapes whose fill passes the threshold test (dark = board, like the
    raster path) are used exactly: no pixel grid, and no smoothing pass,
    because there is no staircase to melt. Raises if the SVG can't be
    parsed exactly; the caller falls back to the raster silhouette.
    """
    from shapely.affinity import affine_transform
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    ex = pcb.OUTLINE_EXTENT
    regions, frame = svgart.svg_color_regions(data)
    threshold = int(el.get("threshold", 128))
    invert = bool(el.get("invert", False))
    dark = []
    for rgb, geom in regions:
        d = (299 * rgb[0] + 587 * rgb[1] + 114 * rgb[2]) / 1000 < threshold
        if invert:
            d = not d
        if d:
            dark.append(geom)
    if not dark:
        return None
    cx = min(max(float(el.get("cx", 10.16)), ex[0] + 2), ex[2] - 2)
    cy = min(max(float(el.get("cy", 10.16)), ex[1] + 2), ex[3] - 2)
    matrix, w, h = svgart.fit_transform(frame, cx, cy, float(el.get("w", 16)), ex)
    g = affine_transform(unary_union(dark), matrix)
    g = g.intersection(sbox(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)).simplify(0.01)
    return None if g.is_empty else g


def _image_silhouette(el: dict, data: bytes | None, raster: bytes | None,
                      smooth_r: float, budget: "_RectBudget | None" = None):
    """Silhouette geometry of one image outline element (SVG-exact or raster).

    `budget` is charged for the pixel rectangles this element contributes, so
    a composition of twelve elements cannot spend twelve times the ceiling.
    """
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    if data is None:
        return None
    ex = pcb.OUTLINE_EXTENT
    if svgart.is_svg(data):
        try:
            _check_svg_complexity(data)
            return _svg_shape_geometry(data, el)
        except _TooComplex:
            # Too intricate to trace exactly in reasonable time. The browser's
            # raster render of the same file is a perfectly good silhouette,
            # so degrade to it; only say no when the client sent none.
            if raster is None:
                raise
            data = raster
        except Exception:  # noqa: BLE001 (any parse issue means "not exact")
            # Not exactly parseable (gradients, malformed markup, ...):
            # use the browser's raster render of the same SVG instead.
            if raster is None:
                raise ValueError("could not parse the SVG board shape")
            data = raster
    ci = classify_image(
        data,
        cx=min(max(float(el.get("cx", 10.16)), ex[0] + 2), ex[2] - 2),
        cy=min(max(float(el.get("cy", 10.16)), ex[1] + 2), ex[3] - 2),
        width_mm=float(el.get("w", 16)),
        mode="threshold",
        threshold=int(el.get("threshold", 128)),
        invert=bool(el.get("invert", False)),
        material="silk",
        board=pcb.OUTLINE_EXTENT,
        max_cols=OUTLINE_COLS,
    )
    rects = grid_to_rects(ci, "silk", [], board=pcb.OUTLINE_EXTENT)
    if not rects:
        return None
    if budget is not None:
        budget.spend(len(rects))
    # Pad each pixel rect slightly: rect coordinates are rounded to 4
    # decimals, and the ~1e-4 mm seams would otherwise keep neighbouring
    # rows as separate polygons (slicing the outline to ribbons).
    shape = unary_union(
        [sbox(x - 0.02, y - 0.02, x + w + 0.02, y + h + 0.02) for x, y, w, h in rects]
    )
    if smooth_r > 0:
        shape = (shape.buffer(smooth_r, quad_segs=3)
                 .buffer(-2 * smooth_r, quad_segs=3)
                 .buffer(smooth_r, quad_segs=3))
    shape = shape.simplify(0.02)
    return None if shape.is_empty else shape


# Generic outline elements the user can compose (plus "image").
SHAPE_KINDS = ("image", "circle", "rect", "triangle", "hex", "star")


def _element_geometry(el: dict, data: bytes | None, raster: bytes | None,
                      smooth_r: float, budget: "_RectBudget | None" = None):
    """Geometry of a single outline element, or None."""
    import math

    from shapely.affinity import rotate as srotate
    from shapely.geometry import Point, Polygon
    from shapely.geometry import box as sbox

    kind = str(el.get("kind", "circle"))
    if kind == "image":
        return _image_silhouette(el, data, raster, smooth_r, budget)
    ex = pcb.OUTLINE_EXTENT
    cx = min(max(float(el.get("cx", 10.16)), ex[0] + 2), ex[2] - 2)
    cy = min(max(float(el.get("cy", 10.16)), ex[1] + 2), ex[3] - 2)
    w = min(max(float(el.get("w", 20.0)), 2.0), 118.0)
    rot = float(el.get("rot", 0)) % 360
    if kind == "circle":
        return Point(cx, cy).buffer(w / 2, quad_segs=64)
    if kind == "rect":
        h = min(max(float(el.get("h", w)), 2.0), 122.0)
        g = sbox(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    elif kind == "triangle":
        h = w * 0.866
        g = Polygon([(cx, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)])
    elif kind == "hex":
        # Regular N-gon (wire kind stays "hex" for compat), point-up.
        n = min(max(int(el.get("sides", 6) or 6), 3), 12)
        r = w / 2
        g = Polygon([
            (cx + r * math.cos(math.radians(-90 + i * 360 / n)),
             cy + r * math.sin(math.radians(-90 + i * 360 / n)))
            for i in range(n)
        ])
    elif kind == "star":
        ro, ri = w / 2, w / 2 * 0.382
        pts = []
        for i in range(10):
            rr = ro if i % 2 == 0 else ri
            a = math.radians(-90 + i * 36)
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        g = Polygon(pts)
    else:
        return None
    if rot:
        g = srotate(g, rot, origin=(cx, cy))
    return g


def _shape_geometry(shape_meta: dict, uploads: dict, rasters: dict):
    """The combined user outline (no pad plates), or None for the square.

    "custom" mode composes a list of elements (images and generic shapes)
    where each element either adds board material or cuts it away (cuts
    apply after all adds). Legacy single-shape metas still work.
    """
    from shapely.geometry import Point
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    mode = str(shape_meta.get("mode", "square"))
    smooth_r = _smooth_radius(shape_meta)
    # One allowance for the whole composition, not per element.
    budget = _RectBudget()
    if mode == "circle":  # legacy
        d = min(max(float(shape_meta.get("d", 20.0)), 12.0), 100.0)
        return Point(10.16, 10.16).buffer(d / 2, quad_segs=64)
    if mode == "image":  # legacy single image
        return _image_silhouette(
            shape_meta, uploads.get("legacy"), rasters.get("legacy"), smooth_r,
            budget,
        )
    if mode != "custom":
        return None
    adds, cuts = [], []
    for i, el in enumerate(list(shape_meta.get("elements", []))[:12]):
        g = _element_geometry(el, uploads.get(i), rasters.get(i), smooth_r,
                              budget)
        if g is None or g.is_empty:
            continue
        (cuts if el.get("op") == "cut" else adds).append(g)
    if not adds:
        if not cuts:
            return None
        # Cut-only compositions carve the standard square: "take the normal
        # badge and punch shapes out of it" needs no explicit base part.
        adds = [sbox(0.16, 0.16, 20.16, 20.16)]
    shape = unary_union(adds)
    if cuts:
        shape = shape.difference(unary_union(cuts))
    shape = shape.simplify(0.02)
    return None if shape.is_empty else shape


def _compute_outline(shape_meta: dict, uploads: dict, pins, rasters: dict,
                     cuts=None):
    """Board outline rings from the shape params: (rings, bridged) or (None, False).

    The combined shape unions with a minimal board tab per kept connector
    pad pair (never a full-width strip), so the shape's own cuts win
    everywhere except directly under the pads. A tab the shape doesn't
    reach solidly gets a 3 mm bridge to the shape's nearest point rather
    than silently falling apart; leftover floating pieces are dropped.

    `cuts` is extra geometry to carve out of the board (art layers whose
    material is "cut"). Like the shape's own cut parts it subtracts before
    the pad tabs, so the connector pads always keep solid board under them.
    """
    from shapely.geometry import LineString, Polygon
    from shapely.geometry import box as sbox
    from shapely.ops import nearest_points, unary_union

    shape = _shape_geometry(shape_meta, uploads, rasters)
    if cuts is not None and not cuts.is_empty:
        if shape is None:
            # Cut-only art carves the standard square, exactly like a
            # cut-only shape composition does.
            shape = sbox(0.16, 0.16, 20.16, 20.16)
        shape = shape.difference(cuts).simplify(0.02)
        if shape.is_empty:
            return None, False
    if shape is None:
        return None, False
    # Only corners that still carry a pin need a tab holding them.
    plates = [sbox(*pcb.PAD_PAIRS[k]["plate"]) for k in pcb.active_pairs(pins)]
    main = shape if shape.geom_type == "Polygon" else max(shape.geoms, key=lambda g: g.area)
    bridges = []
    for plate in plates:
        # A merely-touching corner is not enough: the copper pour needs a
        # solid neck, so bridge unless the overlap is substantial.
        inter = main.intersection(plate)
        if not inter.is_empty and inter.area >= 2.0:
            continue
        p1, p2 = nearest_points(plate.centroid, main)
        seg = LineString([p1, p2])
        # Round caps overlap solidly into both geometries.
        bridges.append(seg.buffer(1.5) if seg.length > 0 else plate.buffer(1.5))
    outline = unary_union([shape, *plates, *bridges])
    if outline.geom_type == "MultiPolygon":
        pieces = [g for g in outline.geoms if any(g.intersects(p) for p in plates)]
        if not pieces:
            return None, False
        outline = max(pieces, key=lambda g: g.area)
    outline = outline.simplify(0.02)
    if not isinstance(outline, Polygon) or outline.is_empty:
        return None, False
    return _geom_rings(outline), bool(bridges)


def _rings_signature(rings) -> tuple:
    """Ring count + total enclosed area + bbox, for "did this change?" tests.

    Comparing vertex lists is not safe: recomputing an identical union can
    reorder them. Area moves whenever a cut lands on the board, whether it
    opens a hole (a new ring) or bites a notch out of the edge.
    """
    if not rings:
        return (0, 0.0, 0.0, 0.0, 0.0, 0.0)
    area = 0.0
    xs: list[float] = []
    ys: list[float] = []
    for ring in rings:
        a2 = 0.0
        for i, (x1, y1) in enumerate(ring):
            x2, y2 = ring[(i + 1) % len(ring)]
            a2 += x1 * y2 - x2 * y1
            xs.append(x1)
            ys.append(y1)
        area += abs(a2) / 2
    return (len(rings), round(area, 2), round(min(xs), 1), round(min(ys), 1),
            round(max(xs), 1), round(max(ys), 1))


def _art_uploads() -> tuple[dict, dict]:
    """Every art layer's upload, read once into memory.

    Werkzeug's file streams can only be read once (`_read_upload` closes them
    behind it), but classification has to be repeatable: a cut-material layer
    is classified to find the hole, and if that hole moves the board's bounds
    the whole set is classified again against the corrected art box. Reading
    up front is what makes the second pass possible.
    """
    uploads: dict = {}
    rasters: dict = {}
    for i in range(MAX_ART):
        data = _read_upload(f"art{i}")
        if data is not None:
            uploads[i] = data
        raster = _read_upload(f"art{i}_raster")
        if raster is not None:
            rasters[i] = raster
    return uploads, rasters


def _classify_art_entry(i: int, meta: dict, art_board, uploads: dict,
                        rasters: dict):
    """Classify one art layer from the request into its material regions.

    Returns (source, window, art_side) — `source` is a {material: geometry}
    dict (basic shapes, exact SVG) or a ClassifiedImage grid — or None when
    the layer contributes nothing (no upload, empty geometry). Shared by the
    decor/window emit passes and the outline's cut-material extraction, so a
    layer's cut regions land exactly where its silk/copper regions do.
    """
    # One "side" value drives everything: front/back place the ink
    # (and open a one-sided bare window there); "through" opens the
    # bare window on both faces with the ink on the front. A legacy
    # explicit bare_side still wins if a client sends it.
    raw_side = meta.get("side")
    if raw_side not in ("front", "back", "through"):
        raw_side = None  # absent/invalid: classic through window
    window = str(meta.get("bare_side", ""))
    if window not in ("through", "front", "back"):
        window = raw_side if raw_side in ("front", "back") else "through"
    art_side = "back" if raw_side == "back" else "front"
    kind = str(meta.get("kind", "image"))
    if kind in SHAPE_KINDS and kind != "image":
        # A basic-shape art layer: exact vector geometry, no upload.
        material = str(meta.get("material", "bare"))
        if material not in pcb.ART_MATERIALS:
            material = "bare"
        geom = _element_geometry(
            {"kind": kind, "cx": meta.get("cx", 10.16), "cy": meta.get("cy", 10.16),
             "w": meta.get("w", 10), "h": meta.get("h", 10),
             "rot": meta.get("rot", 0), "sides": meta.get("sides", 6)},
            None, None, 0.0,
        )
        if geom is None or geom.is_empty:
            return None
        if art_side == "back":
            # Mirror so it reads correctly from the back face.
            from shapely.affinity import scale as _mirror

            geom = _mirror(geom, xfact=-1, yfact=1,
                           origin=(float(meta.get("cx", 10.16)), 0))
        return {material: geom}, window, art_side
    if uploads.get(i) is None:
        return None
    rot = float(meta.get("rot", 0)) % 360
    overrides = []
    for ov in list(meta.get("overrides", []))[:12]:
        mat = str(ov.get("material", "ignore"))
        if mat not in (*pcb.ART_MATERIALS, "ignore"):
            mat = "ignore"
        u = min(max(float(ov.get("u", 0.5)), 0.0), 1.0)
        if art_side == "back":
            u = 1.0 - u  # the placed image is mirrored on the back
        overrides.append((
            u,
            min(max(float(ov.get("v", 0.5)), 0.0), 1.0),
            mat,
        ))
    common = {
        "cx": float(meta.get("cx", 10.16)),
        "cy": float(meta.get("cy", 10.16)),
        "width_mm": float(meta.get("w", 14)),
        "rot": rot,
        # Back-side art mirrors so it reads correctly from the back.
        "flip": bool(meta.get("flip", False)) != (art_side == "back"),
        "overrides": overrides,
        "board": art_board,
    }
    if meta.get("mode") == "palette":
        # Each palette color carries its own material assignment.
        palette = []
        for entry in list(meta.get("palette", []))[:MAX_PALETTE]:
            rgb = [min(max(int(v), 0), 255) for v in list(entry.get("rgb", []))[:3]]
            if len(rgb) != 3:
                continue
            mat = str(entry.get("material", "ignore"))
            if mat not in (*pcb.ART_MATERIALS, "ignore"):
                mat = "ignore"
            palette.append((tuple(rgb), mat))
        if not palette:
            return None
        mode_kw = {"mode": "palette", "palette": palette}
    else:
        material = str(meta.get("material", "silk"))
        if material not in pcb.ART_MATERIALS:
            material = "silk"
        mode_kw = {
            "mode": "threshold",
            "threshold": int(meta.get("threshold", 128)),
            "invert": bool(meta.get("invert", False)),
            "material": material,
        }
    data = uploads.get(i)
    if svgart.is_svg(data):
        # Exact vector pipeline; the browser's raster render of the
        # same SVG is the fallback for gradients etc.
        try:
            _check_svg_complexity(data)
            return _svg_classify(data, **mode_kw, **common), window, art_side
        except Exception as exc:  # any parse issue: fall back to the raster render
            data = rasters.get(i)
            if data is None:
                if isinstance(exc, _TooComplex):
                    raise
                raise ValueError("could not parse an SVG artwork layer") from None
    return classify_image(data, **mode_kw, **common), window, art_side


def _uses_cut(meta) -> bool:
    """Whether one art meta assigns the board-cutout material anywhere."""
    if not isinstance(meta, dict):
        return False
    kind = str(meta.get("kind", "image"))
    if kind in SHAPE_KINDS and kind != "image":
        return str(meta.get("material", "")) == "cut"
    if any(str(o.get("material", "")) == "cut"
           for o in list(meta.get("overrides", []) or [])[:12]
           if isinstance(o, dict)):
        return True
    if meta.get("mode") == "palette":
        return any(str(e.get("material", "")) == "cut"
                   for e in list(meta.get("palette", []) or [])[:MAX_PALETTE]
                   if isinstance(e, dict))
    return str(meta.get("material", "")) == "cut"


def _art_board_of(outline_rings):
    """The art placement box for an outline: its bbox ∪ the standard square."""
    if not outline_rings:
        return (0.16, 0.16, 20.16, 20.16)
    xs = [x for x, _y in outline_rings[0]]
    ys = [y for _x, y in outline_rings[0]]
    return (min(0.16, *xs), min(0.16, *ys), max(20.16, *xs), max(20.16, *ys))


def _art_cut_geometry(sources: list, art_board):
    """Union of the cut-material regions in classified art sources, or None.

    `sources` are _classify_art_entry results ({material: geometry} dicts or
    ClassifiedImage grids), the same objects the decor/window passes consume,
    so a cut region lands exactly where the layer's other materials say it
    is — and each upload stream is still read exactly once. Raster cut edges
    get the same gentle smoothing an image silhouette part gets, so the
    Edge.Cuts contour is a fab-able curve rather than a pixel staircase.
    """
    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    geoms = []
    for source in sources:
        if isinstance(source, dict):
            g = source.get("cut")
            if g is not None and not g.is_empty:
                geoms.append(g)
            continue
        rects = grid_to_rects(source, "cut", [], board=art_board)
        if rects:
            # Pad each pixel rect slightly so rounded coordinates can't
            # slice the union into ribbons (same trick as _svg_classify's
            # raster silhouette).
            geoms.append(unary_union(
                [sbox(x - 0.02, y - 0.02, x + w + 0.02, y + h + 0.02)
                 for x, y, w, h in rects]))
            smooth = 0.12
            geoms[-1] = (geoms[-1]
                         .buffer(smooth, quad_segs=3)
                         .buffer(-2 * smooth, quad_segs=3)
                         .buffer(smooth, quad_segs=3))
    if not geoms:
        return None
    g = unary_union(geoms).simplify(0.02)
    return None if g.is_empty else g


def _read_upload(field: str) -> bytes | None:
    """Whole body of one uploaded file, releasing the stream behind it.

    Werkzeug spills anything over ~500 KB into a temporary file, and the app
    reads every upload exactly once, so holding the handle open past the read
    just leaves a descriptor for the garbage collector to find later. The
    upload cap is 24 MiB, so that is worth not doing.
    """
    f = request.files.get(field)
    if not f:
        return None
    try:
        return f.read()
    finally:
        f.close()


def _shape_uploads() -> tuple[dict, dict]:
    """Collect outline image uploads: legacy "shape" key + per-element keys."""
    uploads: dict = {}
    rasters: dict = {}
    legacy = _read_upload("shape")
    if legacy is not None:
        uploads["legacy"] = legacy
    legacy_r = _read_upload("shape_raster")
    if legacy_r is not None:
        rasters["legacy"] = legacy_r
    for i in range(12):
        f = _read_upload(f"shape{i}")
        if f is not None:
            uploads[i] = f
        fr = _read_upload(f"shape{i}_raster")
        if fr is not None:
            rasters[i] = fr
    return uploads, rasters


@app.get("/")
def index():
    fonts = [{"key": k, "label": v[0]} for k, v in textpoly.FONTS.items()]
    return render_template("index.html", fonts=fonts)


@app.get("/fonts/<key>.ttf")
def font_file(key: str):
    meta = textpoly.FONTS.get(key)
    if not meta:
        return {"error": "unknown font"}, 404
    return send_file(textpoly.FONT_DIR / meta[1], mimetype="font/ttf", max_age=86400)


@app.post("/outline")
def outline_preview():
    """Compute the smoothed board outline for the live preview.

    The same _compute_outline runs again during /generate, so the preview
    polygon is exactly what lands on Edge.Cuts.
    """
    # Every failure here answers "no custom outline", which the UI draws as the
    # ordinary square; the preview is allowed to be more conservative than
    # /generate, never more optimistic. json.JSONDecodeError is a ValueError,
    # and so is the "not an object" / bad-pins case, so one guard covers the
    # envelope; the geometry keeps its own.
    try:
        params = json.loads(request.form.get("params", "{}"))
        if not isinstance(params, dict):
            return {"rings": None, "bridged": False}
        pins = _parse_pins(params)
    except ValueError:
        return {"rings": None, "bridged": False}
    try:
        uploads, rasters = _shape_uploads()
        shape_meta = params.get("shape") or {}
        rings, bridged = _compute_outline(shape_meta, uploads, pins, rasters)
    except _GEOMETRY_ERRORS:
        return {"rings": None, "bridged": False}
    # Art layers with cut-material regions carve the outline too; the client
    # sends those layers (meta + files) along so the preview hole is the
    # exact contour /generate will put on Edge.Cuts.
    cut_ignored = False
    try:
        art_meta = list(params.get("art", []))[:MAX_ART]
        if any(_uses_cut(m) for m in art_meta):
            board = _art_board_of(rings)
            art_uploads, art_rasters = _art_uploads()
            sources = []
            for i, meta in enumerate(art_meta):
                if not _uses_cut(meta):
                    continue
                entry = _classify_art_entry(i, meta, board, art_uploads,
                                            art_rasters)
                if entry is not None:
                    sources.append(entry[0])
            cut_geom = _art_cut_geometry(sources, board)
            if cut_geom is not None:
                before = _rings_signature(rings)
                rings, bridged = _compute_outline(
                    shape_meta, uploads, pins, rasters, cuts=cut_geom)
                # A cut that lands off the board, or only where a connector
                # pad's tab reclaims it, leaves the outline untouched. The
                # preview would just close the hole again with no reason
                # given, so hand the UI something to say.
                cut_ignored = _rings_signature(rings) == before
            else:
                cut_ignored = True  # every cut region classified to nothing
    except (_TooComplex, *_GEOMETRY_ERRORS):
        pass  # a bad art layer never hides the board; /generate reports it
    return {"rings": rings, "bridged": bridged, "cutIgnored": cut_ignored}


@app.post("/generate")
def generate():
    return _generate_impl(render=False)


def _kicad_cli() -> str | None:
    import shutil

    for cand in (os.environ.get("KICAD_CLI"), shutil.which("kicad-cli"),
                 "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                 "/usr/lib/kicad/bin/kicad-cli"):
        if cand and __import__("pathlib").Path(cand).exists():
            return cand
    return None


# ---- 3D model export -------------------------------------------------------
# POST /model3d returns the populated board as a binary glTF (GLB): board
# body, copper, soldermask + silkscreen faces, and the component models.
# The web UI hands it to a WebGL viewer for true free-orbit interaction.


@app.post("/model3d")
def model3d():
    return _generate_impl(render="model")


# Which board layer each exported mesh belongs to. kicad-cli names the board
# meshes "<project>_<layer>"; everything else is a component model. The web
# viewer groups its opacity sliders by these names.
_GLB_LAYER_ROLES = {
    "soldermask": "soldermask",
    "silkscreen": "silkscreen",
    "copper": "copper",
    "via": "copper",
    "pad": "copper",
    "PCB": "board",
}


def _tag_glb_layers(data: bytes) -> bytes:
    """Name each material after the board layer it paints, and start opaque.

    kicad-cli emits materials as mat_0..mat_N with no hint of what they are,
    but the *meshes* that use them are named "<project>_soldermask" and so on.
    Walking that mapping lets the viewer offer a per-layer opacity control
    without guessing from colors, which would break as soon as someone picks
    a black soldermask.

    The mask also ships at a fixed 0.83 alpha whatever its color; over the
    full-face copper pours that is enough bleed-through to tint a dark mask
    olive. Everything starts fully opaque here and the viewer dials it back
    on request.
    """
    import struct

    if len(data) < 20 or data[:4] != b"glTF":
        return data
    ln, typ = struct.unpack_from("<I4s", data, 12)
    if typ != b"JSON" or 20 + ln > len(data):
        return data
    try:
        gltf = json.loads(data[20:20 + ln])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return data

    materials = gltf.get("materials", [])
    if not materials:
        return data

    # material index -> the set of roles whose meshes reference it
    roles: dict[int, set] = {}
    for mesh in gltf.get("meshes", []):
        suffix = str(mesh.get("name", "")).rsplit("_", 1)[-1]
        role = _GLB_LAYER_ROLES.get(suffix, "components")
        for prim in mesh.get("primitives", []):
            mi = prim.get("material")
            if mi is not None:
                roles.setdefault(mi, set()).add(role)

    for i, mat in enumerate(materials):
        # A material shared across roles can't be attributed to one layer;
        # leave those with the components group, which is the catch-all.
        found = roles.get(i, set())
        role = found.pop() if len(found) == 1 else "components"
        mat["name"] = f"{role}:{i}"
        pbr = mat.setdefault("pbrMetallicRoughness", {})
        bcf = pbr.get("baseColorFactor")
        if isinstance(bcf, list) and len(bcf) == 4 and bcf[3] < 1.0:
            bcf[3] = 1.0
        if mat.get("alphaMode") == "BLEND":
            mat["alphaMode"] = "OPAQUE"

    body = json.dumps(gltf, separators=(",", ":")).encode()
    body += b" " * (-len(body) % 4)
    rest = data[20 + ln:]
    return (
        struct.pack("<4sII", b"glTF", 2, 20 + len(body) + len(rest))
        + struct.pack("<I4s", len(body), b"JSON") + body + rest
    )


def _refill_zones(board_path: str, deadline: "_Deadline | None" = None) -> bool:
    """Recompute the copper pours with KiCad's own filler, in place.

    The shipped fills are precomputed so the project is electrically complete
    straight out of the zip, but KiCad's file format stores a fill as one
    hole-free outline, so every void (a via's clearance, a light window) has
    to be slit open to the board edge. Those slits are real, and they show in
    the 3D view as hairlines across the pour.

    Pressing B in KiCad replaces them with properly fractured fills; this
    does the same thing for the preview so it shows the board the way it will
    actually be plotted. Needs KiCad's `pcbnew` module: the server's own
    interpreter has it in the Docker image, and a macOS KiCad install carries
    a bundled Python that has it even when the venv does not; both are
    tried. Without either the preview just keeps the slits, so this stays
    best-effort; the Gerber export, which cannot ship the slits, checks the
    returned bool and refuses instead.
    """
    import subprocess
    import sys

    script = (
        "import sys, pcbnew\n"
        "b = pcbnew.LoadBoard(sys.argv[1])\n"
        "pcbnew.ZONE_FILLER(b).Fill(b.Zones())\n"
        "b.Save(sys.argv[1])\n"
    )
    candidates = [sys.executable]
    cli = _kicad_cli()
    mac_suffix = "/Contents/MacOS/kicad-cli"
    if cli and cli.endswith(mac_suffix):
        candidates.append(
            cli[: -len(mac_suffix)]
            + "/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
        )
    # Remember which interpreter worked so later requests skip the ones that
    # can only fail (each miss costs a full interpreter start).
    if _refill_zones.exe is not None:
        candidates = [_refill_zones.exe]
    for exe in candidates:
        try:
            run = subprocess.run(
                [exe, "-c", script, board_path], capture_output=True,
                timeout=deadline.left() if deadline else 120)
        except (OSError, subprocess.SubprocessError):
            continue
        if run.returncode == 0:
            _refill_zones.exe = exe
            return True
    return False


_refill_zones.exe = None


def _glb_meshes(data: bytes) -> int:
    """Number of meshes in a GLB, or 0 if these bytes are not a usable one.

    `kicad-cli pcb export glb` can exit non-zero and still have written a
    complete, openable model; typically it failed to substitute one component
    footprint and says so on stderr. Reading the file rather than trusting the
    exit code is what lets that case reach the viewer instead of a 500.
    """
    import struct

    if len(data) < 20 or data[:4] != b"glTF":
        return 0
    ln, typ = struct.unpack_from("<I4s", data, 12)
    if typ != b"JSON" or 20 + ln > len(data):
        return 0
    try:
        gltf = json.loads(data[20:20 + ln])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    return len(gltf.get("meshes", []) or []) if isinstance(gltf, dict) else 0


def _model_glb(spec: "pcb.BadgeSpec", slug: str):
    import subprocess
    import tempfile

    cli = _kicad_cli()
    if cli is None:
        return {"error": "3D view needs KiCad (kicad-cli) installed on the "
                         "server; the downloaded project shows the same "
                         "thing in KiCad's 3D viewer (View > 3D)."}, 501
    deadline = _Deadline()
    with tempfile.TemporaryDirectory() as td:
        board = f"{td}/{slug}.kicad_pcb"
        with open(board, "w") as f:
            f.write(pcb.generate_pcb(spec))
        # Show the pours the way KiCad will fill them, not the slit-open
        # form the file format forces on us (no-op without pcbnew).
        _refill_zones(board, deadline)
        glb = f"{td}/{slug}.glb"
        try:
            run = subprocess.run(
                [cli, "pcb", "export", "glb", "--subst-models",
                 "--include-tracks", "--include-pads", "--include-zones",
                 "--include-silkscreen", "--include-soldermask",
                 "--force", "-o", glb, board],
                capture_output=True, timeout=deadline.left(),
            )
        except subprocess.TimeoutExpired:
            return {"error": "the 3D export timed out"}, 500
        try:
            with open(glb, "rb") as f:
                raw = f.read()
        except OSError:
            raw = b""
        # A non-zero exit is not the same as no model. kicad-cli complains and
        # exits 1 when it cannot substitute a component's 3D model, yet still
        # writes the whole board; the honest answer there is the board the
        # user can actually look at, with a warning, not a 500 that hides it.
        if _glb_meshes(raw) == 0:
            note = run.stderr.decode("utf-8", "replace").strip().splitlines()
            return {"error": "KiCad could not export this board"
                             + (f": {note[-1][:200]}" if note else "")}, 500
        data = _tag_glb_layers(raw)
    resp = send_file(io.BytesIO(data), mimetype="model/gltf-binary",
                     download_name=f"{slug}.glb")
    if run.returncode != 0:
        # The viewer gets a real model; say plainly that it may be incomplete
        # rather than presenting a part-less board as the finished article.
        resp.headers["X-Minibadge-Export-Warning"] = (
            "kicad-cli reported problems exporting this board; a component "
            "model may be missing from the 3D view. The downloaded KiCad "
            "project is unaffected.")
    return resp


# ---- Gerber fab package ----------------------------------------------------
# POST /gerbers returns a zip that uploads straight to a board house. One
# universal package covers the popular fabs: Protel filename extensions
# (kicad-cli's default, and what JLCPCB/PCBWay's CAM auto-detects), plain
# RS-274X (PCBWay documents that its CAM mishandles X2 attributes, and
# everyone else merely tolerates them), and a single merged Excellon drill
# file in exactly the dialect the JLCPCB/PCBWay/OSH Park KiCad guides ask
# for (mm, decimal zeros, absolute origin, alternate oval mode). OSH Park
# users are better served by the .kicad_pcb in the project zip, which OSH
# Park accepts natively.


@app.post("/gerbers")
def gerbers():
    return _generate_impl(render="gerbers")


_FAB_LAYERS = "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"

# One file per plotted layer plus the drill file. Presence is judged by
# extension, not by name: kicad-cli renders "F.SilkS" as "-F_Silkscreen.gto"
# and may change such spellings again, but the Protel extensions are the
# contract the board houses parse.
_FAB_EXTENSIONS = {"gtl", "gbl", "gtp", "gbp", "gto", "gbo", "gts", "gbs",
                   "gm1", "drl"}


def _fab_gerbers(spec: "pcb.BadgeSpec", slug: str):
    import pathlib
    import subprocess
    import tempfile

    cli = _kicad_cli()
    if cli is None:
        return {"error": "Gerber export needs KiCad (kicad-cli) installed on "
                         "the server; download the KiCad project instead and "
                         "plot there (the README in the zip walks through "
                         "it)."}, 501
    deadline = _Deadline()
    with tempfile.TemporaryDirectory() as td:
        board = f"{td}/{slug}.kicad_pcb"
        with open(board, "w") as f:
            f.write(pcb.generate_pcb(spec))
        # The 3D preview may shrug off unfilled zones; a fab package must
        # not. Plotting the shipped slit-open fills would put hairline gaps
        # across both power planes on the physical board.
        if not _refill_zones(board, deadline):
            return {"error": "the server could not refill the copper zones "
                             "(KiCad's pcbnew Python module is missing), and "
                             "Gerbers plotted without a refill carry hairline "
                             "gaps across the power planes. Download the "
                             "KiCad project instead: open it, press B, then "
                             "plot."}, 501
        out = f"{td}/fab"
        try:
            plot = subprocess.run(
                [cli, "pcb", "export", "gerbers", "-o", out + "/",
                 "--layers", _FAB_LAYERS, "--no-x2", "--no-netlist",
                 "--subtract-soldermask", board],
                capture_output=True, timeout=deadline.left(),
            )
            drill = subprocess.run(
                [cli, "pcb", "export", "drill", "-o", out + "/",
                 "--format", "excellon", "--drill-origin", "absolute",
                 "--excellon-zeros-format", "decimal",
                 "--excellon-oval-format", "alternate",
                 "--excellon-units", "mm", board],
                capture_output=True, timeout=deadline.left(),
            )
        except subprocess.TimeoutExpired:
            return {"error": "the Gerber export timed out"}, 500
        # Judge the artifacts, not the exit codes (same reasoning as the GLB
        # export): the package is complete when every layer and the drill
        # file exist, and anything short of that is a refusal: a zip with a
        # missing mask or outline plots as a real, wrong board at the fab.
        files = sorted(p for p in pathlib.Path(out).glob("*") if p.is_file())
        missing = _FAB_EXTENSIONS - {p.suffix.lstrip(".").lower() for p in files}
        if missing:
            note = (plot.stderr + drill.stderr).decode("utf-8", "replace")
            note = note.strip().splitlines()
            return {"error": "KiCad could not plot this board"
                             + (f": {note[-1][:200]}" if note else "")}, 500
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in files:
                # Flat, no folder: board-house upload forms expect the layers
                # at the top of the archive.
                zf.writestr(p.name, p.read_bytes())
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{slug}-gerbers.zip",
    )


def _generate_impl(render: bool):
    try:
        params = json.loads(request.form.get("params", "{}"))
    except json.JSONDecodeError:
        return {"error": "invalid params"}, 400
    # Valid JSON that is not an object (null, [], 42, "s") used to sail past
    # the guard above and blow up on the first params.get().
    if not isinstance(params, dict):
        return {"error": "invalid params: expected a JSON object"}, 400

    name = str(params.get("name", "minibadge"))[:60]
    # Whitelisted, not escaped: this value is interpolated into the board's
    # stackup, so an unknown one has to become the default rather than reach
    # the file. Same treatment as `finish` below.
    mask_color = str(params.get("mask_color", "green"))[:20].lower()
    if mask_color not in MASK_COLORS:
        mask_color = "green"
    finish = params.get("finish")
    if finish not in ("enig", "hasl"):
        finish = "enig"
    # Via tenting is a board-wide fab choice like the finish: anything but an
    # explicit opt-out means tented (every manufacturer's default).
    tenting = params.get("tenting") is not False

    try:
        pins = _parse_pins(params)
    except ValueError as exc:
        return {"error": str(exc)}, 400

    # Custom board outline (standard square when shape mode is "square").
    outline_rings = None
    outline_poly = None
    try:
        shape_meta = params.get("shape") or {}
        uploads, rasters = _shape_uploads()
        outline_rings, _bridged = _compute_outline(shape_meta, uploads, pins, rasters)
    except _UPLOAD_REFUSALS as exc:
        return {"error": f"board shape: {exc}"}, 400
    except _GEOMETRY_ERRORS:
        return {"error": "could not process the board shape"}, 400
    if outline_rings:
        from shapely.geometry import Polygon as _Poly

        outline_poly = _Poly(outline_rings[0], outline_rings[1:])

    # Classify every art layer ONCE, up front — each upload stream can be
    # read exactly once, and the decor/window passes reuse these sources.
    # Regions assigned the "cut" material carve the board itself: they
    # subtract from the outline before the pad tabs and bridges run, so the
    # connector pads always keep solid board and everything downstream
    # (fills, keepouts, routing, unit checks) sees the true board shape.
    try:
        art_meta = list(params.get("art", []))[:MAX_ART]
    except TypeError:
        return {"error": "invalid art parameters"}, 400
    art_uploads, art_rasters = _art_uploads()
    classified = []  # (index, source, bare-window side, board face)

    def _classify_all(board):
        out = []
        for i, meta in enumerate(art_meta):
            entry = _classify_art_entry(i, meta, board, art_uploads, art_rasters)
            if entry is not None:
                out.append((i, *entry))
        return out

    art_box = _art_board_of(outline_rings)
    try:
        classified = _classify_all(art_box)
        cut_geom = _art_cut_geometry([src for _i, src, _w, _s in classified], art_box)
    except _UPLOAD_REFUSALS as exc:
        return {"error": f"artwork: {exc}"}, 400
    except _GEOMETRY_ERRORS:
        return {"error": "could not process an artwork image"}, 400
    if cut_geom is not None:
        try:
            outline_rings, _bridged = _compute_outline(
                shape_meta, uploads, pins, rasters, cuts=cut_geom)
            # A cut that bites the board's edge shrinks its bounding box, and
            # the art box (which sizes and places every layer) is derived from
            # it. Classify again against the corrected box, or the exported
            # art would sit at a different scale than the preview drew.
            if _art_board_of(outline_rings) != art_box:
                classified = _classify_all(_art_board_of(outline_rings))
        except _UPLOAD_REFUSALS as exc:
            return {"error": f"artwork: {exc}"}, 400
        except _GEOMETRY_ERRORS:
            return {"error": "could not process the board shape"}, 400
        outline_poly = None
        if outline_rings:
            from shapely.geometry import Polygon as _Poly

            outline_poly = _Poly(outline_rings[0], outline_rings[1:])

    # LED units may go anywhere the board goes: the safe rect follows the
    # custom outline's bounds instead of the standard square.
    spec_probe = pcb.BadgeSpec(pins=pins, outline=outline_rings)
    safe = pcb.unit_safe(spec_probe)

    leds = []
    try:
        for raw in list(params.get("leds", []))[:MAX_LEDS]:
            color = str(raw.get("color", "red"))
            if color not in pcb.LED_COLORS:
                color = "red"
            side = "back" if raw.get("side") == "back" else "front"
            layout = raw.get("layout")
            reverse = bool(raw.get("reverse"))
            if layout == "reverse":  # legacy spelling: stacked + reverse
                layout, reverse = "stacked", True
            if layout not in ("stacked", "inline"):
                layout = "stacked"
            size = raw.get("size")
            if size not in pcb.LED_SIZES:
                size = "0805"
            if reverse:
                size = "1206"  # the through-board hole needs 1206 pad spacing
            rot = float(raw.get("rot", 0)) % 360
            novia = bool(raw.get("novia"))
            # Through-hole leads already cross the board, so moving just the
            # LED across would only co-locate a via with a drilled pad.
            farled = (bool(raw.get("farled")) and not reverse
                      and "drill" not in pcb.PKG.get(size, {}))
            def _bends(key: str) -> tuple:
                return tuple(
                    (max(0.0, min(20.32, float(n[0]))),
                     max(0.0, min(20.32, float(n[1]))))
                    for n in list(raw.get(key) or [])[:8]
                    if isinstance(n, (list, tuple)) and len(n) >= 2)

            # nodes bend the via-less run; anodes/vnodes bend the unit's two
            # internal traces (resistor-to-anode link, pad-to-via stub);
            # cnodes bend the CLK supply run.
            nodes = _bends("nodes")
            anodes = _bends("anodes")
            vnodes = _bends("vnodes")
            cnodes = _bends("cnodes")
            clk = bool(raw.get("clk"))
            # Where the via-less run ends: a chosen connector pad or another
            # unit's pad. Shape-checked only: net, kept-pin, face and chain
            # validity are pcb.novia_term's call, which treats a bad choice
            # as "no choice" like every other sanitized parameter here.
            term = None
            raw_term = raw.get("term")
            if isinstance(raw_term, dict):
                if str(raw_term.get("pad", "")) in pcb.PIN_LABELS:
                    term = ("pad", str(raw_term["pad"]))
                elif isinstance(raw_term.get("unit"), (int, float)):
                    term = ("unit", int(raw_term["unit"]))
            adv = None
            raw_adv = raw.get("adv")
            if isinstance(raw_adv, dict):
                # Free resistor/via placement, clamped to a sane reach so a
                # hand-crafted request can't fling parts across the page.
                off = lambda k: max(-20.0, min(20.0, float(raw_adv.get(k, 0))))
                adv = {"rx": off("rx"), "ry": off("ry"),
                       "rrot": float(raw_adv.get("rrot", 0)) % 360,
                       "lrot": float(raw_adv.get("lrot", 0)) % 360,
                       "vx": off("vx"), "vy": off("vy")}
            led = pcb.Led(x=float(raw.get("x", 10)), y=float(raw.get("y", 7)),
                          color=color, side=side, rot=rot, layout=layout,
                          size=size, reverse=reverse, novia=novia,
                          nodes=nodes, anodes=anodes, vnodes=vnodes,
                          term=term, farled=farled, adv=adv,
                          clk=clk, cnodes=cnodes)
            x, y = pcb.clamp_led_obj(led, safe)
            leds.append(pcb.Led(x=x, y=y, color=color, side=side, rot=rot,
                                layout=layout, size=size, reverse=reverse,
                                novia=novia, nodes=nodes, anodes=anodes,
                                vnodes=vnodes, term=term,
                                farled=farled, adv=adv,
                                clk=clk, cnodes=cnodes))
    except (TypeError, ValueError, AttributeError):
        return {"error": "invalid led parameters"}, 400
    # Placing the units is pure geometry over user-supplied numbers, so a
    # degenerate design (a NaN or absurd coordinate) makes shapely refuse to
    # build the polygon rather than returning a wrong one. That is a bad
    # request, not a server fault -- but the raising calls used to sit
    # outside every `try` and escaped as a 500 (defect #6).
    try:
        # Backstop nudges: units clear the connector pad pairs and each other
        # (the UI prevents both during drag; hand-crafted requests may not).
        leds = [pcb.resolve_pad_overlap(led, pins, safe) for led in leds]
        for i in range(1, len(leds)):
            for prev in leds[:i]:
                leds[i] = pcb.resolve_overlap(prev, leds[i], safe=safe)
        # On custom outlines, every unit must sit on solid board (inside the
        # outline, not over a cut-out hole). Relocate strays on a fixed 1.1 mm
        # grid.
        #
        # This used to claim it scanned "the same grid" as the web UI, and that
        # is no longer true: the client's freeSpot() went adaptive (span/90,
        # floored at 0.25 mm) so the two searches are different algorithms by
        # construction. That is fine; what has to agree is whether a given spot
        # is *acceptable*, not which spot each one picks first. The predicates
        # that decide acceptability are held in parity by
        # tests/test_browser.py (unitInsideBoard, padConflict, clampLedFor);
        # asserting the two searches land on the same coordinate would be a
        # false-positive machine.
        if outline_poly is not None:
            from dataclasses import replace as _replace

            from shapely.prepared import prep as _prep

            solid = _prep(outline_poly.buffer(-0.55))

            def on_board(led):
                # The unit's real footprint: a unit whose parts were placed by
                # hand claims only the room its copper uses, not the empty
                # envelope those parts span (pcb.unit_footprint).
                return solid.contains(pcb.unit_footprint(led, safe))

            def overlaps_any(probe, skip):
                pp = pcb.unit_poly(probe, safe)
                return any(
                    j != skip and pp.distance(pcb.unit_poly(o, safe)) < 0.2
                    for j, o in enumerate(leds)
                )

            for i, led in enumerate(leds):
                if on_board(led):
                    continue
                # Scan the whole board's bounds, the standard square first, so
                # relocated units land near the middle before drifting outward.
                found = None
                spans = [(3.5, 17.0, 3.5, 17.5),
                         (safe[1] + 2, safe[3] - 2, safe[0] + 2, safe[2] - 2)]
                for y_lo, y_hi, x_lo, x_hi in spans:
                    y = y_lo
                    while y <= y_hi and found is None:
                        x = x_lo
                        while x <= x_hi:
                            probe = _replace(led, x=x, y=y)
                            cx, cy = pcb.clamp_led_obj(probe, safe)
                            probe = _replace(probe, x=cx, y=cy)
                            if (on_board(probe) and not overlaps_any(probe, i)
                                    and not pcb.pad_conflict(probe, pins, safe)):
                                found = probe
                                break
                            x += 1.1
                        y += 1.1
                    if found is not None:
                        break
                if found is not None:
                    leds[i] = found
            stranded = [i for i, led in enumerate(leds) if not on_board(led)]
            if stranded:
                return {
                    "error": f"LED {stranded[0] + 1} does not fit on this board shape; "
                             "move it, remove it, or enlarge the shape"
                }, 400
    except _GEOMETRY_ERRORS:
        return {"error": "could not place the LEDs on this board; check for "
                          "missing or out-of-range x/y, rotation or advanced "
                          "offset values"}, 400

    # Board-level CLK hookup: the 3-pad solder jumper (with a position the
    # user may have dragged) or a direct trace to pin 9. Shape-checked here;
    # whether the flag means anything is pcb.clk_info's call (no CLK unit or
    # no pin 9 -> None, and every unit feeds from 3V3 as always).
    clk_jumper, jpos, jrot = True, None, 0.0
    jside, jvia = "front", True
    jnodes, jv3nodes, jv3pin = (), (), None
    raw_clk = params.get("clk")
    try:
        if isinstance(raw_clk, dict):
            clk_jumper = bool(raw_clk.get("jumper", True))
            jside = "back" if raw_clk.get("side") == "back" else "front"
            jvia = bool(raw_clk.get("via", True))
            jrot = float(raw_clk.get("rot", 0)) % 360
            if raw_clk.get("x") is not None and raw_clk.get("y") is not None:
                ex = pcb.OUTLINE_EXTENT
                jpos = (max(ex[0], min(ex[2], float(raw_clk["x"]))),
                        max(ex[1], min(ex[3], float(raw_clk["y"]))))

            def _jbends(key: str) -> tuple:
                return tuple(
                    (max(0.0, min(20.32, float(n[0]))),
                     max(0.0, min(20.32, float(n[1]))))
                    for n in list(raw_clk.get(key) or [])[:8]
                    if isinstance(n, (list, tuple)) and len(n) >= 2)

            jnodes = _jbends("nodes")
            jv3nodes = _jbends("v3nodes")
            if str(raw_clk.get("v3pin", "")) in pcb.PIN_LABELS:
                jv3pin = str(raw_clk["v3pin"])
    except (TypeError, ValueError):
        return {"error": "invalid clk parameters"}, 400
    if any(led.clk for led in leds) and "9" not in pins:
        return {
            "error": "an LED is set to blink with the badge CLK, but "
                     "connector pin 9 (CLK) is dropped; keep pin 9 or turn "
                     "the blink option off"
        }, 400
    clk_i = None
    try:
        spec_clk = pcb.BadgeSpec(pins=pins, outline=outline_rings, leds=leds,
                                 clk_jumper=clk_jumper, jumper=jpos,
                                 jumper_rot=jrot, jumper_side=jside,
                                 jumper_via=jvia, jumper_nodes=jnodes,
                                 jumper_v3nodes=jv3nodes,
                                 jumper_v3pin=jv3pin)
        clk_i = pcb.clk_info(spec_clk)
        if clk_i is not None and clk_i["jumper"]:
            # Backstop nudge, jumper edition: the canvas never drops it on a
            # unit, a pad pair or off the board, but hand-crafted requests
            # may. The jumper yields (units were placed first).
            import math as _math

            from shapely.geometry import Polygon as _JPoly
            from shapely.geometry import box as _jbox
            from shapely.ops import unary_union as _juu

            def _jgeom(info):
                quads = [_JPoly(q) for _lbl, q in pcb.jumper_copper_pieces(info)]
                quads += [_jbox(*b) for b in pcb.jumper_caption_boxes(info)]
                return _juu(quads)

            unit_polys = [pcb.unit_poly(led, safe) for led in leds]
            avoid = [_jbox(*pcb.PAD_PAIRS[k]["keepout"])
                     for k in pcb.active_pairs(pins)]
            avoid += [_jbox(*b) for b in pcb.caption_boxes(pins)]
            solid = (outline_poly.buffer(-0.35) if outline_poly is not None
                     else _jbox(*pcb.OUTLINE).buffer(-0.35))

            def _jok(info):
                geom = _jgeom(info)
                return (solid.contains(geom)
                        and all(geom.distance(p) >= 0.2 for p in unit_polys)
                        and not any(geom.intersects(b) for b in avoid))

            if not _jok(clk_i):
                jx0, jy0, _ = clk_i["jumper"]
                found = None
                r = 0.5
                while r <= 30.0 and found is None:
                    for k in range(16):
                        t = _math.radians(k * 22.5)
                        probe = pcb.BadgeSpec(
                            pins=pins, outline=outline_rings, leds=leds,
                            clk_jumper=True, jumper_rot=jrot,
                            jumper_side=jside, jumper_via=jvia,
                            jumper_nodes=jnodes, jumper_v3nodes=jv3nodes,
                            jumper_v3pin=jv3pin,
                            jumper=(jx0 + r * _math.cos(t),
                                    jy0 + r * _math.sin(t)))
                        info = pcb.clk_info(probe)
                        if info is not None and _jok(info):
                            found = info
                            break
                    r += 0.5
                if found is None:
                    return {
                        "error": "no room for the CLK jumper on this board; "
                                 "move some LEDs or enlarge the shape"
                    }, 400
                clk_i = found
                jpos = (found["jumper"][0], found["jumper"][1])
    except _GEOMETRY_ERRORS:
        return {"error": "could not place the CLK jumper; check its x/y"}, 400

    texts = []
    try:
        for raw in list(params.get("texts", []))[:MAX_TEXTS]:
            content = re.sub(r"[\x00-\x1f\x7f]", "", str(raw.get("text", "")))[:60]
            if not content.strip():
                continue
            size = min(max(float(raw.get("size", 1.5)), 0.6), 6.0)
            side = "back" if raw.get("side") == "back" else "front"
            font = str(raw.get("font", "kicad"))
            if font != "kicad" and font not in textpoly.FONTS:
                font = "kicad"
            material = str(raw.get("material", "silk"))
            # "cut" is art-only: letters cut through the board would drop
            # their counters on the floor, so texts never get it.
            if material not in ("silk", "copper", "glow", "bare") or font == "kicad":
                material = "silk"  # the stroke font only exists as silkscreen
            tx0, ty0, tx1, ty1 = (0.8, 0.8, 19.5, 19.5)
            if outline_poly is not None:
                b = outline_poly.bounds
                tx0, ty0 = min(0.8, b[0] + 0.8), b[1] + 0.8
                tx1, ty1 = max(19.5, b[2] - 0.8), b[3] - 0.8
            x = min(max(float(raw.get("x", 10.16)), tx0), tx1)
            y = min(max(float(raw.get("y", 10.16)), ty0), ty1)
            rot = float(raw.get("rot", 0)) % 360
            texts.append(pcb.Text(x=x, y=y, text=content, size=size, side=side,
                                  font=font, material=material, rot=rot))
    except (TypeError, ValueError, AttributeError):
        return {"error": "invalid text parameters"}, 400

    # TTF texts become exact polygons up front; their real ink bounds drive
    # the art-carving keepouts (the stroke-font width estimate would be wrong
    # for wide display faces). Index -> placed geometry.
    text_geoms: dict[int, object] = {}
    try:
        from shapely.affinity import rotate as _srotate
        from shapely.affinity import scale as _sscale
        from shapely.affinity import translate as _stranslate

        for ti, t in enumerate(texts):
            if t.font == "kicad":
                continue
            g = textpoly.text_geometry(t.text, t.font, t.size)
            if g is None:
                continue
            if t.side == "back":
                # Mirror so the string reads correctly looking at the back,
                # like gr_text's "justify mirror".
                g = _sscale(g, xfact=-1, yfact=1, origin=(0, 0))
            # Mirror first, then turn: the angle is board space (clockwise
            # seen from the front) for both faces, like Led.rot.
            if t.rot:
                g = _srotate(g, t.rot, origin=(0, 0))
            text_geoms[ti] = _stranslate(g, xoff=t.x, yoff=t.y)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {"error": "could not render a text"}, 400

    # Same story as the unit placement above: every keepout here is shapely
    # geometry derived from user numbers, and a degenerate one used to escape
    # as a 500 from outside every `try` (defect #6).
    try:
        # Artwork layers. Silk/copper decorate the front, so front parts and
        # text carve them. Glow/bare windows cut copper from BOTH pours, so they
        # must stay clear of every unit (either side) and give the connector
        # pads a wider berth to keep them solidly attached to the pours.
        kept_pads = [(x, y) for num, x, y, _net, _row in pcb.CONNECTOR_PADS
                     if num in pins]
        # Per-face decor keepouts (pads + that face's LED units); vector text can
        # sit on either face, while image art stays front-only.
        # The printed pin captions live on both silks; art keeps clear of them
        # exactly like it keeps clear of the pads.
        captions = [RectKeepout(*b) for b in pcb.caption_boxes(pins)]
        # The CLK jumper (pads, rail via + stub, the routed link to pin 9,
        # and its CLK/3V3 labels) claims front-face room like a unit does;
        # only its via barrel penetrates to the back.
        jumper_decor: list = []
        jumper_via_keep: list = []
        jface = clk_i["side"] if clk_i is not None else "front"
        if clk_i is not None and clk_i["jumper"]:
            from shapely.geometry import Polygon as _KPoly
            from shapely.ops import unary_union as _kuu

            _jquads = [_KPoly(q) for _, q in pcb.jumper_copper_pieces(clk_i)]
            for _jlink in (pcb.clk_link(leds, pins, safe, outline_rings,
                                        clk_i),
                           pcb.clk_v3_link(leds, pins, safe, outline_rings,
                                           clk_i)):
                if _jlink is not None and len(_jlink["pts"]) > 1:
                    _jquads += [_KPoly(pcb._quad_seg(a, b, 1.1))
                                for a, b in zip(_jlink["pts"],
                                                _jlink["pts"][1:])]
            jumper_decor = ([_GeomKeepout(_kuu(_jquads))]
                            + [RectKeepout(*b)
                               for b in pcb.jumper_caption_boxes(clk_i)])
            jumper_via_keep = [CircleKeepout(v[0], v[1], 0.85)
                               for v in (clk_i["via"], clk_i["v3via"]) if v]
        decor_base = {
            side: [CircleKeepout(x, y, 1.65) for x, y in kept_pads]
            + captions
            + (jumper_decor if side == jface else jumper_via_keep)
            + [_led_keepout(led, safe, pins, leds, outline_rings, side, clk_i)
               for led in leds if led.side == side]
            + [_reverse_hole_keepout(led, safe) for led in leds
               if led.side != side and led.reverse]
            # "LED on the other side" puts that half of the unit on the far face,
            # with a via in each of its pads: art on this face has to clear it too.
            + [_led_keepout(led, safe, pins, leds, outline_rings, side, clk_i)
               for led in leds if led.side != side and led.farled]
            # Through-hole LED pads penetrate both faces: far-side decor keeps
            # clear of the pad annuli (server parity with eraseArtKeepouts).
            + [CircleKeepout(x, y, r + 0.5)
               for led in leds if led.side != side
               for x, y, r in pcb.th_pad_circles(led, safe)]
            for side in ("front", "back")
        }
        bridges = pcb.unit_bridges(
            pcb.BadgeSpec(pins=pins, outline=outline_rings, leds=leds,
                          clk_jumper=clk_jumper, jumper=jpos,
                          jumper_rot=jrot, jumper_side=jside,
                          jumper_via=jvia, jumper_nodes=jnodes,
                          jumper_v3nodes=jv3nodes,
                          jumper_v3pin=jv3pin), safe
        )
        # A window has to keep clear of anything whose copper it would cut. A glow
        # window cuts both faces, so it avoids every unit. A bare window that opens
        # one face only cuts that face, so it need only avoid units mounted there,
        # plus whatever crosses the board regardless (plated pads, routed holes, a
        # LED sitting on the far side). That is what lets a back-only window run
        # right under a part mounted on the front.
        _window_base = ([CircleKeepout(x, y, 2.0) for x, y in kept_pads]
                        + captions + jumper_via_keep)

        def _crossers(face: str) -> list:
            out: list = []
            for led in leds:
                if led.side == face:
                    continue
                g = pcb.led_geometry(led)
                if led.farled and not g["hole"] and "drill" not in pcb.PKG[g["pkg"]]:
                    out.append(_led_keepout(led, safe, pins, leds, outline_rings,
                                            face, clk_i))
                    continue
                if led.reverse:
                    out.append(_reverse_hole_keepout(led, safe))
                out += [CircleKeepout(x, y, r + 0.5)
                        for x, y, r in pcb.th_pad_circles(led, safe)]
            return out

        def _face_keepouts(face: str) -> list:
            # The corridor fallback protects a unit's pour feed on BOTH
            # layers (its band survives in both fills), so a window on either
            # face keeps off it no matter which side the unit is mounted on;
            # the canvas erases the same band from every window it draws.
            return (_window_base
                    + (jumper_decor if face == jface else jumper_via_keep)
                    + [_led_keepout(led, safe, pins, leds, outline_rings, face,
                                    clk_i)
                       for led in leds if led.side == face]
                    + _crossers(face)
                    + [_window_corridor(led, safe) for i, led in enumerate(leds)
                       if None in bridges[i].values()])

        # Every window layer is carved per face: glow and through-bare
        # drawings are emitted once per face below, each cut only by that
        # face's keepouts, so the far side of a unit keeps its via (and
        # whatever else crosses the board) instead of a slab of pour
        # shadowing the whole part.
        bare_keepouts = {face: _face_keepouts(face) for face in ("front", "back")}
        # Windows stay 1.6 mm off the board edge so the copper pours keep a
        # continuous perimeter ring (pcb._fill_geometry enforces this too).
        window_board = (1.26, 1.26, 19.06, 19.06)
        art_board = (0.16, 0.16, 20.16, 20.16)
        if outline_poly is not None:
            b = outline_poly.bounds
            art_board = (
                min(0.16, b[0]), min(0.16, b[1]), max(20.16, b[2]), max(20.16, b[3])
            )
            window_board = (
                art_board[0] + 1.1, art_board[1] + 1.1, art_board[2] - 1.1, art_board[3] - 1.1
            )
            # Clip artwork to the actual board shape.
            outside = _OutsideKeepout(outline_poly.buffer(-0.35))
            decor_base["front"].append(outside)
            decor_base["back"].append(outside)
            ring = _OutsideKeepout(outline_poly.buffer(-1.6))
            bare_keepouts["front"].append(ring)
            bare_keepouts["back"].append(ring)

        # Front texts that put ink on the mask carve the art beneath them (their
        # real ink bounds for TTF texts). Window-material texts ARE windows;
        # they carve nothing.
        def _text_rect(ti: int, t: pcb.Text) -> RectKeepout:
            g = text_geoms.get(ti)
            if g is not None:
                gb = g.bounds
                return RectKeepout(gb[0] - 0.4, gb[1] - 0.4, gb[2] + 0.4, gb[3] + 0.4)
            return _text_keepout(t)

        carve_rects = {
            side: [
                _text_rect(ti, t) for ti, t in enumerate(texts)
                if t.side == side and t.material in ("silk", "copper")
            ]
            for side in ("front", "back")
        }
        decor_of = {s: decor_base[s] + carve_rects[s] for s in ("front", "back")}
    except _GEOMETRY_ERRORS:
        return {"error": "could not work out where the artwork may go; check "
                          "the LED and text positions, rotations and sizes"}, 400

    text_keepouts = carve_rects["front"]
    # `classified` was built up front (art uploads read exactly once).
    try:
        # Pass 1: non-silk materials; their mask openings then carve silk.
        from shapely.geometry import box as sbox
        from shapely.ops import unary_union

        parts: dict[int, list[pcb.ArtLayer]] = {}

        def emit(i, source, material, keepouts, board, side="front",
                 window="through") -> object | None:
            """One material layer -> ArtLayer (+ its geometry for carving)."""
            if isinstance(source, dict):  # exact vector geometry
                g = source.get(material)
                if g is None:
                    return None
                g = _clip_art_geom(g, keepouts, board)
                polys = [
                    svgart.polygon_rings(p)
                    for p in svgart.geom_polygons(g)
                    if p.area > 0.005
                ]
                if not polys:
                    return None
                parts.setdefault(i, []).append(
                    pcb.ArtLayer(material=material, side=side, window=window, polys=polys))
                return g
            rects = grid_to_rects(source, material, keepouts, board=board)
            if not rects:
                return None
            parts.setdefault(i, []).append(
                pcb.ArtLayer(material=material, side=side, window=window, rects=rects))
            return unary_union([sbox(x, y, x + w, y + h) for x, y, w, h in rects])

        # Vector texts ride the same two passes as the art, keyed after it so
        # their polygons land on top.
        text_entries = [
            (MAX_ART + ti, {texts[ti].material: g}, texts[ti].side)
            for ti, g in sorted(text_geoms.items())
        ]

        # Mask openings per face; each face's silk is carved around its own.
        mask_open: dict[str, list] = {"front": [], "back": []}

        def note_opening(material, side, made, window="through"):
            if made is None:
                return
            if material == "copper":
                mask_open[side].append(made)
            elif material == "bare":  # opens the chosen face(s)
                if window in ("through", "front"):
                    mask_open["front"].append(made)
                if window in ("through", "back"):
                    mask_open["back"].append(made)

        def emit_window(i, source, material, window, side, carve_text=True):
            """A window drawing -> one carved ArtLayer per face it cuts.

            Each face's layer is carved by that face's own keepouts, so a
            through window hugs a unit's real copper on its mounting face
            while keeping, on the far face, only what crosses the board
            (the via, a routed hole, TH pad annuli) instead of a slab of
            pour (and, for bare, an unbroken mask island) shadowing the
            whole part from the other side.
            """
            faces = ((window,) if material == "bare" and window in ("front", "back")
                     else ("front", "back"))
            for face in faces:
                keepouts = bare_keepouts[face] + (
                    text_keepouts if material == "bare" and carve_text else [])
                made = emit(i, source, material, keepouts, window_board,
                            side=side, window=face if len(faces) > 1 else window)
                note_opening(material, side, made,
                             face if len(faces) > 1 else window)

        for i, ci, window, art_side in classified:
            for material in ("copper", "glow", "bare"):
                if material in ("glow", "bare"):
                    emit_window(i, ci, material, window, art_side)
                else:
                    made = emit(i, ci, material, decor_of[art_side], art_board, side=art_side)
                    note_opening(material, art_side, made, window)
        for key, src, side in text_entries:
            for material in ("copper", "glow", "bare"):
                if material not in src:
                    continue
                if material in ("glow", "bare"):
                    # Window-material texts ARE windows; they are not carved
                    # around ink texts the way window art is.
                    emit_window(key, src, material, "through", side,
                                carve_text=False)
                else:
                    made = emit(key, src, material, decor_base[side], art_board, side)
                    note_opening(material, side, made)

        def silk_carve(base: list, side: str) -> list:
            ks = list(base)
            if mask_open[side]:
                # 0.16 mm margin: more than half the largest pixel pitch, so
                # silk never sits edge-to-edge with a mask opening (that
                # contact line trips KiCad's silk-clipped-by-mask check).
                ks.append(_GeomKeepout(unary_union(mask_open[side]).buffer(0.16)))
            return ks

        for i, ci, _window, art_side in classified:
            emit(i, ci, "silk", silk_carve(decor_of[art_side], art_side),
                 art_board, side=art_side)
        for key, src, side in text_entries:
            if "silk" in src:
                emit(key, src, "silk", silk_carve(decor_base[side], side), art_board, side)
    except _UPLOAD_REFUSALS as exc:
        return {"error": f"artwork: {exc}"}, 400
    except _GEOMETRY_ERRORS:
        return {"error": "could not process an artwork image"}, 400
    art_layers = [layer for i in sorted(parts) for layer in parts[i]]

    spec = pcb.BadgeSpec(
        name=name, leds=leds, texts=texts, art=art_layers, mask_color=mask_color,
        finish=finish, pins=pins, outline=outline_rings, tenting=tenting,
        clk_jumper=clk_jumper, jumper=jpos, jumper_rot=jrot,
        jumper_side=jside, jumper_via=jvia, jumper_nodes=jnodes,
        jumper_v3nodes=jv3nodes, jumper_v3pin=jv3pin,
    )
    # Via-less units pick their connector pad against the real copper fill so
    # the run cannot fence the pour's own pad onto an island. Refuse rather
    # than ship a board whose LED never lights.
    from dataclasses import replace as _dc_replace

    # Dropping a connector pin is allowed (plenty of badges only populate the
    # pair they use), but the LED circuits draw 3V3 and GND from those pads.
    # With a rail gone there is nothing to light the LEDs, so say so instead
    # of shipping a board that can never work.
    missing = pcb.power_missing(pins)
    if missing and leds:
        rail = " and ".join(missing)
        return {
            "error": f"No {rail} pin left on the connector, so the LEDs have "
                     "nothing to run on; keep at least one "
                     + " and one ".join(missing) + " pin, or remove the LEDs"
        }, 400

    resolved, unroutable = pcb.resolve_novia(spec, safe)
    if unroutable:
        i = unroutable[0]
        if leds[i].clk:
            # The supply run: a front unit's clk_route, a back unit's
            # novia_route (which carries the CLK run there). When it is the
            # problem, blame it; a clean supply run with novia also set means
            # the classic GND run below is the one that failed.
            srun = pcb.clk_route(leds[i], pins, safe, leds,
                                 outline=outline_rings, clk=clk_i)
            if srun is None:
                srun = pcb.novia_route(
                    leds[i], pins, safe, leds, outline=outline_rings,
                    term=pcb.novia_term(leds[i], leds, pins, safe, clk_i),
                    clk=clk_i)
            blame_clk = (srun is None or srun.get("tight")
                         or not (leds[i].side != "back" and leds[i].novia))
            if blame_clk:
                if srun and srun.get("manual") and srun.get("tight"):
                    return {
                        "error": f"LED {i + 1}: a CLK trace bend runs too "
                                 "close to other copper; drag it clear, or "
                                 "select it and press Delete"
                    }, 400
                where = ("the CLK jumper"
                         if clk_i is not None and clk_i["jumper"] else "pin 9")
                return {
                    "error": f"LED {i + 1}: its CLK supply trace has no "
                             f"clear path to {where} (or it fences the power "
                             "pour apart); move the LED"
                             + (" or the jumper" if clk_i is not None
                                and clk_i["jumper"] else "")
                             + ", or turn its blink option off"
                }, 400
        route = pcb.novia_route(
            leds[i], pins, safe, leds, outline=outline_rings,
            term=pcb.novia_term(leds[i], leds, pins, safe, clk_i), clk=clk_i)
        if route and route.get("manual") and route.get("tight"):
            # Their own bends are the problem, so say that rather than blaming
            # the via setting they deliberately turned off.
            return {
                "error": f"LED {i + 1}: a trace bend runs too close to other "
                         "copper; drag it clear, or select it and press "
                         "Delete"
            }, 400
        if route and route.get("term"):
            # Same idea for a hand-picked destination: whether the run cannot
            # reach it or reaches it only by fencing the pour apart, the
            # choice is the problem; name it instead of the via setting.
            return {
                "error": f"LED {i + 1}: no clear path to the chosen trace end; "
                         "drag the endpoint somewhere else, or double-click "
                         "it to go back to the nearest pad"
            }, 400
        return {
            "error": f"LED {i + 1} cannot reach its power without a via on this "
                     "board; switch its power via back on, move it, or enable "
                     "the other connector row"
        }, 400
    if clk_i is not None and clk_i["jumper"]:
        link = pcb.clk_link(leds, pins, safe, outline_rings, clk_i)
        if link is not None and link.get("tight"):
            return {
                "error": "the CLK jumper cannot reach pin 9 cleanly; "
                         + ("a bend on that trace runs too close to other "
                            "copper; drag it clear or select it and press "
                            "Delete"
                            if link.get("manual") else
                            "move the jumper (or the LEDs in the way) so "
                            "its CLK pad has a clear path")
            }, 400
        v3l = pcb.clk_v3_link(leds, pins, safe, outline_rings, clk_i)
        if v3l is not None and v3l.get("tight"):
            return {
                "error": "the jumper's 3V3 trace cannot reach a 3V3 pin "
                         "cleanly; "
                         + ("a bend on that trace runs too close to other "
                            "copper; drag it clear or select it and press "
                            "Delete"
                            if v3l.get("manual") else
                            "no clear path to the chosen 3V3 pin; drag the "
                            "endpoint to the other one, or double-click it "
                            "for the nearest"
                            if v3l.get("term") else
                            "move the jumper (or the LEDs in the way), or "
                            "switch it back to feeding 3V3 through a via")
            }, 400
    spec = _dc_replace(spec, leds=resolved)
    slug = _slug(name)

    if render == "model":
        return _model_glb(spec, slug)
    if render == "gerbers":
        return _fab_gerbers(spec, slug)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{slug}/{slug}.kicad_pcb", pcb.generate_pcb(spec))
        zf.writestr(f"{slug}/{slug}.kicad_pro", pcb.generate_project(slug))
        zf.writestr(f"{slug}/BOM.csv", pcb.generate_bom(spec))
        zf.writestr(f"{slug}/README.txt", pcb.generate_readme(spec, slug=slug))
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{slug}.zip",
    )
