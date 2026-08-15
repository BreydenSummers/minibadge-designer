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
3.  **Every test ends in `ui.assert_clean()`.**  Every editor defect this suite
    has caught so far failed *silently*.  A flow that "passes" with a console
    full of errors is a false negative, so console errors, page errors and
    failed requests are assertions, not logs.

The suite must not be able to hang.  A control covered by an overlay turns one
click into a 30 s stall on Playwright's default timeout, so every wait here
carries an explicit, short timeout (see the `*_TIMEOUT` constants).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

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
# Nothing here may use Playwright's 30 s default: an intercepted click would
# stall for 30 s, and a suite that hangs is worse than one that fails.
ELEMENT_TIMEOUT = 5_000       # any locator / wait_for_function on local state
UPLOAD_TIMEOUT = 10_000       # Image.onload for an uploaded bitmap
OUTLINE_TIMEOUT = 15_000      # debounced POST /outline round-trip
GENERATE_TIMEOUT = 30_000     # POST /generate, which builds a real KiCad project
CONTROL_TIMEOUT = 2_500       # a control that must be reachable, checked fast
LOAD_TIMEOUT = 20_000         # goto + `ready === true` on a cold app

# 1400x1000 keeps both canvases above the fold.
WIDE = (1400, 1000)
# Where the old floating toast stack used to swallow the FRONT/BACK/BOTH tabs.
NARROW = (1024, 768)
# Enough widths to prove the message dock is out of the work area by layout and
# not by luck: below 1120 the rail narrows, below 1400 the legend leaves its
# floating slot, and 1600 is wider than every breakpoint.
WIDTH_SWEEP = [(1600, 1000), (1400, 1000), (1280, 900), (1024, 768), (900, 700)]

# Controls that must stay reachable no matter what the app has to say.  The
# canvases are on this list on purpose: they are the biggest control in the
# app, and an overlay that eats a drag is the same defect as one that eats a
# tab.  Anything absent at a given width is skipped by the probe.
CONTROLS = ["viewtabs", "tab-3d", "restorebar", "legend", "cvF", "cvB",
            "download", "addled", "themebtn", "panelscroll"]


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
    # conftest hands out 1280x900; start every test at a known size instead,
    # so a layout assertion never depends on the fixture's default.
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
        """Cap every implicit wait.  Playwright's 30 s default turns one
        intercepted click into a 30 s stall, and a hung suite is worse than a
        failing one."""
        self.page.set_default_timeout(ELEMENT_TIMEOUT)

    def wait_ready(self):
        # `ready` is a top-level `let` in a classic <script>: not a property of
        # window, but page.evaluate runs in global scope and resolves it.
        self.page.wait_for_function(
            "() => typeof ready !== 'undefined' && ready === true", timeout=LOAD_TIMEOUT
        )

    def set_viewport(self, w, h):
        """Resize and wait on a predicate, never a sleep.  CSS reflows on
        resize, so layout assertions must re-read geometry after this."""
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
        """A REAL click, hit-testing included.  This used to need
        dispatch_event because the standing-warning toasts floated over the
        centred view selector and swallowed the click; the messages now live in
        their own box at the foot of the rail, so if this ever starts timing
        out, something is covering the tabs again."""
        self.page.click(f'#viewtabs div[data-view="{v}"]', timeout=CONTROL_TIMEOUT)
        self.page.wait_for_function("(v) => view === v", arg=v, timeout=ELEMENT_TIMEOUT)

    def overlaps_controls(self, ids=CONTROLS):
        """Which of `ids` the message dock currently intersects on screen.
        Geometry, not a click: a click only proves the one control it hit."""
        return self.js(
            """(ids) => {
                const t = document.getElementById('toasts').getBoundingClientRect();
                if (!t.width && !t.height) return [];   // nothing to say = no box
                const hits = [];
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (!r.width && !r.height) continue;   // hidden at this width
                    if (!(t.right <= r.left || r.right <= t.left
                          || t.bottom <= r.top || r.bottom <= t.top)) hits.push(id);
                }
                return hits;
            }""",
            ids,
        )

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

    def fill_with_leds(self, cap=64):
        """Click + Add LED until it refuses.  Returns the number of units on
        the board.  The counter guard is what makes the bound readable."""
        placed = len(self.leds())
        while self.add_led():
            placed += 1
            assert placed <= cap, f"loop guard: MAX_LEDS is 64, so this must stop by {cap}"
        return placed

    def app_finds_another_spot(self):
        """The app's own answer to "is there room for one more?", asked with
        the app's own predicates.  If this says yes while + Add LED refused,
        the refusal was a lie."""
        return self.js(
            """() => spotForNewLed({x: 10, y: 10, color: 'red', side: 'back', rot: 0,
                                    layout: 'inline', size: '0805', reverse: false,
                                    novia: false, farled: false, adv: null})"""
        )

    def custom_square_board(self, mm):
        """Switch to a custom outline holding one `mm` x `mm` rectangle, and
        wait for the server to answer.  This is how a test moves off the
        default 20.32 mm square, which is where every placement bug has hid."""
        self.show_panel("shape")
        self.page.select_option("#shapemode", "custom", timeout=ELEMENT_TIMEOUT)
        self.page.select_option("#eladdkind", "rect", timeout=ELEMENT_TIMEOUT)
        self.page.click("#eladdshape", timeout=ELEMENT_TIMEOUT)
        self.wait_state("state.shape.elements.length === 1")
        card = self.page.locator("#shapeopts .item").first
        for cls in ("ewd", "ehd"):
            card.locator(f"input.{cls}n").fill(str(mm), timeout=ELEMENT_TIMEOUT)
        # The outline is a debounced server round-trip: settle on the answer
        # matching the size just asked for, not on "some answer arrived".
        self.page.wait_for_function(
            "(mm) => state.shape.rings && state.shape.rings.length"
            " && Math.abs((outlineBounds()[2] - outlineBounds()[0]) - mm) < 1.0",
            arg=mm,
            timeout=OUTLINE_TIMEOUT,
        )
        return self.js("() => outlineBounds()")


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
    # Removing the selected card must leave nothing selected, so the keyboard
    # has nothing to act on and draw() has nothing to trip over.
    assert ui.selected() is None
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Delete")
    ui.js("() => draw()")
    assert ui.js("() => state.texts.length") == 0

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
    #    refusal, never the click count: + Add LED stops when the board is
    #    full, well below MAX_LEDS.
    ui.show_panel("leds")
    ui.fill_with_leds()
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

    # 10. hammer the view tabs; each one resizes both canvases.  Real clicks,
    #     with an unfixed warning standing in the message dock the whole time.
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
# Placement: + Add LED must place a unit or say why not.  Never nothing.
# ===========================================================================
def test_add_led_refuses_out_loud_and_honestly_when_the_board_is_full(ui):
    """The refusal path is a contract: it must speak, it must not except, and
    it must be TRUE — no orientation the app is willing to use may still have
    a spot when the user is told there is none.  A false refusal is how a
    badge ends up with fewer LEDs than its owner asked for."""
    ui.show_panel("leds")
    ui.fill_with_leds()
    assert ui.has_toast(r"No room for another LED")
    # The refusal is checked against the app's own placement search, so it
    # cannot drift away from what + Add LED actually does.
    assert ui.app_finds_another_spot() is None, (
        "+ Add LED said the board was full while spotForNewLed() still finds "
        f"room at {ui.app_finds_another_spot()}"
    )
    # Refusing must not corrupt the design.  Bind and prove non-empty first:
    # `all(... for led in ui.leds())` is satisfied by a design the refusal wiped
    # out, which is the catastrophe it looks like it is guarding against.
    placed_leds = ui.leds()
    assert placed_leds, "refusing an LED must leave the placed ones alone, not wipe state.leds"
    assert all(0 <= led["x"] <= 21 and 0 <= led["y"] <= 21 for led in placed_leds)
    assert ui.js("() => state.leds.length <= MAX_LEDS")
    # Auto-placed units must be buildable, not merely present.
    assert ui.blocking() == [], ui.toast_texts()
    ui.assert_clean("add-led refusal")


def test_add_led_keeps_placing_while_the_board_has_room(ui):
    """Giving up early is silent damage: the user asks for another LED, the
    board has room, and the badge ships with fewer lights than they wanted."""
    ui.show_panel("leds")
    placed = ui.fill_with_leds()
    ui.assert_clean("filling the default square")

    max_leds = ui.js("() => MAX_LEDS")
    assert max_leds == 64, "MAX_LEDS moved; re-read the reasoning below"
    # Floor, not an expectation.  Measured on the default 20.32 mm square with
    # the app's own ledPosOk(): an 0805 in-line unit's envelope is 10.3 x 2.8 mm
    # and six of them fit clear of the connector pads.  The old 0.55 mm lattice
    # stopped at five -- one whole unit of real room thrown away by the search,
    # not by the board.
    assert placed >= 6, (
        f"+ Add LED stopped at {placed} units on the default square with "
        f"MAX_LEDS={max_leds}; toast said: {ui.toast_texts()}"
    )


def test_a_bigger_board_takes_more_led_units_than_the_small_default_square(ui):
    """Capacity has to follow the board.  This is the same search on a custom
    outline -- the parameter every placement bug so far has hidden behind --
    and the relationship holds whatever the exact numbers are."""
    ui.show_panel("leds")
    on_square = ui.fill_with_leds()

    # A 32 mm outline is ~2.5x the area of the 20.32 mm square.
    bounds = ui.custom_square_board(32)
    assert bounds[2] - bounds[0] > 28, bounds
    ui.show_panel("leds")
    on_big = ui.fill_with_leds()

    assert on_big > on_square, (
        f"the board grew from ~20 mm to {bounds[2] - bounds[0]:.0f} mm and "
        f"+ Add LED still stopped at {on_big} units (was {on_square} on the "
        f"square); toast said: {ui.toast_texts()}"
    )
    # Growing the board and packing it must not strand a unit off the outline.
    assert ui.blocking() == [], ui.toast_texts()
    ui.assert_clean("bigger board")


def test_the_text_layer_limit_refuses_out_loud_instead_of_ignoring_the_click(ui):
    """A click that does nothing and says nothing is indistinguishable from a
    broken button.  At the cap, + Add text has to admit the cap exists."""
    page = ui.page
    ui.show_panel("text")
    cap = ui.js("() => MAX_TEXTS")
    assert cap == 24, "MAX_TEXTS moved; the loop guard below assumes it"
    for _ in range(cap):
        page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state(f"state.texts.length === {cap}")

    # The click over the cap must produce a message.  A bare wait would report
    # this as a Playwright timeout, which reads like a broken test rather than
    # a mute button, so name the failure.
    page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    try:
        ui.wait_toast(r"limit", timeout=CONTROL_TIMEOUT)
        spoke = True
    except PWTimeout:
        spoke = False
    assert spoke, (
        f"+ Add text was clicked at the {cap}-layer cap and said nothing: "
        f"toasts = {ui.toast_texts()}"
    )
    assert ui.js("() => state.texts.length") == cap, "the cap must actually hold"
    ui.assert_clean("text layer cap")


# ===========================================================================
# Layout: what the app has to say must never cover what the user has to click
# ===========================================================================
def test_a_standing_warning_never_covers_a_control_at_any_width(ui):
    """A design warning lives exactly as long as the problem does -- it is
    re-raised on every draw -- so anything it covers is covered permanently.
    It used to float over the centred FRONT/BACK/BOTH selector below ~1300 px
    and make the view tabs unclickable for good."""
    page = ui.page

    # Provoke a standing warning: refreshWarnings() re-raises it on every draw.
    ui.drop_all_pins()
    ui.wait_toast(r"no 3V3 or GND pin")

    for w, h in WIDTH_SWEEP:
        ui.set_viewport(w, h)
        hits = ui.overlaps_controls()
        assert hits == [], f"the message dock covered {hits} at {w}x{h}"
        # Geometry is the guarantee; a real click is the proof it is the right
        # geometry.  CONTROL_TIMEOUT, not the 30 s default: a covered control
        # must fail this test in seconds rather than hang the suite.
        page.click('#viewtabs div[data-view="front"]', timeout=CONTROL_TIMEOUT)
        page.wait_for_function("() => view === 'front'", timeout=ELEMENT_TIMEOUT)
        ui.set_view("both")

    # ...and the warning is still standing, i.e. this was not a vacuous pass.
    assert ui.has_toast(r"no 3V3 or GND pin"), ui.toast_texts()
    ui.assert_clean("warning vs controls")


def test_a_new_message_is_visible_even_when_older_ones_fill_the_dock(ui):
    """Out of sight is the same as unsaid.  With more standing warnings than
    the dock can show, the answer to what the user just did must still land
    where they can read it -- otherwise Download appears to do nothing."""
    page = ui.page
    ui.set_viewport(*NARROW)

    # Overfill the dock: five texts the board font cannot print, plus the
    # no-power-rail warning.
    ui.show_panel("text")
    for _ in range(5):
        page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 5")
    ui.js("() => { state.texts.forEach(t => t.text = '\\u2603 snow');"
          " renderTextList(); draw(); }")
    ui.drop_all_pins()
    ui.wait_state("document.querySelectorAll('#toasts .toast').length >= 3")

    # How many messages fit is a layout detail (toast padding, how many
    # warnings the app shows at once), so squeeze the window until the dock
    # genuinely overflows rather than assuming one size does it.
    overflows = "() => { const d = document.getElementById('toasts');" \
                " return d.scrollHeight > d.clientHeight + 1; }"
    for height in (NARROW[1], 640, 560, 480):
        ui.set_viewport(NARROW[0], height)
        if ui.js(overflows):
            break
    assert ui.js(overflows), (
        "the dock never overflowed, so this test proves nothing; give it more "
        "messages or a shorter window"
    )

    # A refused Download is the message that matters most here.
    page.click("#download", timeout=ELEMENT_TIMEOUT)
    ui.wait_toast(r"Can.t build this board")
    assert ui.js(
        """() => {
            const d = document.getElementById('toasts').getBoundingClientRect();
            const t = [...document.querySelectorAll('#toasts .toast')]
                .find(el => /Can.t build this board/.test(el.textContent));
            if (!t) return false;
            const r = t.getBoundingClientRect();
            return r.top >= d.top - 1 && r.bottom <= d.bottom + 1;
        }"""
    ), f"the newest message was scrolled out of the dock: {ui.toast_texts()}"
    ui.assert_clean("message visibility")


# ===========================================================================
# Selection: `selected` names a live object, or nothing
# ===========================================================================
def test_removing_the_selected_item_clears_the_selection(ui):
    """Removing the card the user has selected must not silently hand the
    keyboard the item that slides into its slot."""
    ui.show_panel("leds")
    assert ui.add_led() is True
    led = ui.leds()[0]
    ui.click_mm(led["x"], led["y"], side=led["side"])
    assert ui.selected() == {"kind": "led", "index": 0, "side": led["side"]}

    ui.remove_card("ledlist", 0)
    ui.wait_state("state.leds.length === 1")
    ui.assert_clean("remove the selected card")

    assert ui.selected() is None, (
        f"selection survived the delete as {ui.selected()}; "
        f"selectionInfo() -> {ui.js('() => selectionInfo() !== null')}"
    )


def test_arrow_keys_do_not_move_an_unselected_object(ui):
    """The consequence a user actually sees: nudging something they never
    picked, on a board they thought they had finished."""
    page = ui.page
    ui.show_panel("leds")
    assert ui.add_led() is True
    # Select LED 0, then remove LED 0.  Slot 0 now holds the OTHER LED - the
    # one the user never touched.
    led = ui.leds()[0]
    ui.click_mm(led["x"], led["y"], side=led["side"])
    assert ui.selected()["index"] == 0

    ui.remove_card("ledlist", 0)
    ui.wait_state("state.leds.length === 1")
    survivor = ui.leds()[0]
    ui.assert_clean("remove then nudge")

    for key in ["ArrowRight", "ArrowRight", "ArrowDown", "ArrowDown"]:
        page.keyboard.press(key)
    after = ui.leds()[0]

    assert (after["x"], after["y"]) == (survivor["x"], survivor["y"]), (
        f"arrow keys moved an LED the user never selected: "
        f"({survivor['x']},{survivor['y']}) -> ({after['x']},{after['y']}); "
        f"selected = {ui.selected()}"
    )


def test_reordering_a_card_keeps_the_selection_on_the_object_the_user_picked(ui):
    """Same invariant, reached by a different edit: Move up renumbers the
    layers, and an index-only selection would quietly follow the number
    instead of the object -- so Delete would remove the wrong layer."""
    page = ui.page
    ui.show_panel("art")
    for kind in ("star", "circle"):
        page.click("#addartshape", timeout=ELEMENT_TIMEOUT)
        page.click(f'#shapemenu button[data-kind="{kind}"]', timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.art.length === 2")

    art = ui.art()
    ui.click_mm(art[1]["cx"], art[1]["cy"], side=art[1].get("side", "front"))
    assert ui.selected() is not None and ui.selected()["kind"] == "art"
    picked = ui.js("() => state.art[selected.index].kind")

    # Move the OTHER layer, so the selected one changes index without the user
    # touching it.  Both buttons exist on exactly one of the two cards.
    other = 1 - ui.selected()["index"]
    ui.card("artlist", other).locator(
        'button[title="Move up"]:not([disabled]), button[title="Move down"]:not([disabled])'
    ).first.click(timeout=ELEMENT_TIMEOUT)

    assert ui.selected() is not None, "reordering must not drop the selection"
    assert ui.js("() => state.art[selected.index].kind") == picked, (
        f"the selection jumped from the {picked} layer to a "
        f"{ui.js('() => state.art[selected.index].kind')} layer; "
        f"selected = {ui.selected()}"
    )
    ui.assert_clean("reorder keeps the selection")


def test_undo_never_injects_an_undefined_item(ui):
    """The worst failure this suite has found, and it was found by the error
    assertion, not by inspection: remove the selected text card, press Delete,
    press Ctrl+Z.  A stale index made noteDeleted() record `undefined`, undo
    spliced that hole into state.texts, and blockingProblems() then threw on
    EVERY draw -- no toast, no DOM change, no failed request, editor bricked
    until reload."""
    page = ui.page
    ui.show_panel("text")
    page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    text = ui.js("() => JSON.parse(JSON.stringify(state.texts[0]))")
    ui.click_mm(text["x"], text["y"], side=text.get("side", "front"))
    assert ui.selected() is not None

    ui.remove_card("textlist", 0)
    ui.wait_state("state.texts.length === 0")
    page.keyboard.press("Delete")
    page.keyboard.press("Control+z")
    ui.wait_state("state.texts.length === 1")

    holes = ui.js("() => state.texts.filter(t => !t || typeof t.text !== 'string').length")
    assert holes == 0, f"undo injected {holes} undefined item(s) into state.texts"
    # The bricking is what makes this fatal rather than untidy: prove the
    # editor still runs, not just that the array looks right.
    assert isinstance(ui.blocking(), list), "blockingProblems() must not throw"
    ui.js("() => draw()")
    ui.click_mm(10.0, 10.0, side="front")
    assert ui.assert_clean("delete + undo on a removed card") is None


def test_deleting_a_card_never_throws_and_selection_info_stays_guarded(ui):
    """Whatever `selected` ends up pointing at, selectionInfo() must not blow
    up and draw() must still run -- the last line of defence behind the
    selection invariant, for any edit path that forgets it."""
    ui.show_panel("leds")
    assert ui.add_led() is True
    led = ui.leds()[1]
    ui.click_mm(led["x"], led["y"], side=led["side"])
    assert ui.selected() is not None
    ui.remove_card("ledlist", 0)
    ui.wait_state("state.leds.length === 1")
    ui.js("() => draw()")
    ui.js("() => selectionInfo()")
    ui.assert_clean("delete guard")


# ===========================================================================
# The preview must draw what the generator makes
# ===========================================================================

#: A bridge start on a custom outline where the two implementations of
#: BRIDGE_INSET genuinely disagree. Measured against HEAD: the pre-fix preview
#: put this endpoint 2.58 mm from where the board puts it. Without a case like
#: this the parity test passes vacuously — on a plain square with no obstacles
#: the nearest exit is always perpendicular, sin(angle) is 1, and the two
#: formulas agree no matter which is wrong.
_BRIDGE_CALIBRATION_START = (3.0, 3.5)

_OCTAGON = [[[5.0, 0.16], [15.16, 0.16], [20.16, 5.0], [20.16, 15.16],
             [15.16, 20.16], [5.0, 20.16], [0.16, 15.16], [0.16, 5.0]]]

_BRIDGE_STARTS = [_BRIDGE_CALIBRATION_START, (10.0, 10.0), (4.5, 15.5),
                  (16.5, 4.0), (6.0, 13.0), (13.5, 6.2)]


@pytest.mark.browser
def test_the_previewed_bridge_lands_where_the_generated_one_does(page):
    """The canvas draws the same power bridge the downloaded board contains.

    A preview that lies is worse than either half being wrong on its own: the
    user approves a board they never saw. `bridgeRoute` in index.html and
    `pcb._bridge_route` are two hand-maintained copies of one algorithm, and
    they have drifted before — the JS kept subtracting BRIDGE_INSET along the
    ray after the generator started dividing it by sin(ray, edge), which put the
    drawn endpoint 2.58 mm from the real one on a custom outline.

    The JS is exercised directly rather than through the UI: the point is
    whether two implementations of one formula agree, and driving the canvas to
    reach them would add flakiness without adding evidence.
    """
    from minibadge_designer import pcb

    html = (Path(pcb.__file__).parent / "templates" / "index.html").read_text()
    start = html.index("function rayExit(")
    end = html.index("let _bridgeCache")
    page.add_script_tag(content=(
        f"const BRIDGE_INSET = {pcb.BRIDGE_INSET}, BRIDGE_MIN = {pcb.BRIDGE_MIN};\n"
        + html[start:end]
    ))

    off_by = {}
    for s in _BRIDGE_STARTS:
        board = pcb._bridge_route(s, [], [], [], _OCTAGON)
        drawn = page.evaluate("([s, r]) => bridgeRoute(s, [], [], [], r)",
                              [list(s), _OCTAGON])
        assert (board is None) == (drawn is None), (
            f"from {s} the generator {'refuses' if board is None else 'routes'} "
            f"but the preview {'refuses' if drawn is None else 'routes'} — the "
            "user is shown a bridge that will not exist, or none where one will")
        if board is None:
            continue
        d = max(abs(a - b) for pb, pd in zip(board, drawn) for a, b in zip(pb, pd))
        if d >= 1e-6:
            off_by[s] = (board[1], drawn[1], d)

    assert not off_by, "\n".join(
        f"from {s} the board runs its bridge to {tuple(round(v, 3) for v in b)} "
        f"but the canvas draws it to {tuple(round(v, 3) for v in d)} — {gap:.4f} mm out"
        for s, (b, d, gap) in off_by.items())
