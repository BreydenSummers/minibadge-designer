"""Runs the whole in-process invariant battery over a parameter-varied corpus.

Why this file exists
--------------------
``tests/invariants.py`` ships 44 checks, each with a measured true- and
false-positive rate over a 259-board sweep. Until this file landed, the suite
called exactly **two** of them. The battery was validated in a throwaway harness
and then never wired to anything that runs, so a defect it was built to catch
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
that ``size="0805"`` (the value every pre-existing example test used) is where
a board-shorting defect hid on lines that already had 100% line coverage.
"""

import re

import pytest

import invariants
from minibadge_designer import pcb

_ART = [(4.0, 4.0, 5.0, 0.2), (4.0, 4.4, 4.0, 0.2)]


def _ring(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


#: A window shape with COUNTERS: the axis every art row here missed. Each of
#: the 30-odd rows above draws its window as plain rectangles, so a window
#: whose outline encloses a void -- which is every glyph with a counter (8, 4,
#: 0, 6, A) and every ring of artwork -- was never generated at all, and the
#: keepout emitter's handling of them went unexercised for its whole life.
#:
#: The two voids sit at DIFFERENT x on purpose. Any scheme for breaking a
#: holed shape up divides it per void, so it takes voids offset from one
#: another before the material between and below them comes away as separate
#: pieces -- and a scheme that then loses a piece is what shipped copper into
#: a light window. Aligned voids (the obvious way to draw an 8) come apart as
#: one notch and never trigger it: measured on the defect, which is why this
#: row is drawn the awkward way round rather than as a tidy 8.
_COUNTERS = [[_ring(6.0, 6.0, 14.0, 15.0),
              _ring(7.6, 7.6, 12.4, 10.0),
              _ring(7.0, 11.0, 11.0, 13.4)]]


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
    # A window whose shape has holes, cut on both faces and on one; the second
    # row keeps the default units so the window is also carved around real
    # copper, which is how the pieces get their awkward shapes.
    *[(f"window-counters-{m}", _spec(leds=[], art=[pcb.ArtLayer(m, [], _COUNTERS)]))
      for m in ("glow", "bare")],
    ("window-counters-carved", _spec(art=[pcb.ArtLayer("bare", [], _COUNTERS)])),
    ("one-face-window", _spec(art=[pcb.ArtLayer("bare", [(5.0, 6.0, 11.0, 8.0)],
                                                [], side="back", window="back")])),
    # --- connector pin subsets: a whole row, a corner, a single pad ---------
    ("pins-top-row", _spec(pins=("1", "2", "7", "8"))),
    ("pins-one-corner", _spec(pins=("1", "2"))),
    ("pins-power-only", _spec(pins=("2", "7"))),
    # --- stackup ------------------------------------------------------------
    ("mask-purple-hasl", _spec(mask_color="purple", finish="hasl")),
    # Part labels off: the other half of the refdes switch, which every row
    # above leaves at its default. Without it the "no ink when off" branch of
    # assert_printed_references_sit_on_printable_board never runs.
    ("no-part-labels", _spec(refdes=False)),
    # --- nothing at all: the degenerate board still has to be legal ---------
    ("bare-board", _spec(leds=[], art=[])),
    # --- axes this corpus originally missed entirely ------------------------
    # Line-tracing the battery showed 0 of 26 rows set farled, novia, texts or
    # a custom outline, so several checks were *called* on every row and
    # evaluated no assertion on any of them: reachable and inert. Adding a row
    # per axis is the cheap fix; the gate below now measures execution rather
    # than trusting that a call means a check ran.
    ("far-side-led", _spec(leds=[pcb.Led(10.0, 10.0, "red", farled=True)])),
    ("via-less-unit", _spec(leds=[pcb.Led(10.0, 6.0, "red", novia=True)])),
    # Chosen trace terminals: one run to a hand-picked far pad, one chained
    # onto a sibling's cathode pad so the two share a single path to GND.
    ("via-less-chained", _spec(leds=[
        pcb.Led(6.0, 6.0, "red", novia=True, term=("unit", 1)),
        pcb.Led(13.5, 12.5, "blue", novia=True, term=("pad", "16"))])),
    ("text-front-and-back", _spec(
        leds=[pcb.Led(6.0, 6.0, "red")], art=[],
        texts=[pcb.Text(10.0, 15.0, "MINIBADGE", size=1.5),
               pcb.Text(10.0, 5.0, "back", size=1.2, side="back",
                        material="copper")])),
    # The bottom of the size range the UI offers (webapp.TEXT_SIZE_MM), which
    # nothing else here reaches: every other row's text is >= 1.2 mm, where the
    # proportional stroke width is comfortably over the board rules' pen floor.
    # 0.66 mm is the break-even -- 0.15 * 0.66 rounds to 0.099 -- so it is the
    # size a pen that follows the height alone gets wrong first.
    ("text-at-the-smallest-size-offered", _spec(
        leds=[pcb.Led(6.0, 6.0, "red")], art=[],
        texts=[pcb.Text(10.16, 15.5, "floor", size=0.6),
               pcb.Text(10.16, 16.5, "break even", size=0.66, side="back")])),
    ("custom-outline", _spec(leds=[pcb.Led(10.0, 10.0, "red")], art=[],
                             outline=_octagon_outline())),
    # --- CLK drive: units running off the badge's blink clock ---------------
    # Off-default on purpose: a 0603 at 90 deg and an inline back unit, so the
    # supply reroute is exercised somewhere other than the stacked-0805-rot-0
    # path every example test walks.
    ("clk-jumper", _spec(leds=[pcb.Led(6.5, 6.0, "red", size="0603", rot=90,
                                       clk=True),
                               pcb.Led(14.0, 13.0, "blue", side="back",
                                       layout="inline", clk=True)])),
    ("clk-trace", _spec(clk_jumper=False,
                        leds=[pcb.Led(6.5, 6.0, "red", rot=45, clk=True),
                              pcb.Led(14.0, 13.0, "blue", side="back",
                                      clk=True)])),
    # The jumper dragged somewhere else and stood on end; its link to pin 9
    # has to route across half the board.
    ("clk-jumper-moved", _spec(jumper=(5.0, 10.0), jumper_rot=90,
                               leds=[pcb.Led(13.0, 6.0, "red", clk=True),
                                     pcb.Led(13.5, 13.5, "blue", side="back",
                                             size="1206", clk=True)])),
    # Back-face-only blinkers (the classic glow badge): the rail via is the
    # ONLY thing carrying the jumper's centre pad to their layer, so this row
    # is the one that notices it keyed to the wrong side.
    ("clk-back-only", _spec(leds=[pcb.Led(7.0, 10.0, "red", side="back",
                                          clk=True),
                                  pcb.Led(14.0, 13.0, "blue", side="back",
                                          rot=180, clk=True)])),
    # The jumper mounted on the BACK: its pads live in the GND pour's layer,
    # its 3V3 pad reaches the front pour through its own via, and the FRONT
    # blinker now needs the rail via instead of the back one.
    ("clk-jumper-back", _spec(jumper_side="back",
                              leds=[pcb.Led(6.5, 6.0, "red", clk=True),
                                    pcb.Led(14.0, 13.0, "blue", side="back",
                                            clk=True)])),
    # The back jumper's steady pad fed by a same-face TRACE instead of its
    # via (jumper_via off): the 3V3 run lands on a pin's plated hole, and
    # the front blinker still crosses through the rail via.
    ("clk-jumper-back-traced", _spec(jumper_side="back", jumper_via=False,
                                     leds=[pcb.Led(6.5, 6.0, "red", clk=True),
                                           pcb.Led(14.0, 13.0, "blue",
                                                   side="back", clk=True)])),
    # The 3V3 trace sent to a hand-picked pin (15) while pin 7 is nearer
    # to the mid-board jumper: the choice must win over the automatic
    # nearest. (Placement matters: a long chosen-pin channel across the
    # back pour can fence a kept GND pad, and the app correctly refuses
    # such boards; this layout ships clean.)
    ("clk-v3pin-chosen", _spec(jumper_side="back", jumper_via=False,
                               jumper_v3pin="15", jumper=(9.0, 10.0),
                               leds=[pcb.Led(6.5, 6.0, "blue", side="back",
                                             clk=True)])),
    # Hand-placed bends on both of the jumper's routed links.
    ("clk-link-bends", _spec(jumper_side="back", jumper_via=False,
                             jumper_nodes=((4.0, 14.0),),
                             jumper_v3nodes=((14.0, 16.0),),
                             leds=[pcb.Led(7.0, 8.0, "red", side="back",
                                           clk=True)])),
    # A unit that is BOTH via-less and blinking: two routed runs, no via.
    ("clk-with-novia", _spec(leds=[pcb.Led(6.0, 6.0, "red", clk=True,
                                           novia=True),
                                   pcb.Led(14.0, 13.0, "blue")])),
    # Pin 9 dropped: the flag must be inert, the board indistinguishable from
    # a non-CLK one (the invariants assert no jumper, no CLK nets, pin 9 row
    # absent). The webapp refuses this with a 400; the generator must still
    # not ship a supply pad wired to a pin that is not there.
    ("clk-pin9-dropped-inert", _spec(pins=("1", "2", "7", "8", "10", "15", "16"),
                                     leds=[pcb.Led(6.5, 6.0, "red", clk=True)])),
]


@pytest.mark.parametrize("spec", [s for _, s in CORPUS],
                         ids=[i for i, _ in CORPUS])
def test_every_board_in_the_corpus_satisfies_every_invariant(spec):
    """Whatever the user builds, the board obeys every rule we can check cheaply.

    A failure here means a real badge would be wrong in the way the raised
    message names: a severed power plane, a via that does not cross the board,
    a track carrying a net its pads do not, copper over the edge. The message is
    the diagnosis; read it rather than this docstring.

    The spec goes through ``resolved_spec`` first because ``generate_pcb`` is not
    responsible for DRC cleanliness; the webapp's placement backstop is
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
    ``pcb.py:2027`` produces 46 real kicad-cli DRC violations: clearance
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
    ``pcb._fill_geometry`` (the function the generator itself calls), so it
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
        "proves nothing; the filled_polygon syntax must have moved")
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
    # while this test stayed green. These three describe copper (where it
    # spills, what it shorts, what it floods) and each must see it itself.
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
    library is a ``for`` loop over one of these, and a loop over ``[]`` passes,
    so a generator regression that emitted no tracks (every LED unconnected),
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
        "cannot demonstrate anything; pick a spec that produces some")

    if collection in ("fills", "emitted_fills"):
        setattr(b, collection, lambda *a, **k: [])
    else:
        setattr(b, collection, {} if collection == "nets" else [])

    fired = [fn.__name__ for fn in invariants.ALL_CHECKS
             if _fails(fn, b, spec)]
    assert fired, (
        f"a board with no {collection} at all passes every one of the "
        f"{len(invariants.ALL_CHECKS)} invariants; the battery is blind to "
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
#: 3.925 / 4.075, which *are* exact at four decimals, which is why a
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
    reported as having no via when every one of them did**; worst offset
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
        "quantum (the via genuinely is in the pad); move the LED back onto a "
        "coordinate needing a fifth decimal")


def test_a_far_side_via_displaced_past_the_file_precision_is_still_caught():
    """Widening the tolerance to the file's precision did not switch the check off.

    The red-proof for the fix above. 3e-4 mm is three rounding quanta (far
    below anything a human would notice and far above what the emitter can
    excuse), and the check must still call it a missing via. A real defect
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
        "a via 3e-4 mm out of its pad (three times the file's own precision) "
        "was accepted. The rounding tolerance has been widened into a hole")


# ===========================================================================
# Regression boards for defects #17 and #18: a unit's rail stranded on a live
# copper island. Both were confirmed with real kicad-cli DRC; both are fixed,
# and these two boards are the minimised survivors of the 445-board sweep that
# found them, kept so a relapse is caught by the mechanism that caught it once.
# ===========================================================================

#: One 0805 on the back plus a glow window nowhere near it. ``_fill_geometry``
#: used to vent every interior void with a 0.12 mm slit running straight out to
#: the y-max board edge. Two of those slits landed either side of the unit and
#: cut the perimeter ring that is supposed to keep the plane continuous,
#: fencing the unit's GND copper onto a 23.9 mm^2 island; the measured gap to
#: the main pour was exactly 0.1200 mm, the slit width, at the board edge.
#: ``_open_holes`` now vents one hole at a time, shortest slit first, and only
#: down a direction that leaves the island whole.
_SLIT_STRANDS_THE_RAIL = pcb.BadgeSpec(
    leds=[pcb.Led(9.6, 15.0, "red", side="back", size="0805")], texts=[],
    art=[pcb.ArtLayer("glow", [(4.0, 4.0, 5.0, 3.0)], [], window="through")],
    pins=pcb.ALL_PINS)

#: Three via-less units against a two-pin connector. Their runs cut channels
#: across both pours, and unit 1's rail ends up on an island of its own.
#: ``pcb.resolve_novia`` is the only rail-reachability gate in the product, and
#: it used to ask each via-less unit only whether *its own* contact and the
#: connector pads share a fill polygon, never whether the copper it can reach
#: actually gets to a pad, and never about a unit the channel merely passed by.
#:
#: Measured through the real endpoint, before and after the fix:
#:
#: ===================  ==========================================
#: ``POST /generate``   result
#: ===================  ==========================================
#: before               **200**, and the downloaded ``.kicad_pcb``
#:                      answers kicad-cli with exactly one
#:                      ``[unconnected_items]``
#: after                **400**, "LED 1 cannot reach its power
#:                      without a via on this board"
#: ===================  ==========================================
#:
#: **The geometry is still stranded, and that is the design.** ``novia_route``
#: is explicit that hunting for a further connector pad salvages under 1% of
#: placements and would make the browser preview lie, so the product's answer
#: to an unreachable via-less placement is to refuse the download and say so.
#: The fix had to be in the gate, and the guarantee this board pins is the
#: **400**, not a clean board.
#:
#: The coordinates are a fixed point of the placement backstop (asserted below),
#: so nothing here rests on this file's arithmetic: the D12 trap. The
#: placement is also free of unit-vs-unit overlap and of pad conflicts, which
#: is what leaves the via-less channel as the only available explanation.
_NOVIA_RUNS_STRAND_A_NEIGHBOUR = pcb.BadgeSpec(
    leds=[pcb.Led(9.29, 3.32, "red", side="front", rot=270, layout="stacked",
                  size="1.8mm", novia=True),
          pcb.Led(13.9775, 6.7935, "red", side="front", rot=180,
                  layout="stacked", size="1206", novia=True),
          pcb.Led(16.1, 14.5706, "red", side="back", rot=0, layout="inline",
                  size="5x2mm", novia=True)],
    texts=[], art=[], pins=("2", "15"))

#: (id, spec). Only the #17 board belongs here: it is the one whose *geometry*
#: was wrong, so a clean board is the right expectation for it. The #18 board
#: is asserted through the gate instead, two tests below.
_STRANDED = [
    ("17-slit-crosses-the-perimeter-ring", _SLIT_STRANDS_THE_RAIL),
]


@pytest.mark.parametrize("spec", [s for _, s in _STRANDED],
                         ids=[i for i, _ in _STRANDED])
def test_every_units_rail_contact_reaches_a_connector_pad(spec):
    """No LED ships with its power fenced onto an isolated copper island.

    This spec is a minimised survivor of a 445-board randomised sweep in which
    8 boards failed this way. It passed every gate ``/generate`` applies
    (``power_missing`` and ``resolve_novia`` both said yes), so the user
    downloaded it and the LED did not light.

    Its mechanism was the pour's own hole-venting slit, which ran out to the
    board edge through the perimeter ring; the connectivity is recomputed here
    from the **emitted** fill polygons, so it measures the copper that ships.
    """
    invariants.check_board(invariants.resolved_spec(spec),
                           checks=[invariants.assert_every_unit_reaches_its_rails])


@pytest.mark.kicad
@pytest.mark.parametrize("spec", [s for _, s in _STRANDED],
                         ids=[i for i, _ in _STRANDED])
def test_a_stranded_rail_island_is_confirmed_by_real_drc(spec, kicad_cli,
                                                         tmp_path):
    """The external oracle agrees, which is what made this a product defect.

    Broken, this board produced **exactly one** kicad-cli violation,
    ``unconnected_items``, on the same net and layer the in-process check names:
    no shorts, no clearance, nothing else to confound it. Both oracles run;
    neither is optional (decision D13).
    """
    invariants.assert_drc_clean(spec, kicad_cli, tmp_path=tmp_path)


@pytest.mark.slow
def test_a_unit_whose_via_less_run_strands_its_rail_is_refused_not_shipped():
    """A via-less unit whose rail lands on an island is refused, by index.

    The value at stake is the whole download: this board used to come back 200
    with a zip whose first LED was wired to copper that reaches no connector
    pad. Nobody finds that until the badge is soldered, and kicad-cli on the
    downloaded file says so in one line, ``[unconnected_items]``.

    The assertion names the **index**, not merely "some problem". The old gate
    walked the same units; what it never asked was whether the copper the
    contact sits on gets to a pad at all. A test happy with any non-empty list
    would pass on a gate that blamed the wrong LED, and the message the user
    reads names the LED by number.

    ``slow``: this board pays ~1 s in ``novia_route``'s visibility search
    (three via-less units against a two-pin connector is the expensive corner)
    and the fast tier has a 10 s budget.
    """
    spec = invariants.resolved_spec(_NOVIA_RUNS_STRAND_A_NEIGHBOUR)
    # The board is a fixed point of the placement backstop, so a failure here
    # is the generator's and not this file's coordinates (decision D12).
    moved = [i for i, (got, want) in enumerate(
        zip(spec.leds, _NOVIA_RUNS_STRAND_A_NEIGHBOUR.leds))
        if abs(got.x - want.x) > 1e-6 or abs(got.y - want.y) > 1e-6]
    assert not moved, (
        f"the backstop moved unit(s) {moved}, so the board under test is no "
        "longer the one the docstring measured")
    _leds, problems = pcb.resolve_novia(spec, pcb.unit_safe(spec))
    assert problems == [0], (
        "the unit whose via-less run strands its own rail is LED 1 (index 0); "
        f"the gate reported {problems}. An empty list means /generate hands "
        "the user a 200 and a zip whose LED is wired to nothing")


#: Separation the backstop owes any two units, stated here rather than read
#: from ``resolve_overlap``'s `gap` default: a check sourcing its expectation
#: from the constant the code reads cannot detect an edit to that constant.
UNIT_SEPARATION_MM = 0.2

#: Pairs ``resolve_overlap`` used to give up on. Its four candidate slides each
#: clear the other unit's whole axis-aligned ENVELOPE, which two big tilted
#: packages cannot afford inside ``unit_safe``, so it returned the unit exactly
#: where it was and shipped a pad-to-pad short. Measured over 600 random
#: two-unit boards: 35 shorted, and all 35 were give-ups rather than bad slides.
#:
#: kicad-cli on the first pair, before the fix: two ``[clearance]`` violations
#: (0.1036 mm and 0.1012 mm against a 0.2 mm netclass rule) plus two
#: ``[courtyards_overlap]``. Both classes are gone now.
_GAVE_UP_PAIRS = [
    ("1206-stacked-vs-inline-tilted", [
        pcb.Led(4.914393463976127, 9.069013567590666, "red", rot=180,
                layout="stacked", size="1206"),
        pcb.Led(1.384138021989186, 8.262767472239016, "blue", rot=22.5,
                layout="inline", size="1206")]),
    ("bar-package-vs-dome-mid-board", [
        pcb.Led(9.5, 9.5, "red", rot=45, layout="inline", size="5x2mm"),
        pcb.Led(10.0, 10.0, "blue", rot=90, layout="stacked", size="3mm",
                side="back")]),
]


@pytest.mark.parametrize("leds", [ls for _, ls in _GAVE_UP_PAIRS],
                         ids=[i for i, _ in _GAVE_UP_PAIRS])
def test_the_backstop_separates_two_units_it_used_to_give_up_on(leds):
    """Two units never ship touching, even when neither can slide off the other.

    If this breaks the user downloads a board whose D1 pad sits on R2's, which
    KiCad calls a clearance violation and a fab either rejects or builds as a
    dead short. Both faces are covered because a unit's via penetrates the
    whole stack, so front-vs-back is a real conflict too.

    Deliberately off every default: 1206 and 5x2mm rather than 0805, tilted
    22.5 and 45 degrees rather than square, inline as well as stacked, and one
    pair on opposite faces. The separation is measured on the TIGHT rotated
    footprints (``unit_poly``), which is what the resolver promises, not on
    the axis-aligned envelope, which would pass a pair that genuinely touches.
    """
    spec = invariants.resolved_spec(
        pcb.BadgeSpec(name="pair", leds=leds, texts=[], art=[],
                      pins=pcb.ALL_PINS))
    safe = pcb.unit_safe(spec)
    polys = [pcb.unit_poly(led, safe) for led in spec.leds]
    gap = polys[0].distance(polys[1])
    assert gap >= UNIT_SEPARATION_MM - 1e-9, (
        f"the two units end up {gap:.4f} mm apart (rule "
        f"{UNIT_SEPARATION_MM} mm); the backstop ran out of slide candidates "
        "and shipped the pair overlapping")


# The three through-hole rows used to leave a copper sliver between the two
# units' pad keepouts, on every THT package and at two independent coordinate
# pairs, while a single THT unit, the same pair on opposite faces, and one THT
# beside an 0805 were all clean, which is what showed it was the geometry and
# not this file's coordinates. It came from the pour's hole-venting slit
# grazing a neighbouring void and leaving a hair of copper beside it; the vent
# now shaves anything under SLIVER_W off the piece it leaves behind. Severity
# was only `warning`, but a fab flags it and a sliver that lifts can bridge.


@pytest.mark.kicad
@pytest.mark.parametrize("spec", [s for _, s in CORPUS],
                         ids=[i for i, _ in CORPUS])
def test_every_board_in_the_corpus_passes_real_drc(spec, kicad_cli, tmp_path):
    """KiCad's own DRC finds nothing wrong with any board in the corpus.

    The external oracle. It sees what the in-process battery cannot (courtyard
    overlap, silk over copper, mask bridges, slivers, pad-to-pad shorts), while
    the battery sees via layer spans, stackup and track-vs-pad net agreement
    that DRC cannot. Both run; neither is optional (decision D13).

    **The art layers are stripped first, and that is not a dodge.** This corpus
    varies packages, rotations, faces, pin subsets and stackup; its art
    rectangles are fixed placeholders chosen to exercise the *material* axis, and
    they sit wherever they happen to land. Asserting DRC-clean on them measured
    my own arbitrary coordinates, not the generator: it produced 12 failures,
    every one a `warning`-severity `silk_overlap` / `silk_over_copper` /
    `copper_sliver` from a placeholder rectangle overlapping an LED's
    silkscreen. That is the D12 trap in a second costume: the first is
    hand-placed LEDs, this is hand-placed art. `tests/test_kicad_integration.py`
    owns DRC over deliberately laid-out artwork; this test owns DRC over the
    placement and routing axes, which is what actually varies here.
    """
    from dataclasses import replace
    invariants.assert_drc_clean(replace(spec, art=[]), kicad_cli,
                                tmp_path=tmp_path)
