"""Shared test infrastructure: tool gating, fixtures, tier bookkeeping.

Four things live here.

1. **Tool gating.** kicad-cli, a browser, and the fetched font/vendor assets
   may each be absent. A test that needs one declares
   ``@pytest.mark.needs("kicad")``; the matching fixtures (``kicad_cli``,
   ``page``) gate themselves. On a machine without the tool the test skips
   with a reason naming the fix. With ``--strict-tools`` (or
   ``MINIBADGE_STRICT_TOOLS=1``) the same situation is a hard failure, so CI
   can never go green on a suite that quietly skipped its only real
   validation -- there are mutations in this codebase that *only*
   ``test_kicad_integration.py`` catches.

   Probes **run the tool**; they never stat a path. Two measured reasons:
   a path probe for kicad-cli that does not consult ``$KICAD_CLI`` skips on a
   box where the app works (the Dockerfile sets exactly that variable), and a
   Playwright install can have chromium on disk at the expected location and
   still refuse to launch because the revision does not match the library.

2. **Fixtures** the whole suite shares -- the frozen API:

   ==================  ====================================================
   ``client``          Flask test client, app config restored afterwards
   ``board(**kw)``     -> str, generated .kicad_pcb text for a spec
   ``board_dir(**kw)`` -> Path, a written KiCad project dir, for kicad-cli
   ``params(**over)``  -> dict, the default ``/generate`` payload
   ``logo_png``        canonical PNG upload bytes
   ``logo_svg``        canonical SVG upload bytes
   ``kicad_cli``       path to a working kicad-cli 9.x, or skip
   ``page``            Playwright page with console/pageerror capture wired
   ==================  ====================================================

3. **Asset guards.** The bundled TTFs and ``static/vendor`` are gitignored and
   fetched by ``scripts/fetch_assets.py``. A fresh clone therefore has neither,
   and tests that touch them used to die -- one with a bare
   ``FileNotFoundError``, one with ``assert 400 == 200``. The
   ``pytest_runtest_call`` wrapper turns both into a skip whose message names
   the fetch script, so a fresh clone is green.

4. **Tier bookkeeping and the end-of-run summary.** Unmarked tests are the
   fast tier, and tier markers are *derived* from the fixtures a test resolves
   so a forgotten decorator cannot leak a subprocess into it. Three things are
   reported at the end of every run, because each is a way a suite goes green
   while something real is switched off:

   * ``external tools`` -- what was missing, why, and how many tests it took
     with it. Includes ``UNGATED`` entries: a test that skipped *itself* with
     a module-level ``skipif`` naming a tool, which the probe cannot see.
   * ``xfail health`` -- an xfail that never reached its own assertion,
     because ``xfail`` turns *any* failure green, a broken fixture included.
   * ``fast-tier budget`` -- unmarked tests that drifted over the budget.

   The first two become hard failures under ``--strict-tools``; the third is
   advisory and never fails the build.

   **``--strict-tools`` is cheap, and here is the measurement, because it was
   once reported as unaffordable and re-tiering it on that number would have
   been a mistake.** Serial, this machine, tools present, re-measured against
   the 417-passing suite (``tests/*.py`` byte-identical across all six runs):

   ============================  ==========  ==============  ==============
   run                           normal      --strict-tools  delta
   ============================  ==========  ==============  ==============
   full suite (417 + 44 xfail)   59.14 s     59.89 s         +0.75 s (1.3 %)
   fast tier (385 + 23 xfail)     6.83 s      6.91 s         +0.08 s (1.2 %)
   tests/test_properties.py      14.63 s     14.94 s         +0.31 s (2.1 %)
   full, both tools disabled     33.06 s     32.83 s         -0.23 s (noise)
   ============================  ==========  ==============  ==============

   The flag stays in the **full** tier: 59.9 s of a 90 s budget. It cannot
   cost much by construction: it never *enables* a test, it only changes what
   a skip is reported as, so no strict run executes work a normal run skips.
   The last row is the proof -- with both tools forced missing, 31 tests skip
   normally and the *same* 31 error under strict, in the same wall time.

   None of the 44 ``xfail(strict=True)`` defect markers fires under the flag:
   a strict run reports the identical ``417 passed, 44 xfailed``. That is the
   property that makes it usable at all, since a suite documenting live
   defects is the normal state here.

   The 83-85 s once attributed to it is *not* this machinery: the file that
   number came from now runs in 14.63 s and the flag is 0.31 s of that. The
   likeliest culprit is Hypothesis **shrinking** -- see ``test_properties.py``
   around the ``_NO_SHRINK`` definition, which measures one property at
   102.7 s with shrinking and 1.5 s without -- but that is an inference, not a
   re-run: ``@HTTP``/``@HEAVY`` bind their settings at import, so shrinking
   cannot be put back from a ``-p`` plugin to reproduce the old number. What
   *is* re-measured is the table above. A strict and a non-strict run taken
   either side of the ``phases`` change are not comparable at all. If
   ``--strict-tools`` ever looks expensive again, time the *file*, not the
   flag: at a DRC or HTTP tier where one example costs 0.5-1.5 s, ``phases``
   is worth ~100x and this flag ~1 %.

There is deliberately **no session-scoped board cache**. It was measured:
``generate_pcb`` runs 122 times per full suite with 109 distinct argument
sets, so a perfect memo saves 0.09 s of 14 s (0.6 %). ``BadgeSpec``, ``Led``,
``ArtLayer`` and ``Text`` are all *mutable* dataclasses, and a shared mutable
fixture is the most common source of order-dependent flakes in a pytest suite.
0.6 % is not worth that. Every fixture here hands out fresh objects.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from minibadge_designer import pcb

REPO_ROOT = Path(__file__).resolve().parent.parent

# Wall-clock budget, in seconds, for a single unmarked (fast-tier) test.
# Measured basis: 120 of the 141 existing tests run in under 5 ms, and the
# whole non-kicad set runs in ~2.6 s. Anything over this should carry
# @pytest.mark.slow so `-m "not slow"` stays honest.
FAST_TEST_BUDGET_S = 0.25

# Directories populated by scripts/fetch_assets.py and gitignored, so a fresh
# clone has neither. Anything that blows up reaching into one of these skips.
FETCHED_ASSET_DIRS = (
    Path(pcb.__file__).parent / "fonts",
    Path(pcb.__file__).parent / "static" / "vendor",
)
FETCH_HINT = "run `python scripts/fetch_assets.py` to download it"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    group = parser.getgroup("minibadge")
    group.addoption(
        "--strict-tools",
        action="store_true",
        default=os.environ.get("MINIBADGE_STRICT_TOOLS", "") not in ("", "0"),
        help="Treat a missing external tool as a failure instead of a skip. "
             "Use in CI so a broken KiCad/browser/asset install cannot pass "
             "as green. Also settable as MINIBADGE_STRICT_TOOLS=1.",
    )
    group.addoption(
        "--fast-budget",
        type=float,
        default=FAST_TEST_BUDGET_S,
        help="Seconds an unmarked (fast-tier) test may take before it is "
             "reported as over budget at the end of the run.",
    )


# ---------------------------------------------------------------------------
# Tool probes -- each one RUNS the tool. A path check is not a probe.
# ---------------------------------------------------------------------------

class ToolStatus:
    """Result of probing one external tool. Falsy when the tool is unusable.

    **Immutable**, and that is load-bearing rather than tidy. One of these is
    computed once and then read by every later test in the session, so a test
    that could write to it would decide, from inside the run, whether the run
    is allowed to notice a missing tool -- the exact shape of D15 (a gate the
    gated party can move). ``st.path = "/anything"`` raises.
    """

    __slots__ = ("name", "path", "reason", "version")

    def __init__(self, name, path=None, reason="", version=""):
        for k, v in (("name", name), ("path", path),
                     ("reason", reason), ("version", version)):
            object.__setattr__(self, k, v)

    def __setattr__(self, key, value):
        raise AttributeError(
            f"ToolStatus is immutable: refusing to set {key!r} on {self!r}. "
            "The probe result is shared by every test in the session; a test "
            "that can rewrite it can switch off the tool gate from inside the "
            "run it is being gated by.")

    def __delattr__(self, key):
        self.__setattr__(key, None)

    def __bool__(self):
        return self.path is not None

    def __repr__(self):
        state = f"ok {self.path}" if self else f"missing ({self.reason})"
        return f"<{self.name}: {state}>"


def _probe_kicad() -> ToolStatus:
    """Find kicad-cli the way the *app* does, then prove the binary runs.

    Delegating to ``webapp._kicad_cli`` is the whole point. A second locator
    in the test tree drifts: the one in test_kicad_integration.py checks
    ``shutil.which`` and a hardcoded macOS path but never ``$KICAD_CLI``,
    which the app consults *first* and which the Dockerfile sets -- so 14
    tests skip green inside the container where the feature demonstrably
    works. One locator, owned by the app.
    """
    from minibadge_designer import webapp

    cli = webapp._kicad_cli()
    if cli is None:
        return ToolStatus("kicad", reason=(
            "kicad-cli not found in $KICAD_CLI, on PATH, or at the known "
            "install locations (webapp._kicad_cli found nothing)"))
    try:
        out = subprocess.run([cli, "version"], capture_output=True,
                             text=True, timeout=60, check=False)
    except OSError as exc:
        return ToolStatus("kicad", reason=f"{cli} is not executable: {exc}")
    except subprocess.TimeoutExpired:
        return ToolStatus("kicad", reason=f"{cli} version timed out")
    if out.returncode != 0:
        return ToolStatus("kicad", reason=(
            f"{cli} version exited {out.returncode}: {out.stderr.strip()[:200]}"))
    ver = out.stdout.strip()
    if not ver.startswith("9."):
        # Generated boards resolve 3D models through ${KICAD9_3DMODEL_DIR}.
        return ToolStatus("kicad", reason=f"KiCad {ver} found, but 9.x is required")
    return ToolStatus("kicad", path=cli, version=ver)


def _probe_browser() -> ToolStatus:
    """Launch a browser for real. Presence on disk proves nothing.

    Playwright pins a browser revision. A machine can have
    ``~/Library/Caches/ms-playwright/chromium-1217`` fully installed while
    the library demands 1234; a path-based probe calls that "available" and
    every browser test then *errors* instead of skipping. Falling back to the
    system Chrome/Edge channel is what actually works in that state, so the
    probe records which one launched and the `page` fixture reuses it.

    Cost, measured: 637 ms, once per session, and only when a browser test is
    actually selected. The `browser` fixture then launches a *second* Chromium,
    so that 637 ms is duplicated work -- and it is deliberately left duplicated.
    Handing the probe's browser to the fixture means holding a playwright
    driver process open across the whole session with a teardown hook to match,
    which is a new failure mode for 1.1 % of a full run. The probe's launch is
    also the only one that happens *before* any test body, which is what makes
    a mismatched-revision install a clean skip instead of nine errors.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ToolStatus("browser", reason='playwright is not installed '
                                            '(pip install -e ".[dev]")')
    detail = ""
    try:
        with sync_playwright() as p:
            for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
                try:
                    b = p.chromium.launch(**kwargs)
                except Exception as exc:  # noqa: BLE001 - any launch failure
                    detail = detail or str(exc).strip().splitlines()[0][:160]
                    continue
                ver = b.version
                b.close()
                return ToolStatus("browser", path=kwargs.get("channel", "bundled"),
                                  version=ver)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - env dependent
        return ToolStatus("browser", reason=f"playwright driver failed to start: {exc}")
    return ToolStatus("browser", reason=(
        "no launchable chromium: the bundled build is missing or its revision "
        "does not match the installed playwright (`playwright install chromium`) "
        f"and no system Chrome/Edge channel is present. First error: {detail}"))


def _probe_assets() -> ToolStatus:
    """The bundled TTFs and vendored JS are fetched, not committed."""
    fonts, vendor = FETCHED_ASSET_DIRS
    ttfs = sorted(fonts.glob("*.ttf")) if fonts.is_dir() else []
    js = sorted(vendor.glob("*.js")) if vendor.is_dir() else []
    missing = []
    if not ttfs:
        missing.append(f"no TTFs in {fonts}")
    if not js:
        missing.append(f"no vendored JS in {vendor}")
    if missing:
        return ToolStatus("assets", reason=f"{'; '.join(missing)} -- {FETCH_HINT}")
    return ToolStatus("assets", path=str(fonts.parent),
                      version=f"{len(ttfs)} faces, {len(js)} vendor file(s)")


_PROBES = {
    "kicad": _probe_kicad,
    "browser": _probe_browser,
    "assets": _probe_assets,
}

class _ProbeCache(dict):
    """Write-once map of tool name -> ToolStatus.

    Probing is session-scoped because it has to be: the kicad probe spawns
    ``kicad-cli version`` and the browser probe launches a real Chromium, and
    re-running either per test would cost more than the tests do. Round 1's
    warning about session-scoped state is about state a *test* can reach; this
    is the fix for that half.

    Measured over a full ``--strict-tools`` run (417 tests), by wrapping
    ``_PROBES`` and counting: **each probe runs exactly once** -- browser
    710 ms, kicad 120 ms, assets 1 ms, 831 ms in total, 1.4 % of the run. On
    the fast tier only ``assets`` probes at all (0.2 ms); `kicad` and `browser`
    are deselected, so neither is ever touched.

    A first write for a tool is allowed. Rebinding, deleting, clearing or
    ``update()``-ing an existing entry raises, so the two ways a cache turns
    into an order-dependent flake are both closed:

    * test A poisons the entry and test B silently inherits it (the run's
      answer to "is kicad here?" then depends on collection order);
    * a test clears the cache so the next probe re-reads
      ``$MINIBADGE_DISABLE_TOOLS`` under whatever ``monkeypatch.setenv`` is
      live at that instant, and two identical tests disagree.

    The entries themselves are immutable ``ToolStatus`` objects, so the pair
    gives the whole property: **once probed, no test can change what this
    session believes about an external tool by accident.** Twelve mutation
    vectors were tried against a seeded cache and all twelve raise:
    ``st.path = x``, ``st.reason = x``, ``del st.path``,
    ``st.__dict__[...] = x`` (``__slots__``, so there is no ``__dict__``),
    and ``cache[k] = x`` / ``clear`` / ``pop`` / ``popitem`` / ``update`` /
    ``setdefault`` / ``del cache[k]``.

    Two ways through remain and are named here rather than papered over,
    because "no test can" would be a stronger claim than Python supports:
    ``object.__setattr__(st, ...)`` (the constructor's own back door) and
    rebinding the module global, ``monkeypatch.setattr(conftest,
    "_STATUS_CACHE", {})``. Neither can happen by accident -- they are the
    difference between a stray assignment and a deliberate act -- and no
    guard here would survive a test that simply monkeypatched
    ``tool_status`` itself. What is closed is the accident that turns into an
    order-dependent flake.
    """

    def __setitem__(self, key, value):
        if key in self:
            raise RuntimeError(
                f"the {key!r} tool probe already ran this session and returned "
                f"{self[key]!r}; refusing to rebind it to {value!r}. Probe "
                f"results are write-once so that a test cannot change what a "
                f"later test believes about an external tool. To exercise the "
                f"missing-tool path, start the process with "
                f"MINIBADGE_DISABLE_TOOLS={key}.")
        super().__setitem__(key, value)

    def _refuse(self, *_args, **_kw):
        raise RuntimeError(
            "tool probe results are write-once; clearing them would let the "
            "next probe re-read $MINIBADGE_DISABLE_TOOLS under a monkeypatched "
            "environment, so two identical tests could disagree about whether "
            "kicad-cli exists. Use MINIBADGE_DISABLE_TOOLS at process start.")

    clear = pop = popitem = update = setdefault = __delitem__ = _refuse


_STATUS_CACHE: dict[str, ToolStatus] = _ProbeCache()
_SKIP_LOG: dict[str, list[str]] = {}


def tool_status(name: str) -> ToolStatus:
    """Probe ``name`` once per session and cache the result.

    Once per session, and lazily: the probe fires the first time a *selected*
    test needs the tool, so the fast tier (which deselects `kicad` and
    `browser`) pays nothing at all -- measured, it prints no `external tools`
    section because neither probe ever ran.

    ``MINIBADGE_DISABLE_TOOLS=kicad,browser`` forces a tool to report missing.
    That is how you exercise the skip path on a machine that has everything --
    and how CI runs a deliberate no-KiCad job to prove the library-only tests
    do not secretly depend on it. It is read at probe time and then frozen; see
    ``_ProbeCache`` for why it is not re-read per test.
    """
    if name not in _PROBES:
        raise pytest.UsageError(
            f"unknown tool {name!r} in a needs() marker; known tools: "
            f"{', '.join(sorted(_PROBES))}")
    if name not in _STATUS_CACHE:
        disabled = {s.strip() for s in
                    os.environ.get("MINIBADGE_DISABLE_TOOLS", "").split(",") if s.strip()}
        _STATUS_CACHE[name] = (
            ToolStatus(name, reason="disabled via $MINIBADGE_DISABLE_TOOLS")
            if name in disabled else _PROBES[name]())
    return _STATUS_CACHE[name]


def require(name: str, config=None) -> ToolStatus:
    """Skip (or, under --strict-tools, fail) unless tool ``name`` is usable.

    The one gate. Markers, fixtures and asset guards all funnel through here
    so that *every* tool skip is recorded and shown in the terminal summary --
    a skip nobody reads is how a suite goes green with its only real oracle
    switched off.
    """
    st = tool_status(name)
    if st:
        return st
    strict = _strict(config)
    msg = f"{name} unavailable: {st.reason}"
    frame = _current_nodeid()
    _SKIP_LOG.setdefault(name, []).append(frame)
    if strict:
        pytest.fail(f"[--strict-tools] {msg}", pytrace=False)
    pytest.skip(msg)


def _strict(config=None) -> bool:
    if config is not None:
        return bool(config.getoption("--strict-tools"))
    return _STRICT[0]


_STRICT = [os.environ.get("MINIBADGE_STRICT_TOOLS", "") not in ("", "0")]
_CURRENT = [""]


def _current_nodeid() -> str:
    return _CURRENT[0] or "<unknown test>"


def pytest_configure(config):
    _STRICT[0] = bool(config.getoption("--strict-tools"))


def pytest_runtest_protocol(item, nextitem):
    _CURRENT[0] = item.nodeid


def pytest_runtest_setup(item):
    for mark in item.iter_markers(name="needs"):
        for name in mark.args:
            require(name, item.config)


# -- automatic tier markers -------------------------------------------------
# A tier boundary that depends on the author remembering a decorator is a tier
# boundary that leaks. It already had: test_kicad_integration.py carries no
# `kicad` marker at all, so `-m "not kicad"` still ran all 14 of its DRC
# subprocesses and the "fast" tier was 6x its budget. So the marker is derived
# from what the test actually *does* -- which fixtures it resolves -- and only
# ever added, never removed. An explicit marker in the test always wins.

_FIXTURE_TIER = {
    "kicad_cli": ("kicad", "kicad"),
    "run_drc": ("kicad", "kicad"),
    "browser": ("browser", "browser"),
    "page": ("browser", "browser"),
    "live_server": ("browser", None),
    "client": ("webapp", None),
    "post_generate": ("webapp", None),
}

# Bridge for files that predate the markers and cannot be edited from here.
_MODULE_TIER = {
    "test_kicad_integration": ("kicad", "kicad"),
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        present = {m.name for m in item.iter_markers()}
        needed = {n for m in item.iter_markers(name="needs") for n in m.args}
        fixtures = set(getattr(item, "fixturenames", ()))
        rules = [_FIXTURE_TIER[f] for f in fixtures if f in _FIXTURE_TIER]
        mod = item.module.__name__.rsplit(".", 1)[-1] if item.module else ""
        if mod in _MODULE_TIER:
            rules.append(_MODULE_TIER[mod])
        for mark_name, tool in rules:
            if mark_name not in present:
                item.add_marker(getattr(pytest.mark, mark_name))
                present.add(mark_name)
            if tool is not None and tool not in needed:
                item.add_marker(pytest.mark.needs(tool))
                needed.add(tool)


# -- foreign skips ----------------------------------------------------------
# The gate above only sees tests that opt in. A module that writes its own
# `pytestmark = pytest.mark.skipif(shutil.which(...) is None, ...)` bypasses it
# entirely and vanishes from the run in total silence -- which is exactly what
# test_kicad_integration.py does, and there are mutations (every resistor
# wired to ground, so no LED can ever light) that *only* that file catches.
# So: any skip whose reason names an external tool is counted here too, shown
# in the terminal summary, and promoted to a failure under --strict-tools.

_FOREIGN_SKIP_HINTS = {
    "kicad": ("kicad", "kicad-cli", "pcbnew"),
    "browser": ("playwright", "browser", "chromium", "chrome"),
    "assets": ("font", "ttf", "fetch_assets", "vendor"),
}
_FOREIGN_SKIPS: dict[str, list[tuple[str, str]]] = {}


def _classify_skip(reason: str) -> str | None:
    low = reason.lower()
    for tool, hints in _FOREIGN_SKIP_HINTS.items():
        if any(h in low for h in hints):
            return tool
    return None


# ---------------------------------------------------------------------------
# Fetched-asset guard: a fresh clone must be green
# ---------------------------------------------------------------------------

def _is_fetched_asset(path: str | None) -> bool:
    if not path:
        return False
    try:
        p = Path(path).resolve()
    except OSError:  # pragma: no cover - defensive
        return False
    return any(p.is_relative_to(d.resolve()) for d in FETCHED_ASSET_DIRS)


def _asset_tokens() -> set[str]:
    """Lowercase strings whose presence in a test body means "needs a TTF"."""
    from minibadge_designer import textpoly

    toks = {"/fonts/", ".ttf", "static/vendor", "model-viewer"}
    toks.update(k.lower() for k in textpoly.FONTS)
    return toks


def _mentions_fetched_asset(item) -> bool:
    import inspect

    try:
        src = inspect.getsource(item.function).lower()
    except (OSError, TypeError, AttributeError):  # pragma: no cover - defensive
        return False
    return any(t in src for t in _asset_tokens())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Turn "the fetched assets are not here" into a skip, not a failure.

    ``minibadge_designer/fonts/*.ttf`` and ``static/vendor/`` are gitignored
    and downloaded by ``scripts/fetch_assets.py``, so a fresh clone has
    neither and two tests died there -- one with a bare ``FileNotFoundError``
    naming a path and nothing else, the other with ``assert 400 == 200``
    because the app catches the missing font and answers with a 400. The
    single worst first impression a repo can make. Now the same clone skips
    both, with the command that fixes it.

    Two gates, both narrow, and **both cost nothing when the assets are
    present** -- the second one never even looks:

    * ``OSError`` whose filename resolves *inside* a fetched-asset directory.
    * any failure in a test whose own source names a bundled font, ``.ttf``,
      ``/fonts/`` or the vendored JS -- but only while the ``assets`` probe
      says they are absent. A test that merely mentions a font name and still
      passes (``test_index_lists_fonts`` reads the FONTS dict, not the files)
      is untouched, because only *failing* tests are examined.

    A missing file anywhere else is still a real failure. Under
    ``--strict-tools`` these are failures again, because CI must not pass
    with half the font pipeline untested.
    """
    try:
        yield
    except Exception as exc:
        by_path = isinstance(exc, OSError) and _is_fetched_asset(
            getattr(exc, "filename", None))
        if not by_path:
            st = tool_status("assets")
            if st or not _mentions_fetched_asset(item):
                raise
        st = tool_status("assets")
        reason = st.reason or f"{getattr(exc, 'filename', '?')} is missing -- {FETCH_HINT}"
        _SKIP_LOG.setdefault("assets", []).append(item.nodeid)
        if item.config.getoption("--strict-tools"):
            pytest.fail(f"[--strict-tools] fetched assets unavailable: {reason}\n"
                        f"underlying failure: {type(exc).__name__}: {exc}",
                        pytrace=False)
        pytest.skip(f"fetched assets unavailable: {reason}")


@pytest.fixture(autouse=True)
def _no_repo_writes(request):
    """Fail loudly if a test drops a file into the repository root.

    Cheap insurance with a real precedent: ``*.rpt`` is gitignored, so the
    stray ``helmet-drc.rpt`` sitting in the repo root has been invisible to
    ``git status`` since the day it appeared. A DRC report written to the repo
    instead of ``tmp_path`` would be too. Write outputs to ``tmp_path``.
    """
    before = {p.name for p in REPO_ROOT.iterdir()}
    yield
    new = {p.name for p in REPO_ROOT.iterdir()} - before
    assert not new, (
        f"{request.node.nodeid} wrote into the repo root: {sorted(new)} -- "
        f"use the tmp_path fixture (and note *.rpt is gitignored, so a stray "
        f"DRC report would never show up in `git status`)")


# ---------------------------------------------------------------------------
# Tier bookkeeping + the end-of-run summary
# ---------------------------------------------------------------------------

_OVER_BUDGET: list[tuple[str, float]] = []
_TIERED_MARKS = {"slow", "kicad", "browser", "visual", "heavy"}

# xfail tests that never got as far as the assertion they were written around.
# (nodeid, phase, detail)
_XFAIL_NOTES: list[tuple[str, str, str]] = []


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call" and report.passed:
        budget = item.config.getoption("--fast-budget")
        if report.duration > budget and not _TIERED_MARKS.intersection(
                m.name for m in item.iter_markers()):
            _OVER_BUDGET.append((item.nodeid, report.duration))
    # -- xfail that never reached its assertion ----------------------------
    # A round-2 agent shipped nine xfail(strict=True) tests documenting known
    # defects, and its first run reported "4 xfailed, 5 errors" while *every
    # one* of them had died in fixture setup. Four of them looked healthy and
    # had not executed a single line of their own body. An xfail marker turns
    # any failure into a green tick, including a failure that has nothing to
    # do with the defect being documented -- so an xfail resolved outside the
    # call phase is a silent false green, and gets its own report.
    if hasattr(report, "wasxfail"):
        if report.when != "call":
            _XFAIL_NOTES.append((
                item.nodeid, report.when,
                (f"the xfail was resolved during {report.when}, so the test body"
                 " never ran -- it is documenting a fixture error, not the defect")))
            if item.config.getoption("--strict-tools"):
                report.outcome = "failed"
                report.longrepr = (
                    f"[--strict-tools] {item.nodeid} is marked xfail but failed "
                    f"during {report.when}: the assertion it was written around "
                    f"never executed.\n{report.longrepr}")
        elif call.excinfo is not None and not call.excinfo.errisinstance(
                AssertionError):
            _XFAIL_NOTES.append((
                item.nodeid, "call",
                (f"failed with {call.excinfo.typename}, not an assertion -- check"
                 " this is the defect it claims and not a broken test")))

    # `report.skipped` is also true for xfail, whose longrepr is the test
    # source, not a reason string -- classifying that matched "kicad" inside a
    # `run_drc` traceback and reported a real xfail as an ungated tool skip.
    # Only the (path, lineno, reason) 3-tuple form is a genuine skip.
    if report.skipped and not hasattr(report, "wasxfail") and \
            isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
        reason = str(report.longrepr[2])
        # Skips this file issued are already accounted for by require().
        if not any(item.nodeid in v for v in _SKIP_LOG.values()):
            tool = _classify_skip(reason)
            if tool is not None:
                _FOREIGN_SKIPS.setdefault(tool, []).append((item.nodeid, reason))
                if item.config.getoption("--strict-tools"):
                    report.outcome = "failed"
                    report.longrepr = (
                        f"[--strict-tools] {item.nodeid} skipped itself because an "
                        f"external tool ({tool}) looked unavailable: {reason}\n"
                        f"A module-level skipif is invisible in CI. Use "
                        f"@pytest.mark.needs({tool!r}) so the probe (and this flag) "
                        f"can see it.")
    return report


# -- xdist bridge -----------------------------------------------------------
# Both summaries are built in the process that ran the tests. Under xdist that
# is a worker, while pytest_terminal_summary runs in the controller -- without
# this the tool report silently disappears exactly when you are least likely
# to notice, which is the failure mode this file exists to prevent.

def pytest_sessionfinish(session):
    out = getattr(session.config, "workeroutput", None)
    if out is not None:
        out["minibadge_tools"] = {
            n: (s.path, s.reason, s.version, len(_SKIP_LOG.get(n, [])))
            for n, s in _STATUS_CACHE.items()}
        out["minibadge_over_budget"] = _OVER_BUDGET
        out["minibadge_foreign_skips"] = _FOREIGN_SKIPS
        out["minibadge_xfail_notes"] = _XFAIL_NOTES


@pytest.hookimpl(optionalhook=True)  # xdist-only hook; without optionalhook
def pytest_testnodedown(node, error):  # pluggy refuses to load this plugin
    data = getattr(node, "workeroutput", {}) or {}
    for name, (path, reason, version, nskip) in data.get("minibadge_tools", {}).items():
        if name not in _STATUS_CACHE:
            _STATUS_CACHE[name] = ToolStatus(name, path, reason, version)
        if nskip:
            _SKIP_LOG.setdefault(name, []).extend(
                [f"<worker {node.gateway.id}>"] * nskip)
    known = {n for n, _ in _OVER_BUDGET}
    for nodeid, dur in data.get("minibadge_over_budget", []):
        if nodeid not in known:
            _OVER_BUDGET.append((nodeid, dur))
    seen_x = {n for n, _, _ in _XFAIL_NOTES}
    for note in data.get("minibadge_xfail_notes", []):
        if note[0] not in seen_x:
            _XFAIL_NOTES.append(tuple(note))
    for tool, items in data.get("minibadge_foreign_skips", {}).items():
        seen = {n for n, _ in _FOREIGN_SKIPS.get(tool, [])}
        for nodeid, reason in items:
            if nodeid not in seen:
                _FOREIGN_SKIPS.setdefault(tool, []).append((nodeid, reason))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tr = terminalreporter
    strict = config.getoption("--strict-tools")
    if _STATUS_CACHE or _FOREIGN_SKIPS:
        tr.write_sep("-", "external tools")
        for name, st in sorted(_STATUS_CACHE.items()):
            n = len(_SKIP_LOG.get(name, []))
            if st:
                tr.write_line(f"  {name:8s} OK       {st.path} {st.version}")
            else:
                verb = "FAILED" if strict else "skipped"
                tr.write_line(f"  {name:8s} MISSING  {n} test(s) {verb} -- {st.reason}")
        for name, items in sorted(_FOREIGN_SKIPS.items()):
            verb = "FAILED" if strict else "skipped"
            tr.write_line(f"  {name:8s} UNGATED  {len(items)} test(s) {verb} "
                          f"themselves -- {items[0][1][:80]}")
            tr.write_line(f"           first: {items[0][0]}")
            tr.write_line(f"           (module-level skipif, not @pytest.mark.needs"
                          f"({name!r}) -- invisible to the probe)")
        if not strict and (any(not st for st in _STATUS_CACHE.values()) or _FOREIGN_SKIPS):
            tr.write_line("  (a green run with a MISSING/UNGATED tool is not a green "
                          "suite: re-run with --strict-tools to make these failures)")
    if _XFAIL_NOTES:
        tr.write_sep("-", "xfail health")
        tr.write_line(f"  {len(_XFAIL_NOTES)} xfail(ed) test(s) did not fail the way "
                      f"they were written to:")
        for nodeid, phase, detail in _XFAIL_NOTES[:10]:
            tr.write_line(f"    {nodeid}")
            tr.write_line(f"      {detail}")
        if not strict:
            tr.write_line("  (an xfail turns ANY failure green, including a broken "
                          "fixture. Verify with: pytest --runxfail --tb=short)")
    if _OVER_BUDGET:
        tr.write_sep("-", "fast-tier budget")
        tr.write_line(f"  {len(_OVER_BUDGET)} unmarked test(s) over "
                      f"{config.getoption('--fast-budget')}s -- add @pytest.mark.slow:")
        for nodeid, dur in sorted(_OVER_BUDGET, key=lambda kv: -kv[1])[:10]:
            tr.write_line(f"    {dur:6.2f}s  {nodeid}")


# ---------------------------------------------------------------------------
# Flask fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def flask_app():
    """The app singleton with TESTING on, config restored afterwards.

    ``webapp.app`` is a module-level object, so flipping config on it leaks
    into every later test in the process. Save and restore, so test order
    cannot change behaviour.
    """
    from minibadge_designer.webapp import app

    saved = dict(app.config)
    app.config["TESTING"] = True
    try:
        yield app
    finally:
        app.config.clear()
        app.config.update(saved)


@pytest.fixture
def client(flask_app):
    """Flask test client. Pair with ``@pytest.mark.webapp``."""
    return flask_app.test_client()


# ---------------------------------------------------------------------------
# Payload / spec factories
#
# D6 ruling: parameter-space coverage is the highest-yield rule of the round.
# Every existing example test used size="0805"; a board-shorting defect hid in
# exactly that parameter on lines with 100 % line coverage. So these factories
# take every knob -- size, reverse, layout, side, rot, adv, material, pins --
# and make varying them a keyword, not a rewrite.
# ---------------------------------------------------------------------------

DEFAULT_LEDS = (
    {"x": 6.5, "y": 6.0, "color": "red"},
    {"x": 14.0, "y": 6.0, "color": "blue"},
)


def build_params(**over) -> dict:
    """The default ``/generate`` payload, as a plain dict.

    Three near-identical ``_params()`` helpers already exist across the test
    files and have drifted apart; each divergence is a coverage hole nobody
    notices. This is the one that ships.

    Fresh lists/dicts every call: the caller may mutate what it gets back.
    """
    params = {
        "name": over.pop("name", "test-badge"),
        "mask_color": over.pop("mask_color", "green"),
        "finish": over.pop("finish", "enig"),
        "leds": [dict(led) for led in over.pop("leds", DEFAULT_LEDS)],
        "texts": [dict(t) for t in over.pop("texts", ())],
        "art": [dict(a) for a in over.pop("art", ())],
    }
    pins = over.pop("pins", None)
    if pins is not None:
        params["pins"] = list(pins)
    params.update(over)
    return params


@pytest.fixture
def params():
    """``params(**over) -> dict`` -- the default ``/generate`` payload."""
    return build_params


def _led(v):
    return v if isinstance(v, pcb.Led) else pcb.Led(**v)


def _text(v):
    return v if isinstance(v, pcb.Text) else pcb.Text(**v)


def _art(v):
    return v if isinstance(v, pcb.ArtLayer) else pcb.ArtLayer(**v)


def build_spec(**over) -> pcb.BadgeSpec:
    """Build a ``BadgeSpec`` from a short, readable description.

    ``leds``, ``texts`` and ``art`` accept dataclasses *or* plain dicts, so a
    test can write ``build_spec(leds=[{"x": 6, "y": 6, "size": "1206"}])``
    without importing ``Led``. ``n_leds`` asks for that many stock LEDs.
    Any other keyword is set on the ``BadgeSpec`` directly, so unknown-field
    typos raise instead of being silently ignored.

    Every call constructs fresh objects. ``BadgeSpec``/``Led``/``ArtLayer``/
    ``Text`` are unfrozen dataclasses, so a shared instance would make
    failures order dependent. Nothing here is cached.
    """
    n_leds = over.pop("n_leds", None)
    leds = over.pop("leds", None)
    if leds is None:
        leds = DEFAULT_LEDS if n_leds is None else DEFAULT_LEDS[:n_leds]
    spec = pcb.BadgeSpec(
        name=over.pop("name", "test-badge"),
        leds=[_led(v) for v in leds],
        texts=[_text(v) for v in over.pop("texts", ())],
        art=[_art(v) for v in over.pop("art", ())],
    )
    for key, val in over.items():
        if not hasattr(spec, key):
            raise TypeError(f"BadgeSpec has no field {key!r}")
        setattr(spec, key, val)
    return spec


@pytest.fixture
def make_spec():
    """``make_spec(**kw) -> BadgeSpec``. Same keywords as the `board` fixture."""
    return build_spec


def build_board(**spec_kwargs) -> str:
    """Generated ``.kicad_pcb`` text for ``build_spec(**spec_kwargs)``."""
    return pcb.generate_pcb(build_spec(**spec_kwargs))


@pytest.fixture
def board():
    """``board(**spec_kwargs) -> str`` -- generated .kicad_pcb text.

    Not cached, on purpose: measured, a perfect session memo across the whole
    suite saves 0.09 s of 14 s. A single call is ~7 ms.
    """
    return build_board


def write_project(directory: Path, **spec_kwargs) -> Path:
    """Write a full KiCad project into ``directory``; return the directory.

    The returned path carries ``.pcb`` / ``.pro`` attributes so callers do not
    have to re-derive the slug, and is a plain ``Path`` otherwise.
    """
    spec = build_spec(**spec_kwargs)
    name = spec.name
    d = _ProjectDir(directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.kicad_pcb").write_text(pcb.generate_pcb(spec))
    (d / f"{name}.kicad_pro").write_text(pcb.generate_project(name))
    d._stem = name
    return d


class _ProjectDir(type(Path())):  # type: ignore[misc]
    """A directory that knows which board file it holds."""

    _stem = "test-badge"

    @property
    def pcb(self) -> Path:
        return Path(self) / f"{self._stem}.kicad_pcb"

    @property
    def pro(self) -> Path:
        return Path(self) / f"{self._stem}.kicad_pro"


@pytest.fixture
def board_dir(tmp_path):
    """``board_dir(**spec_kwargs) -> Path`` -- a written KiCad project dir.

    Feed it to kicad-cli. ``d.pcb`` is the ``.kicad_pcb`` inside it and
    ``d.pro`` the project file; the directory is named after the board, and
    each call gets its own subdirectory of ``tmp_path`` so DRC reports and
    fill caches from one board never leak into another.

    Nothing is written into the repository. ``*.rpt`` is gitignored, so a DRC
    report dropped next to the source would be invisible forever.
    """
    seq = [0]

    def _dir(**spec_kwargs) -> Path:
        seq[0] += 1
        name = spec_kwargs.get("name", "test-badge")
        return write_project(tmp_path / f"{seq[0]:02d}-{name}", **spec_kwargs)
    return _dir


# ---------------------------------------------------------------------------
# Upload fixtures
# ---------------------------------------------------------------------------

def build_logo_png(draw_fn=None, size=(120, 120), bg="white") -> bytes:
    """The canonical raster upload: a black disc on white."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    if draw_fn is None:
        w, h = size
        d.ellipse((w * .17, h * .17, w * .83, h * .83), fill="black")
    else:
        draw_fn(d)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def build_logo_svg(body: str = '<circle cx="50" cy="50" r="40" fill="#000"/>',
                   w: int = 100, h: int = 100) -> bytes:
    """The canonical vector upload: the same disc, as an SVG document."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}">{body}</svg>').encode()


@pytest.fixture
def logo_png() -> bytes:
    """Canonical PNG upload bytes (black disc on white, 120x120)."""
    return build_logo_png()


@pytest.fixture
def logo_svg() -> bytes:
    """Canonical SVG upload bytes (the same disc, as vector)."""
    return build_logo_svg()


@pytest.fixture
def make_png():
    """``make_png(draw_fn=None, size=..., bg=...) -> bytes`` for non-stock art."""
    return build_logo_png


@pytest.fixture
def make_svg():
    """``make_svg(body, w=100, h=100) -> bytes`` -- an SVG around ``body``."""
    return build_logo_svg


@pytest.fixture
def post_generate(client):
    """POST a params dict (+ optional uploads) to an endpoint.

    ``files`` maps a form field to bytes or ``(bytes, filename)``; bare bytes
    get a filename inferred from their leading magic.
    """
    def _post(params, files=None, endpoint="/generate"):
        data = {"params": json.dumps(params)}
        for field, val in (files or {}).items():
            blob, name = val if isinstance(val, tuple) else (val, None)
            if name is None:
                name = "upload.svg" if blob[:5] in (b"<svg ", b"<?xml") else "upload.png"
            data[field] = (io.BytesIO(blob), name)
        return client.post(endpoint, data=data, content_type="multipart/form-data")
    return _post


@pytest.fixture
def project_files():
    """Unpack a ``/generate`` zip response into ``{path: bytes}``."""
    def _unpack(resp):
        assert resp.status_code == 200, (
            f"/generate returned {resp.status_code}: "
            f"{resp.get_json(silent=True) or resp.data[:300]!r}")
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        return {n: zf.read(n) for n in zf.namelist()}
    return _unpack


@pytest.fixture
def response_board(project_files):
    """The ``.kicad_pcb`` text out of a ``/generate`` zip (there is exactly one)."""
    def _board(resp) -> str:
        files = project_files(resp)
        pcbs = [n for n in files if n.endswith(".kicad_pcb")]
        assert len(pcbs) == 1, f"expected one .kicad_pcb in the zip, got {pcbs}"
        return files[pcbs[0]].decode()
    return _board


# ---------------------------------------------------------------------------
# Tool fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def kicad_cli(request) -> str:
    """Path to a *working* kicad-cli 9.x, found by the app's own locator.

    Skips (or fails, under --strict-tools) when it is unusable. Pair with
    ``@pytest.mark.kicad`` so the fast tier can deselect it.
    """
    return require("kicad", request.config).path


@pytest.fixture
def run_drc(kicad_cli):
    """``run_drc(board_path, *extra) -> (returncode, report text)``.

    ~0.55 s per invocation, ~115 ms of which is bare process spawn. That cost
    is irreducible and is exactly why DRC lives in the full tier.
    """
    def _drc(board: Path, *extra):
        board = Path(board)
        report = board.with_suffix(".drc.txt")
        r = subprocess.run(
            [kicad_cli, "pcb", "drc", "--exit-code-violations",
             "-o", str(report), *extra, str(board)],
            capture_output=True, text=True, check=False)
        return r.returncode, (report.read_text() if report.exists() else r.stderr)
    return _drc


@pytest.fixture(scope="session")
def browser(request):
    """One Chromium for the whole session.

    Measured: ~0.4 s to launch, ~0.3 s for the first page, ~0.1 s for later
    ones. A per-test browser would cost ~0.7 s each; tests get a fresh
    ``page`` on this shared instance instead.
    """
    st = require("browser", request.config)
    from playwright.sync_api import sync_playwright

    kwargs = {} if st.path == "bundled" else {"channel": st.path}
    with sync_playwright() as p:
        b = p.chromium.launch(**kwargs)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser):
    """A fresh Playwright page with error capture pre-wired.

    Own context per test, so cookies and localStorage never leak between
    tests. Console errors and uncaught page exceptions are collected on
    ``page.errors`` and, unless the test claims them, **fail the test at
    teardown** -- a silent JS exception that leaves the UI half-rendered is
    otherwise invisible to a test that only asserts on the DOM.

    Call ``page.expect_errors()`` (or ``page.expect_errors("some substring")``)
    inside a test that is deliberately provoking one.
    """
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    errors: list[str] = []
    allowed: list[str] = []

    pg.errors = errors
    pg.expect_errors = lambda *substrings: allowed.extend(substrings or ("",))
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
          if m.type == "error" else None)
    pg.on("requestfailed", lambda r: errors.append(
        f"requestfailed: {r.url} ({(r.failure or '')})"))
    try:
        yield pg
    finally:
        unclaimed = [e for e in errors
                     if not any(s in e for s in allowed)] if allowed else list(errors)
        ctx.close()
        assert not unclaimed, (
            "the page reported errors the test did not claim:\n  "
            + "\n  ".join(unclaimed)
            + "\n(if this is deliberate, call page.expect_errors(<substring>))")


@pytest.fixture
def live_server(flask_app):
    """The app on a real socket, for Playwright. Yields the base URL.

    Threaded server on an ephemeral port; shut down at teardown. Browser tests
    cannot use the Flask test client -- they need a URL a browser can open.
    """
    import threading

    from werkzeug.serving import make_server

    srv = make_server("127.0.0.1", 0, flask_app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join(timeout=5)
