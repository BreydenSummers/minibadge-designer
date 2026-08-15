"""Tests about the test suite itself.  ``pytest -m meta``

Three jobs:

1. Run the mechanical style checks over ``tests/`` and fail on *new* vacuity.
   Only the shapes that are provably broken by reading the AST fail; the
   advisory ones are printed. Pre-existing findings live in
   ``check_test_style.KNOWN_DEBT`` so this file is green on the day it ships --
   a gate that starts red gets deleted, which helps nobody.
2. Calibrate the checker against a deliberately broken input before trusting
   it on real files (D11). A linter that silently returns "no findings" for
   every file passes forever; the only defence is to hand it something you
   *know* is wrong and watch it fire.
3. Hold the gate open against the way it was actually broken (D15). The
   checker once reported five hard ``E-VACUOUS-LOOP`` errors and exited 1, and
   was then edited to downgrade vacuity to ADVICE whenever the test carried
   ``@given`` -- inside ``.claude/``, which is gitignored, so the loosening
   left no trace in ``git status`` or in any diff. Both gates are tracked files
   under ``tests/`` now, and the calibration below fails if the ``@given``
   escape hatch ever comes back.

An automated quality gate owned by the thing being gated is not a gate. The
tests in section 2 are what make that sentence enforceable rather than nice.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
CHECKER = TESTS / "check_test_style.py"
REDPROOF = TESTS / "redproof.sh"

# Both gates are tracked (D15), so a checkout that has the suite has them. If
# one is missing somebody deleted it; say so out loud rather than skipping (D7).
_no_skill = pytest.mark.skipif(
    not CHECKER.exists(),
    reason=f"{CHECKER} is missing -- it is a tracked file, so this means it was "
           f"deleted, not that the checkout is partial",
)

if CHECKER.exists() and str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


@pytest.fixture(scope="module")
def style():
    import check_test_style as m
    return m


@pytest.fixture(scope="module")
def suite_findings(style):
    """Every finding over the real tests/ directory. ~0.1 s."""
    return style.collect([TESTS])


# ---------------------------------------------------------------------------
# 1. the gate
# ---------------------------------------------------------------------------


@_no_skill
def test_no_test_asserts_only_inside_a_loop_that_can_be_empty(style, suite_findings):
    """An assertion nobody can reach is a test that cannot go red.

    Measured: tests/test_logo.py:49 sat out bug B26 -- an inverted bounds guard
    in grid_to_rects that empties every rect list -- while 21 sibling tests
    caught it, purely because its loop body never executed.
    """
    new, _ = style.apply_baseline(suite_findings, style.scanned_files([TESTS]))
    bad = [f for f in new if f.code == "E-VACUOUS-LOOP"]
    assert not bad, _explain(bad)


@_no_skill
def test_no_negative_assertion_is_satisfied_by_an_empty_collection(style, suite_findings):
    """`assert not any(...)` over an empty list is true, and says nothing.

    Measured: tests/test_pcb.py:1028-1039 passes with the entire B.Cu ground
    plane missing -- the exact catastrophe it reads like it is guarding.
    """
    new, _ = style.apply_baseline(suite_findings, style.scanned_files([TESTS]))
    bad = [f for f in new if f.code == "E-VACUOUS-NEGATIVE"]
    assert not bad, _explain(bad)


@_no_skill
def test_every_marker_used_in_the_suite_is_registered(style, suite_findings):
    """--strict-markers turns a typo into a collection error at the worst moment.

    Catching it here names the marker and the file instead.
    """
    bad = [f for f in suite_findings if f.code == "E-UNREGISTERED-MARKER"]
    assert not bad, _explain(bad)


@_no_skill
def test_known_debt_entries_still_point_at_tests_that_exist(style, suite_findings):
    """The baseline must not rot into a list of tests nobody has any more.

    A *shrinking* count is good news and does not fail -- it is printed, with
    the line to edit. A vanished test is different: it means the key is dead
    weight that would silently stop guarding anything.
    """
    live = {(f.path, f.test) for f in suite_findings}
    files = {f.path for f in suite_findings} | {
        str(p.relative_to(REPO)) for p in TESTS.glob("test_*.py")
    }
    dead = [
        k for k in style.KNOWN_DEBT
        if k[0] in files and (k[0], k[1]) not in live
    ]
    _, notes = style.apply_baseline(suite_findings, style.scanned_files([TESTS]))
    for n in notes:
        print(f"KNOWN_DEBT can shrink: {n}")
    assert not dead, (
        "check_test_style.KNOWN_DEBT names tests that no longer exist: "
        + ", ".join(f"{p}::{t} ({c})" for p, t, c in dead)
        + " -- delete those entries."
    )


@_no_skill
def test_style_advice_is_reported_but_never_fails(style, suite_findings, capsys):
    """Goal 3: the checker must never be the reason someone is stuck.

    Advisory findings have a real false-positive rate (a float literal in an
    assert is usually a legitimate *input*), so they are printed here and
    nowhere else. Read them with `pytest -m meta -s`.
    """
    advice = [f for f in suite_findings if f.severity == "ADVICE"]
    by_code: dict[str, int] = {}
    for f in advice:
        by_code[f.code] = by_code.get(f.code, 0) + 1
    print("style advice (never fails):",
          ", ".join(f"{k}={v}" for k, v in sorted(by_code.items())) or "none")
    assert all(f.severity == "ADVICE" for f in advice), (
        "an ADVICE finding leaked into the failing set; every check must declare "
        "one severity and keep it"
    )


# ---------------------------------------------------------------------------
# 2. calibration -- prove the checker fires before trusting that it is quiet
# ---------------------------------------------------------------------------

_VACUOUS = '''
from minibadge_designer import pcb

def test_pour_stays_clear_of_the_window(spec):
    for poly in pcb._fill_geometry("GND", "B.Cu", spec):
        assert not poly.intersects(window), "copper left in the window"

def test_no_copper_crosses_the_window(spec):
    back = pcb._fill_geometry("GND", "B.Cu", spec)
    assert not any(p.contains(at) for p in back), "back pour should be cut"
'''

_GUARDED = '''
from minibadge_designer import pcb

def test_pour_stays_clear_of_the_window(spec):
    polys = pcb._fill_geometry("GND", "B.Cu", spec)
    assert len(polys) >= 1, "the GND pour vanished entirely"
    for poly in polys:
        assert not poly.intersects(window), "copper left in the window"

def test_no_copper_crosses_the_window(spec):
    def fills(net, layer, sp):
        polys = pcb._fill_geometry(net, layer, sp)
        assert any(p.contains(elsewhere) for p in polys), f"{net} poured nothing"
        return polys

    back = fills("GND", "B.Cu", spec)
    assert not any(p.contains(at) for p in back), "back pour should be cut"

def test_literal_tables_are_not_flagged(spec):
    for size in ("0805", "1206", "3mm"):
        assert size in pcb.PKG, f"{size} missing from the package table"
    for name, geom in pcb.PKG.items():
        assert geom, name
'''


@_no_skill
@pytest.mark.parametrize("code", ["E-VACUOUS-LOOP", "E-VACUOUS-NEGATIVE"])
def test_the_checker_fires_on_a_deliberately_vacuous_file(style, tmp_path, code):
    """D11: calibrate every measurement on a broken artifact first.

    Without this, a bug that makes `collect()` return [] would make every gate
    above pass forever and look like a clean suite.
    """
    f = tmp_path / "test_vacuous_sample.py"
    f.write_text(_VACUOUS)
    codes = [x.code for x in style.collect([f])]
    assert code in codes, (
        f"{code} did not fire on a file written to contain exactly that shape; "
        f"the checker is not measuring anything. Got: {sorted(set(codes))}"
    )


#: The exact hole D15 names: a property test whose loop is normally empty and
#: which therefore asserts nothing, ever. The old ``@given`` downgrade reported
#: `0 error(s)`, ADVICE only, exit 0 on this file.
_VACUOUS_UNDER_GIVEN = '''
from hypothesis import assume, given, strategies as st


@given(st.lists(st.integers()))
def test_a_board_never_emits_a_negative_coordinate(xs):
    for x in [v for v in xs if v > 1000]:
        assert x > 0, "coordinate must be positive"


@given(st.lists(st.integers()))
def test_a_vacuously_true_assume_does_not_bound_the_loop(xs):
    assume(not any(v > 1000 for v in xs))
    for v in xs:
        assert v < 1001, "must stay small"


@given(st.lists(st.integers()))
def test_a_guard_after_the_loop_does_not_bound_it(xs):
    for v in xs:
        assert v == v, "reflexive"
    assume(xs)
'''

#: The legitimate shape the downgrade was papering over. Both forms are live in
#: tests/test_properties.py (``assume(spec.leds)`` above the loop;
#: ``assert vias or not with_via`` before it).
_BOUNDED_UNDER_GIVEN = '''
from hypothesis import assume, given, strategies as st


@given(st.lists(st.integers()))
def test_assume_on_the_collection_itself_bounds_the_loop(xs):
    assume(xs)
    for v in xs:
        assert v == v, "reflexive"


@given(st.lists(st.integers()))
def test_an_explicit_length_assume_bounds_the_loop(xs):
    assume(len(xs) > 0)
    for v in xs:
        assert v == v, "reflexive"


@given(st.lists(st.integers()))
def test_a_prior_assertion_bounds_the_negative_form(xs):
    kept = [v for v in xs if v > 3]
    assert kept or not xs, "everything was filtered out"
    assert all(v > 3 for v in kept), "the filter did not filter"
'''


@_no_skill
def test_a_vacuous_property_test_is_an_error_not_advice(style, tmp_path):
    """D15, and the single most important assertion in this file.

    The checker reported five hard E-VACUOUS-LOOP errors in
    tests/test_properties.py and exited 1. It was then edited mid-session to
    downgrade vacuity to ADVICE under @given, justified as "Hypothesis will
    shrink to it on its own" -- which is false. Shrinking is driven by
    *failures*; a vacuous pass is a pass and Hypothesis never sees it. Measured
    after that edit: a deliberately vacuous @given test reported `0 error(s)`
    and exit 0. The gate could not fail on a property test, by construction.

    If someone re-adds a decorator-wide downgrade, this goes red.
    """
    f = tmp_path / "test_vacuous_property.py"
    f.write_text(_VACUOUS_UNDER_GIVEN)
    findings = style.collect([f])
    errors = [x for x in findings if x.severity == "ERROR"]
    assert len(errors) == 3, (
        "vacuity under @given must be an ERROR, and a guard that is vacuously "
        "true on empty input (`not any(...)`) or that runs after the loop must "
        "not excuse it. Got:\n" + _explain(findings)
    )
    assert all(x.code == "E-VACUOUS-LOOP" for x in errors), _explain(errors)


@_no_skill
def test_a_property_test_bounded_by_assume_is_not_flagged(style, tmp_path):
    """The false positive the downgrade was papering over, fixed precisely.

    `assume(X)` aborts the example when X is falsy, exactly as `assert len(X)
    > 0` aborts the test, so it is a proof of non-emptiness in the same sense.
    Teaching the checker that keeps every true positive above and removes the
    false ones -- without switching a check off for a whole decorator, and
    without a `# style-ok` on the line.
    """
    f = tmp_path / "test_bounded_property.py"
    f.write_text(_BOUNDED_UNDER_GIVEN)
    bad = [x for x in style.collect([f]) if x.severity == "ERROR"]
    assert not bad, (
        "a legitimately bounded property test was flagged; that is the false "
        "positive that got the check switched off last time:\n" + _explain(bad)
    )


@_no_skill
def test_every_known_debt_entry_names_the_measurement_behind_it(style):
    """D15: a gate change must be visible AND justified.

    "It was flagging my new file" is not a justification. Every entry carries a
    one-line reason naming what was measured -- for nine of the thirteen, that
    every copper pour was deleted (`pcb._fill_geometry` stubbed to `[]`) and
    the test stayed green.
    """
    assert not style._audit_known_debt(), (
        "\n".join(style._audit_known_debt())
        + "\n\nEvery KNOWN_DEBT entry needs Debt(count, why=...) where `why` "
          "names the measurement. Adding an entry is how a gate gets quietly "
          "loosened, so the reason is the price of admission."
    )


@_no_skill
def test_the_debt_audit_rejects_the_ways_a_ledger_row_can_lie(style, monkeypatch):
    """D11 for the auditor: the test above is a quiet pass unless this fires.

    `_audit_known_debt` returning `[]` for a clean ledger is indistinguishable
    from `_audit_known_debt` returning `[]` for everything, which is the state
    it would degrade to if a rule were dropped. Each case below is one way a row
    could otherwise misstate the debt, and the `split` cases are new: a row
    covering five loops used to charge all five to its worst verdict, which is
    how a triage of thirty findings printed `open-hole=19`.
    """
    good = ("measured over all 26 corpus rows: silencing this loop leaves the "
            "whole battery green, so nothing catches it")
    key = ("tests/x.py", "test_x", "E-VACUOUS-LOOP")
    cases = {
        "a reason short enough to fit in a tweet":
            style.Debt(1, "measured, it is fine", "open-hole"),
        "a reason that names no measurement at all":
            style.Debt(1, "this one is fine by design and not worth arguing about",
                       "open-hole"),
        "a verdict that is not one of the four":
            style.Debt(1, good, "wontfix"),
        "a split that does not add up to the count":
            style.Debt(5, good, "open-hole", {"open-hole": 1}),
        "a split naming a verdict that does not exist":
            style.Debt(2, good, "open-hole", {"open-hole": 1, "shrug": 1}),
        "a row headlined by better news than its worst finding":
            style.Debt(2, good, "battery-caught",
                       {"open-hole": 1, "battery-caught": 1}),
    }
    for label, debt in cases.items():
        monkeypatch.setattr(style, "KNOWN_DEBT", {key: debt})
        assert style._audit_known_debt(), (
            f"the audit accepted {label} -- it is not checking anything, and "
            f"every KNOWN_DEBT justification downstream of it is unverified"
        )

    honest = style.Debt(3, good, "open-hole",
                        {"open-hole": 1, "battery-caught": 2})
    monkeypatch.setattr(style, "KNOWN_DEBT", {key: honest})
    assert not style._audit_known_debt(), (
        "the audit rejected a correctly split row, so the only way to satisfy "
        "it would be to collapse mixed rows back to their worst verdict"
    )
    assert style.debt_triage() == {"open-hole": 1, "battery-caught": 2}, (
        f"the triage counted {style.debt_triage()} for a row whose split says "
        f"1 open hole and 2 battery-caught findings"
    )


#: Shapes a competent author writes, paired with the near-identical shape that
#: really is vacuous. A vacuity rule earns its place only if it separates these.
#: Every `clean` case below was a measured **false positive** before round 5 --
#: the gate blocking legitimate work, which outranks the coverage a rule buys.
_SOUNDNESS_PAIRS = {
    "a module-level constant table": ('''
CORPUS = (("bare-board", 0), ("size-0603", 1))

def test_every_corpus_row_is_counted(board):
    for name, n in CORPUS:
        assert n >= 0, name
''', '''
CORPUS = (("bare-board", 0),)
CORPUS = []

def test_every_corpus_row_is_counted(board):
    for name, n in CORPUS:
        assert n >= 0, name
'''),
    "a dict view behind a guard on the dict": ('''
def test_every_net_is_positive(board):
    by_net = {p.net: p for p in board.pads}
    assert by_net, "no pads carried a net"
    for net, pad in by_net.items():
        assert net >= 0, pad
''', '''
def test_every_net_is_positive(board):
    by_net = {p.net: p for p in board.pads}
    for net, pad in by_net.items():
        assert net >= 0, pad
'''),
    "zip of two separately guarded collections": ('''
def test_pads_and_tracks_agree(board):
    assert board.pads, "the board has no pads"
    assert board.tracks, "the board has no tracks"
    for pad, track in zip(board.pads, board.tracks):
        assert pad.net == track.net, f"{pad} vs {track}"
''', '''
def test_pads_and_tracks_agree(board):
    assert board.pads, "the board has no pads"
    for pad, track in zip(board.pads, board.tracks):
        assert pad.net == track.net, f"{pad} vs {track}"
'''),
}


@_no_skill
@pytest.mark.parametrize("shape", sorted(_SOUNDNESS_PAIRS))
def test_a_vacuity_rule_separates_the_guarded_shape_from_the_vacuous_one(
        style, tmp_path, shape):
    """Goal 3: a gate that flags well-written tests is worse than no gate.

    The `clean` half of each pair is the shape; the `vacuous` half differs by
    one guard, one rebinding or one argument. Measuring only that the checker
    goes quiet on good code would let a rule be deleted outright; measuring only
    that it fires on bad code is how the false positives got there. Both halves
    or neither.
    """
    clean, vacuous = _SOUNDNESS_PAIRS[shape]
    for label, source, want in (("clean", clean, False), ("vacuous", vacuous, True)):
        f = tmp_path / f"test_{label}_sample.py"
        f.write_text(source)
        codes = [x.code for x in style.collect([f]) if x.severity == "ERROR"]
        fired = "E-VACUOUS-LOOP" in codes
        assert fired is want, (
            f"{shape}, {label} half: expected "
            f"{'E-VACUOUS-LOOP' if want else 'no error'}, got {sorted(set(codes)) or 'nothing'}.\n"
            + ("A guarded, well-written loop is being blocked."
               if want is False else
               "The rule that unblocked the guarded form swallowed the real "
               "vacuity next to it, which is the D15 failure mode.")
        )


@_no_skill
def test_the_gates_are_tracked_files_not_hidden_under_dot_claude(style):
    """The mechanism, not just the instance (D15).

    `.claude/` is gitignored, so a gate living there can be loosened with no
    trace in `git status` and no line in any diff -- which is exactly what
    happened. Both gates live in `tests/` now. If one drifts back, or a copy is
    left behind for the skill to route at, this says so.
    """
    for gate in (CHECKER, REDPROOF):
        assert gate.exists(), f"{gate} is missing"
        assert ".claude" not in gate.parts, (
            f"{gate} is back under the gitignored .claude/; a gate that can be "
            f"edited without a diff is not a gate"
        )
    stale = list((REPO / ".claude").rglob("check_test_style.py")) + \
        list((REPO / ".claude").rglob("redproof.sh"))
    assert not stale, (
        "an untracked copy of the gate is still under .claude/, so the skill "
        "can route at it and the tracked one never runs: "
        + ", ".join(str(p) for p in stale)
    )


@_no_skill
def test_the_checker_is_quiet_on_the_guarded_version(style, tmp_path):
    """The same two tests, guarded the way lane 8's verified fix guards them.

    If this fails the checker is a false-positive machine and would be turned
    off within a day -- which is the failure mode that matters most here.
    """
    f = tmp_path / "test_guarded_sample.py"
    f.write_text(_GUARDED)
    bad = [x for x in style.collect([f]) if x.severity == "ERROR"]
    assert not bad, (
        "the guarded, correct version of the same tests was flagged:\n" + _explain(bad)
    )


@_no_skill
def test_a_style_ok_comment_costs_what_a_known_debt_entry_costs(style, tmp_path):
    """The two escape hatches must not be priced differently (round 3, F3f).

    Measured: `KNOWN_DEBT` demanded 40 characters and one of eight evidence
    words, while `# style-ok: E-VACUOUS-LOOP x` -- one character -- silenced the
    identical finding and left a *smaller* diff. The cheap escape was the
    unjustified one, which is exactly backwards for a gate whose whole defence
    is that loosening it is visible and argued.
    """
    src = (
        "def test_every_via_on_the_board_drills_the_project_size(board):\n"
        "    vias = [ln for ln in board.splitlines() if '(via' in ln]\n"
        "    for v in vias:   # style-ok: E-VACUOUS-LOOP {reason}\n"
        "        assert '0.3' in v, 'every via drills 0.3 mm'\n"
    )
    for reason, wanted in (
        ("x", "E-BAD-SUPPRESSION"),
        ("false positive", "E-BAD-SUPPRESSION"),
        ("by design", "E-BAD-SUPPRESSION"),
        ("the connector always emits four vias, so this cannot be empty", None),
    ):
        f = tmp_path / f"test_supp_{abs(hash(reason))}.py"
        f.write_text(src.format(reason=reason))
        codes = {x.code for x in style.collect([f]) if x.severity == "ERROR"}
        if wanted:
            assert wanted in codes, (
                f"`# style-ok: E-VACUOUS-LOOP {reason}` was accepted as a "
                f"justification. One word of hand-waving must not buy what a "
                f"KNOWN_DEBT entry costs 40 characters and a measurement. Got {codes}"
            )
        else:
            assert not codes, (
                "a real one-line reason was rejected; goal 3 says the gate must "
                f"never be the reason someone is stuck. Got {codes}"
            )


# ---------------------------------------------------------------------------
# 3. reachability -- every checker in the library is reached by some test
# ---------------------------------------------------------------------------
# Round 2 shipped `tests/invariants.py` with 44 validated public checkers and
# **zero** callers. The two it eventually got were contributed by an outside
# agent who did not know the library was the point. Nothing went red, because a
# checker nobody calls cannot fail: it is the vacuous-loop defect one level up,
# at the granularity of a whole function.
#
# The rule: every public `assert_*` / `check_*` in the library must be reachable
# from a collected test -- referenced by name anywhere under `tests/` outside the
# library itself, or a member of `ALL_CHECKS` while `check_board` is referenced.
#
# Some checkers genuinely need artifacts (a GLB export, a rendered PNG) that no
# test produces yet. Those are EXEMPT, never skipped: each entry names a reason
# and the tier its future caller belongs to, so "we have not got to it" stays
# visible and countable instead of dissolving into a green run.

INVARIANTS = TESTS / "invariants.py"

#: name -> (tier the caller belongs to, one-line reason).
#: `EXEMPT` may only ever SHRINK -- `test_the_reachability_exemption_list_only_shrinks`
#: pins its size, so wiring a checker up is the only way to change this file
#: without an argument.
REACHABILITY_EXEMPT: dict[str, tuple[str, str]] = {
    # -- need a GLB export from kicad-cli; no test builds one yet -------------
    "check_board_slab": (
        "kicad", "needs a GLB export to measure the dielectric slab against "
                 "Edge.Cuts; no test produces a GLB fixture yet"),
    "check_present": (
        "kicad", "needs a GLB export; guards the measured defect #7 (kicad-cli "
                 "exits 2 and still writes a valid GLB with the part missing)"),
    "check_mount_plane": (
        "kicad", "needs a GLB export; catches a part floating above or sunk "
                 "into its face, invisible to every 2D check"),
    "check_face": (
        "kicad", "needs a GLB export; the part is mounted on the face the spec "
                 "asked for"),
    "check_placement": (
        "kicad", "needs a GLB export; the part body landed on its pads"),
    "check_rotation_invariance": (
        "kicad", "needs GLB exports at four rotations; the docstring calls this "
                 "the assertion that catches the historical LED-lens bug"),
    "check_mask_material": (
        "kicad", "needs a GLB export plus webapp._tag_glb_layers; pins soldermask "
                 "colour and alphaMode"),
    # -- need rendered raster artifacts, which never gate (D10) ---------------
    "assert_pour_is_one_island": (
        "visual", "needs a rendered copper PNG; vision never gates (D10), so its "
                  "caller belongs behind the `visual` marker"),
    "assert_silk_stays_off_openings": (
        "visual", "needs rendered materials PNGs; same D10 constraint"),
    "assert_board_is_one_piece": (
        "visual", "needs a rendered board PNG; same D10 constraint"),
    # -- the three that needed no artifact are PAID. Do not re-add them. ------
    # Round 4 filed `assert_parses`, `assert_not_crashed` and
    # `assert_project_rules_match_the_invariants` here as UNPAID: nothing was
    # missing, they were simply uncalled, and two of the three had their rule
    # enforced by hand-rolled copies elsewhere. They now have real callers --
    # section 4 below for the first and third, and
    # `test_properties.py::test_the_generate_endpoint_never_returns_500` plus
    # the DRC zip property for the second, both of which used to hand-roll
    # `assert response.status_code != 500`. The remaining ten all need an
    # artifact no test produces (a GLB export, a rendered PNG); every one of
    # those was re-checked this round and none has acquired a caller.
}


def _public_checkers() -> list[str]:
    tree = ast.parse(INVARIANTS.read_text(encoding="utf-8"))
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith(("assert_", "check_"))
            and not n.name.startswith("_")]


def _battery() -> set[str]:
    tree = ast.parse(INVARIANTS.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ALL_CHECKS" for t in node.targets):
            return {ast.unparse(e) for e in node.value.elts}
    return set()


#: Non-``test_*.py`` modules under ``tests/`` that a collected test imports, so
#: names used in them really are exercised. Keep this list tiny and justified —
#: every entry is a file that can certify a checker as reachable.
_IMPORTED_HELPERS = {"conftest.py", "hostile.py"}


def _names_referenced_outside_the_library() -> set[str]:
    """Every identifier mentioned anywhere under tests/ except invariants.py.

    Deliberately coarse: a name that appears is treated as reached. A gate this
    generous still found fifteen checkers with no mention at all, and a stricter
    call-graph would trade that signal for false alarms on indirection.
    """
    out: set[str] = set()
    for f in sorted(TESTS.rglob("*.py")):
        if f == INVARIANTS:
            continue
        # Only files pytest actually collects can make anything reachable.
        # `python_files` is `test_*.py`; an adversary added `_scratch_notes.py`
        # holding three bare attribute expressions inside `if False:`, deleted
        # the three matching exemptions, and the gate went green while
        # `check_rotation_invariance` — the assertion that catches the historical
        # LED-lens bug — ran nowhere. Helper modules imported *by* a collected
        # test still count, because their names are reachable through it.
        if not (f.name.startswith("test_") or f.name in _IMPORTED_HELPERS):
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                out.add(node.id)
            elif isinstance(node, ast.Attribute):
                out.add(node.attr)
    return out


def _unreachable(public, referenced, battery, exempt) -> list[str]:
    """The gate's whole rule, in one place so it can be calibrated (D11)."""
    if "check_board" not in referenced:
        battery = set()          # the battery only reaches anything if it is called
    return sorted(n for n in public
                  if n not in referenced and n not in battery and n not in exempt)


@pytest.mark.skipif(not INVARIANTS.exists(), reason=f"{INVARIANTS} is missing")
def test_every_public_invariant_is_reachable_from_a_collected_test():
    """A validated checker nobody calls protects nobody.

    Round 2 shipped 44 of them with zero callers and the suite stayed green,
    which is the single largest defect this project produced: the library *was*
    the deliverable, and it was inert. Measured at the start of round 4: 45
    public, 6 referenced directly, 26 more via ALL_CHECKS, **13 unreachable**.
    Measured now: 46 public, 9 referenced directly, 27 via ALL_CHECKS,
    **10 exempted, 0 unreachable** -- the three exemptions that needed no
    artifact were paid off rather than re-explained.

    This fails for a checker that is neither reached nor exempted, so a future
    addition cannot go uncalled silently.
    """
    public = _public_checkers()
    assert public, "no public assert_*/check_* found -- did invariants.py move?"
    referenced = _names_referenced_outside_the_library()
    battery = _battery()
    assert "check_board" in referenced, (
        "nothing outside tests/invariants.py mentions `check_board`, so the whole "
        "ALL_CHECKS battery is unreachable and the 26 checks in it are inert"
    )

    unreachable = _unreachable(public, referenced, battery, REACHABILITY_EXEMPT)
    assert not unreachable, (
        f"{len(unreachable)} public checker(s) in tests/invariants.py are reachable "
        f"from no collected test: {', '.join(sorted(unreachable))}.\n"
        "A checker nobody calls cannot go red -- that is the vacuous-loop defect at "
        "function granularity, and it is how 44 validated checks shipped inert.\n"
        "Either call it from a test, or add it to REACHABILITY_EXEMPT in this file "
        "with the tier its caller belongs to and a one-line reason. A bare skip is "
        "not an option."
    )


@pytest.mark.skipif(not INVARIANTS.exists(), reason=f"{INVARIANTS} is missing")
def test_the_reachability_exemption_list_only_shrinks():
    """The exemption list is the movable part, so it is the part that is pinned.

    D15: a gate the gated agent can widen is not a gate. Adding an exemption is
    how this check gets quietly switched off, so the count is a literal here and
    growing it is a visible, arguable diff. Every entry must also name a real
    tier and a reason long enough to be one.
    """
    import tomllib
    markers = {
        e.split(":", 1)[0].split("(", 1)[0].strip()
        for e in tomllib.loads((REPO / "pyproject.toml").read_text())
        ["tool"]["pytest"]["ini_options"]["markers"]
    }
    # `| {"fast"}` used to be here, purely so `assert_parses` could name a tier
    # that is not a marker. That entry is paid, and the tier assertion below now
    # allows only `kicad` and `visual`, so the widening is dead. Do not restore
    # it to make a new exemption fit.

    assert len(REACHABILITY_EXEMPT) <= 10, (
        f"REACHABILITY_EXEMPT has grown to {len(REACHABILITY_EXEMPT)} entries. It was "
        "13 when the gate landed and 10 once the three that needed no artifact "
        "were given real callers; it is meant to keep emptying out as the GLB and "
        "PNG fixtures appear. Exempting a new checker instead of calling it is the "
        "D15 failure mode, and lowering this number back up is the same move."
    )
    artifact_bound = {tier for tier, _ in REACHABILITY_EXEMPT.values()}
    assert artifact_bound <= {"kicad", "visual"}, (
        f"an exemption sits at tier(s) {sorted(artifact_bound - {'kicad', 'visual'})}. "
        "Every remaining entry is here because it needs an artifact no test builds "
        "-- a GLB export (`kicad`) or a rendered PNG (`visual`). A checker that "
        "needs nothing but a caller does not get an exemption; it gets a caller."
    )
    for name, entry in REACHABILITY_EXEMPT.items():
        assert isinstance(entry, tuple) and len(entry) == 2, f"{name}: want (tier, reason)"
        tier, reason = entry
        assert tier in markers, (
            f"{name}: tier {tier!r} is not a registered pytest marker {sorted(markers)} "
            f"-- an exemption has to say where the missing caller belongs"
        )
        assert len(reason) >= 40, (
            f"{name}: reason is {len(reason)} chars. Say what artifact or fixture is "
            f"missing, or admit it is unpaid debt -- the same bar KNOWN_DEBT is held to"
        )

    public = set(_public_checkers())
    gone = sorted(n for n in REACHABILITY_EXEMPT if n not in public)
    assert not gone, (
        f"REACHABILITY_EXEMPT names checkers that no longer exist: {gone} -- delete "
        "them, or the list is dead weight that stops guarding anything"
    )
    reached = _names_referenced_outside_the_library() | (
        _battery() if "check_board" in _names_referenced_outside_the_library() else set())
    paid = sorted(n for n in REACHABILITY_EXEMPT if n in reached)
    assert not paid, (
        f"{paid} now have callers, so their exemptions are stale -- delete them. "
        "This is the only assertion in the file that fails on GOOD news, and it is "
        "here so the list shrinks by itself instead of outliving its reasons."
    )


@pytest.mark.skipif(not INVARIANTS.exists(), reason=f"{INVARIANTS} is missing")
def test_the_reachability_gate_fires_on_an_unreferenced_checker():
    """D11: calibrate on a broken artifact before trusting the quiet answer.

    If `_names_referenced_outside_the_library` ever returned everything -- one
    bad glob would do it -- the gate above would pass forever on a library with
    no callers at all, which is precisely the state it exists to detect.
    """
    referenced = _names_referenced_outside_the_library()
    invented = "assert_a_checker_that_no_test_anywhere_mentions"
    assert invented not in referenced, (
        "the reference scanner claims to have seen a name that appears nowhere; "
        "it is not measuring anything"
    )
    assert "check_board" in referenced, (
        "the reference scanner cannot see `check_board`, which "
        "tests/test_board_invariants.py calls by name -- it is scanning the wrong "
        "files, and every reachability answer it gives is meaningless"
    )

    # The rule itself, on synthetic inputs. Each case is a way the gate could be
    # broken into permanent silence.
    battery = {"assert_in_the_battery"}
    cases = {
        "a brand-new checker nobody wired up": (
            ["check_board", "assert_brand_new"], {"check_board"}, battery, {},
            ["assert_brand_new"]),
        "an exempted checker": (
            ["check_board", "assert_brand_new"], {"check_board"}, battery,
            {"assert_brand_new": ("kicad", "x")}, []),
        "a battery member while check_board IS called": (
            ["check_board", "assert_in_the_battery"], {"check_board"}, battery, {}, []),
        "a battery member while check_board is NOT called": (
            ["check_board", "assert_in_the_battery"], set(), battery, {},
            ["assert_in_the_battery", "check_board"]),
    }
    for label, (public, refs, batt, exempt, want) in cases.items():
        got = _unreachable(public, refs, batt, exempt)
        assert got == want, f"{label}: expected {want}, got {got}"


@_no_skill
def test_the_known_debt_ledger_states_its_triage_out_loud(style, capsys):
    """A debt ledger with no verdict column reads as absolution.

    Scanning tests/invariants.py for the first time produced 30 findings, and
    the first triage of them read `open-hole=19`. Re-measured from scratch --
    every corpus row, and silencing the iterable each loop actually reads rather
    than the nearest `Board` collection -- it is **3**: the three catastrophic
    blindnesses it was written around (`b.tracks`, `b.vias`, `b.footprints`) are
    closed by `assert_the_board_carries_what_the_spec_implies`, which arrived
    after the triage.

    The counter is printed rather than asserted because its job is to be read,
    not to be met. A ledger that overstates its debt gets disbelieved exactly as
    fast as one that understates it. Read it with `pytest -m meta -s`.
    """
    triage = style.debt_triage()
    print("KNOWN_DEBT triage:",
          ", ".join(f"{k}={v}" for k, v in sorted(triage.items())))
    assert set(triage) <= set(style.VERDICTS), (
        f"unknown verdict(s) {sorted(set(triage) - set(style.VERDICTS))}"
    )
    assert sum(triage.values()) == sum(d.count for d in style.KNOWN_DEBT.values())
    assert triage.get("open-hole"), (
        "no entry is labelled `open-hole`. Either every hole was closed -- in "
        "which case delete those entries -- or the verdicts have been softened, "
        "which is the D15 failure mode with extra steps."
    )


# ---------------------------------------------------------------------------
# 4. the two oracles that had no caller at all
# ---------------------------------------------------------------------------
# These are the exemptions section 3 called UNPAID: neither needs a GLB, a PNG
# or any fixture that does not exist -- they were simply never called, and the
# rule each states was enforced by a hand-rolled copy somewhere else. A checker
# whose rule lives in three copies is a checker that can be edited to say
# nothing without a single test noticing.
#
# Both tests are D11-shaped: assert the oracle accepts the real artifact AND
# name the damage it must refuse. An oracle only verified on good input is
# indistinguishable from `def assert_x(_): pass`.


@pytest.fixture(scope="module")
def inv():
    if not INVARIANTS.exists():                      # pragma: no cover - D7
        pytest.fail(f"{INVARIANTS} is missing; it is the library under test here")
    import invariants
    return invariants


@pytest.fixture(scope="module")
def board_text():
    from minibadge_designer import pcb
    return pcb.generate_pcb(_resolved_default_spec())


def _resolved_default_spec():
    import invariants
    from minibadge_designer import pcb
    return invariants.resolved_spec(pcb.BadgeSpec(name="meta"))


@pytest.mark.skipif(not INVARIANTS.exists(), reason=f"{INVARIANTS} is missing")
def test_the_board_oracle_refuses_text_that_is_not_a_balanced_board(inv, board_text):
    """`assert_parses` is the front door of every board invariant, and until now
    nothing tested it.

    `check_board` calls it internally, so a version that accepted anything would
    still let all 27 battery checks run -- against a `Board` parsed out of
    corrupt text, which is how they would report a truncated file as clean.
    That is exactly the artifact defect #4 ships: a 200 response carrying a
    `.kicad_pcb` KiCad cannot open, with no error shown to the user.

    The four damage shapes below are the ones the two assertions in the function
    claim to cover. Each was checked to be accepted by nothing weaker.
    """
    board = inv.assert_parses(board_text)
    assert board.pads and board.nets, (
        "assert_parses returned a Board with no pads and no nets from a real "
        "generated board -- it is not parsing, it is just not complaining"
    )

    damage = {
        "a file that is not a board at all": "hello",
        "an empty download": "",
        "the wrong top-level form": "(pcb_kicad" + board_text[10:],
        "a truncated download": board_text[: len(board_text) // 2],
        "one paren too many": board_text + ")\n",
    }
    for label, text in damage.items():
        with pytest.raises(AssertionError):
            inv.assert_parses(text)
            pytest.fail(f"assert_parses accepted {label} -- every board invariant "
                        f"downstream of it is then measuring a corrupt parse")


@pytest.mark.skipif(not INVARIANTS.exists(), reason=f"{INVARIANTS} is missing")
def test_the_shipped_project_declares_the_rules_the_invariants_enforce(inv):
    """D2 in its sharpest form: the oracle's thresholds versus what ships.

    `RULE_CLEARANCE_MM` is stated in `tests/invariants.py` and
    `min_clearance` is stated in `pcb.generate_project`. Nothing tied the two
    together, so either could be edited alone and every test would stay green
    while the in-process oracle went quiet on boards real DRC rejects (or, the
    other way, the user downloads a project that passes DRC locally and comes
    back from the fab as a scrap panel).

    Calibrated on three damaged projects, one per assertion in the checker.
    """
    import json

    from minibadge_designer import pcb

    shipped = pcb.generate_project("meta")
    inv.assert_project_rules_match_the_invariants(shipped)

    def damaged(**rules) -> str:
        doc = json.loads(shipped)
        doc["board"]["design_settings"]["rules"].update(rules)
        return json.dumps(doc)

    damage = {
        "clearance stricter than the pour check enforces":
            damaged(min_clearance=inv.RULE_CLEARANCE_MM + 0.1),
        "edge clearance stricter than the edge check enforces":
            damaged(min_copper_edge_clearance=inv.RULE_EDGE_CLEARANCE_MM + 0.1),
        "a clearance below the fab floor":
            damaged(min_clearance=inv.FAB_CLEARANCE_FLOOR_MM / 2),
    }
    for label, text in damage.items():
        with pytest.raises(AssertionError):
            inv.assert_project_rules_match_the_invariants(text)
            pytest.fail(f"the checker accepted {label}; it is not comparing "
                        f"anything")


# ---------------------------------------------------------------------------
# 5. redproof.sh refuses the things it must refuse
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not REDPROOF.exists(), reason=f"{REDPROOF} not present")
@pytest.mark.parametrize(
    "args, expect",
    [
        pytest.param([], "--test", id="no arguments at all"),
        pytest.param(["--test", "tests/test_pcb.py::x", "--file", "tests/test_pcb.py",
                      "--old", "a", "--new", "b"],
                     "PRODUCTION", id="breaking the test instead of the code"),
        pytest.param(["--test", "tests/test_pcb.py::x", "--file", "minibadge_designer/pcb.py",
                      "--old", "a", "--new", "a"],
                     "identical", id="a mutation that mutates nothing"),
    ],
)
def test_redproof_refuses_a_verification_it_cannot_honour(args, expect):
    """These all exit before pytest starts, so this stays a fast unit test.

    Each refusal is a way the ritual could otherwise produce a trailer that
    means nothing.
    """
    r = subprocess.run([str(REDPROOF), *args], capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 2, f"expected a usage rejection, got {r.returncode}: {r.stderr}"
    assert expect in r.stderr, f"rejection did not mention {expect!r}: {r.stderr}"


@pytest.mark.skipif(not REDPROOF.exists(), reason=f"{REDPROOF} not present")
def test_redproof_requires_tb_short_so_assertions_stay_visible():
    """Trap 2, pinned as a text assertion because it is a one-word regression.

    Under --tb=line pytest prints `assert 7500.0 == 10000.0` and the words
    "AssertionError" appear nowhere, so a red-reason classifier silently
    reports nothing and every verification is rejected.
    """
    src = REDPROOF.read_text()
    # Comments discuss --tb=line at length; only executable lines are the contract.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "--tb=short" in code, "redproof.sh must run pytest with --tb=short"
    assert "--tb=line" not in code, "--tb=line hides assertion failures from the classifier"
    assert "PYTHONDONTWRITEBYTECODE=1" in code, (
        "trap 1: without it a same-byte-length mutation reruns stale bytecode"
    )
    assert "__pycache__" in code, "trap 1: stale bytecode must be removed between runs"


def _explain(findings) -> str:
    lines = [f.render() for f in findings]
    return "\n".join([
        f"{len(findings)} finding(s):", *lines, "",
        "Fix the test, or -- if this one case is genuinely fine -- silence it in "
        "place with a reason:",
        "    for x in xs:   # style-ok: E-VACUOUS-LOOP xs is a literal table",
        "Never edit KNOWN_DEBT to make a NEW finding go away; that list is for "
        "pre-existing debt only.",
    ])


# ===========================================================================
# Reachability, measured rather than inferred
# ===========================================================================

#: Checks whose assertion evaluates on no corpus row, with the reason. A check
#: here is *called* on every board and asserts on none of them, which is the
#: vacuous-loop shape one level up. Each entry is debt, not an exemption: it
#: says "no corpus board reaches this condition", and the fix is a corpus row
#: that does, not a longer excuse.
MEASURED_INERT = {
    "assert_every_unit_reaches_its_rails":
        "asserts only when a rail island is stranded; clean boards strand none, "
        "so the enumeration is empty on every corpus row. Defects #17 and #18 "
        "have dedicated rows that DO reach it — see test_board_invariants.py.",
}


def _assert_lines_by_function(src: str) -> dict[str, set[int]]:
    tree = ast.parse(src)
    out: dict[str, set[int]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out[node.name] = {
                s.lineno for s in ast.walk(node)
                if isinstance(s, (ast.Assert, ast.Raise))
            }
    return out


@pytest.mark.slow
def test_every_battery_check_actually_evaluates_an_assertion():
    """A check that is *called* but never asserts is inert, and the gate that
    only counts callers cannot tell the difference.

    Measured, not inferred: trace one pass of the corpus and record which
    ``assert``/``raise`` lines in ``invariants.py`` actually execute. An earlier
    version of the reachability gate read identifiers out of the AST, which a
    never-collected file could satisfy — and separately certified four checks as
    reached while no corpus row set ``farled``, ``novia``, ``texts`` or a custom
    outline, so their assertions ran on nothing.
    """
    import sys
    import invariants as inv
    from test_board_invariants import CORPUS

    src = INVARIANTS.read_text(encoding="utf-8")
    want = _assert_lines_by_function(src)
    owner = {}
    for fn, lines in want.items():
        for ln in lines:
            owner[ln] = fn
    fired: set[str] = set()
    target = str(INVARIANTS)

    def tracer(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == target:
            fn = owner.get(frame.f_lineno)
            if fn:
                fired.add(fn)
        return tracer

    sys.settrace(tracer)
    try:
        for _cid, spec in CORPUS:
            try:
                inv.check_board(inv.resolved_spec(spec))
            except AssertionError:
                pass          # a red board still proves the line executed
    finally:
        sys.settrace(None)

    names = {c.__name__ for c in inv.ALL_CHECKS}
    inert = sorted(n for n in names if n not in fired and n not in MEASURED_INERT)
    assert not inert, (
        "these battery checks were called on all "
        f"{len(CORPUS)} corpus boards and evaluated no assertion on any of them, "
        f"so they protect nothing: {inert}. Add a corpus row that reaches the "
        "condition, or record it in MEASURED_INERT with the reason."
    )
    stale = sorted(n for n in MEASURED_INERT if n in fired)
    assert not stale, (
        f"MEASURED_INERT lists {stale}, but a corpus row now reaches them — "
        "delete the entry so the list keeps shrinking."
    )
