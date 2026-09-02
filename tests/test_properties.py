"""Property-based tests: the same law, checked against hundreds of boards.

Why this file exists (read `references/property-tests.md` before adding to it):
line coverage of `pcb.py` is already ~93 %, and a board-*shorting* defect still
hid on lines with 100 % line coverage because it only fires when
``size != "0805"``, and every hand-written example test in this repo uses
0805. Coverage here means *parameter-space* coverage: size, reverse, layout,
side, rot, adv, material, pin subset. That is what the strategies below buy.

Layout of the file:

* strategies      -- domains read off ``pcb.PKG`` / ``pcb.LED_COLORS`` /
                     ``pcb.ART_MATERIALS`` and the validation in
                     ``webapp._generate_impl``. Never invent a domain here;
                     if a value is not reachable through the webapp it is not
                     in-contract and belongs in ``hostile_leds``.
* ``normalise()`` -- the webapp's LED pipeline. Whole-board properties MUST
                     go through it (decision D12): ``generate_pcb`` is not
                     responsible for de-confliction, so feeding it raw
                     coordinates and asserting DRC-clean is a false-positive
                     factory.
* fast tier       -- pure geometry, sub-millisecond per example. 9 properties,
                     1.2 s total, unmarked so `-m "not slow"` keeps them.
* board tier      -- whole-board emission, tens to hundreds of ms per example;
                     ``slow``, so the fast tier stays inside its 10 s budget.
* xfail tier      -- properties that currently fail on a live defect. STRICT:
                     they flip to passing on their own when the bug is fixed,
                     and shout if someone weakens them. Defects #2 and #3 are
                     fixed, so only #1 (webapp.py's `rows` NameError) is left
                     here.
* kicad tier      -- real ``kicad-cli pcb drc`` on generated boards, gated by
                     ``@pytest.mark.needs("kicad")``.

Whole file: ~15 s with kicad-cli present, ~8 s without.

``derandomize=True`` + ``database=None`` in every profile is deliberate and
measured; see the reference doc. Do not add ``@seed(...)`` -- it is a
permanent blindfold, not reproducibility.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, assume, example, given, settings
from hypothesis import strategies as st

import invariants
from minibadge_designer import pcb, textpoly, webapp

pytestmark = pytest.mark.property

#: Unique subdirectory per Hypothesis example (tmp_path is function-scoped and
#: therefore shared across every example of one property).
_counter = itertools.count()


# ---------------------------------------------------------------------------
# settings profiles
# ---------------------------------------------------------------------------
# deadline=None is load-bearing, not cosmetic: generate_pcb ranges 0.8-31 ms
# depending on LED count and via-less routing, and Hypothesis' default 200 ms
# deadline flags that tail on a loaded machine. Deadlines are the single
# biggest source of Hypothesis flakes and this repo gets nothing from them.

#: Pure geometry: microseconds per example, so buy a lot of them.
CHEAP = settings(max_examples=200, deadline=None, derandomize=True,
                 database=None, suppress_health_check=[HealthCheck.too_slow])

#: Same, but for the handful of pure-geometry properties that go through
#: shapely (~2.7 ms per example instead of ~0.3 ms) -- enough examples to keep
#: exploring, few enough to stay under the 0.25 s fast-tier budget.
SHAPELY = settings(CHEAP, max_examples=80)

#: Whole-board emission: normalise() + generate_pcb, ~150-300 ms per example
#: once art, a custom outline and a via-less route are in play.
BOARD = settings(max_examples=25, deadline=None, derandomize=True,
                 database=None,
                 suppress_health_check=[HealthCheck.too_slow,
                                        HealthCheck.data_too_large])

#: For board properties whose per-example cost is small (one LED, no art):
#: buy more of them, because parameter-space coverage is the whole point.
BOARD_WIDE = settings(BOARD, max_examples=60)

# Shrinking is normally free -- it re-runs a property that costs microseconds.
# On the tiers below one example costs 0.5-1.5 s (a DRC spawn, or the whole
# /generate path), and Hypothesis will happily spend 50-200 extra calls
# shrinking. Measured on `test_the_generate_endpoint_never_returns_500`:
# **102.7 s with shrinking, 1.5 s without**, same counterexample class.
# So the expensive tiers skip the shrink phase and pin their counterexample
# with `@example(...)` instead, which is readable, diffable, and permanent.
# Investigating a *new* failure here? Put Phase.shrink back for that run.
_NO_SHRINK = (Phase.explicit, Phase.generate, Phase.target)

#: HTTP boundary: ~40 ms for a good request, ~1 s for one that trips a router.
HTTP = settings(max_examples=20, deadline=None, derandomize=True,
                database=None, phases=_NO_SHRINK,
                suppress_health_check=list(HealthCheck))

#: kicad-cli tier: ~0.5 s of DRC plus the webapp path per example.
HEAVY = settings(max_examples=15, deadline=None, derandomize=True,
                 database=None, phases=_NO_SHRINK,
                 suppress_health_check=list(HealthCheck))


# ---------------------------------------------------------------------------
# strategies -- domains read out of the source, never invented
# ---------------------------------------------------------------------------
COLORS = sorted(pcb.LED_COLORS)                    # pcb.py:312
SIZES = list(pcb.LED_SIZES)                        # 0603 0805 1206 1.8mm 3mm 5x2mm
SIDES = ["front", "back"]
LAYOUTS = ["stacked", "inline"]
MATERIALS = list(pcb.ART_MATERIALS)                # silk copper glow bare
WINDOWS = ["through", "front", "back"]
MASKS = ["green", "red", "blue", "black", "white", "purple", "yellow"]
FINISHES = ["enig", "hasl"]
# Only fonts whose TTF is actually on disk: the bundled faces are fetched by
# scripts/fetch_assets.py and gitignored, so a fresh clone has none of them.
FONTS = ["kicad", *sorted(k for k, (_label, fname) in textpoly.FONTS.items()
                          if (textpoly.FONT_DIR / fname).exists())]

#: Board-mm, deliberately overshooting the 20x20 square so clamping is exercised.
coord = st.floats(min_value=-2.0, max_value=22.0,
                  allow_nan=False, allow_infinity=False)
#: The UI snaps to the four classic orientations; the API takes any float.
rot = st.one_of(st.sampled_from([0.0, 90.0, 180.0, 270.0]),
                st.floats(0, 360, exclude_max=True, allow_nan=False))


@st.composite
def adv_dicts(draw):
    """Advanced (free) resistor/via placement, as webapp.py:765 clamps it.

    NOTE the +/-20 mm reach is larger than the board. That is a real contract
    gap, not a strategy bug -- see references/property-tests.md. Properties
    about board containment must use ``leds(allow_adv=False)``.
    """
    off = st.floats(-20.0, 20.0, allow_nan=False)
    return {"rx": draw(off), "ry": draw(off), "rrot": draw(rot),
            "lrot": draw(rot), "vx": draw(off), "vy": draw(off)}


@st.composite
def leds(draw, allow_adv=True, allow_novia=True, allow_nodes=True):
    """A ``Led`` every field of which ``webapp._generate_impl`` would accept."""
    reverse = draw(st.booleans())
    # webapp.py:747 -- the through-board hole needs 1206 pad spacing.
    size = "1206" if reverse else draw(st.sampled_from(SIZES))
    through_hole = "drill" in pcb.PKG[size]
    # webapp.py:754 -- TH leads already cross the board; reverse mounts too.
    farled = draw(st.booleans()) and not reverse and not through_hole
    novia = allow_novia and draw(st.booleans())
    nodes = ()
    if novia and allow_nodes and draw(st.booleans()):
        # webapp.py:758 -- at most 8 nodes, each clamped to 0..20.32.
        nodes = tuple(draw(st.lists(
            st.tuples(st.floats(0.0, 20.32, allow_nan=False),
                      st.floats(0.0, 20.32, allow_nan=False)),
            min_size=1, max_size=3)))
    return pcb.Led(x=draw(coord), y=draw(coord),
                   color=draw(st.sampled_from(COLORS)),
                   side=draw(st.sampled_from(SIDES)), rot=draw(rot),
                   layout=draw(st.sampled_from(LAYOUTS)), size=size,
                   reverse=reverse, novia=novia, nodes=nodes, farled=farled,
                   adv=draw(adv_dicts()) if allow_adv and draw(st.booleans())
                   else None)


@st.composite
def texts(draw):
    return pcb.Text(
        x=draw(coord), y=draw(coord),
        text=draw(st.text(alphabet=st.characters(min_codepoint=32,
                                                 max_codepoint=126),
                          min_size=1, max_size=12)),
        # the whole range webapp.TEXT_SIZE_MM admits, ends included
        size=draw(st.floats(0.6, 119.0, allow_nan=False)),
        side=draw(st.sampled_from(SIDES)), rot=draw(rot),
        font=draw(st.sampled_from(FONTS)),
        material=draw(st.sampled_from(MATERIALS)))


#: Raster art: (x, y, w, h) board-mm boxes, as the pixel-grid pipeline emits.
rects = st.lists(st.tuples(st.floats(0.0, 20.0, allow_nan=False),
                           st.floats(0.0, 20.0, allow_nan=False),
                           st.floats(0.1, 8.0, allow_nan=False),
                           st.floats(0.1, 8.0, allow_nan=False)), max_size=8)


@st.composite
def ring(draw, cx=10.16, cy=10.16, rmin=1.0, rmax=6.0, n=None):
    """A star-shaped closed ring around (cx, cy) -- always a simple polygon."""
    k = n or draw(st.integers(3, 8))
    radii = draw(st.lists(st.floats(rmin, rmax, allow_nan=False),
                          min_size=k, max_size=k))
    return [(cx + r * math.cos(2 * math.pi * i / k),
             cy + r * math.sin(2 * math.pi * i / k))
            for i, r in enumerate(radii)]


polys = st.lists(ring().map(lambda r: [r]), max_size=3)


@st.composite
def art_layers(draw):
    return pcb.ArtLayer(material=draw(st.sampled_from(MATERIALS)),
                        rects=draw(rects),
                        polys=draw(polys) if draw(st.booleans()) else [],
                        side=draw(st.sampled_from(SIDES)),
                        window=draw(st.sampled_from(WINDOWS)))


@st.composite
def pin_sets(draw, allow_empty=True):
    """Any subset of the connector pins, in board order.

    ``active_pairs()`` raises on a non-empty set holding no known pin, so the
    empty tuple is the only in-contract "no pads" value.
    """
    keep = draw(st.lists(st.sampled_from(list(pcb.ALL_PINS)), unique=True,
                         min_size=0 if allow_empty else 1, max_size=8))
    return tuple(p for p in pcb.ALL_PINS if p in keep)


@st.composite
def outlines(draw):
    """A custom outline unioned with the pad plates, as _compute_outline does,
    so the connector pads always sit on solid board."""
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union

    geom = Polygon(draw(ring(rmin=4.0, rmax=9.0))).buffer(0)
    if geom.is_empty or geom.geom_type != "Polygon":
        geom = box(2.0, 2.0, 18.0, 18.0)
    geom = unary_union([geom, *[box(*p) for row in pcb.PAD_PLATES.values()
                                for p in row]])
    if geom.geom_type != "Polygon":
        geom = box(*pcb.OUTLINE)
    return ([list(geom.exterior.coords)[:-1]]
            + [list(i.coords)[:-1] for i in geom.interiors])


@st.composite
def specs(draw, n_leds=(0, 3), custom_outline=True, led_strategy=None,
          with_art=True, with_text=True):
    lo, hi = n_leds
    return pcb.BadgeSpec(
        name=draw(st.text(alphabet=st.characters(min_codepoint=32,
                                                 max_codepoint=126),
                          min_size=1, max_size=12)),
        leds=draw(st.lists(led_strategy if led_strategy is not None else leds(),
                           min_size=lo, max_size=hi)),
        texts=draw(st.lists(texts(), max_size=2)) if with_text else [],
        art=draw(st.lists(art_layers(), max_size=2)) if with_art else [],
        mask_color=draw(st.sampled_from(MASKS)),
        finish=draw(st.sampled_from(FINISHES)),
        pins=draw(pin_sets()),
        outline=draw(outlines()) if custom_outline and draw(st.booleans())
        else None)


# --- out of domain: only for probing the HTTP boundary's own validation ----
# generate_pcb TRUSTS color / size / layout / side / material / window /
# finish / mask_color to be in-domain and escapes only Text.text and the
# project name. Feeding it garbage directly is not a bug report.
HOSTILE_STRINGS = st.one_of(
    st.text(max_size=8),
    st.sampled_from(["", "PURPLE", "0402", "REVERSE", "top", "bottom",
                     "0805 ", "1.8MM", "'; DROP", "\\", '"']))


@st.composite
def hostile_leds(draw):
    """Out-of-domain LED fields, for the HTTP boundary only (never generate_pcb)."""
    return pcb.Led(x=draw(st.floats(-1e4, 1e4, allow_nan=False)),
                   y=draw(st.floats(-1e4, 1e4, allow_nan=False)),
                   color=draw(HOSTILE_STRINGS), side=draw(HOSTILE_STRINGS),
                   rot=draw(st.floats(-1e4, 1e4, allow_nan=False)),
                   layout=draw(HOSTILE_STRINGS), size=draw(HOSTILE_STRINGS),
                   reverse=draw(st.booleans()), novia=draw(st.booleans()),
                   farled=draw(st.booleans()))


# ---------------------------------------------------------------------------
# the webapp's LED pipeline, as a helper
# ---------------------------------------------------------------------------
def normalise(spec: pcb.BadgeSpec) -> pcb.BadgeSpec:
    """What ``webapp._generate_impl`` does to LEDs before generating.

    Mirrors webapp.py:774-825 and 1221-1237: clamp into the safe rect, slide
    off the connector pads, separate from earlier units, (custom outlines
    only) relocate anything sitting over a cut-out, and drop any via-less
    unit ``resolve_novia`` cannot route. The webapp answers 400 for each of
    the last two; dropping the unit is the same thing as far as any board
    property is concerned.

    Whole-board properties MUST go through this, or state the precondition
    with ``assume()``. A raw ``BadgeSpec`` may legally park a unit on a
    connector pad; ``generate_pcb`` will faithfully emit the resulting short
    and the failure is the test's fault, not the code's (decision D12).

    Keep this in step with the webapp. An earlier draft stopped after
    ``resolve_overlap`` and promptly "found" a track running clean off the
    board -- from a via-less LED whose dragged bend sat at (0, 0). The real
    endpoint answers **400** for that spec; the missing step was mine. See
    the worked example in references/property-tests.md.
    """
    safe = pcb.unit_safe(spec)
    out = [replace(led, **dict(zip(("x", "y"), pcb.clamp_led_obj(led, safe))))
           for led in spec.leds]
    out = pcb.resolve_placement(out, spec.pins, safe, spec.pin_labels)
    if spec.outline:
        out = _relocate_onto_solid_board(out, spec, safe)
    resolved, unroutable = pcb.resolve_novia(replace(spec, leds=out), safe)
    bad = set(unroutable)
    return replace(spec, leds=[led for i, led in enumerate(resolved)
                               if i not in bad])


def _relocate_onto_solid_board(leds_, spec, safe):
    """webapp.py:790-825 -- the grid rescan for units over a cut-out."""
    from shapely.prepared import prep

    solid = prep(pcb.outline_polygon(spec).buffer(-0.55))

    def on_board(led):
        return solid.contains(pcb.unit_poly(led, safe))

    def overlaps_any(probe, skip):
        pp = pcb.unit_poly(probe, safe)
        return any(j != skip and pp.distance(pcb.unit_poly(o, safe)) < 0.2
                   for j, o in enumerate(leds_))

    for i, led in enumerate(leds_):
        if on_board(led):
            continue
        found = None
        spans = [(3.5, 17.0, 3.5, 17.5),
                 (safe[1] + 2, safe[3] - 2, safe[0] + 2, safe[2] - 2)]
        for y_lo, y_hi, x_lo, x_hi in spans:
            y = y_lo
            while y <= y_hi and found is None:
                x = x_lo
                while x <= x_hi:
                    probe = replace(led, x=x, y=y)
                    cx, cy = pcb.clamp_led_obj(probe, safe)
                    probe = replace(probe, x=cx, y=cy)
                    if (on_board(probe) and not overlaps_any(probe, i)
                            and not pcb.pad_conflict(probe, spec.pins, safe)):
                        found = probe
                        break
                    x += 1.1
                y += 1.1
            if found is not None:
                break
        if found is not None:
            leds_[i] = found
    # The webapp 400s on a unit it could not place; drop it instead.
    return [led for led in leds_ if on_board(led)]


def led_json(led: pcb.Led) -> dict:
    """A ``Led`` as the browser posts it, for the HTTP-boundary properties."""
    out = {"x": led.x, "y": led.y, "color": led.color, "side": led.side,
           "rot": led.rot, "layout": led.layout, "size": led.size,
           "reverse": led.reverse, "novia": led.novia, "farled": led.farled,
           "nodes": [list(n) for n in led.nodes]}
    if led.adv:
        out["adv"] = led.adv
    return out


def balanced(text: str) -> bool:
    """True if every '(' in the s-expression closes, ignoring string bodies.

    The escape handling is a *state machine*, not a look-behind at the
    previous character. The look-behind version is the obvious one and it is
    wrong: on the correctly escaped literal ``"\\\\"`` (four characters:
    quote, backslash, backslash, quote) it reads the closing quote as
    escaped, never leaves the string, and reports the board unbalanced.
    Hypothesis found that with ``Text(text='\\\\')`` -- a *test* bug, not a
    generator bug; ``pcb._esc`` is correct. See the reference doc.
    """
    depth, in_string, escaped = 0, False, False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


SEGMENT_RE = re.compile(
    r'\(segment \(start ([-\d.]+) ([-\d.]+)\) \(end ([-\d.]+) ([-\d.]+)\) '
    r'\(width ([\d.]+)\) \(layer "([^"]+)"\)')
VIA_RE = re.compile(r"\(via \(at ([-\d.]+) ([-\d.]+)\)")


def copper_segments(board_text: str):
    """(shapely LineString in board mm, width, layer) for every track."""
    from shapely.geometry import LineString

    for x0, y0, x1, y1, w, layer in SEGMENT_RE.findall(board_text):
        line = LineString([(float(x0) - pcb.ORIGIN, float(y0) - pcb.ORIGIN),
                           (float(x1) - pcb.ORIGIN, float(y1) - pcb.ORIGIN)])
        yield line, float(w), layer


# ===========================================================================
# fast tier -- pure geometry, no board emission
# ===========================================================================
@given(st.floats(-1e3, 1e3, allow_nan=False),
       st.floats(-1e3, 1e3, allow_nan=False))
@CHEAP
def test_four_quarter_turns_are_bit_exact_identity(dx, dy):
    """pcb._r documents that multiples of 90 stay exact. Drift there would
    walk every classic-orientation unit off its pads over four rotations."""
    point = (dx, dy)
    for _ in range(4):
        point = pcb._r(*point, 90)
    assert point == (dx, dy)


@given(st.floats(-100, 100, allow_nan=False),
       st.floats(-100, 100, allow_nan=False),
       st.floats(0, 360, allow_nan=False))
@CHEAP
def test_rotation_preserves_length(dx, dy, angle):
    length = math.hypot(dx, dy)
    qx, qy = pcb._r(dx, dy, angle)
    assert abs(length - math.hypot(qx, qy)) <= 1e-9 * max(1.0, length)


@given(leds(), st.booleans())
@CHEAP
def test_clamping_a_unit_twice_changes_nothing(led, custom):
    """Non-idempotent clamping means the preview and the board disagree: the
    browser clamps on drag, the server clamps again on generate."""
    safe = (2.0, 2.0, 18.0, 18.0) if custom else None
    x, y = pcb.clamp_led_obj(led, safe)
    assert pcb.clamp_led_obj(replace(led, x=x, y=y), safe) == (x, y)


@given(leds(), st.booleans())
@CHEAP
def test_a_clamped_unit_that_fits_is_inside_the_safe_rect(led, custom):
    safe = (2.0, 2.0, 18.0, 18.0) if custom else pcb.UNIT_SAFE
    bbox = pcb.led_unit_bbox(led, safe)
    # A unit larger than the safe rect cannot be clamped into it. adv units
    # can be that large (contract gap, see the reference doc) and rejecting
    # them is the webapp's job, not clamp_led_obj's.
    assume(bbox[2] - bbox[0] <= safe[2] - safe[0]
           and bbox[3] - bbox[1] <= safe[3] - safe[1])
    assert bbox[0] >= safe[0] - 1e-6 and bbox[1] >= safe[1] - 1e-6
    assert bbox[2] <= safe[2] + 1e-6 and bbox[3] <= safe[3] + 1e-6


@given(leds(), leds())
@SHAPELY
def test_resolve_overlap_separates_by_the_gap_or_gives_up(a, b):
    """Stated with its documented escape hatch ("no room; KiCad DRC will flag
    it"): a unit is either moved clear by >= 0.2 mm, or left exactly alone.
    A move that lands somewhere still overlapping is the real defect."""
    safe = pcb.UNIT_SAFE
    a = replace(a, **dict(zip(("x", "y"), pcb.clamp_led_obj(a, safe))))
    b = replace(b, **dict(zip(("x", "y"), pcb.clamp_led_obj(b, safe))))
    out = pcb.resolve_overlap(a, b, safe=safe)
    if (out.x, out.y) == (b.x, b.y):
        return
    assert (pcb.unit_poly(out, safe).distance(pcb.unit_poly(a, safe))
            >= 0.2 - 1e-6)


@given(pin_sets(allow_empty=False))
@CHEAP
def test_pin_captions_name_only_kept_pins_and_sit_over_their_pads(pins):
    """Silk that names a pin the board does not have is a wiring trap."""
    pairs = pcb.active_pairs(pins)
    assert pairs, f"pins={pins} kept no pad pair, so this test asserts nothing"
    for key in pairs:
        caption = pcb.pair_caption(key, pins)
        kept_labels = {pcb.PIN_LABELS[p] for p in pcb.PAD_PAIRS[key]["pins"]
                       if p in pins}
        assert set(caption.split()) <= kept_labels
        x, _y = pcb.pair_caption_at(key, pins)
        xs = sorted(px for n, px, _y2, _net, _r in pcb.CONNECTOR_PADS
                    if n in pcb.PAD_PAIRS[key]["pins"])
        assert xs[0] - 1e-9 <= x <= xs[-1] + 1e-9


@given(pin_sets())
@CHEAP
def test_power_missing_agrees_with_the_pad_table(pins):
    """The 400 that stops a badge whose LEDs have no rail to sit on."""
    missing = pcb.power_missing(pins)
    for net in ("3V3", "GND"):
        have = any(n in pins and pad_net == net
                   for n, _x, _y, pad_net, _r in pcb.CONNECTOR_PADS)
        assert (net in missing) is (not have)


@given(st.lists(st.tuples(st.floats(0, 20, allow_nan=False),
                          st.floats(0, 20, allow_nan=False)),
                min_size=2, max_size=5))
@CHEAP
def test_mitre45_keeps_the_route_endpoints(pts):
    """Stated with the code's OWN epsilon, on purpose. mitre45 drops hops
    shorter than 1e-9 (pcb.py:625-628), so exact float equality on the last
    point is a false positive -- Hypothesis found pts=[(0,0),(0,1.9e-303)]
    for the first draft of this property. See the reference doc."""
    out = pcb.mitre45(pts, lambda a, b: True)
    assert out[0] == pts[0]
    drift = max(abs(out[-1][0] - pts[-1][0]), abs(out[-1][1] - pts[-1][1]))
    assert drift < 1e-9


# ===========================================================================
# board tier -- whole-board emission (marked slow: ~4 ms per example)
# ===========================================================================
@pytest.mark.slow
@given(specs(n_leds=(1, 3), led_strategy=leds(allow_novia=False)))
@BOARD
def test_the_board_is_a_balanced_sexpr(spec):
    """An unbalanced file is a 200 response KiCad cannot open -- exactly how
    the unescaped mask_color defect ships (defect #4).

    Via-less units are excluded here for cost, not correctness: the shape of
    the s-expression does not depend on routing, and one via-less pair on a
    custom outline made a single ``generate_pcb`` call take **2.1 s** (the
    router's clearance scan), which was 90 % of this property's runtime.
    Via-less coverage lives in the determinism, via-on-board and DRC
    properties instead."""
    out = pcb.generate_pcb(normalise(spec))
    assert out.startswith("(kicad_pcb") and out.endswith(")\n"), (
        f"not a single top-level (kicad_pcb ...) form: starts {out[:20]!r}, "
        f"ends {out[-20:]!r}")
    assert balanced(out), "unbalanced parens -- KiCad cannot open this file"


@pytest.mark.slow
@given(specs(n_leds=(1, 3)))
@BOARD
def test_generating_the_same_spec_twice_is_byte_identical(spec):
    """The cheapest high-value property here. The preview, the board, the 3D
    model and the render all assume the browser and the server agree bit for
    bit; a stray set iteration or a time-seeded uuid breaks all four at once."""
    spec = normalise(spec)
    assert pcb.generate_pcb(spec) == pcb.generate_pcb(spec)


@pytest.mark.slow
@given(specs(n_leds=(0, 3)))
@BOARD
def test_the_bom_has_exactly_two_rows_per_led(spec):
    """One LED, one series resistor -- an assembler orders from this file.

    Stated as a pairing rather than a total row count: the file also lists the
    connector header, and on a CLK board the solder jumper, so a total would
    have to be edited every time an unrelated line item is added -- and would
    still pass if a resistor row were swapped for a second header row.
    """
    refs = [row.split(",", 1)[0]
            for row in pcb.generate_bom(spec).strip().splitlines()[1:]]
    for i in range(len(spec.leds)):
        assert f"D{i + 1}" in refs, f"LED {i + 1} is missing from the BOM"
        assert f"R{i + 1}" in refs, (
            f"LED {i + 1} has no series resistor row; the assembler orders a "
            "bare LED and it sits across the rail")
    assert len([r for r in refs if r.startswith("D")]) == len(spec.leds)
    assert len([r for r in refs if r.startswith("R")]) == len(spec.leds)
    pcb.generate_readme(spec)          # must not raise on any legal spec


@pytest.mark.slow
@given(specs(n_leds=(1, 3), led_strategy=leds(allow_adv=False)))
@BOARD
def test_every_via_lands_on_the_board(spec):
    """adv units are excluded deliberately: their +/-20 mm offsets can push
    the envelope past the outline. That is a missing webapp contract, not a
    generate_pcb defect -- see the reference doc."""
    from shapely.geometry import Point

    spec = normalise(spec)
    board = pcb.outline_polygon(spec)
    assume(spec.leds)          # normalise drops units the webapp would 400 on
    vias = VIA_RE.findall(pcb.generate_pcb(spec))
    # Not every board has a via -- a purely via-less design has none -- but a
    # unit that keeps its via must emit one, or this loop asserts nothing.
    with_via = [led for led in spec.leds if not led.novia]
    assert vias or not with_via, (
        f"no vias emitted although {len(with_via)} unit(s) keep theirs")
    for mx, my in vias:
        point = Point(float(mx) - pcb.ORIGIN, float(my) - pcb.ORIGIN)
        assert board.covers(point), f"via {point.wkt} outside {board.bounds}"


@pytest.mark.slow
@given(specs(n_leds=(1, 2), custom_outline=False, with_art=False,
             with_text=False,
             led_strategy=leds(allow_adv=False, allow_novia=False)))
@BOARD
def test_each_unit_reaches_both_power_rails(spec):
    """The whole point of the board: the resistor input sits in the 3V3 pour
    and the cathode (or its via) in the GND pour. An unlit LED is the defect
    a user cannot see until the badge is soldered.

    Terminals are modelled as COPPER, never as centre points: the reverse
    hole is vented with a 0.12 mm fracture slit that a via centre can land
    in while the 0.7 mm via disc still bridges it (DRC agrees: 0 violations).
    """
    from shapely.geometry import Point
    from shapely.ops import unary_union

    spec = replace(normalise(spec), pins=pcb.ALL_PINS)
    assume(spec.leds)          # normalise drops units the webapp would 400 on
    safe = pcb.unit_safe(spec)
    # Units parked on a connector pad are defect #2, filed and xfailed below;
    # assuming them away here is legitimate only because of that.
    assume(not any(pcb.pad_conflict(led, spec.pins, safe) for led in spec.leds))
    front_pour = unary_union(pcb._fill_geometry("3V3", "F.Cu", spec))
    back_pour = unary_union(pcb._fill_geometry("GND", "B.Cu", spec))
    for led in spec.leds:
        geom, front = pcb.led_geometry(led), led.side != "back"
        cx, cy = pcb.clamp_led_obj(led, safe)

        def at(offset, cx=cx, cy=cy, led=led):
            rx, ry = pcb._r(offset[0], offset[1], led.rot)
            return Point(cx + rx, cy + ry)

        p3v3 = (at(geom["res_in"]).buffer(0.2) if front
                else at(geom["via_back"]).buffer(pcb.VIA_SIZE / 2))
        pgnd = (at(geom["via_front"]).buffer(pcb.VIA_SIZE / 2) if front
                else at(geom["led_k"]).buffer(0.2))
        assert front_pour.intersects(p3v3), f"3V3 terminal off the pour: {led}"
        assert back_pour.intersects(pgnd), f"GND terminal off the pour: {led}"


# ===========================================================================
# open defects -- strict xfail. DO NOT weaken a property to make these green;
# delete the marker when the defect is fixed and the property passes on its own.
# ===========================================================================
@given(leds(allow_adv=False), pin_sets())
@CHEAP
@example(led=pcb.Led(x=0.0, y=0.0, size="0603"), pins=("1",))
def test_resolve_pad_overlap_gets_the_unit_off_the_pads(led, pins):
    """Left on a pad, the unit's copper shorts GND to 3V3 -- and the user
    still gets a 200 and a zip. Every existing example test uses 0805, the
    one package where clamp_led and clamp_led_obj happen to agree.

    `allow_adv=False` is not a dodge here: without advanced offsets the unit
    envelope is always small enough that a legal position exists, so the
    guarantee is unconditional. The advanced case, where the envelope can be
    larger than the board and no position exists at all, is
    :func:`test_no_unit_sits_on_a_kept_connector_pad`, which states the
    contract conditionally because the code documents that it may give up.
    """
    safe = pcb.UNIT_SAFE
    led = replace(led, **dict(zip(("x", "y"), pcb.clamp_led_obj(led, safe))))
    moved = pcb.resolve_pad_overlap(led, pins, safe)
    assert not pcb.pad_conflict(moved, pins, safe), (
        f"{moved} still overlaps a kept pad keepout (pins={pins}); its copper "
        f"shorts the connector's GND and 3V3 together")


def _somewhere_clears_the_pads(led, pins, safe, step=0.5):
    """Is there ANY position in `safe` where this unit clears every kept pair?

    Only ever called to explain a failure, so the cost of the scan is paid on
    boards that are about to be reported anyway.
    """
    from shapely.geometry import box as sbox
    keepouts = [sbox(*pcb.pair_keepout(k)) for k in pcb.active_pairs(pins)]
    y = safe[1]
    while y <= safe[3]:
        x = safe[0]
        while x <= safe[2]:
            probe = replace(led, x=x, y=y)
            if (pcb.clamp_led_obj(probe, safe) == (x, y)
                    and not any(pcb.unit_poly(probe, safe).intersects(k)
                                for k in keepouts)):
                return True
            x += step
        y += step
    return False


def test_the_placement_pipeline_never_parks_a_unit_back_on_the_pads():
    """The hypothesis-shrunk counterexample, pinned deterministically.

    The defect it pinned (found 2026-08-16, fixed 2026-08-26): run once each
    in sequence, the pairwise resolve-overlap sweep could park the
    reverse-1206 unit back on a pad keepout resolve_pad_overlap had already
    cleared it from -- only the moving pair was re-checked -- and the shipped
    board shorted 3V3 to GND while a clearing position existed. Worse, the
    two passes could LIVELOCK: the pad slide (blind to other units) parked
    the unit on a neighbour, the sweep pushed it straight back onto the
    pads. pcb.resolve_placement now alternates the passes to a fixed point
    and gives the pad escape the neighbours' copper, and this counterexample
    is why both halves exist.

    The property test below regenerates draws on every run
    (derandomize=True), but only for as long as the sequence happens to land
    somewhere interesting; this pin survives strategy and dataclass changes
    that would shift the draws. Re-derived on 2026-08-24, when the pad
    keepouts stopped reserving the pin captions' band: same mechanism, same
    third unit -- only the pair it parked on moved.
    """
    zeros = {"rx": 0.0, "ry": 0.0, "rrot": 0.0, "lrot": 0.0,
             "vx": 0.0, "vy": 0.0}
    spec = normalise(pcb.BadgeSpec(name="0", pins=("7", "8"), leds=[
        pcb.Led(0.0, 0.0, "blue", size="0805", adv=dict(zeros)),
        pcb.Led(0.0, 0.0, "blue", size="0603", rot=90.0,
                adv=dict(zeros, vx=10.0, vy=2.0)),
        pcb.Led(0.0, 0.0, "blue", size="1206", reverse=True),
    ]))
    safe = pcb.unit_safe(spec)
    offenders = [
        i for i, led in enumerate(spec.leds)
        if pcb.pad_conflict(led, spec.pins, safe)
        and _somewhere_clears_the_pads(led, spec.pins, safe)]
    assert not offenders, (
        f"unit(s) {offenders} sit on a kept connector pad keepout after the "
        "full placement pipeline, and a position clearing every kept pair "
        "exists; the copper shorts the connector's rails together")


@pytest.mark.slow
@given(specs(n_leds=(1, 3), custom_outline=False,
             led_strategy=leds(allow_novia=False)))
@BOARD
def test_no_unit_sits_on_a_kept_connector_pad(spec):
    """Defect #2 as the user meets it: through the whole webapp LED pipeline,
    not just the one function.

    Via-less units are excluded for cost, not correctness -- pad conflict is
    pure envelope geometry and does not involve routing, while shrinking one
    counterexample through a via-less route cost **67 s** here.

    **The escape clause is the code's own documented contract, not a
    softening.** ``resolve_pad_overlap`` says it gives up when there is no
    room, and with ``Led.adv`` the user can drag a via 20 mm off its LED: the
    envelope then spans more than the board and *no* position clears the pads.
    Measured on the shrunk counterexample -- a reverse 1206 with the via at
    (12, 15) and only pin 1 kept -- the unit's bbox is 15.25 x 18.25 mm inside
    an 18.92 mm square, so it overlaps the ``tl`` keepout in both axes at every
    legal x and y. Asserting "always clears" there would demand the impossible.
    So the assertion is: it cleared, **or** nothing could have. A regression to
    first-legal-wins still goes red, because those units always had somewhere
    to go.
    """
    spec = normalise(spec)
    assume(spec.leds)          # normalise drops units the webapp would 400 on
    safe = pcb.unit_safe(spec)
    for led in spec.leds:
        if not pcb.pad_conflict(led, spec.pins, safe):
            continue
        assert not _somewhere_clears_the_pads(led, spec.pins, safe), (
            f"{led} overlaps a kept pad keepout, pins={spec.pins} -- and a "
            "position clearing every kept pair does exist, so the backstop "
            "had somewhere to put it and did not; its copper shorts the "
            "connector's GND and 3V3 together")


#: Restated independently of pcb.NOVIA_EDGE and of generate_project()'s
#: "min_copper_edge_clearance" -- an assertion that reads the same constant
#: the code reads cannot detect an edit to that constant (decision D2).
EDGE_CLEARANCE = 0.2


@pytest.mark.slow
@given(leds(allow_adv=False), pin_sets(allow_empty=False))
@BOARD_WIDE
@example(led=pcb.Led(x=2.1, y=14.2, rot=90), pins=pcb.ALL_PINS)
def test_copper_keeps_its_distance_from_the_board_edge(led, pins):
    """Copper this close to the routed edge is a real fab reject, and it
    happens on the standard 20x20 square with one utterly ordinary LED.

    The pinned example is the one that used to fail: `_bridge_route` measured
    BRIDGE_INSET along its own ray instead of perpendicular to the edge it
    met, so a ray arriving at 22.5 deg kept only 0.1561 mm of copper-to-edge.
    kicad-cli called that `[copper_edge_clearance] ... actual 0.1561 mm`.
    """
    spec = normalise(pcb.BadgeSpec(name="edge", leds=[led], pins=pins))
    assume(spec.leds)
    board = pcb.outline_polygon(spec)
    tracks = list(copper_segments(pcb.generate_pcb(spec)))
    # Every in-domain single-unit board emits copper (verified over 300
    # generated specs); an empty list here would silently pass the loop.
    assert tracks, f"no copper tracks at all on a board carrying {led}"
    for line, width, layer in tracks:
        assert board.covers(line), f"track off the board on {layer}: {line.wkt}"
        gap = board.boundary.distance(line) - width / 2
        assert gap >= EDGE_CLEARANCE - 1e-9, (
            f"{layer} copper {gap:.4f} mm from the edge (rule "
            f"{EDGE_CLEARANCE} mm) for {led}")


# ===========================================================================
# HTTP boundary -- the only honest place to assert anything about placement
# ===========================================================================
#: Two hand-verified unroutable via-less boards, both reachable from the UI
#: with "no via" plus a dragged bend / two units boxing each other in. Both
#: used to raise ``NameError: name 'rows' is not defined`` out of
#: ``_generate_impl``'s refusal branch (defect #1) and hand the user a 500;
#: they are pinned as regression examples for the friendly 400.
_NOVIA_500_A = pcb.BadgeSpec(
    name="x", leds=[pcb.Led(x=10.0, y=10.0, novia=True, nodes=((19.0, 19.05),))])
_NOVIA_500_B = pcb.BadgeSpec(
    name="x", leds=[pcb.Led(x=10.16, y=10.16, novia=True, size="1206"),
                    pcb.Led(x=10.16, y=5.0, novia=True, size="1206")])


@pytest.mark.webapp
@pytest.mark.slow
@given(specs(n_leds=(1, 2), custom_outline=False, with_art=False,
             with_text=False,
             led_strategy=leds(allow_adv=False, allow_nodes=True)))
@HTTP
@example(spec=_NOVIA_500_A)
@example(spec=_NOVIA_500_B)
def test_the_generate_endpoint_never_returns_500(spec):
    """Drives the real endpoint, which is the ONLY honest way to assert
    anything about placement: de-confliction lives in webapp.py:774-825, not
    in generate_pcb (decision D12). A 400 is a fine answer; a 500 is not --
    and here the 500 makes the two friendly 400s below it dead code."""
    params = {"name": "propbadge", "mask_color": spec.mask_color,
              "finish": spec.finish, "pins": list(spec.pins),
              "leds": [led_json(led) for led in spec.leds]}
    response = webapp.app.test_client().post(
        "/generate", data={"params": json.dumps(params)})
    # `invariants.assert_not_crashed`, not a hand-rolled `!= 500`. The library
    # version had no caller at all while three copies of its rule were scattered
    # across the suite, and a rule with three copies is one that can be edited
    # to say nothing in the copy that matters. It is also strictly stronger
    # (`< 500` catches the 502/503 a proxy in front of the app returns) and it
    # prints the response body, which the hand-rolled form did not.
    invariants.assert_not_crashed(response)


# ===========================================================================
# kicad tier -- the external oracle. ~0.55 s of DRC per example.
# ===========================================================================
# Gating goes through conftest's `needs` marker, which RUNS kicad-cli via the
# app's own locator (honouring $KICAD_CLI) rather than testing a path, and
# which records the skip in the end-of-run summary. `kicad_cli` is
# session-scoped; `run_drc` and `tmp_path` are function-scoped, which
# Hypothesis flags -- suppressed in HEAVY because both are stateless here
# (fresh subdirectory per example, closure over a session-scoped path).
def _write_project(directory: Path, board_text: str, name: str = "prop") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    board = directory / f"{name}.kicad_pcb"
    board.write_text(board_text)
    (directory / f"{name}.kicad_pro").write_text(pcb.generate_project(name))
    return board


#: The board defect #2 used to ship: an inline reverse 1206 whose four
#: candidate slides were all rejected by the 0805-only ``clamp_led``, leaving
#: it parked on pad 15. Hand-verified through both paths (generate_pcb and the
#: /generate zip): kicad-cli used to answer with exactly two error-severity
#: violations, both against ``PTH pad 15 [3V3] of J1`` -- ``[shorting_items]``
#: and ``[solder_mask_bridge]``. It is pinned as a regression example: the
#: package/layout/reverse combination is the one the old code could not move,
#: so a return to first-legal-wins puts these two markers straight back to red.
#:
#: `_NO_SHRINK` keeps `Phase.explicit`, so this example always runs.
_PAD_SHORT = pcb.BadgeSpec(
    name="padshort", pins=pcb.ALL_PINS,
    leds=[pcb.Led(x=11.4425, y=18.02, color="orange", side="back", rot=0.0,
                  layout="inline", size="1206", reverse=True)])


@pytest.mark.kicad
@pytest.mark.slow
@pytest.mark.needs("kicad")
@given(spec=specs(n_leds=(1, 2), custom_outline=False, with_art=False,
                  with_text=False, led_strategy=leds(allow_adv=False)))
@HEAVY
@example(spec=_PAD_SHORT)
def test_a_generated_board_passes_real_drc(spec, run_drc, tmp_path):
    """The external oracle. It sees courtyards, mask bridges, slivers and
    shorts that no in-process invariant models -- and it misses via layer
    spans and net-number-only shorts, which is why the skill says BOTH
    oracles, never either (decision D13).

    ``n_leds`` is back to ``(1, 2)``. It was pinned to one unit while this
    marker was a strict xfail for defect #2, because a second unit brought in a
    **unit-vs-unit** short (D1 pad vs R2 pad, 1 board in 45) from
    ``resolve_overlap`` giving up -- a different routine, and one that would
    have made the xfail red for the wrong reason. ``resolve_overlap`` no longer
    gives up on a two-unit board (measured: 35 of 600 random two-unit boards
    shorted, now 1), so the second unit earns its place again.

    It does **not** make this test the guard for that routine, and a red-proof
    says so: deleting ``resolve_overlap``'s fallbacks leaves this test green,
    because a two-unit board drawn at random rarely overlaps at all in 15
    examples. The pinned counterexamples live in
    ``test_board_invariants.py::test_the_backstop_separates_two_units_it_used_to_give_up_on``.

    ``--severity-error`` stays. The broad sweep over the placement and routing
    axes at every severity lives in
    ``test_board_invariants.py::test_every_board_in_the_corpus_passes_real_drc``.
    """
    spec = normalise(spec)
    assume(spec.leds and not pcb.power_missing(spec.pins))
    board = _write_project(tmp_path / f"drc{next(_counter)}",
                           pcb.generate_pcb(spec))
    code, report = run_drc(board, "--severity-error")
    assert code == 0, f"{spec}\n{report}"


@pytest.mark.kicad
@pytest.mark.webapp
@pytest.mark.slow
@pytest.mark.needs("kicad")
@given(spec=specs(n_leds=(1, 2), custom_outline=False, with_art=False,
                  with_text=False,
                  led_strategy=leds(allow_adv=False, allow_novia=False)))
@HEAVY
@example(spec=_PAD_SHORT)
def test_the_downloaded_zip_passes_real_drc(spec, run_drc, tmp_path):
    """End to end: what the user actually receives must be fab-ready.

    **Via-less units are out of the domain, and that is the point of this
    test existing.** With them in, every run died on the ``!= 500`` guard
    below with ``NameError: name 'rows' is not defined`` (defect #1,
    webapp.py:1224) and **never reached the DRC assertion at all** -- so the
    marker's old reason named #2 and #3 on a path that never exercised them,
    and the test was a slower duplicate of
    ``test_the_generate_endpoint_never_returns_500``, which owns #1 for 1/15th
    of the kicad-cli cost. Excluding ``novia`` puts #1 out of reach and lets
    this test assert the thing it was written for.

    ``n_leds=(1, 2)`` and ``--severity-error`` follow
    ``test_a_generated_board_passes_real_drc``; the measurements behind them
    are in that docstring.
    """
    import io
    import zipfile

    params = {"name": "propbadge", "mask_color": spec.mask_color,
              "finish": spec.finish, "pins": list(spec.pins),
              "leds": [led_json(led) for led in spec.leds]}
    response = webapp.app.test_client().post(
        "/generate", data={"params": json.dumps(params)})
    invariants.assert_not_crashed(response)
    assume(response.status_code == 200)
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    name = next(n for n in archive.namelist() if n.endswith(".kicad_pcb"))
    board = _write_project(tmp_path / f"zip{next(_counter)}",
                           archive.read(name).decode())
    code, report = run_drc(board, "--severity-error")
    assert code == 0, f"{json.dumps(params)[:600]}\n{report}"
