"""Runs the whole in-process invariant battery over a parameter-varied corpus.

Why this file exists
--------------------
``tests/invariants.py`` ships 44 checks, each with a measured true- and
false-positive rate over a 259-board sweep. Until this file landed, the suite
called exactly **two** of them. The battery was validated in a throwaway harness
and then never wired to anything that runs — so a defect it was built to catch
would still have shipped. Measured at the time: dropping *every copper fill on
the board* was caught by three incidental assertions in ``test_webapp.py`` and by
real DRC, and by none of the invariants, purely because nothing invoked them.

That is the failure this module closes. ``check_board`` is one call that runs the
lot (decision D5), so the cost of covering a new parameter combination is one
row in the matrix below.

Why a hand-written matrix and not Hypothesis
--------------------------------------------
``tests/test_properties.py`` already fuzzes this space and is the better tool for
finding the unexpected. This module is the complement: a fixed, named,
deterministic corpus that pins the combinations we *know* matter, runs in the
fast tier, and names each case in its test id so a failure says which
combination broke without anyone reading a shrink report.

Every row deliberately moves off a default (decision D6). The whole point is
that ``size="0805"`` — the value every pre-existing example test used — is where
a board-shorting defect hid on lines that already had 100% line coverage.
"""

import re

import pytest

import invariants
from minibadge_designer import pcb

_ART = [(4.0, 4.0, 5.0, 0.2), (4.0, 4.4, 4.0, 0.2)]


def _spec(**over):
    """A BadgeSpec whose every axis can be moved off its default by keyword."""
    leds = over.pop("leds", [pcb.Led(6.5, 6.0, "red"), pcb.Led(14.0, 13.0, "blue")])
    art = over.pop("art", [pcb.ArtLayer("silk", _ART, [])])
    texts = over.pop("texts", [])
    return pcb.BadgeSpec(leds=leds, texts=texts, art=art, **over)


def _octagon_outline():
    """A non-square board, unioned with the pad plates the way the webapp does.

    The connector pads must land on solid board or the outline is not one a user
    could actually order, and the row would be testing our arithmetic instead of
    the generator.
    """
    from shapely.geometry import box, Polygon
    from shapely.ops import unary_union
    c = 5.0
    oct_pts = [(c, 0.16), (20.16 - c, 0.16), (20.16, c), (20.16, 20.16 - c),
               (20.16 - c, 20.16), (c, 20.16), (0.16, 20.16 - c), (0.16, c)]
    geom = unary_union([Polygon(oct_pts),
                        *[box(*p) for row in pcb.PAD_PLATES.values() for p in row]])
    return ([list(geom.exterior.coords)[:-1]]
            + [list(i.coords)[:-1] for i in geom.interiors])


# Each entry is (id, spec). The id is the failure message a developer reads
# first, so it names the combination, not an index.
CORPUS = [
    # --- package sizes: the axis that hid live defect #2 -------------------
    *[(f"size-{s}", _spec(leds=[pcb.Led(6.5, 6.0, "red", size=s),
                                pcb.Led(14.0, 13.5, "blue", size=s)]))
      for s in pcb.PKG],
    # --- mounting side, rotation, layout ----------------------------------
    *[(f"rot-{r}", _spec(leds=[pcb.Led(6.5, 6.0, "red", rot=r),
                               pcb.Led(14.0, 13.5, "blue", rot=r, side="back")]))
      for r in (0, 90, 180, 270)],
    ("inline", _spec(leds=[pcb.Led(12.0, 16.0, "red", layout="inline")])),
    ("back-face-only", _spec(leds=[pcb.Led(10.0, 10.0, "red", side="back")])),
    ("reverse-mount", _spec(leds=[pcb.Led(10.0, 10.0, "red", size="1206",
                                          reverse=True)])),
    ("through-hole-dome", _spec(leds=[pcb.Led(10.0, 10.0, "red", size="3mm")])),
    ("bar-package", _spec(leds=[pcb.Led(10.0, 10.0, "red", size="5x2mm")])),
    # --- art materials, both faces, both window modes ----------------------
    *[(f"art-{m}", _spec(art=[pcb.ArtLayer(m, [(4.0, 9.0, 6.0, 3.0)], [])]))
      for m in ("silk", "copper", "glow", "bare")],
    ("art-on-back", _spec(art=[pcb.ArtLayer("copper", _ART, [], side="back")])),
    ("one-face-window", _spec(art=[pcb.ArtLayer("bare", [(5.0, 6.0, 11.0, 8.0)],
                                                [], side="back", window="back")])),
    # --- connector pin subsets: a whole row, a corner, a single pad ---------
    ("pins-top-row", _spec(pins=("1", "2", "7", "8"))),
    ("pins-one-corner", _spec(pins=("1", "2"))),
    ("pins-power-only", _spec(pins=("2", "7"))),
    # --- stackup ------------------------------------------------------------
    ("mask-purple-hasl", _spec(mask_color="purple", finish="hasl")),
    # --- nothing at all: the degenerate board still has to be legal ---------
    ("bare-board", _spec(leds=[], art=[])),
    # --- axes this corpus originally missed entirely ------------------------
    # Line-tracing the battery showed 0 of 26 rows set farled, novia, texts or
    # a custom outline, so several checks were *called* on every row and
    # evaluated no assertion on any of them — reachable and inert. Adding a row
    # per axis is the cheap fix; the gate below now measures execution rather
    # than trusting that a call means a check ran.
    ("far-side-led", _spec(leds=[pcb.Led(10.0, 10.0, "red", farled=True)])),
    ("via-less-unit", _spec(leds=[pcb.Led(10.0, 6.0, "red", novia=True)])),
    ("text-front-and-back", _spec(
        leds=[pcb.Led(6.0, 6.0, "red")], art=[],
        texts=[pcb.Text(10.0, 15.0, "SAINTCON", size=1.5),
               pcb.Text(10.0, 5.0, "back", size=1.2, side="back",
                        material="copper")])),
    ("custom-outline", _spec(leds=[pcb.Led(10.0, 10.0, "red")], art=[],
                             outline=_octagon_outline())),
]


@pytest.mark.parametrize("spec", [s for _, s in CORPUS],
                         ids=[i for i, _ in CORPUS])
def test_every_board_in_the_corpus_satisfies_every_invariant(spec):
    """Whatever the user builds, the board obeys every rule we can check cheaply.

    A failure here means a real badge would be wrong in the way the raised
    message names — a severed power plane, a via that does not cross the board,
    a track carrying a net its pads do not, copper over the edge. The message is
    the diagnosis; read it rather than this docstring.

    The spec goes through ``resolved_spec`` first because ``generate_pcb`` is not
    responsible for DRC cleanliness — the webapp's placement backstop is
    (decision D12). Feeding hand-written coordinates straight in and asserting
    would test this file's arithmetic instead of the generator.
    """
    invariants.check_board(invariants.resolved_spec(spec))


@pytest.mark.parametrize("spec", [s for _, s in CORPUS],
                         ids=[i for i, _ in CORPUS])
def test_generating_the_same_design_twice_gives_the_same_board(spec):
    """Two downloads of one design are byte-identical.

    Non-determinism would make every measurement in this suite unreliable, and
    the user would see two downloads of "the same" badge differ.
    """
    invariants.assert_deterministic(invariants.resolved_spec(spec))


# ===========================================================================
# Calibration (decision D11): a measurement that has never been shown to move
# on a broken artifact is not a measurement. These two tests break a board on
# purpose and require the battery to say so. Without them the battery can rot
# into passing on everything and nothing here would go red.
# ===========================================================================

_CALIBRATION_SPEC = "art-glow"          # LEDs on both faces plus a light window

#: Stated here rather than imported from ``invariants``: this is the mutation
#: side, and a test that used the reader's own pattern to build the board it
#: then asks the reader to read could agree with a broken reader.
_FILLED = re.compile(r'\(filled_polygon \(layer "([^"]+)"\) \(pts (.*?)\)\)\n', re.S)


def _shift_emitted_copper(text: str, dx: float) -> str:
    """Move every ``filled_polygon`` vertex ``dx`` mm in x, and nothing else.

    Reproduces, at the text level, an emission-path bug: the pour is computed
    correctly and written somewhere else. The same defect injected at
    ``pcb.py:2027`` produces 46 real kicad-cli DRC violations — clearance
    ``actual 0.0000 mm`` to other-net pads, i.e. a board that shorts.
    """
    def bump(m):
        moved = re.sub(
            r"\(xy (-?[\d.]+) (-?[\d.]+)\)",
            lambda p: f"(xy {float(p.group(1)) + dx:.4f} {p.group(2)})", m.group(2))
        return f'(filled_polygon (layer "{m.group(1)}") (pts {moved}))\n'
    return _FILLED.sub(bump, text)


def test_the_battery_fails_when_the_pour_is_written_where_it_was_not_computed():
    """Copper in the wrong place is caught, not just copper that is missing.

    The failure this pins: every pour invariant used to read
    ``pcb._fill_geometry`` — the function the generator itself calls — so it
    verified what the generator *would* compute and never what it *wrote*.
    Measured at the time: the whole fast tier stayed green (397 passed, 0
    failed with kicad-cli disabled) on a board with 46 DRC violations.

    If someone points a pour check back at ``Board.fills``, this test is what
    goes red. The control below it is what stops the mutation itself rotting
    into a no-op.
    """
    spec = invariants.resolved_spec(dict(CORPUS)[_CALIBRATION_SPEC])
    good = pcb.generate_pcb(spec)
    invariants.check_board(spec, good)          # control: the real board is fine

    bad = _shift_emitted_copper(good, 5.0)
    assert bad != good, (
        "the injected mutation changed nothing in the board text, so this test "
        "proves nothing — the filled_polygon syntax must have moved")
    assert bad.count("(filled_polygon") == good.count("(filled_polygon"), (
        "the mutation removed copper instead of moving it; that is the easier "
        "defect and it is already covered")

    # Board(...) rather than assert_parses(...): the text is damaged on purpose
    # and the parse-level assertions are not what is under test here.
    b = invariants.Board(bad)
    fired = {fn.__name__ for fn in invariants.ALL_CHECKS if _fails(fn, b, spec)}
    # Naming them is the point. `assert_pours_actually_contain_copper` compares
    # emitted bounds against intended bounds and would fire on its own, which
    # would let the geometry checks quietly go back to reading `Board.fills`
    # while this test stayed green. These three describe copper — where it
    # spills, what it shorts, what it floods — and each must see it itself.
    want = {"assert_pours_stay_inside_the_outline",
            "assert_pours_keep_fab_clearance",
            "assert_light_windows_are_clear_of_copper"}
    assert want <= fired, (
        f"{sorted(want - fired)} did not notice a pour written 5 mm off its "
        f"computed position. Checks that fired: {sorted(fired)}. A pour check "
        "reading `Board.fills` is asking the generator what it meant to emit")


#: Every collection a ``Board`` exposes, with whether an LED-bearing board is
#: allowed to have it empty. ``fills`` is the generator's intent; ``emitted``
#: is the copper in the file. Both are stubbed by name.
_COLLECTIONS = ["pads", "tracks", "vias", "zones", "footprints", "nets",
                "fills", "emitted_fills"]


@pytest.mark.parametrize("collection", _COLLECTIONS)
def test_the_battery_fails_when_a_whole_board_collection_is_missing(collection):
    """Emptying any one collection must make some check say so.

    Measured before this test existed: emptying ``b.tracks``, ``b.vias`` or
    ``b.footprints`` left **26 of 26 checks green**. Every invariant in the
    library is a ``for`` loop over one of these, and a loop over ``[]`` passes
    — so a generator regression that emitted no tracks (every LED unconnected),
    no vias, or no footprints at all went through the whole in-process battery
    without a murmur.

    This is the regression test for that, and it is deliberately about the
    *battery*, not about any one check: it does not care which invariant fires,
    only that silence is impossible.
    """
    spec = invariants.resolved_spec(dict(CORPUS)[_CALIBRATION_SPEC])
    b = invariants.Board(pcb.generate_pcb(spec))
    assert getattr(b, collection), (
        f"the calibration board has no {collection} to remove, so this case "
        "cannot demonstrate anything — pick a spec that produces some")

    if collection in ("fills", "emitted_fills"):
        setattr(b, collection, lambda *a, **k: [])
    else:
        setattr(b, collection, {} if collection == "nets" else [])

    fired = [fn.__name__ for fn in invariants.ALL_CHECKS
             if _fails(fn, b, spec)]
    assert fired, (
        f"a board with no {collection} at all passes every one of the "
        f"{len(invariants.ALL_CHECKS)} invariants — the battery is blind to "
        "the entire collection being absent, which is the catastrophic version "
        "of the defect each of those checks describes")


def _fails(fn, b, spec) -> bool:
    try:
        fn(b, spec)
    except AssertionError:
        return True
    return False


# ===========================================================================
# The file writes four decimals. Comparisons against it must say so.
# ===========================================================================

#: A far-side unit whose pad lands on a coordinate ``pcb._n`` cannot write
#: exactly. Every structured corpus in this repo places units on values like
#: 3.925 / 4.075, which *are* exact at four decimals — which is why a
#: 445-board randomised sweep was the first thing to fire.
_FIFTH_DECIMAL_FAR_UNIT = pcb.Led(7.8959963, 11.4417900, "red", rot=270,
                                  layout="inline", size="1206", farled=True)


def _far_pad_wanted(spec):
    """Where ``assert_far_side_leds_have_a_via_in_each_pad`` expects a via."""
    led = spec.leds[0]
    g = pcb.led_geometry(led)
    cx, cy = pcb.clamp_led_obj(led, pcb.unit_safe(spec))
    rx, ry = pcb._r(g["led_k"][0], g["led_k"][1], led.rot)
    return cx + rx, cy + ry


def test_a_far_side_via_is_found_when_its_pad_needs_a_fifth_decimal():
    """A via that *is* in the pad is not reported missing because of rounding.

    The regression this pins: the check compared the emitted via against the
    intended position with ``_EPS`` (1e-6) while ``pcb._n`` writes coordinates
    at four decimals. Measured over a 445-board sweep, **52 far-side pads were
    reported as having no via when every one of them did** — worst offset
    4.914e-5 mm, i.e. inside the file's own quantum. 22 boards' worth of false
    accusation sitting in the safety library.

    The two assertions are a pair. The first is the property; the second stops
    this test rotting into a green no-op if someone "tidies" the coordinate to
    something four decimals can hold, at which point it would pass under the
    old tolerance too and prove nothing.
    """
    spec = invariants.resolved_spec(_spec(leds=[_FIFTH_DECIMAL_FAR_UNIT], art=[]))
    b = invariants.check_board(spec)

    want = _far_pad_wanted(spec)
    off = min(max(abs(v.x - want[0]), abs(v.y - want[1])) for v in b.vias)
    assert 0.0 < off < invariants.EMITTED_ROUNDING_MM, (
        f"the nearest emitted via is {off} mm from the intended pad centre. "
        "This case only means something while that gap is non-zero (the file "
        "genuinely cannot write the coordinate) and under one rounding "
        "quantum (the via genuinely is in the pad) — move the LED back onto a "
        "coordinate needing a fifth decimal")


def test_a_far_side_via_displaced_past_the_file_precision_is_still_caught():
    """Widening the tolerance to the file's precision did not switch the check off.

    The red-proof for the fix above. 3e-4 mm is three rounding quanta — far
    below anything a human would notice and far above what the emitter can
    excuse — and the check must still call it a missing via. A real defect
    moves a via by a pad pitch (~1 mm), four orders of magnitude past this.
    """
    spec = invariants.resolved_spec(_spec(leds=[_FIFTH_DECIMAL_FAR_UNIT], art=[]))
    good = pcb.generate_pcb(spec)
    want = _far_pad_wanted(spec)

    # File coordinates carry pcb.ORIGIN; Board subtracts it back off again.
    fx, fy = pcb.ORIGIN + want[0], pcb.ORIGIN + want[1]
    marker = f"(via (at {pcb._n(fx)} {pcb._n(fy)})"
    assert good.count(marker) == 1, (
        f"expected exactly one via emitted at {marker!r}; the emitter's via "
        "syntax has moved and this mutation would silently do nothing")
    bad = good.replace(marker, f"(via (at {pcb._n(fx + 3e-4)} {pcb._n(fy)})")
    assert bad != good, "the mutation changed nothing, so it proves nothing"

    assert _fails(invariants.assert_far_side_leds_have_a_via_in_each_pad,
                  invariants.Board(bad), spec), (
        "a via 3e-4 mm out of its pad — three times the file's own precision — "
        "was accepted. The rounding tolerance has been widened into a hole")


# ===========================================================================
# Candidate defects #17 and #18: a unit's rail stranded on a live copper
# island. Both confirmed with real kicad-cli DRC below. Do not fix pcb.py.
# ===========================================================================

#: One 0805 on the back plus a glow window nowhere near it. ``_fill_geometry``
#: cuts a 0.12 mm slit from each interior void straight out to the y-max board
#: edge (pcb.py:1864-1874, "KiCad stores fills as simple outlines"). Two of
#: those slits land either side of the unit and cross the perimeter ring the
#: comment at pcb.py:1834 says keeps the plane continuous, fencing the unit's
#: GND copper onto a 23.9 mm^2 island. Measured gap to the main pour: exactly
#: 0.1200 mm, the slit width, at the board edge.
_SLIT_STRANDS_THE_RAIL = pcb.BadgeSpec(
    leds=[pcb.Led(9.6, 15.0, "red", side="back", size="0805")], texts=[],
    art=[pcb.ArtLayer("glow", [(4.0, 4.0, 5.0, 3.0)], [], window="through")],
    pins=pcb.ALL_PINS)

#: Two via-less units route their 3V3 across B.Cu; their two runs jointly
#: enclose a third unit's GND via on a 21.3 mm^2 island (gap 0.50 mm, no slit
#: involved). ``pcb.resolve_novia`` is the only rail-reachability gate in the
#: product and it asks, per via-less unit, whether *its own* contact and the
#: connector pads share a fill polygon (pcb.py:1047) — never whether the
#: channel it just cut stranded somebody else. Unit 2 here has a via, so it is
#: never examined at all, and ``/generate`` returns 200.
_NOVIA_RUNS_STRAND_A_NEIGHBOUR = pcb.BadgeSpec(
    leds=[pcb.Led(14.0, 14.0, "red", side="back", layout="inline", size="3mm",
                  novia=True),
          pcb.Led(8.0, 7.0, "red", side="back", rot=270, layout="inline",
                  size="0603", novia=True),
          pcb.Led(11.0, 10.0, "red", side="front", rot=270, layout="inline",
                  size="3mm")],
    texts=[], art=[], pins=("2", "7"))

#: (id, spec, xfail reason, extra marks). The #18 board pays 1.65 s in
#: ``novia_route``'s search — two via-less units against a two-pin connector is
#: the expensive corner — so it is ``slow`` and the fast tier keeps only #17.
#: Measured: leaving it in took `fast` from 7.6 s to 9.2 s against a 10 s budget.
_STRANDED = [
    ("17-slit-crosses-the-perimeter-ring", _SLIT_STRANDS_THE_RAIL,
     "defect #17 (candidate): _fill_geometry's hole-to-edge slit "
     "(pcb.py:1864-1874) cuts through the pour's perimeter ring and strands a "
     "live island, leaving the unit on it wired to nothing", ()),
    ("18-novia-runs-fence-a-neighbour", _NOVIA_RUNS_STRAND_A_NEIGHBOUR,
     "defect #18 (candidate): resolve_novia (pcb.py:1047) only asks whether a "
     "via-less unit's own contact still reaches a pad, so two via-less runs "
     "may fence a third unit's via onto an island and /generate still ships it",
     (pytest.mark.slow,)),
]


@pytest.mark.parametrize("spec,reason", [
    pytest.param(s, r, marks=(pytest.mark.xfail(strict=True, reason=r), *extra))
    for _i, s, r, extra in _STRANDED], ids=[i for i, *_ in _STRANDED])
def test_every_units_rail_contact_reaches_a_connector_pad(spec, reason):
    """No LED ships with its power fenced onto an isolated copper island.

    These two specs are the minimised survivors of a 445-board randomised
    sweep, in which 8 boards failed this way. 7 of the 8 pass every gate
    ``/generate`` applies — ``power_missing`` and ``resolve_novia`` both say
    yes — so the user downloads them, and the LED does not light.

    ``xfail(strict=True)``: fix either mechanism in ``pcb.py`` and the matching
    row goes red here, which is the signal to promote it out of the ledger.
    """
    invariants.check_board(invariants.resolved_spec(spec),
                           checks=[invariants.assert_every_unit_reaches_its_rails])


@pytest.mark.kicad
@pytest.mark.parametrize("spec,reason", [
    pytest.param(s, r, marks=pytest.mark.xfail(strict=True, reason=r))
    for _i, s, r, _extra in _STRANDED], ids=[i for i, *_ in _STRANDED])
def test_a_stranded_rail_island_is_confirmed_by_real_drc(spec, reason, kicad_cli,
                                                         tmp_path):
    """The external oracle agrees these two boards are broken (decision D13).

    This is what makes #17 and #18 product defects rather than a check with a
    tuning problem, and it is why the row above is an ``xfail`` and not a
    softened assertion. Each board produces **exactly one** kicad-cli
    violation, ``unconnected_items``, on the same net and layer the in-process
    check names — no shorts, no clearance, nothing else to confound it.
    """
    invariants.assert_drc_clean(spec, kicad_cli, tmp_path=tmp_path)


# Two through-hole units sharing a row leave a sliver of GND copper between
# their pad keepouts. Reproduced on all three THT packages at (6.5, 6.0) +
# (14.0, 13.5) and again at (6, 10) + (14, 10); a *single* THT unit at either
# coordinate is clean, the same pair on opposite faces is clean, one THT beside
# an 0805 is clean, and the pair far apart at (5,5) + (15,15) is clean. So it is
# the geometry of two THT keepouts in one row, not this file's coordinates.
# Severity is `warning`: a fab will flag it, and a sliver can lift and short.
# Filed rather than tuned away — moving the corpus off the coordinates would
# hide a real defect behind a green test.
_THT_ROW_SLIVER = {"size-1.8mm", "size-3mm", "size-5x2mm"}


@pytest.mark.kicad
@pytest.mark.parametrize("spec,case", [
    pytest.param(s, i, marks=pytest.mark.xfail(strict=True, reason=(
        "defect #15 (candidate): two through-hole units in one row leave a "
        "copper sliver in the B.Cu GND pour between their pad keepouts")))
    if i in _THT_ROW_SLIVER else pytest.param(s, i)
    for i, s in CORPUS],
    ids=[i for i, _ in CORPUS])
def test_every_board_in_the_corpus_passes_real_drc(spec, case, kicad_cli,
                                                   tmp_path):
    """KiCad's own DRC finds nothing wrong with any board in the corpus.

    The external oracle. It sees what the in-process battery cannot — courtyard
    overlap, silk over copper, mask bridges, slivers, pad-to-pad shorts — while
    the battery sees via layer spans, stackup and track-vs-pad net agreement
    that DRC cannot. Both run; neither is optional (decision D13).

    **The art layers are stripped first, and that is not a dodge.** This corpus
    varies packages, rotations, faces, pin subsets and stackup; its art
    rectangles are fixed placeholders chosen to exercise the *material* axis, and
    they sit wherever they happen to land. Asserting DRC-clean on them measured
    my own arbitrary coordinates, not the generator: it produced 12 failures,
    every one a `warning`-severity `silk_overlap` / `silk_over_copper` /
    `copper_sliver` from a placeholder rectangle overlapping an LED's
    silkscreen. That is the D12 trap in a second costume — the first is
    hand-placed LEDs, this is hand-placed art. `tests/test_kicad_integration.py`
    owns DRC over deliberately laid-out artwork; this test owns DRC over the
    placement and routing axes, which is what actually varies here.
    """
    from dataclasses import replace
    invariants.assert_drc_clean(replace(spec, art=[]), kicad_cli,
                                tmp_path=tmp_path)
