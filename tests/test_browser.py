"""Browser tier — the canvas editor that lives inside index.html.

`minibadge_designer/templates/index.html` is ~5,200 lines, ~4,600 of which are a
single inline classic `<script>`.  Line coverage cannot see one byte of it, so
these tests are the only thing in the repo that exercises the editor at all.

Three rules make this layer non-flaky; the long form is in
`.claude/skills/writing-tests/references/browser-tests.md`.

1.  **Assert on JS state, never on pixels.**  The script is a classic script, so
    its top-level `const`/`let` land in the global lexical scope and
    `page.evaluate` resolves them: `state`, `SCALE`, `VIEW`, `selected`,
    `activePanel`, `view`, plus `designJSON()`, `blockingProblems()`,
    `selectionInfo()`, `customActive()`, `outlineBounds()`.  Every gesture ends
    in a synchronous `draw()`, so state is authoritative the instant the gesture
    returns.  If index.html ever becomes `type="module"` every test here breaks
    at once — that is a deliberate tripwire, not a bug.
2.  **Compute mm->px in-page, at call time.**  `SCALE`, `VIEW.tx/ty` and
    `cvF.width` (440 <-> 620) all change at runtime.  `UI.board_to_client`
    inverts the app's own `boardCoords()` and refuses to emit an off-screen
    point, because a `page.mouse` event outside the viewport hits nothing and
    raises nothing.
3.  **Every test ends in `ui.assert_clean()`.**  All three editor defects this
    suite documents fail *silently*.  A flow that "passes" with a console full
    of errors is a false negative, so console errors, page errors and failed
    requests are assertions, not logs.

The suite must not be able to hang.  Defect #8 makes the FRONT tab permanently
unclickable below ~1300 px wide, and Playwright's default 30 s timeout turns
that into a 30 s stall per attempt.  Every wait here carries an explicit,
short timeout (see the `*_TIMEOUT` constants).
"""

from __future__ import annotations

import re
import zipfile

import pytest

try:
    from playwright.sync_api import TimeoutError as PWTimeout
except ImportError:  # pragma: no cover - needs("browser") skips before use
    PWTimeout = TimeoutError

# `needs("browser")` is what the conftest tool probe understands: it launches a
# browser for real and reports an honest skip.  A module-level importorskip
# would be invisible to `--strict-tools` and could hide a broken install.
pytestmark = [pytest.mark.browser, pytest.mark.needs("browser")]

# --- timeouts ---------------------------------------------------------------
# Nothing here may use Playwright's 30 s default: defect #8 would turn a blocked
# click into a 30 s stall, and a suite that hangs is worse than one that fails.
ELEMENT_TIMEOUT = 5_000       # any locator / wait_for_function on local state
UPLOAD_TIMEOUT = 10_000       # Image.onload for an uploaded bitmap
OUTLINE_TIMEOUT = 15_000      # debounced POST /outline round-trip
GENERATE_TIMEOUT = 30_000     # POST /generate, which builds a real KiCad project
BLOCKED_TIMEOUT = 2_500       # a click we EXPECT to be intercepted (defect #8)
LOAD_TIMEOUT = 20_000         # goto + `ready === true` on a cold app

# 1400x1000 keeps both canvases above the fold and clear of the toast stack.
WIDE = (1400, 1000)
# Defect #8 reproduces here: #toasts overlaps #viewtabs below ~1300 px wide.
NARROW = (1024, 768)


# ---------------------------------------------------------------------------
# fixtures
#
# `page` and `logo_png` come from tests/conftest.py.  `page` already has
# pageerror / console.error / requestfailed capture wired before navigation and
# collects them on `page.errors`; its teardown fails the test on any error the
# test did not claim.  `ui.assert_clean()` asserts the same list explicitly, in
# the test body, where the failure names the flow that produced it.
# ---------------------------------------------------------------------------
@pytest.fixture
def ui(page, live_server, tmp_path):
    u = UI(page, tmp_path)
    u.prepare()
    # conftest hands out a 1280x900 viewport, which is inside defect #8's
    # blast radius.  Every test starts wide; the ones that care go narrow.
    u.set_viewport(*WIDE)
    page.goto(live_server, timeout=LOAD_TIMEOUT)
    u.wait_ready()
    u.dismiss_restore_bar()
    return u


@pytest.fixture
def logo(logo_png):
    """The canonical upload, in memory: set_input_files takes bytes, so the
    browser tier needs no temp file on disk."""
    return {"name": "logo.png", "mimeType": "image/png", "buffer": logo_png}


# ---------------------------------------------------------------------------
# the editor driver
# ---------------------------------------------------------------------------
_BOARD_TO_CLIENT = """([mx, my, side]) => {
    // Exact inverse of the app's boardCoords(), recomputed at call time.
    const cv = side === 'back' ? cvB : cvF;
    if (cv.offsetParent === null) return {hidden: true};
    const r = cv.getBoundingClientRect();
    const sx = side === 'back' ? cv.width - VIEW.tx - mx * SCALE
                               : mx * SCALE + VIEW.tx;
    const sy = my * SCALE + VIEW.ty;
    return {x: r.left + sx * r.width / cv.width,
            y: r.top + sy * r.height / cv.height,
            vw: window.innerWidth, vh: window.innerHeight, hidden: false};
}"""


class UI:
    def __init__(self, page, downloads_dir):
        self.page = page
        self.downloads_dir = downloads_dir

    @property
    def errors(self):
        """pageerror / console.error / requestfailed, captured by the conftest
        `page` fixture before navigation.  A JS exception during init leaves a
        half-drawn UI that still looks plausible; nothing else reports it."""
        return list(getattr(self.page, "errors", []))

    # -- lifecycle ---------------------------------------------------------
    def prepare(self):
        """Cap every implicit wait.  Playwright's 30 s default would turn
        defect #8's blocked click into a 30 s stall, and a hung suite is worse
        than a failing one."""
        self.page.set_default_timeout(ELEMENT_TIMEOUT)

    def wait_ready(self):
        # `ready` is a top-level `let` in a classic <script>: not a property of
        # window, but page.evaluate runs in global scope and resolves it.
        self.page.wait_for_function(
            "() => typeof ready !== 'undefined' && ready === true", timeout=LOAD_TIMEOUT
        )

    def set_viewport(self, w, h):
        """Resize and wait on a predicate, never a sleep.  CSS reflows, so the
        toast/viewtabs overlap of defect #8 appears and disappears with this."""
        self.page.set_viewport_size({"width": w, "height": h})
        self.page.wait_for_function(
            "([w, h]) => window.innerWidth === w && window.innerHeight === h",
            arg=[w, h],
            timeout=ELEMENT_TIMEOUT,
        )

    def dismiss_restore_bar(self):
        """A reused context carries a `bm-design` autosave, whose restore bar
        covers the top of the canvas zone.  Harmless when absent."""
        bar = self.page.locator("#restorebar")
        if bar.count() and bar.is_visible():
            self.page.click("#dismissbtn")
            bar.wait_for(state="hidden", timeout=ELEMENT_TIMEOUT)

    def assert_clean(self, note=""):
        assert not self.errors, f"{note}: browser reported {self.errors}"

    # -- state readers -----------------------------------------------------
    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def design(self):
        return self.js("() => designJSON()")

    def leds(self):
        return self.js("() => JSON.parse(JSON.stringify(state.leds))")

    def art(self):
        return self.js("() => designJSON().art")

    def selected(self):
        return self.js("() => selected")

    def panel(self):
        return self.js("() => activePanel")

    def toast_texts(self):
        return self.js(
            "() => [...document.querySelectorAll('#toasts .toast')].map(t => t.textContent)"
        )

    def has_toast(self, pattern):
        # Match a short distinctive phrase; toast wording is not a contract.
        return any(re.search(pattern, t) for t in self.toast_texts())

    def wait_toast(self, pattern, timeout=ELEMENT_TIMEOUT):
        self.page.wait_for_function(
            "(p) => [...document.querySelectorAll('#toasts .toast')]"
            ".some(t => new RegExp(p).test(t.textContent))",
            arg=pattern,
            timeout=timeout,
        )

    def blocking(self):
        return self.js("() => blockingProblems()")

    # -- async settle points (there are exactly three) ----------------------
    def wait_state(self, predicate, timeout=ELEMENT_TIMEOUT):
        self.page.wait_for_function(f"() => {predicate}", timeout=timeout)

    def wait_outline(self):
        """requestOutline() nulls state.shape.rings, debounces 250 ms and drops
        out-of-order replies by sequence number.  The only correct settle
        predicate is "the server answered"."""
        self.page.wait_for_function(
            "() => !customActive()"
            " || (state.shape.rings && state.shape.rings.length)"
            " || state.shape.outlineEmpty === true",
            timeout=OUTLINE_TIMEOUT,
        )

    # -- canvas geometry ---------------------------------------------------
    def board_to_client(self, mm_x, mm_y, side):
        pt = self.js(_BOARD_TO_CLIENT, [mm_x, mm_y, side])
        if pt.get("hidden"):
            raise AssertionError(f"canvas {side} is hidden - select its view tab first")
        if not (0 <= pt["x"] <= pt["vw"] and 0 <= pt["y"] <= pt["vh"]):
            raise AssertionError(
                f"board point ({mm_x},{mm_y}) on {side} maps to "
                f"({pt['x']:.0f},{pt['y']:.0f}), outside the {pt['vw']}x{pt['vh']} "
                "viewport - scroll it in first.  A mouse event out there hits "
                "nothing and raises nothing."
            )
        return pt["x"], pt["y"]

    def scroll_into_view(self, side):
        # At <=768 px tall the BACK canvas starts below the fold of the
        # internally scrolling canvas zone.  The document itself never scrolls.
        self.js("(s) => (s === 'back' ? cvB : cvF).scrollIntoView({block: 'center'})", side)

    def click_mm(self, mm_x, mm_y, side="front"):
        self.scroll_into_view(side)
        x, y = self.board_to_client(mm_x, mm_y, side)
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        self.page.mouse.up()

    def drag_mm(self, frm, to, side="front", steps=8):
        self.scroll_into_view(side)
        x0, y0 = self.board_to_client(frm[0], frm[1], side)
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        # Recompute the destination AFTER the press: pointerdown runs
        # showPanel(), which can change the layout under us.
        x1, y1 = self.board_to_client(to[0], to[1], side)
        for i in range(1, steps + 1):
            self.page.mouse.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        self.page.mouse.up()

    def drag_by_px(self, frm, dx, dy, side="front", steps=10):
        """Drag by a raw pixel delta that may leave the canvas.  pointerdown
        calls setPointerCapture, so the handler still receives the moves — this
        is how edge clamping is tested without tripping the viewport guard."""
        self.scroll_into_view(side)
        x0, y0 = self.board_to_client(frm[0], frm[1], side)
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        for i in range(1, steps + 1):
            self.page.mouse.move(x0 + dx * i / steps, y0 + dy * i / steps)
        self.page.mouse.up()

    # -- panels and views --------------------------------------------------
    def show_panel(self, key):
        """One panel is visible at a time and Playwright times out on hidden
        elements, so never assume a panel: clicking a canvas object calls
        showPanel() and moves the rail out from under the next step."""
        self.page.click(f"#tab-{key}", timeout=ELEMENT_TIMEOUT)
        self.page.wait_for_function(
            "(k) => activePanel === k", arg=key, timeout=ELEMENT_TIMEOUT
        )

    def set_view(self, v):
        """DEFECT #8 workaround: #toasts (top-left) overlaps #viewtabs (centred)
        below ~1300 px wide, and warning toasts are re-raised by
        refreshWarnings() on every draw so they never expire.  dispatch_event
        bypasses hit-testing.  Delete this once the CSS is fixed —
        test_defect8_front_tab_stays_clickable_under_a_warning_toast will flip
        green and tell you."""
        self.page.locator(f'#viewtabs div[data-view="{v}"]').dispatch_event("click")
        self.page.wait_for_function("(v) => view === v", arg=v, timeout=ELEMENT_TIMEOUT)

    # -- list cards --------------------------------------------------------
    def card(self, list_id, i):
        return self.page.locator(f"#{list_id} .item").nth(i)

    def remove_card(self, list_id, i):
        self.card(list_id, i).locator('button[title="Remove"]').click(timeout=ELEMENT_TIMEOUT)

    # -- editor actions ----------------------------------------------------
    def add_led(self) -> bool:
        """Click + Add LED and report whether one actually appeared.

        NOT additive.  See defect #9: freeSpot() gives up long before
        MAX_LEDS and the refusal is a toast, not an exception.  Asserting
        `len(leds) == clicks` is a guaranteed false positive.
        """
        n = len(self.leds())
        self.page.click("#addled", timeout=ELEMENT_TIMEOUT)
        self.page.wait_for_function(
            "(n) => state.leds.length > n"
            " || [...document.querySelectorAll('#toasts .toast')]"
            ".some(t => /No room for another LED/.test(t.textContent))",
            arg=n,
            timeout=ELEMENT_TIMEOUT,
        )
        return len(self.leds()) > n

    def drop_all_pins(self):
        self.show_panel("shape")
        for pin in self.js("() => ALL_PINS"):
            box = self.page.locator(f'#pingrid input[data-pin="{pin}"]')
            if box.is_checked():
                box.uncheck(timeout=ELEMENT_TIMEOUT)
        self.wait_state("state.pins.length === 0")


# ===========================================================================
# Flow 1 — the happy path, end to end, ending in a real KiCad project
# ===========================================================================
def test_flow_upload_assign_material_drag_add_led_and_download(ui, logo):
    page = ui.page

    # --- upload artwork -------------------------------------------------
    ui.show_panel("art")
    page.set_input_files("#artfile", files=[logo], timeout=ELEMENT_TIMEOUT)
    # The layer appears from an Image.onload callback: wait on app state.
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    assert ui.art()[0]["fname"] == "logo.png"

    # --- assign a material to a palette entry ---------------------------
    card = ui.card("artlist", 0)
    entries = card.locator("select.pm")
    n_entries = entries.count()
    assert n_entries >= 2, f"a two-tone logo should yield >=2 palette entries, got {n_entries}"
    entries.nth(n_entries - 1).select_option("copper", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("designJSON().art[0].palette.some(e => e.material === 'copper')")

    # --- drag the artwork on the FRONT canvas ---------------------------
    before = ui.art()[0]
    ui.drag_mm((before["cx"], before["cy"]), (6.0, 13.0), side="front")
    ui.wait_state("selected && selected.kind === 'art'")
    after = ui.art()[0]
    # Tolerance, not equality: objects are clamped and pushed apart.
    assert abs(after["cx"] - 6.0) < 0.7, after
    assert abs(after["cy"] - 13.0) < 0.7, after
    # Dragging an object auto-switches the rail; confirm rather than assume.
    assert ui.panel() == "art"

    # --- add an LED and split the two across both faces -----------------
    ui.show_panel("leds")
    assert ui.add_led() is True
    ui.wait_state("state.leds.length === 2")
    ui.card("ledlist", 0).locator("select.s").select_option("front", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.leds[0].side === 'front'")
    assert ui.leds()[1]["side"] == "back"

    # --- drag the back LED on the mirrored BACK canvas ------------------
    back_led = ui.leds()[1]
    ui.drag_mm((back_led["x"], back_led["y"]), (13.5, 14.0), side="back")
    moved = ui.leds()[1]
    assert abs(moved["x"] - 13.5) < 0.7, moved
    assert abs(moved["y"] - 14.0) < 0.7, moved

    # --- the app's own gate must agree before we build ------------------
    assert ui.blocking() == [], ui.toast_texts()

    # --- download and inspect what the user actually receives -----------
    with page.expect_download(timeout=GENERATE_TIMEOUT) as dl:
        page.click("#download", timeout=ELEMENT_TIMEOUT)
    path = ui.downloads_dir / "project.zip"
    dl.value.save_as(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        pcb = next(n for n in names if n.endswith(".kicad_pcb"))
        text = zf.read(pcb).decode()
    assert any(n.endswith(".kicad_pro") for n in names), names
    # The design we built must be in the board, not just any board.
    assert text.count("(footprint ") >= 2, "two LED units should have reached the board"
    assert "F.Cu" in text and "B.Cu" in text

    ui.assert_clean("flow 1 happy path")


# ===========================================================================
# Flow 2 — chaos monkey: every control in a hostile order
#
# This flow asserts almost nothing about values.  Its job is that nothing
# throws, nothing logs, and the app's own coherence predicates still hold
# after a sequence no sane user would perform.
# ===========================================================================
def test_chaos_hostile_control_ordering(ui):
    page = ui.page

    # 1. rotate/resize with nothing selected: an empty-board click deselects.
    ui.click_mm(19.5, 19.5, side="front")
    assert ui.selected() is None

    # 2. add a text layer, select it on the canvas, delete it, keep editing.
    ui.show_panel("text")
    page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    text = ui.js("() => JSON.parse(JSON.stringify(state.texts[0]))")
    ui.click_mm(text["x"], text["y"], side=text.get("side", "front"))
    ui.remove_card("textlist", 0)
    ui.wait_state("state.texts.length === 0")
    # `selected` may still point into the now-empty list (defect #10).  The
    # nudge handler and draw() must both survive that.  (Pressing Delete here
    # too is NOT harmless — see test_defect10c_* below, which owns that case.)
    page.keyboard.press("ArrowRight")
    ui.js("() => draw()")

    # 3. undo past the start: Ctrl+Z five times with one thing to restore.
    for _ in range(5):
        page.keyboard.press("Control+z")
    ui.wait_state("state.texts.length === 1")
    assert len(ui.js("() => state.texts")) == 1, "undo must not resurrect the same item twice"

    # 4. drop every connector pin, then try to build anyway.
    ui.drop_all_pins()
    assert ui.blocking(), "a board with no power pins must be blocked"
    page.click("#download", timeout=ELEMENT_TIMEOUT)
    ui.wait_toast(r"Can.t build this board")

    # 5. add LEDs with no power rail at all.  Assert the outcome or the
    #    refusal, never the click count (defect #9).
    ui.show_panel("leds")
    placed = 1
    while ui.add_led():
        placed += 1
        assert placed <= 64, "MAX_LEDS is 64; the loop must terminate"
    assert ui.has_toast(r"No room for another LED")
    assert 2 <= len(ui.leds()) <= 64

    # 6. drag an LED far off the board: it must clamp, not escape.  A raw pixel
    #    delta, because a board coordinate that far out is not on screen.
    led = ui.leds()[0]
    ui.drag_by_px((led["x"], led["y"]), 900, 700, side=led["side"])
    assert ui.js(
        """() => { const L = state.leds[0], b = outlineBounds();
                   return L.x >= b[0] - 0.5 && L.x <= b[2] + 0.5
                       && L.y >= b[1] - 0.5 && L.y <= b[3] + 0.5; }"""
    ), ui.leds()[0]

    # 7. switch rail tabs mid-drag: press, change panel, then release.
    escaped = ui.leds()[0]
    side = escaped["side"]
    ui.scroll_into_view(side)
    x0, y0 = ui.board_to_client(escaped["x"], escaped["y"], side)
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.click("#tab-shape", timeout=ELEMENT_TIMEOUT)
    x1, y1 = ui.board_to_client(4.0, 4.0, side)
    page.mouse.move(x1, y1)
    page.mouse.up()
    ui.js("() => draw()")

    # 8. put pins back; the app must recover from step 4.
    ui.show_panel("shape")
    for pin in ["1", "8", "9", "16"]:
        page.locator(f'#pingrid input[data-pin="{pin}"]').check(timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.pins.length === 4")

    # 9. theme toggle mid-flight.
    page.click("#themebtn", timeout=ELEMENT_TIMEOUT)
    assert ui.js("() => document.documentElement.dataset.theme") in ("light", "dark")
    page.click("#themebtn", timeout=ELEMENT_TIMEOUT)

    # 10. hammer the view tabs; each one resizes both canvases.  dispatch_event
    #     because an open warning toast covers FRONT at narrow widths.
    for v in ["front", "back", "both", "front", "both"]:
        ui.set_view(v)
    assert ui.js("() => view") == "both"

    # 11. the design must still serialise and still gate itself coherently.
    design = ui.design()
    assert set(design) >= {"art", "leds", "texts", "pins", "shape"}
    assert isinstance(ui.blocking(), list)

    ui.assert_clean("flow 2 chaos")


# ===========================================================================
# Flow 3 — the custom outline: the debounced POST /outline round-trip
# ===========================================================================
def test_custom_outline_survives_slider_spam(ui, logo):
    page = ui.page
    ui.show_panel("shape")

    page.select_option("#shapemode", "custom", timeout=ELEMENT_TIMEOUT)
    # An empty custom composition is not active: the board stays square and
    # nothing is fetched.
    assert ui.js("() => customActive()") is False
    ui.wait_outline()

    # #shapefile's change handler returns immediately unless shapeFileTarget is
    # armed by "+ Image...".  Skip the click and set_input_files is a SILENT
    # no-op that surfaces as a timeout much later.  #artfile has no such rule.
    page.click("#eladdimg", timeout=ELEMENT_TIMEOUT)
    page.set_input_files("#shapefile", files=[logo], timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.shape.elements.length === 1", timeout=UPLOAD_TIMEOUT)
    ui.wait_outline()
    assert ui.js("() => state.shape.rings.length") >= 1
    assert ui.js("() => customActive()") is True

    # Spam the smoothing slider.  Each change nulls rings and fires a new
    # debounced POST; outlineSeq drops the out-of-order replies, so the settled
    # state must match the LAST value set, not whichever answered last.
    for value in ["0.4", "0.04", "0.32", "0", "0.28"]:
        page.locator("#ssm").fill(value, timeout=ELEMENT_TIMEOUT)
    ui.wait_outline()
    assert ui.js("() => state.shape.smooth") == 0.28
    assert ui.js("() => state.shape.rings.length") >= 1
    assert ui.js("() => state.shape.outlineEmpty") is False

    # Back to square: rings dropped, nothing outstanding.
    page.select_option("#shapemode", "square", timeout=ELEMENT_TIMEOUT)
    ui.wait_outline()
    assert ui.js("() => customActive()") is False

    ui.assert_clean("flow 3 custom outline")


# ===========================================================================
# Behaviour pinned as it is TODAY, so the defect tests below can be fixed
# without leaving these surfaces untested.
# ===========================================================================
def test_add_led_refuses_out_loud_and_never_throws(ui):
    """The refusal path itself is a contract: it must toast, not except."""
    ui.show_panel("leds")
    placed = 1
    while ui.add_led():
        placed += 1
        assert placed <= 64, "loop guard: MAX_LEDS is 64, so this must terminate"
    assert ui.has_toast(r"No room for another LED")
    # Refusing must not corrupt the design.  Bind and prove non-empty first:
    # `all(... for led in ui.leds())` is satisfied by a design the refusal wiped
    # out, which is the catastrophe it looks like it is guarding against.
    placed_leds = ui.leds()
    assert placed_leds, "refusing an LED must leave the placed ones alone, not wipe state.leds"
    assert all(0 <= led["x"] <= 21 and 0 <= led["y"] <= 21 for led in placed_leds)
    assert ui.js("() => state.leds.length <= MAX_LEDS")
    ui.assert_clean("add-led refusal")


def test_deleting_a_card_never_throws_and_selection_info_stays_guarded(ui):
    """Whatever `selected` ends up pointing at, selectionInfo() must not blow
    up and draw() must still run.  This is the guard that keeps defect #10
    merely wrong instead of fatal."""
    ui.show_panel("leds")
    assert ui.add_led() is True
    led = ui.leds()[1]
    ui.click_mm(led["x"], led["y"], side=led["side"])
    assert ui.selected() is not None
    ui.remove_card("ledlist", 0)
    ui.wait_state("state.leds.length === 1")
    ui.js("() => draw()")
    ui.js("() => selectionInfo()")  # must not throw even with a stale index
    ui.assert_clean("delete guard")


# ===========================================================================
# Documented live defects.  strict=True, so each flips to a hard failure the
# moment it is fixed and the xfail marker must then be removed.
# ===========================================================================
@pytest.mark.xfail(
    strict=True,
    reason="defect #8: #toasts overlays #viewtabs below ~1300 px wide and "
    "warning toasts are sticky, so FRONT is permanently unclickable",
)
def test_defect8_front_tab_stays_clickable_under_a_warning_toast(ui):
    page = ui.page
    ui.set_viewport(*NARROW)

    # Provoke a sticky warning: refreshWarnings() re-raises it on every draw().
    ui.drop_all_pins()
    ui.wait_toast(r"no 3V3 or GND pin")

    overlaps = ui.js(
        """() => {
            const t = document.getElementById('toasts').getBoundingClientRect();
            const v = document.getElementById('viewtabs').getBoundingClientRect();
            return !(t.right <= v.left || v.right <= t.left
                     || t.bottom <= v.top || v.bottom <= t.top);
        }"""
    )
    ui.assert_clean("defect 8 setup")

    # BLOCKED_TIMEOUT, not the 30 s default: this is the one click in the suite
    # that is expected to be intercepted, and it must fail in seconds.
    try:
        page.click('#viewtabs div[data-view="front"]', timeout=BLOCKED_TIMEOUT)
        clicked = True
    except PWTimeout:
        clicked = False

    assert not overlaps and clicked, (
        f"FRONT was unreachable at {NARROW[0]}x{NARROW[1]} "
        f"(toasts/viewtabs overlap={overlaps}); "
        "when this passes, delete this xfail and UI.set_view's dispatch_event"
    )


@pytest.mark.xfail(
    strict=True,
    reason="defect #9: + Add LED gives up at 5 on the default square while "
    "MAX_LEDS is 64; freeSpot() only tries inline/stacked at 0 and 90 deg",
)
def test_defect9_add_led_keeps_placing_while_the_board_has_room(ui):
    ui.show_panel("leds")
    placed = 1
    while ui.add_led():
        placed += 1
        assert placed <= 64, "loop guard"
    ui.assert_clean("defect 9")

    max_leds = ui.js("() => MAX_LEDS")
    assert max_leds == 64, "MAX_LEDS moved; re-derive the expectation below"
    # A 20.32 mm square has two faces and an 0805 unit is ~2.0 x 1.25 mm.  Five
    # is not "full"; it is freeSpot() running out of ideas.
    assert placed > 5, (
        f"+ Add LED stopped at {placed} units with MAX_LEDS={max_leds}; "
        f"toast said: {ui.toast_texts()}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="defect #10: the list-card Remove button leaves `selected` a stale "
    "index (the keyboard Delete path clears it correctly)",
)
def test_defect10_removing_the_selected_item_clears_the_selection(ui):
    ui.show_panel("leds")
    assert ui.add_led() is True
    led = ui.leds()[0]
    ui.click_mm(led["x"], led["y"], side=led["side"])
    assert ui.selected() == {"kind": "led", "index": 0, "side": led["side"]}

    ui.remove_card("ledlist", 0)
    ui.wait_state("state.leds.length === 1")
    ui.assert_clean("defect 10a")

    # Today `selected` still says {led, 0}, which now designates a DIFFERENT
    # LED — selectionInfo() happily draws handles on the survivor.
    assert ui.selected() is None, (
        f"selection survived the delete as {ui.selected()}; "
        f"selectionInfo() -> {ui.js('() => selectionInfo() !== null')}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="defect #10: a stale `selected` makes the arrow keys nudge an "
    "object the user never selected",
)
def test_defect10_arrow_keys_do_not_move_an_unselected_object(ui):
    page = ui.page
    ui.show_panel("leds")
    assert ui.add_led() is True
    # Select LED 0, then remove LED 0.  `selected` keeps saying index 0, which
    # now designates the OTHER LED - the one the user never touched.
    led = ui.leds()[0]
    ui.click_mm(led["x"], led["y"], side=led["side"])
    assert ui.selected()["index"] == 0

    ui.remove_card("ledlist", 0)
    ui.wait_state("state.leds.length === 1")
    survivor = ui.leds()[0]
    ui.assert_clean("defect 10b")

    for key in ["ArrowRight", "ArrowRight", "ArrowDown", "ArrowDown"]:
        page.keyboard.press(key)
    after = ui.leds()[0]

    assert (after["x"], after["y"]) == (survivor["x"], survivor["y"]), (
        f"arrow keys moved an LED the user never selected: "
        f"({survivor['x']},{survivor['y']}) -> ({after['x']},{after['y']}); "
        f"selected = {ui.selected()}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="defect #10, escalated: a stale `selected` lets the Delete key call "
    "noteDeleted() with an out-of-range index, so undoDelete() splices "
    "`undefined` into state.texts and blockingProblems() then throws on "
    "EVERY draw - the editor is bricked until reload",
)
def test_defect10c_undo_never_injects_an_undefined_item(ui):
    """Found by the chaos flow, not by inspection.  This is what makes defect
    #10 more than cosmetic: it is three benign-looking steps from a hard
    TypeError that no amount of DOM assertion would have noticed."""
    page = ui.page
    # This test deliberately provokes the exception; claim it so the conftest
    # page teardown does not double-report it, then assert it should not exist.
    page.expect_errors("Cannot read properties of undefined")

    ui.show_panel("text")
    page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    text = ui.js("() => JSON.parse(JSON.stringify(state.texts[0]))")
    ui.click_mm(text["x"], text["y"], side=text.get("side", "front"))
    assert ui.selected() is not None

    ui.remove_card("textlist", 0)          # leaves `selected` at {text, 0}
    ui.wait_state("state.texts.length === 0")
    page.keyboard.press("Delete")          # noteDeleted("text", 0, undefined)
    page.keyboard.press("Control+z")       # splices that undefined back in
    ui.wait_state("state.texts.length === 1")

    holes = ui.js("() => state.texts.filter(t => !t || typeof t.text !== 'string').length")
    assert holes == 0, f"undo injected {holes} undefined item(s) into state.texts"
    assert isinstance(ui.blocking(), list), "blockingProblems() must not throw"
    assert not ui.errors, ui.errors
