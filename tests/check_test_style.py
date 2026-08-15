#!/usr/bin/env python3
"""Mechanical test-style checks for minibadge-designer.

Two severities, and the split is the whole design:

  ERROR    a shape that is *provably* broken by reading the AST alone. Fails
           the build (``pytest -m meta``). There are only three, and each one
           flags **zero** legitimate tests in the existing suite.
  ADVICE   a shape that is usually worth fixing but has a real false-positive
           rate. Printed, counted, never fails anything.

Round 1 measured what happens when a style gate is loose: a check that flags
forty good tests is a check somebody deletes on day one. So the ERROR set is
deliberately tiny and the ADVICE set is deliberately toothless. If you disagree
with an ERROR on one specific line, silence it in place::

    for x, y, w, h in rects:   # style-ok: E-VACUOUS-LOOP rects is a hard-coded 4x4 grid of literals
        ...

The reason after the code is mandatory, and it has to be a sentence: 30
characters and five words, checked, because "somebody typed a magic comment" is
not evidence. Round 3 measured the old rule -- one character passed, while the
``KNOWN_DEBT`` ledger demanded forty and an evidence word for the identical
finding, so the *unjustified* escape was the cheap one.

Where this file lives, and why (decision D15)
---------------------------------------------
It used to live under ``.claude/skills/writing-tests/scripts/``. ``.claude/`` is
gitignored (``.gitignore:56``, zero files tracked under it), so an agent that
did not like a finding could loosen the gate judging it and **nothing appeared
in ``git status`` or in any diff**. That is not hypothetical: this checker once
reported five hard ``E-VACUOUS-LOOP`` errors in ``tests/test_properties.py`` and
exited 1, and was then edited mid-session to downgrade vacuity to ADVICE under
``@given``, invisibly. The gate now lives in tracked space next to the tests it
judges. Every loosening is a reviewable diff.

Consequences, which are load-bearing:

* Severities are fixed per check. There is **no** mechanism that rewrites a
  finding's severity after the fact -- the only per-site escape is a
  ``# style-ok: CODE <reason>`` comment, which is itself a visible diff on the
  line it excuses.
* Every ``KNOWN_DEBT`` entry carries a ``why`` naming the measurement that
  justifies it (``_audit_known_debt`` fails the run if one is missing or is
  boilerplate). "It was flagging my new file" is not a measurement.

Usage
-----
    python tests/check_test_style.py                     # tests/
    python tests/check_test_style.py tests/test_pcb.py   # one file
    python tests/check_test_style.py --advice            # show ADVICE too
    python tests/check_test_style.py --strict            # ADVICE fails too
    python tests/check_test_style.py --json              # machine-readable

Exit 0 = no ERROR findings.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Markers pytest itself defines; never "unregistered".
BUILTIN_MARKERS = {
    "parametrize", "skip", "skipif", "xfail", "usefixtures",
    "filterwarnings", "tryfirst", "trylast", "no_cover",
}

# Fixture -> tier marker that should accompany it. Grounded in D7: a test whose
# only gate is a silent skipif is indistinguishable from a passing one.
TIER_FIXTURES = {
    "client": "webapp",
    "flask_app": "webapp",
    "kicad_cli": "kicad",
    "board_dir": "kicad",
    "page": "browser",
    "browser": "browser",
}

# Iterables that cannot plausibly be empty because they are spelled out.
# Membership is not enough -- the elements have to actually be there. `out = []`
# is a literal list and is empty by construction; treating it as bounded let an
# accumulator launder every loop that read it back.
_LITERAL_NODES = (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Constant, ast.JoinedStr)
_SAFE_CALLS = {"range", "reversed", "sorted", "zip", "enumerate", "product"}


# ---------------------------------------------------------------------------
# Known debt
# ---------------------------------------------------------------------------
# Vacuity holes that already existed when this check was written, keyed by
# (file, test, code). They are *not* silenced -- `--all` prints them and
# `pytest -m meta` reports the total -- they just do not fail the build, because
# a gate that is red on the day it ships is a gate somebody deletes.
#
# The count is load-bearing: adding a sixth vacuous assertion to a test that
# already has five still fails. Shrinking an entry is fine; the check tells you
# to update it, which is how the list gets smaller instead of quietly staying.
#
# `why` is mandatory and `_audit_known_debt()` fails the run without it (D15).
# It must name the *measurement* that justifies leaving the hole open, not an
# opinion. "It was flagging my new file" is not a measurement.
#
# The measurement behind nine of the thirteen entries below was run for this
# ledger and is reproducible in one line -- a pytest plugin that does
# `pcb._fill_geometry = lambda *a, **k: []`, i.e. deletes every copper pour on
# every board, then runs the eight tests that iterate it:
#
#     7 passed, 1 failed  (only test_one_face_bare_window..., and only because
#                          of the ONE positive `assert any(...)` on line 1027)
#
# That is the harm these entries record: with no copper anywhere on the board,
# these tests are green.


#: What the triage concluded about an entry. Every entry declares one, and
#: `_audit_known_debt` rejects anything else, so a ledger row cannot be added
#: without saying out loud which of these it is.
#:
#: This exists because a debt ledger with no verdict column reads as absolution.
#: Nineteen of the thirty findings below are **open holes** -- assertions that
#: really do go silent and that nothing else catches -- and the ledger has to
#: say so on its face, not bury it in prose.
VERDICTS = {
    "open-hole":          "measured: the assertion goes silent AND no sibling "
                          "check fails either. A real hole, still open.",
    "battery-caught":     "measured: the assertion goes silent, but check_board "
                          "still fails via a named sibling check.",
    "legitimately-empty": "the iterable is empty by design for a valid spec "
                          "(bare board, via-less, no art), so an unconditional "
                          "guard would be a false positive.",
    "pre-existing":       "predates the verdict column; see `why`.",
}

#: Worst first. A ledger row covering several loops takes the worst verdict of
#: the loops it covers, which is what `verdict` states; `split` is what the
#: counter adds up. Before `split` existed, a five-loop row whose ONE bad loop
#: was an open hole contributed **5** to `open-hole`, which is how a triage of
#: 30 findings printed `open-hole=19` when only a handful were.
_VERDICT_SEVERITY = ("open-hole", "legitimately-empty", "battery-caught",
                     "pre-existing")


@dataclass(frozen=True)
class Debt:
    """A pre-existing finding that does not fail the build, and why.

    `count` is how many findings the row covers. `verdict` is the worst of them
    -- the honest headline for a reader scanning the row. `split` is the
    per-finding breakdown and is what `debt_triage` counts, so the summary line
    cannot overstate the debt by rounding a mixed row up to its worst member.
    A single-verdict row leaves `split` at None.
    """
    count: int
    why: str
    verdict: str = "pre-existing"
    split: dict[str, int] | None = None

    def triage(self) -> dict[str, int]:
        return dict(self.split) if self.split else {self.verdict: self.count}


#: Deleting every pour and watching these stay green -- the measurement above.
_NO_POUR = ("measured: still passes with `pcb._fill_geometry` stubbed to `[]`, "
            "i.e. with every copper pour deleted from every board")

KNOWN_DEBT: dict[tuple[str, str, str], Debt] = {
    ("tests/test_logo.py", "test_keepouts_exclude_pixels", "E-VACUOUS-LOOP"):
        Debt(1, "measured in round 1: sat out bug B26 (an inverted bounds guard in "
                "grid_to_rects that empties every rect list) while 21 sibling tests "
                "caught it, purely because this loop body never ran"),
    ("tests/test_pcb.py", "test_advanced_led_own_rotation", "E-VACUOUS-LOOP"):
        Debt(1, _NO_POUR),
    ("tests/test_pcb.py", "test_advanced_placement_moves_resistor_and_via", "E-VACUOUS-LOOP"):
        Debt(1, _NO_POUR),
    ("tests/test_pcb.py", "test_glow_and_bare_cut_both_pours", "E-VACUOUS-LOOP"):
        Debt(1, _NO_POUR),
    ("tests/test_pcb.py", "test_novia_art_keepout_follows_the_trace", "E-VACUOUS-LOOP"):
        Debt(1, "measured: `novia_route(...)['pts']` returns 3 points today, so the "
                "loop runs -- but nothing in the test says so, and a router that "
                "returned an empty path would be reported as a passing keepout"),
    ("tests/test_pcb.py", "test_novia_route_clears_the_units_own_copper_and_the_board_edge",
     "E-VACUOUS-LOOP"):
        Debt(1, "measured: still passes with `pcb._unit_copper_quads` stubbed to `[]`, "
                "i.e. with the clearance check between the run and the unit's own "
                "copper -- the thing the test is named for -- switched off entirely"),
    ("tests/test_pcb.py", "test_one_face_bare_window_leaves_the_other_pour_alone",
     "E-VACUOUS-NEGATIVE"):
        Debt(5, "measured: with every pour deleted this test still fails, but only on "
                "the ONE positive `assert any(... for p in front)` at line 1027; all "
                "five `not any(...)` assertions counted here pass on empty input"),
    ("tests/test_pcb.py", "test_reverse_layout_routes_hole_and_forces_1206", "E-VACUOUS-LOOP"):
        Debt(2, _NO_POUR),
    ("tests/test_pcb.py", "test_through_hole_pads_shape_both_pours", "E-VACUOUS-LOOP"):
        Debt(1, _NO_POUR),
    ("tests/test_pcb.py", "test_zone_fills_have_no_holes", "E-VACUOUS-LOOP"):
        Debt(1, _NO_POUR),
    ("tests/test_svgart.py", "test_svg_art_emits_exact_polygons", "E-VACUOUS-LOOP"):
        Debt(1, "measured: the guard above it (`assert max(counts) > 40`) proves SOME "
                "polygon is dense, not that `polys[0]` is; the radius loop it bounds "
                "reads `pts[:20]` of a different polygon and can still be empty"),
    ("tests/test_svgart.py", "test_svg_glow_window_cuts_pours_exactly", "E-VACUOUS-LOOP"):
        Debt(1, _NO_POUR),
    ("tests/test_svgart.py", "test_svg_silk_carved_around_text_exactly", "E-VACUOUS-LOOP"):
        Debt(1, "provable false positive: each `p` was matched by a regex whose body is "
                "`(?:\\(xy [-\\d. ]+\\) ?)+`, so `re.findall('\\\\(xy ...', p)` cannot be "
                "empty -- the proof lives in a regex, which no AST check can read"),
}

# ---------------------------------------------------------------------------
# tests/invariants.py -- the 30 findings the gate could not see until now
# ---------------------------------------------------------------------------
# The gate globbed `test_*.py`, so the largest assertion library in the repo was
# never opened: 26 corpus cases route 100% of their assertions through
# `invariants.check_board`, and `tests/test_board_invariants.py` -- whose entire
# test body is that one call -- reported `0 error(s)`, correctly and uselessly.
# Pointing the gate at the file produces 30 ERRORs.
#
# THE MEASUREMENT BEHIND EVERY VERDICT BELOW, re-run from scratch each round
# (~40 s, `<scratchpad>/round5/strip5.py`): for every one of the 26 corpus rows,
# silence the iterable one flagged loop actually reads, then re-run all 27
# checks in ALL_CHECKS and record whether ANY of them goes red.
#
# Two things about that sentence are the whole point, because the first version
# of this ledger got both wrong and overstated its own debt by 6x:
#
#   * "the iterable the loop actually reads", not "the nearest Board
#     collection". Eight of the thirty findings iterate something derived --
#     `b.fills(...)`, `b.emitted_fills(layer)`, `_interior_probes(geom)`,
#     `_spec_window_regions(...)`, `_kids(node, 'pad')` -- and those verdicts
#     were originally assigned by reading rather than measuring. Two of the
#     readings were wrong, in opposite directions.
#   * "every one of the 26 rows", not one. A verdict from a single row is a
#     worst case only by luck. Rows where the loop would not have run at all
#     are skipped, so a legitimately empty row is not counted as evidence
#     either way.
#
# Result, rows where the WHOLE battery stays green after the silencing:
#
#     b.pads 0/26   b.tracks 0/25   b.vias 0/25   b.zones 0/26   b.nets 0/26
#     b.footprints 0/26   spec.leds 0/25   b.fills() 0/26
#     b.emitted_fills() 0/26   _interior_probes() 0/3
#     _spec_window_regions()  3/3   *** BLIND ***
#     _kids(node,'pad')      25/25  *** BLIND ***
#
# The three catastrophic blindnesses this ledger was written around --
# `b.tracks`, `b.vias`, `b.footprints` emptying with all checks green -- are
# CLOSED. They were closed by `assert_the_board_carries_what_the_spec_implies`,
# which arrived after the first triage. **The split is now 3 open-hole,
# 9 legitimately-empty, 18 battery-caught**, not 19/4/7.
#
# Read `battery-caught` narrowly. For `b.tracks`, `b.vias` and `b.footprints`
# the sole witness on every row is that one presence check: three of the four
# catastrophes are one deletion away from being open holes again. It is
# battery-caught by the letter of the definition and nothing more comfortable
# than that.
#
# These are entered as debt rather than fixed because `tests/invariants.py` is
# owned by another agent. NOTHING HERE IS ABSOLUTION: the three open-hole
# findings are live defects in the safety-critical file, and `pytest -m meta`
# prints the triage every run so the number cannot quietly rot -- in either
# direction. A ledger that overstates its debt gets disbelieved exactly as fast
# as one that understates it.

_MEAS = ("measured over all 26 corpus rows: silencing this loop's own iterable "
         "leaves ")
_SOLE = (_MEAS + "check_board red, but on every row the ONLY witness is "
                 "assert_the_board_carries_what_the_spec_implies -- delete that "
                 "one presence check and this is an open hole again")
_BARE = ("measured over all 26 corpus rows: non-empty on 25 and empty on the "
         "`bare-board` row, which is a legal spec -- so an unconditional guard "
         "would be a false positive and the fix is a spec-conditional one. "
         "Silencing it on any of the other 25 leaves check_board red")

KNOWN_DEBT.update({
    # -- OPEN HOLES: measured blind. Three, not nineteen. --------------------
    ("tests/invariants.py", "assert_light_windows_exist_when_art_asks_for_them",
     "E-VACUOUS-LOOP"):
        Debt(2, "measured on the three window-bearing corpus rows (art-glow, "
                "art-bare, one-face-window): make `_spec_window_regions` return "
                "{} and all 27 checks stay GREEN on all three, so the whole "
                "light-window defence rests on a helper nothing verifies. The "
                "inner `b.emitted_fills(layer)` loop is battery-caught. Filed as "
                "legitimately-empty in round 4 on the reading that the face set "
                "comes from `spec.art` -- true on the 23 rows with no window and "
                "irrelevant on the 3 where a window is the point",
             "open-hole", {"open-hole": 1, "battery-caught": 1}),
    ("tests/invariants.py", "assert_back_side_parts_live_on_back_layers", "E-VACUOUS-LOOP"):
        Debt(2, "measured: `_kids(node, 'pad')` returning [] leaves all 27 checks "
                "GREEN on 25 of 25 relevant rows -- a footprint emitted with no "
                "pads at all is reported as correctly faced. "
                "assert_the_board_carries_what_the_spec_implies counts footprints, "
                "not their pads, so nothing downstream notices. The outer "
                "`b.footprints` loop is battery-caught by that same check",
             "open-hole", {"open-hole": 1, "battery-caught": 1}),
    ("tests/invariants.py", "assert_back_side_parts_live_on_back_layers",
     "E-VACUOUS-NEGATIVE"):
        Debt(1, "measured with the same silencer as the loop above: "
                "`lay = _kid(p, 'layers')[1:]` empty satisfies "
                "`assert all(ly.startswith(face) for ly in lay)`, i.e. a pad on no "
                "copper layer at all passes the line that reads as guarding "
                "exactly that, and no sibling check goes red",
             "open-hole"),

    # -- LEGITIMATELY EMPTY: measured empty on a legal spec, so no -----------
    # -- unconditional guard exists. Silenced on any other row, check_board --
    # -- still goes red. The fix is a spec-conditional guard, not a bare one. -
    ("tests/invariants.py", "assert_led_circuits_complete", "E-VACUOUS-LOOP"):
        Debt(1, _BARE + " (via assert_pour_fills_have_no_holes and "
                "assert_the_board_carries_what_the_spec_implies). The honest fix "
                "is a caller-side assertion that the LED-bearing rows have LEDs",
             "legitimately-empty"),
    ("tests/invariants.py", "assert_far_side_leds_have_a_via_in_each_pad", "E-VACUOUS-LOOP"):
        Debt(1, _BARE + ". Worse than the loop verdict suggests, and re-counted "
                "this round: **no corpus row sets `farled` on any unit**, so the "
                "body of this check evaluates ZERO assertions on 26/26 rows and "
                "could be deleted with every case still green. A corpus row, not "
                "a guard, is what this needs",
             "legitimately-empty"),
    ("tests/invariants.py", "assert_vias_cross_the_board", "E-VACUOUS-LOOP"):
        Debt(1, _BARE + ". Needs `assert b.vias or <spec asks for none>`",
             "legitimately-empty"),
    ("tests/invariants.py", "assert_via_geometry_is_fab_safe", "E-VACUOUS-LOOP"):
        Debt(1, _BARE + ". This is the check that caught round 1's B22 "
                "(VIA_DRILL 0.3 -> 0.5) when all 62 tests in test_pcb.py did not",
             "legitimately-empty"),
    ("tests/invariants.py", "assert_copper_clears_the_board_edge", "E-VACUOUS-LOOP"):
        Debt(2, _BARE + " (tracks and vias, both empty only on `bare-board`). "
                "This is the in-process stand-in for kicad-cli's "
                "copper_edge_clearance, i.e. the check a developer without "
                "kicad-cli is relying on",
             "legitimately-empty"),
    ("tests/invariants.py", "assert_tracks_carry_the_net_of_the_pads_they_touch",
     "E-VACUOUS-LOOP"):
        Debt(2, _BARE + " -- that is the `b.tracks` loop. The `b.pads` loop is "
                "battery-caught (pads are non-empty on 26/26). This check's own "
                "docstring calls it 'the only thing between the user and a board "
                "where the series resistor is shorted out', and real DRC is "
                "measured blind to that mutation, so a spec-conditional guard "
                "here is worth writing before the others",
             "legitimately-empty", {"legitimately-empty": 1, "battery-caught": 1}),
    ("tests/invariants.py", "assert_every_net_is_declared", "E-VACUOUS-LOOP"):
        Debt(3, _BARE + " -- that is the `for it in items` loop, which cycles "
                "pads, tracks and vias, and tracks/vias are empty on `bare-board`. "
                "The `b.zones` and `b.nets.items()` loops are battery-caught "
                "(measured: zones=[] fails assert_exactly_one_pour_per_face)",
             "legitimately-empty", {"legitimately-empty": 1, "battery-caught": 2}),
    ("tests/invariants.py", "assert_pours_keep_fab_clearance", "E-VACUOUS-LOOP"):
        Debt(5, _BARE + " -- that is `for t in b.tracks`, so pour-to-signal "
                "clearance, the short this check exists for, is unguarded on a "
                "board that has tracks. The three `fills` loops and the `b.pads` "
                "loop are battery-caught. Round 4 recorded all five as open-hole; "
                "measured, exactly one is even conditional",
             "legitimately-empty", {"legitimately-empty": 1, "battery-caught": 4}),

    # -- BATTERY-CAUGHT: measured, check_board still goes red ----------------
    ("tests/invariants.py", "assert_signal_pins_never_powered", "E-VACUOUS-LOOP"):
        Debt(1, _MEAS + "assert_connector_power_pins_wired, "
                "assert_led_circuits_complete and "
                "assert_the_board_carries_what_the_spec_implies red on 26/26 rows",
             "battery-caught"),
    ("tests/invariants.py", "assert_connector_power_pins_wired", "E-VACUOUS-LOOP"):
        Debt(1, _MEAS + "this function red on its own -- `assert present == "
                "set(spec.pins)` two lines above the loop is what goes red when "
                "b.pads is emptied; the checker cannot read a set comprehension "
                "with an `if` as a proof",
             "battery-caught"),
    ("tests/invariants.py", "assert_net_index_matches_name", "E-VACUOUS-LOOP"):
        Debt(1, _MEAS + "check_board red on 26/26 rows via at least "
                "assert_connector_power_pins_wired and "
                "assert_the_board_carries_what_the_spec_implies",
             "battery-caught"),
    ("tests/invariants.py", "assert_through_hole_pads_reach_both_faces", "E-VACUOUS-LOOP"):
        Debt(1, _MEAS + "check_board red on 26/26 rows via at least "
                "assert_connector_power_pins_wired and "
                "assert_the_board_carries_what_the_spec_implies",
             "battery-caught"),
    ("tests/invariants.py", "assert_units_sit_inside_the_safe_region", "E-VACUOUS-LOOP"):
        Debt(1, _MEAS + "check_board red on 26/26 rows via at least "
                "assert_connector_power_pins_wired and "
                "assert_the_board_carries_what_the_spec_implies",
             "battery-caught"),
    ("tests/invariants.py", "assert_pours_stay_inside_the_outline", "E-VACUOUS-LOOP"):
        Debt(1, _MEAS + "assert_pour_fills_have_no_holes and "
                "assert_pours_actually_contain_copper red on 26/26 rows when "
                "b.emitted_fills() comes back empty",
             "battery-caught"),
    ("tests/invariants.py", "assert_pour_fills_have_no_holes", "E-VACUOUS-LOOP"):
        Debt(1, _MEAS + "this same function red on 26/26 rows via its sibling "
                "assertion, which compares emitted against intended copper area",
             "battery-caught"),
    ("tests/invariants.py", "assert_light_windows_are_clear_of_copper", "E-VACUOUS-LOOP"):
        Debt(2, "re-measured this round and the verdict IMPROVED: "
                "`_interior_probes` returning [] is now caught on all three "
                "window rows by assert_light_windows_exist_when_art_asks_for_them "
                "(`assert probes, \'...too thin to sample...\'`), which did not "
                "exist when this was filed as an open hole. The "
                "`b.emitted_fills(layer)` loop is caught by "
                "assert_pours_actually_contain_copper",
             "battery-caught"),
})
#: A `why` has to be a sentence about evidence, not an adjective. Keyword
#: matching is friction, not proof -- the real gate is that this file is
#: tracked, so every edit to it is a reviewable diff (D15).
_EVIDENCE_WORDS = ("measured", "verified", "reproduced", "provable", "sat out",
                   "still passes", "counted", "observed")


def _audit_known_debt() -> list[str]:
    """Every debt entry must name the measurement that justifies it (D15)."""
    bad = []
    for key, debt in KNOWN_DEBT.items():
        where = "::".join(key)
        if not isinstance(debt, Debt) or debt.count < 1:
            bad.append(f"{where}: not a Debt(count>=1, why=...)")
        elif len(debt.why.strip()) < 40:
            bad.append(f"{where}: `why` is {len(debt.why.strip())} chars; a reason "
                       f"that fits in a tweet is not a measurement")
        elif not any(w in debt.why.lower() for w in _EVIDENCE_WORDS):
            bad.append(f"{where}: `why` names no measurement (say one of "
                       f"{', '.join(_EVIDENCE_WORDS)} and what you ran)")
        elif debt.verdict not in VERDICTS:
            bad.append(f"{where}: verdict {debt.verdict!r} is not one of "
                       f"{sorted(VERDICTS)}")
        elif debt.split is not None:
            # `unknown` is tested FIRST and `worst` computed only after: ranking
            # an unrecognised verdict raises `ValueError` out of the auditor
            # instead of returning it as a finding, which turns a bad ledger row
            # into a crash in whatever called the audit. Found by the
            # calibration in test_meta.py, which is the entire point of it.
            unknown = sorted(set(debt.split) - set(VERDICTS))
            if unknown:
                bad.append(f"{where}: split names unknown verdict(s) {unknown}")
                continue
            worst = min(debt.split, key=_VERDICT_SEVERITY.index) if debt.split else None
            if sum(debt.split.values()) != debt.count:
                bad.append(f"{where}: split sums to {sum(debt.split.values())} but "
                           f"count is {debt.count} -- every finding the row covers "
                           f"has to land in exactly one verdict")
            elif worst != debt.verdict:
                bad.append(f"{where}: verdict is {debt.verdict!r} but the worst "
                           f"member of the split is {worst!r}; a row must be "
                           f"headlined by its worst finding, not its average")
    return bad


def debt_triage() -> dict[str, int]:
    """Findings per verdict, for the one-line summary `pytest -m meta` prints.

    Counts the per-finding `split` where a row has one, so a mixed row is not
    charged to its worst verdict N times over.
    """
    out: dict[str, int] = {}
    for debt in KNOWN_DEBT.values():
        for verdict, n in debt.triage().items():
            out[verdict] = out.get(verdict, 0) + n
    return out


@dataclass
class Finding:
    path: str
    line: int
    code: str
    severity: str          # "ERROR" | "ADVICE"
    test: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line} {self.severity} {self.code} [{self.test}] {self.message}"


# ---------------------------------------------------------------------------
# small AST helpers
# ---------------------------------------------------------------------------


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


# Calls whose result is non-empty *if and only if* their first positional
# argument is. Peeling them is truth-preserving in both directions, so a guard
# that proves the argument non-empty also proves the wrapper non-empty.
#
# Measured false positive this fixes (adversary round 3, F3d): these two lines
# differ by four characters and the gate disagreed about them --
#
#     assert len(vias) > 0, "the board must carry at least one via"
#     for v in sorted(vias):   -> ERROR E-VACUOUS-LOOP
#     for v in vias:           -> clean
#
# Asking for deterministic iteration order is something a reviewer requests, and
# under the old rule complying with that request turned a correctly guarded loop
# red. That is goal 3 (do not block legitimate work) being violated by the gate.
#
# `zip` and `range` are deliberately absent: `zip(xs, [])` is empty however
# non-empty `xs` is, and `range(n)` does not read its argument's length at all.
# `dict(...)` is absent because `dict(pairs)` can collapse but not empty --
# true, but `dict()` with no args is empty and the extra case is not worth it.
_LENGTH_PRESERVING = {"sorted", "reversed", "list", "tuple", "set", "frozenset",
                      "iter", "enumerate"}

#: Views: `d.items()` is non-empty exactly when `d` is, in both directions.
#: Measured false positive this fixes (round 5, probe `round5/fp`):
#:
#:     by_net = {p.net: p for p in board.pads}
#:     assert by_net, "no pads carried a net"
#:     for net, pad in by_net.items():   -> ERROR before this
#:
#: The guard is the textbook one and it was not being credited, because the
#: proven source is spelled `by_net` and the loop is spelled `by_net.items()`.
_LENGTH_PRESERVING_VIEWS = {"items", "keys", "values"}

#: Non-empty iff **every** argument is. `zip(xs, [])` is empty however non-empty
#: `xs` is -- which is why `zip` cannot go in `_LENGTH_PRESERVING`, where peeling
#: to the first argument would be a lie -- but `zip(a, b)` with *both* proven is
#: sound, and refusing it blocks this, which is well-written by any standard:
#:
#:     assert board.pads, "the board has no pads"
#:     assert board.tracks, "the board has no tracks"
#:     for pad, track in zip(board.pads, board.tracks):   -> ERROR before this
#:
#: `product` is the same shape. `range` is deliberately absent: it does not read
#: its argument's length at all and `range(len(xs))` must keep failing.
_ALL_ARGS_NONEMPTY = {"zip", "product"}


def _peel(node: ast.AST) -> ast.AST:
    """Strip length-preserving wrappers down to the collection underneath.

    `sorted(vias)` -> `vias`;  `list(reversed(rows))` -> `rows`;
    `[p for p in fills]` -> `fills`. A comprehension only peels when it has
    exactly one `for` and **no** `if`: `[v for v in xs if v > 1000]` is empty
    for plenty of non-empty `xs`, and that shape is the probe this whole check
    exists to catch, so it must keep failing.
    """
    seen = 0
    while seen < 8:
        seen += 1
        if isinstance(node, ast.Call) and _callee_name(node) in _LENGTH_PRESERVING \
                and node.args and not any(isinstance(a, ast.Starred) for a in node.args):
            node = node.args[0]
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _LENGTH_PRESERVING_VIEWS \
                and not node.args and not node.keywords:
            node = node.func.value
            continue
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) \
                and len(node.generators) == 1 \
                and not node.generators[0].ifs \
                and not node.generators[0].is_async:
            node = node.generators[0].iter
            continue
        return node
    return node


def _root_name(node: ast.AST) -> str | None:
    """`front` -> 'front';  `rings[0]` -> 'rings';  `pcb._fill_geometry(s)` -> None.

    Subscripts resolve to their base so that `assert rings` guards
    `for x, y in rings[0]` (tests/test_svgart.py:182-183) instead of being a
    false positive. Length-preserving wrappers are peeled first, so
    `sorted(vias)` resolves to `vias` and the generous earlier-assert fallback
    below can see it -- before this it returned None for any Call and the
    fallback was skipped entirely.
    """
    node = _peel(node)
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _callee_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    return None


# ---------------------------------------------------------------------------
# proof of non-emptiness
# ---------------------------------------------------------------------------
# This block is the answer to D15. The gate previously downgraded both vacuity
# ERRORs to ADVICE whenever the test carried `@given`, on the theory that "an
# empty draw is a case Hypothesis chose, and Hypothesis will shrink to it on its
# own." That is false: shrinking is driven by *failures*, and a vacuous pass is
# a pass. Verified -- a deliberately vacuous @given test (a loop over a filter
# that is normally empty, asserting nothing) reported `0 error(s)` and exit 0.
# The downgrade made the gate unable to fail on a property test, by construction.
#
# It was, however, papering over a real false positive. `tests/test_properties.py`
# writes
#
#     assume(spec.leds)          # normalise drops units the webapp would 400 on
#     for led in spec.leds:
#         assert not pcb.pad_conflict(led, spec.pins, safe), ...
#
# and that loop is *not* vacuous: `assume(X)` aborts the example when X is
# falsy, exactly as `assert len(X) > 0` aborts the test. So the fix is to teach
# the checker what proves an iterable non-empty, rather than to switch the
# check off for a whole decorator.
#
# The rule below is deliberately PRECISE, unlike the older name-based `assert`
# heuristic it sits beside. A generous reading of `assume` would relaunch the
# hole from the other side: the same file also contains
#
#     assume(not any(pcb.pad_conflict(led, spec.pins, safe) for led in spec.leds))
#
# which is *satisfied by an empty* `spec.leds` and therefore proves nothing.
# Anything that is vacuously true on empty input (`not any`, `all`, `len(X) >= 0`)
# must not appear as a proof here, or the gate launders vacuity through assume().

_NONEMPTY_LITERALS = (ast.List, ast.Tuple, ast.Set)


def _int_const(node: ast.AST) -> int | None:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _int_const(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    return None


def _len_arg(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Call) and _callee_name(node) == "len" and len(node.args) == 1:
        return node.args[0]
    return None


def _nonempty_literal(node: ast.AST) -> bool:
    if isinstance(node, _NONEMPTY_LITERALS):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(node.value)
    return False


def _proves_nonempty(expr: ast.AST) -> set[str]:
    """Source of every iterable this condition, *if true*, proves is non-empty.

    Truth-preserving by construction: every branch below is a statement that is
    false on empty input. Nothing that survives an empty collection is listed.
    """
    out: set[str] = set()
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        for value in expr.values:
            out |= _proves_nonempty(value)
        return out
    if isinstance(expr, ast.UnaryOp):
        return out                      # `not X` says nothing about X's length
    if isinstance(expr, ast.Compare):
        return _proves_nonempty_compare(expr)
    if isinstance(expr, ast.Call):
        # `any(<comp>)` is False on an empty iterable, so a true any() proves it.
        if _callee_name(expr) == "any" and expr.args:
            comp = expr.args[0]
            if isinstance(comp, (ast.GeneratorExp, ast.ListComp, ast.SetComp)) \
                    and len(comp.generators) == 1:
                _add_source(out, comp.generators[0].iter)
            return out
        if _callee_name(expr) == "len":
            return out                  # `assume(len(x))` -- handled as truthiness
        _add_source(out, expr)          # bare truthiness of a call's result
        return out
    if isinstance(expr, (ast.Name, ast.Attribute, ast.Subscript)):
        _add_source(out, expr)          # `assume(spec.leds)` / `assert tracks`
        return out
    return out


def _add_source(out: set[str], node: ast.AST) -> None:
    """Record an expression as proven non-empty, in every spelling that follows.

    `assert sorted(vias)` proves both `sorted(vias)` and `vias`, because the
    wrapper preserves length in both directions.
    """
    out.add(ast.unparse(node))
    out.add(ast.unparse(_peel(node)))


def _proves_nonempty_compare(cmp_: ast.Compare) -> set[str]:
    if len(cmp_.ops) != 1:
        return set()
    op, left, right = cmp_.ops[0], cmp_.left, cmp_.comparators[0]

    out: set[str] = set()

    # `x in X` -- membership is false on an empty container.
    if isinstance(op, ast.In):
        _add_source(out, right)
        return out

    # `X == [a, b]` / `[a, b] == X`
    if isinstance(op, ast.Eq):
        if _nonempty_literal(right) and _len_arg(left) is None:
            _add_source(out, left)
            return out
        if _nonempty_literal(left) and _len_arg(right) is None:
            _add_source(out, right)
            return out

    # len(X) <op> k, and its mirror image.
    for a, b, flipped in ((left, right, False), (right, left, True)):
        inner = _len_arg(a)
        k = _int_const(b)
        if inner is None or k is None:
            continue
        kind = type(op)
        if flipped:                      # k < len(X)  ==  len(X) > k
            kind = {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt,
                    ast.GtE: ast.LtE}.get(kind, kind)
        proves = (
            (kind is ast.Gt and k >= 0)          # len(X) > 0
            or (kind is ast.GtE and k >= 1)      # len(X) >= 1
            or (kind is ast.Eq and k >= 1)       # len(X) == 4
            or (kind is ast.NotEq and k == 0)    # len(X) != 0
        )
        if proves:
            _add_source(out, inner)
            return out
    return out


# Calls that never return, so a branch ending in one cannot fall through to the
# code below the `if`. `pytest.fail` under an explicit `if not X:` is a
# *stronger* guard than `assert X` -- `-O` cannot strip it and it carries a
# message by construction -- and until this list existed the gate rejected it
# (adversary round 3, F3e). `return` is deliberately absent: `if not vias:
# return` makes the test pass silently on empty input, which is the exact
# vacuity this file exists to find.
_ABORTING_CALLS = {
    "pytest.fail", "pytest.skip", "pytest.xfail", "pytest.exit",
    "fail", "skip", "xfail",
}


def _always_aborts(body: list[ast.stmt]) -> bool:
    """True if control cannot reach the statement after this block."""
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            if ast.unparse(stmt.value.func) in _ABORTING_CALLS:
                return True
        if isinstance(stmt, ast.If) and stmt.orelse \
                and _always_aborts(stmt.body) and _always_aborts(stmt.orelse):
            return True
    return False


def _negate(expr: ast.AST) -> ast.AST:
    """`not X` -> `X`; `len(X) == 0` -> `len(X) != 0`; anything else -> `not X`.

    Only the truth-preserving rewrites are listed. Anything that falls through
    to the `not X` wrapper is read by `_proves_nonempty` as proving nothing,
    which is the safe direction.
    """
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return expr.operand
    if isinstance(expr, ast.Compare) and len(expr.ops) == 1:
        flip = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE,
                ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt}.get(type(expr.ops[0]))
        if flip is not None:
            return ast.Compare(left=expr.left, ops=[flip()],
                               comparators=expr.comparators)
    return ast.UnaryOp(op=ast.Not(), operand=expr)


def _spans(body: list[ast.stmt], lineno: int) -> bool:
    """True if `lineno` falls inside this block."""
    return any(s.lineno <= lineno <= (s.end_lineno or s.lineno) for s in body)


def _guard_expressions(fn: ast.AST, lineno: int):
    """Conditions that must have held before `lineno` for execution to get there.

    Three proof forms, all with the same meaning -- *if this were false we would
    not be here*:

      ``assert C``                 the plain one
      ``assume(C)``                Hypothesis aborts the example (D15)
      ``if not C: pytest.fail()``  the branch cannot fall through, so C held
    """
    for node in ast.walk(fn):
        if getattr(node, "lineno", lineno) >= lineno:
            continue
        if isinstance(node, ast.Assert):
            yield node.test
        elif isinstance(node, ast.Expr) and _callee_name(node.value) == "assume":
            call = node.value
            if call.args:
                yield call.args[0]
        elif isinstance(node, ast.If):
            if _spans(node.body, lineno):
                yield node.test                 # we are *inside* `if C:`, so C held
            elif _spans(node.orelse, lineno):
                yield _negate(node.test)        # inside `else:`, so C was false
            elif not node.orelse and _always_aborts(node.body):
                # Reaching `lineno` means the branch was not taken, so its
                # condition was false -- i.e. the negation held.
                yield _negate(node.test)


def _proven_nonempty_before(fn: ast.AST, lineno: int) -> set[str]:
    out: set[str] = set()
    for cond in _guard_expressions(fn, lineno):
        out |= _proves_nonempty(cond)
    return out


def _is_proven_nonempty(iter_node: ast.AST, fn: ast.AST, lineno: int) -> bool:
    # `zip(a, b)` / `product(a, b)`: non-empty exactly when every argument is,
    # so a guard on each argument bounds the whole call. Peeling to `a` would be
    # unsound (an empty `b` empties the zip), which is why this is a separate
    # rule rather than an entry in `_LENGTH_PRESERVING`.
    if isinstance(iter_node, ast.Call) and _callee_name(iter_node) in _ALL_ARGS_NONEMPTY \
            and iter_node.args \
            and not any(isinstance(a, ast.Starred) for a in iter_node.args):
        return all(_nonempty_literal(a) or _is_proven_nonempty(a, fn, lineno)
                   for a in iter_node.args)

    proven = _proven_nonempty_before(fn, lineno)
    if not proven:
        return False
    # Compare both the expression as written and with length-preserving
    # wrappers peeled, so `assert len(vias) > 0` bounds `sorted(vias)`.
    for node in (iter_node, _peel(iter_node)):
        if ast.unparse(node) in proven:
            return True
        # `assert rings` also bounds `rings[0]`.
        while isinstance(node, ast.Subscript):
            node = node.value
            if ast.unparse(node) in proven:
                return True
    return False


def _decorator_markers(fn: ast.FunctionDef) -> set[str]:
    out: set[str] = set()
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        # @pytest.mark.foo  /  @pytest.mark.foo(...)
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute):
            if target.value.attr == "mark":
                out.add(target.attr)
    return out


def _stmts_before(fn: ast.AST, lineno: int):
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert) and node.lineno < lineno:
            yield node


def _derived_from(fn: ast.AST) -> dict[str, set[str]]:
    """target name -> the names its value was computed from.

    Used one way only: if `names = [m["x"] for m in got["materials"]]` and a
    later assertion pins `names` to a four-element list, then `got["materials"]`
    is proven non-empty. Non-emptiness of a derivative implies non-emptiness of
    its source; the converse is false and is *not* inferred.
    """
    out: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            sources = _names_in(node.value)
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, set()).update(sources)
    return out


def _has_earlier_assert_mentioning(fn: ast.AST, lineno: int, name: str) -> bool:
    """A prior `assert` naming this variable -- or anything computed from it --
    counts as its non-empty proof.

    Deliberately generous. `assert rects`, `assert len(rects) > 20` and
    `assert any(p.contains(at) for p in front)` all qualify -- the last one is
    exactly how the *good* half of test_pcb.py:1027 proves its front pour
    exists, which is why the check fires on `back` and not on `front`.
    """
    asserted: set[str] = set()
    for a in _stmts_before(fn, lineno):
        asserted |= _names_in(a.test)
    if name in asserted:
        return True
    derived = _derived_from(fn)
    return any(name in derived.get(a, ()) for a in asserted)


class _ModuleFacts:
    """Everything a check needs to know about the file as a whole."""

    def __init__(self, tree: ast.Module) -> None:
        self.module_aliases: set[str] = set()
        self.imported_symbols: set[str] = set()
        self.helpers: dict[str, ast.FunctionDef] = {}
        self.referenced: set[str] = set()

        # Module-level constant tables. `CORPUS = (("bare-board", ...), ...)` at
        # the top of a file and iterated in a test was reported vacuous, which is
        # a false positive on one of the most common shapes in this suite: the
        # table is right there in the source and is not empty.
        #
        # Only names assigned EXACTLY ONCE at module scope count. A name rebound
        # later could be rebound to `[]`, and the whole value of this check is
        # that it does not guess.
        assigned: dict[str, list[ast.AST]] = {}
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, ast.AnnAssign) and node.value
                       else [])
            for t in targets:
                if isinstance(t, ast.Name):
                    assigned.setdefault(t.id, []).append(node.value)
        self.module_constants: dict[str, ast.AST] = {
            name: values[0] for name, values in assigned.items() if len(values) == 1
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.module_aliases.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    self.imported_symbols.add(a.asname or a.name)
                    self.module_aliases.add(a.asname or a.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.helpers.setdefault(node.name, node)
            elif isinstance(node, ast.Name):
                self.referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                self.referenced.add(node.attr)

    def returns_nonempty(self, fn: ast.AST, _seen: frozenset[str] = frozenset()) -> bool:
        """True if every value this helper can return is provably non-empty.

        Extracting a guarded lookup into a helper and reusing it is the *right*
        shape -- it is lane 8's verified fix to `test_pcb.py:1026`::

            def fills(net, layer, spec):
                polys = pcb._fill_geometry(net, layer, spec)
                assert any(p.contains(elsewhere) for p in polys), f"{net} poured nothing"
                return polys

        The rule used to be "the helper contains an `Assert` node anywhere",
        which the round-3 adversary broke in one line (F3c): a helper whose only
        assertion was ``assert isinstance(net, str)`` -- nothing to do with
        length -- silenced `E-VACUOUS-LOOP` for **every** call to it in the
        file. The assert had to be *about the thing being returned*, and now it
        is: the returned expression itself must be proven non-empty, by exactly
        the same `_proves_nonempty` machinery a guard inside a test uses.

        A helper that can fall off the end (implicit `return None`) is never
        bounded, however well guarded its explicit returns are.
        """
        body = getattr(fn, "body", None)
        if not body:
            return False
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        if not returns or not isinstance(body[-1], (ast.Return, ast.Raise)):
            return False
        for ret in returns:
            if ret.value is None:
                return False
            if _nonempty_literal(ret.value):
                continue
            if _is_proven_nonempty(ret.value, fn, ret.lineno):
                continue
            if self.is_bounded(ret.value, fn, _seen):
                continue
            return False
        return True

    def is_bounded(self, expr: ast.AST, fn: ast.AST, _seen: frozenset[str] = frozenset()) -> bool:
        """True if this iterable is spelled out and cannot silently be empty."""
        if isinstance(expr, _LITERAL_NODES):
            return _nonempty_literal(expr) or isinstance(expr, ast.JoinedStr)
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            # `[Polygon(r).exterior for r in _outline_rings(spec)]` is non-empty
            # exactly when `_outline_rings(spec)` is -- but only with no `if`.
            gen = expr.generators[0] if len(expr.generators) == 1 else None
            return bool(gen) and not gen.ifs and not gen.is_async \
                and self.is_bounded(gen.iter, fn, _seen)
        if isinstance(expr, ast.Call):
            callee = _callee_name(expr)
            if callee in _SAFE_CALLS:
                return (bool(expr.args)
                        and all(self.is_bounded(a, fn, _seen) for a in expr.args)) \
                    or callee == "range"
            if callee in {"items", "keys", "values"} and isinstance(expr.func, ast.Attribute):
                return self.is_bounded(expr.func.value, fn, _seen)
            helper = self.helpers.get(callee) if callee not in _seen else None
            if helper is not None and helper is not fn:
                return self.returns_nonempty(helper, _seen | {callee})
            return False
        if isinstance(expr, ast.Attribute):
            # `pcb.PKG`, `pcb.CONNECTOR_PADS` -- a module-level constant table.
            root = expr
            while isinstance(root, ast.Attribute):
                root = root.value
            return isinstance(root, ast.Name) and root.id in self.module_aliases
        if isinstance(expr, ast.Name):
            if expr.id in _seen:
                return False
            # Bound to something already proven bounded -- a literal, or a call
            # to a helper that asserts. `back = fills("GND", "B.Cu", spec)` is
            # the shape of lane 8's verified fix to test_pcb.py:1026.
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and node.lineno < expr.lineno:
                    if any(isinstance(t, ast.Name) and t.id == expr.id for t in node.targets):
                        if self.is_bounded(node.value, fn, _seen | {expr.id}):
                            return True
            # Falling back to module scope LAST matters: a local binding shadows
            # the constant, so the local answer has to be the one that counts.
            const = self.module_constants.get(expr.id)
            if const is not None:
                return self.is_bounded(const, fn, _seen | {expr.id})
            return False
        return False


# ---------------------------------------------------------------------------
# suppression comments
# ---------------------------------------------------------------------------

_SUPPRESS = re.compile(r"#\s*style-ok:\s*([A-Z][A-Z0-9-]+)\s*(.*)$")

#: The two escape hatches have to cost roughly the same, or the cheap one is the
#: one everybody uses. Round 3's adversary measured the gap: a `KNOWN_DEBT`
#: entry needed 40 characters and one of eight evidence words, while
#: `# style-ok: E-VACUOUS-LOOP x` -- one character of justification -- silenced
#: the identical finding and left a smaller diff. The unjustified escape was the
#: easy one, which is backwards.
#:
#: A per-line reason is held slightly *below* a ledger reason on purpose, and the
#: difference is a real compensating control rather than an oversight: a
#: `# style-ok` comment sits on the line it excuses, so a reader of that code
#: cannot miss it, whereas a `KNOWN_DEBT` row lives in another file and excuses a
#: test its own author may never reopen. Evidence words are not demanded here for
#: the same reason -- "spec.leds is empty on a bare board by design" is a
#: complete justification and contains none of them.
_SUPPRESS_MIN_CHARS = 30
_SUPPRESS_MIN_WORDS = 5
_SUPPRESS_BOILERPLATE = {"false positive", "known issue", "not a problem", "wont fix",
                         "won't fix", "see above", "as discussed", "by design",
                         "intentional", "checker bug", "ok", "fine", "todo", "wip"}


#: Every code any check can emit. A `# style-ok:` naming anything else silences
#: nothing, so it is reported as stale ADVICE rather than held to the reason
#: rule -- holding a dead comment to a live standard fails the build for a
#: string nobody reads.
CODES = {
    "E-SYNTAX", "E-BAD-SUPPRESSION", "E-VACUOUS-LOOP", "E-VACUOUS-NEGATIVE",
    "E-UNREGISTERED-MARKER", "A-MISSING-TIER-MARKER", "A-NO-ASSERT-MESSAGE",
    "A-NAME-NOT-A-SENTENCE", "A-EXPECTED-FLOAT-LITERAL", "A-STALE-SUPPRESSION",
}


def _suppressions(src: str) -> tuple[dict[int, set[str]], list[tuple[int, str, str]]]:
    """line -> codes silenced there, plus (line, severity, message) to report."""
    ok: dict[int, set[str]] = {}
    bad: list[tuple[int, str, str]] = []
    for i, line in enumerate(src.splitlines(), 1):
        if "style-ok" not in line:
            continue
        m = _SUPPRESS.search(line)
        if not m:
            bad.append((i, "ERROR",
                        "`# style-ok: CODE <reason>` needs a code and a reason"))
            continue
        code, reason = m.group(1), m.group(2).strip().rstrip(".")
        if code not in CODES:
            bad.append((i, "ADVICE",
                        f"`# style-ok: {code}` names a code no check emits, so it "
                        f"silences nothing -- delete the comment or fix the code"))
            continue
        problem = _bad_suppression_reason(reason, code)
        if problem:
            bad.append((i, "ERROR", problem))
            continue
        ok.setdefault(i, set()).add(code)
    return ok, bad


def _bad_suppression_reason(reason: str, code: str) -> str | None:
    """Why this `# style-ok` reason does not count as one, or None if it does."""
    words = reason.split()
    if len(reason) < _SUPPRESS_MIN_CHARS or len(words) < _SUPPRESS_MIN_WORDS:
        return (f"`# style-ok: {code} <reason>` needs a real reason "
                f"({_SUPPRESS_MIN_CHARS}+ chars, {_SUPPRESS_MIN_WORDS}+ words) saying "
                f"why this iterable cannot be empty -- got {reason!r}. Silencing a "
                f"finding is the same act as adding a KNOWN_DEBT entry and costs the "
                f"same; if you cannot say why, the finding is real.")
    if reason.lower().strip("- ") in _SUPPRESS_BOILERPLATE or reason.strip() == code:
        return (f"`# style-ok: {code} {reason}` is boilerplate, not a reason. Say what "
                f"makes the collection non-empty on every path that reaches this line.")
    return None


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


@dataclass
class Checker:
    registered_markers: set[str] = field(default_factory=set)

    def check_file(self, path: Path) -> list[Finding]:
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as exc:
            return [Finding(str(path), exc.lineno or 1, "E-SYNTAX", "ERROR", "-", str(exc))]

        facts = _ModuleFacts(tree)
        suppress, malformed = _suppressions(src)
        rel = relative(path)
        found: list[Finding] = [
            Finding(rel, line,
                    "E-BAD-SUPPRESSION" if sev == "ERROR" else "A-STALE-SUPPRESSION",
                    sev, "-", msg)
            for line, sev, msg in malformed
        ]

        for fn in assertion_functions(tree):
            # No severity is rewritten here or anywhere else. A check declares
            # one severity and keeps it; the only per-site escape is an
            # in-place `# style-ok: CODE <reason>`, which is a visible diff.
            found += self._vacuous_loop(rel, fn, facts)
            found += self._vacuous_negative(rel, fn, facts)
            found += self._assert_messages(rel, fn)
            found += self._expected_float_literals(rel, fn)
            if fn.name.startswith("test_"):
                # Marker and name-shape rules are about *collected* tests. A
                # library checker is not collected and has no tier marker.
                found += self._markers(rel, fn)
                found += self._name_is_a_sentence(rel, fn, facts)

        return [f for f in found if f.code not in suppress.get(f.line, ())]

    # -- ERROR ------------------------------------------------------------

    def _vacuous_loop(self, rel, fn, facts) -> list[Finding]:
        """`for x in <computed>: assert ...` with nothing proving the loop runs.

        Measured: tests/test_logo.py:49 sat out bug B26 while 21 sibling tests
        caught it, purely because its rect list came back empty.
        """
        out = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.For):
                continue
            body_asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
            if not body_asserts:
                continue
            if facts.is_bounded(node.iter, fn):
                continue
            if _is_proven_nonempty(node.iter, fn, node.lineno):
                continue
            target = _root_name(node.iter)
            if target and _has_earlier_assert_mentioning(fn, node.lineno, target):
                continue
            if target is None:
                # iterating a call result directly: no name to guard, so the
                # only proof would be inside the loop, which cannot exist.
                pass
            src = _short(ast.unparse(node.iter))
            out.append(Finding(
                rel, node.lineno, "E-VACUOUS-LOOP", "ERROR", fn.name,
                f"the assertions in this loop run only if `{src}` is non-empty, and "
                f"nothing proves it is; they all pass when it comes back []. "
                f"Add `assert len(...) > N, \"...\"` before the loop.",
            ))
        return out

    def _vacuous_negative(self, rel, fn, facts) -> list[Finding]:
        """`assert not any(...)` / `assert all(...)` over a maybe-empty iterable.

        Measured: tests/test_pcb.py:1028-1039 passes with the entire B.Cu
        ground plane missing.
        """
        out = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            for call in _vacuous_calls(node.test):
                comp = call.args[0] if call.args else None
                if not isinstance(comp, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                    continue
                if len(comp.generators) != 1:
                    continue
                it = comp.generators[0].iter
                if facts.is_bounded(it, fn):
                    continue
                if _is_proven_nonempty(it, fn, node.lineno):
                    continue
                target = _root_name(it)
                if target and _has_earlier_assert_mentioning(fn, node.lineno, target):
                    continue
                kind = "not any" if _callee_name(call) == "any" else "all"
                src = ast.unparse(it)
                out.append(Finding(
                    rel, node.lineno, "E-VACUOUS-NEGATIVE", "ERROR", fn.name,
                    f"`assert {kind}(... for ... in {src})` is satisfied by an empty "
                    f"{src} -- which is the catastrophe it looks like it is guarding "
                    f"against. Prove {src} is non-empty first.",
                ))
        return out

    def _markers(self, rel, fn) -> list[Finding]:
        out = []
        marks = _decorator_markers(fn)
        for m in sorted(marks - BUILTIN_MARKERS - self.registered_markers):
            out.append(Finding(
                rel, fn.lineno, "E-UNREGISTERED-MARKER", "ERROR", fn.name,
                f"@pytest.mark.{m} is not in pyproject.toml [tool.pytest.ini_options] "
                f"markers; under --strict-markers this is a collection error.",
            ))
        args = {a.arg for a in fn.args.args}
        for fixture, marker in TIER_FIXTURES.items():
            if fixture in args and marker not in marks:
                out.append(Finding(
                    rel, fn.lineno, "A-MISSING-TIER-MARKER", "ADVICE", fn.name,
                    f"uses the `{fixture}` fixture but carries no @pytest.mark.{marker}, "
                    f"so `-m {marker}` and the fast tier cannot see it.",
                ))
        return out

    # -- ADVICE -----------------------------------------------------------

    def _assert_messages(self, rel, fn) -> list[Finding]:
        """Only where pytest's own rewriting prints nothing you can act on.

        `assert a == b` already reports both sides, so demanding a message there
        would flag ~400 perfectly good assertions in this suite. `assert
        re.search(...)` reports `assert None`, and `assert all(... for ...)`
        reports `assert False`. Those are the ones worth a sentence.
        """
        out = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert) or node.msg is not None:
                continue
            if not _opaque_assert(node.test):
                continue
            out.append(Finding(
                rel, node.lineno, "A-NO-ASSERT-MESSAGE", "ADVICE", fn.name,
                f"`assert {_short(ast.unparse(node.test))}` fails as `assert False` -- "
                f"pytest cannot show you why. Add `, \"...\"` saying what must hold.",
            ))
        return out

    def _name_is_a_sentence(self, rel, fn, facts) -> list[Finding]:
        stem = fn.name[len("test_"):]
        words = [w for w in stem.split("_") if w]
        if stem in facts.referenced or stem in facts.imported_symbols:
            return [Finding(
                rel, fn.lineno, "A-NAME-NOT-A-SENTENCE", "ADVICE", fn.name,
                f"named after the symbol `{stem}` it calls, not after the behaviour it "
                f"pins. Say what must be true, e.g. test_<subject>_<claim>.",
            )]
        if len(words) < 3:
            return [Finding(
                rel, fn.lineno, "A-NAME-NOT-A-SENTENCE", "ADVICE", fn.name,
                f"{len(words)}-word name is a label, not a claim. A test name should "
                f"read as the sentence that goes red.",
            )]
        return []

    def _expected_float_literals(self, rel, fn) -> list[Finding]:
        """Float literals used as *expected outputs* (D2/R7).

        Scoped hard on purpose, twice over. Lane 7 counted 201 float literals in
        asserts across this suite and almost all are legitimate *inputs*
        (coordinates handed to `pcb.Led(...)`) or *tolerances*
        (`abs(r - 4.5) < 0.02`).

          - inputs are call arguments, never comparison operands, so they never
            reach here at all;
          - tolerances sit on the loose side of `<`/`>=`, so only `==` and `!=`
            are considered. An equality against a bare float is the shape that
            freezes an emitted coordinate.
        """
        out = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            for cmp_ in [n for n in ast.walk(node.test) if isinstance(n, ast.Compare)]:
                if not all(isinstance(o, (ast.Eq, ast.NotEq)) for o in cmp_.ops):
                    continue
                operands = [cmp_.left, *cmp_.comparators]
                floats = [o for o in operands if _is_float_const(o)]
                others = [o for o in operands if not _is_float_const(o)]
                if floats and others and not all(_is_all_literal(o) for o in others):
                    val = ast.unparse(floats[0])
                    out.append(Finding(
                        rel, cmp_.lineno, "A-EXPECTED-FLOAT-LITERAL", "ADVICE", fn.name,
                        f"expected value {val} is a bare float. If it is a fab rule, name "
                        f"it; if it is an output coordinate, assert the relation instead "
                        f"(a cosmetic change to _n() turned 18 tests red in round 1).",
                    ))
        return out


def _vacuous_calls(test: ast.AST):
    """Yield the any()/all() calls whose truth is vacuous on an empty iterable."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = test.operand
        if _callee_name(inner) == "any":
            yield inner
        return
    if isinstance(test, ast.BoolOp):
        for v in test.values:
            yield from _vacuous_calls(v)
        return
    if _callee_name(test) == "all":
        yield test


def _opaque_assert(test: ast.AST) -> bool:
    """True if pytest's rewritten output would print nothing informative."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _opaque_assert(test.operand)
    if isinstance(test, ast.BoolOp):
        return any(_opaque_assert(v) for v in test.values)
    if isinstance(test, ast.Call):
        callee = _callee_name(test)
        if callee in {"any", "all"}:
            return True
        # `assert re.search(...)` -> `assert None`; `assert x.startswith(...)`
        # -> `assert False`. Either way the value tells you nothing.
        return callee not in {"len", "int", "float", "str", "round", "abs", "sorted", "set", "list"}
    return False


def _is_float_const(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        node = node.operand
    return isinstance(node, ast.Constant) and isinstance(node.value, float)


def _is_all_literal(node: ast.AST) -> bool:
    return all(
        isinstance(n, (ast.Constant, ast.Tuple, ast.List, ast.Compare, ast.Load,
                       ast.UnaryOp, ast.USub, ast.BinOp, ast.Add, ast.Sub, ast.Mult,
                       ast.Div, *_COMPARE_OPS))
        for n in ast.walk(node)
    )


_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
                ast.In, ast.NotIn)


def _short(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


#: Prefixes that mark a function whose job is to assert. `test_*` is collected
#: by pytest; `assert_*` / `check_*` is the naming convention `tests/invariants.py`
#: uses for the shared assertion library, and a vacuous loop is exactly as
#: harmful there -- more so, because one library checker backs 26 corpus cases.
ASSERTION_PREFIXES = ("test_", "assert_", "check_")


def assertion_functions(tree: ast.Module):
    """Every function whose body is expected to hold assertions.

    Round 3's adversary measured what this used to miss: restricting the scan
    to `test_*` meant `tests/invariants.py` -- 1,997 lines and every assertion
    the 26-case board corpus makes -- was examined by nothing, while
    `tests/test_board_invariants.py`, whose entire test body is
    `invariants.check_board(...)`, was scanned and reported 0 findings
    "correctly and uselessly".
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name.startswith(ASSERTION_PREFIXES):
            yield node


def registered_markers(pyproject: Path) -> set[str]:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    entries = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
    return {e.split(":", 1)[0].split("(", 1)[0].strip() for e in entries}


#: Files under ``tests/`` that a directory scan skips, and why. Every entry is
#: a decision with a reason, not a side effect of a glob pattern -- which is
#: how ``tests/invariants.py`` went unscanned for two rounds.
UNSCANNED = {
    "test_meta.py": "lints the linter; its own asserts are the fixtures, and "
                    "its sample sources are deliberately vacuous",
    "check_test_style.py": "is the linter; it contains no assertions and its "
                           "`_VACUOUS`-shaped strings are data",
}


def test_files(paths: list[Path]) -> list[Path]:
    """The files a run over ``paths`` will actually open.

    Every ``*.py`` under a scanned directory, not just ``test_*.py``. The old
    glob left 2,509 lines holding the assertions of the two largest corpora in
    the repo -- ``tests/invariants.py`` (the 26-case board battery) and
    ``tests/hostile.py`` (243 hostile-input params) -- outside the gate's field
    of view entirely, while reporting `0 error(s)` on the files that merely
    *call* them. An explicit path is always opened, whatever its name.
    """
    files: list[Path] = []
    for p in paths:
        files.extend(sorted(f for f in p.rglob("*.py") if f.name not in UNSCANNED)
                     if p.is_dir() else [p])
    return files


def relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def scanned_files(paths: list[Path]) -> set[str]:
    """Repo-relative names of the files a run over ``paths`` covers.

    Hand this to ``apply_baseline`` so it only reports debt for files this run
    was in a position to observe.
    """
    return {relative(f) for f in test_files(paths)}


def collect(paths: list[Path]) -> list[Finding]:
    checker = Checker(registered_markers(REPO / "pyproject.toml"))
    findings: list[Finding] = []
    for f in test_files(paths):
        findings.extend(checker.check_file(f))
    return findings


def apply_baseline(findings: list[Finding],
                   scanned: set[str] | None = None) -> tuple[list[Finding], list[str]]:
    """Split ERROR findings into new ones and known debt.

    Returns (new_errors, notes). Notes cover two things: a debt entry whose
    count has *dropped* (so the list shrinks deliberately instead of rotting),
    and any entry missing the justification D15 requires.

    ``scanned`` is the set of repo-relative files this run actually looked at.
    Without it, checking one file reported all thirteen entries as "0 remain"
    -- thirteen `note:` lines of pure noise saying the debt for files nobody
    opened had vanished. A file that was not scanned is not evidence.
    """
    seen: dict[tuple[str, str, str], list[Finding]] = {}
    new: list[Finding] = []
    for f in findings:
        if f.severity != "ERROR":
            continue
        key = (f.path, f.test, f.code)
        if key in KNOWN_DEBT:
            seen.setdefault(key, []).append(f)
        else:
            new.append(f)
    for key, group in seen.items():
        allowed = KNOWN_DEBT[key].count
        if len(group) > allowed:
            new.extend(group[allowed:])
    notes = [
        f"KNOWN_DEBT[{k}] says {debt.count} but only {len(seen.get(k, []))} remain -- "
        f"lower or delete the entry in tests/check_test_style.py"
        for k, debt in KNOWN_DEBT.items()
        if (scanned is None or k[0] in scanned) and len(seen.get(k, [])) < debt.count
    ]
    notes += [f"KNOWN_DEBT is not justified -- {b}" for b in _audit_known_debt()]
    return new, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", type=Path, default=None)
    ap.add_argument("--advice", action="store_true", help="print ADVICE findings too")
    ap.add_argument("--strict", action="store_true", help="ADVICE findings fail as well")
    ap.add_argument("--all", action="store_true",
                    help="ignore KNOWN_DEBT and show every ERROR, including pre-existing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    paths = args.paths or [REPO / "tests"]
    findings = collect(paths)
    advice = [f for f in findings if f.severity == "ADVICE"]
    all_errors = [f for f in findings if f.severity == "ERROR"]
    new_errors, notes = apply_baseline(findings, scanned_files(paths))
    errors = all_errors if args.all else new_errors
    unjustified = _audit_known_debt()

    if args.json:
        print(json.dumps([f.__dict__ for f in (findings if args.all else advice + new_errors)],
                         indent=2))
        return 1 if errors or unjustified or (args.strict and advice) else 0

    for f in errors:
        print(f.render())
    if args.advice or args.strict:
        for f in advice:
            print(f.render())
    for n in notes:
        print(f"note: {n}")

    counts: dict[str, int] = {}
    for f in advice:
        counts[f.code] = counts.get(f.code, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    debt = len(all_errors) - len(new_errors)
    triage = ", ".join(f"{k}={v}" for k, v in sorted(debt_triage().items()))
    print(f"\n{len(errors)} error(s)"
          + (f"; {debt} pre-existing in KNOWN_DEBT (--all to see them)" if debt and not args.all else "")
          + f"; advice: {summary}"
          + ("" if args.advice or args.strict else "   (--advice to see them)"))
    print(f"KNOWN_DEBT triage: {triage}")

    # The docstring has always claimed an unjustified KNOWN_DEBT entry fails the
    # run. It did in --json and did not here; `pytest -m meta` covered the gap,
    # but the CLI now agrees with its own documentation.
    return 1 if errors or unjustified or (args.strict and advice) else 0


if __name__ == "__main__":
    sys.exit(main())
