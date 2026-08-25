"""Browser tier: the canvas editor that lives inside index.html.

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
    at once; that is a deliberate tripwire, not a bug.
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

import base64
import hashlib
import io
import json
import math
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
# message stack now floats at the top-left of the work area by design, so the
# canvases are NOT on this list: partial canvas coverage is the accepted price
# (grouping keeps the stack to one card per problem class), but a discrete
# control a card covers is covered permanently.  Anything absent at a given
# width is skipped by the probe.
CONTROLS = ["viewtabs", "tab-3d", "snapbtn", "restorebar", "legend",
            "download", "addled", "themebtn", "panelscroll"]

# The pad square every minibadge is built on is 20.32 mm at a 0.16 mm origin,
# so its centre is at 10.16 mm on both axes.  Stated here on purpose: sourcing
# it from the app's own OUT would make an edit to OUT invisible to these tests.
SQUARE_CENTRE_MM = 10.16
# The grid a snapped drag falls back to when nothing lines up, in mm.  Also
# stated independently: it is a documented number (the SNAP button says it).
SNAP_GRID_MM = 0.5
# A snap is an EXACT assignment, so it is its own signature: no hand drag
# lands on a millimetre value to twelve decimal places by accident.  That is
# what lets these tests discriminate a snap from a lucky cursor position,
# which a 0.7 mm drag tolerance never could.
SNAP_EXACT_MM = 1e-9


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

    def drag_mm_hold(self, frm, to, side="front", steps=8, modifiers=()):
        """Like `drag_mm`, but leaves the button DOWN and the modifiers held.

        Snap guides only exist for the duration of a gesture -- the release
        throws them away on purpose -- so the only way to see what the canvas
        was drawing is to look before letting go.  Callers must `release()`.
        """
        self.scroll_into_view(side)
        x0, y0 = self.board_to_client(frm[0], frm[1], side)
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        for m in modifiers:
            self.page.keyboard.down(m)
        x1, y1 = self.board_to_client(to[0], to[1], side)
        for i in range(1, steps + 1):
            self.page.mouse.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        self._held = tuple(modifiers)

    def release(self):
        self.page.mouse.up()
        for m in getattr(self, "_held", ()):
            self.page.keyboard.up(m)
        self._held = ()

    def drag_mm_with(self, frm, to, side="front", steps=8, modifiers=()):
        """One complete drag with modifier keys held for its whole length."""
        self.drag_mm_hold(frm, to, side=side, steps=steps, modifiers=modifiers)
        self.release()

    # -- snapping ----------------------------------------------------------
    def set_snap(self, on):
        """Flip the SNAP toggle through its button, never by poking `snapOn`:
        the button IS how a user chooses the mode, so a test that bypasses it
        cannot notice the two disagreeing."""
        if self.js("() => snapOn") != on:
            self.page.click("#snapbtn", timeout=CONTROL_TIMEOUT)
        self.wait_state(f"snapOn === {str(bool(on)).lower()}")

    def snap_pressed(self):
        """What the button claims the mode is, as a user reads it."""
        return self.js(
            "() => ({aria: $('snapbtn').getAttribute('aria-pressed'),"
            " lit: $('snapbtn').classList.contains('on')})"
        )

    def snap_guides(self):
        return self.js("() => JSON.parse(JSON.stringify(snapGuides))")

    def visible_centre(self):
        """The middle of the box the canvas draws around the current selection.

        This -- not the position the object stores -- is what a user means by
        "centred": a unit is anchored at its LED with the resistor and via off
        to one side, so the two are a whole resistor apart.  Read through the
        app's own `selectionInfo()`, which is what draws the box.
        """
        return self.js(
            "() => { const si = selectionInfo(); if (!si) return null;"
            " const b = si.sb; return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]; }"
        )

    def drag_by_px(self, frm, dx, dy, side="front", steps=10):
        """Drag by a raw pixel delta that may leave the canvas.  pointerdown
        calls setPointerCapture, so the handler still receives the moves; this
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
        centred view selector and swallowed the click; the stack now sits at
        the top-left with a pointer-transparent container, so if this ever
        starts timing out, something is covering the tabs again."""
        self.page.click(f'#viewtabs div[data-view="{v}"]', timeout=CONTROL_TIMEOUT)
        self.page.wait_for_function("(v) => view === v", arg=v, timeout=ELEMENT_TIMEOUT)

    def overlaps_controls(self, ids=CONTROLS):
        """Which of `ids` a toast CARD currently intersects on screen.
        Geometry, not a click: a click only proves the one control it hit.
        The probe measures the cards, not the #toasts container: the container
        is pointer-transparent by design, so only the cards can eat a click."""
        return self.js(
            """(ids) => {
                const cards = [...document.querySelectorAll('#toasts .toast')]
                    .map(el => el.getBoundingClientRect())
                    .filter(r => r.width && r.height);
                if (!cards.length) return [];   // nothing to say = no box
                const hits = [];
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    if (!r.width && !r.height) continue;   // hidden at this width
                    if (cards.some(t => !(t.right <= r.left || r.right <= t.left
                          || t.bottom <= r.top || r.bottom <= t.top))) hits.push(id);
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
        """Give the board a custom outline of one `mm` x `mm` rectangle, and
        wait for the server to answer.  This is how a test moves off the
        default 20.32 mm square, which is where every placement bug has hid.
        There is no mode switch to flip: adding the part IS the switch."""
        self.show_panel("shape")
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
# Flow 1: the happy path, end to end, ending in a real KiCad project
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
    # New units default to the back (the standard minibadge build); this
    # flow needs one on each face, so both sides are set explicitly.
    ui.show_panel("leds")
    assert ui.add_led() is True
    ui.wait_state("state.leds.length === 2")
    ui.card("ledlist", 0).locator("select.s").select_option("front", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.leds[0].side === 'front'")
    ui.card("ledlist", 1).locator("select.s").select_option("back", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.leds[1].side === 'back'")

    # --- drag the back LED on the mirrored BACK canvas ------------------
    back_led = ui.leds()[1]
    ui.drag_mm((back_led["x"], back_led["y"]), (13.5, 14.0), side="back")
    moved = ui.leds()[1]
    assert abs(moved["x"] - 13.5) < 0.7, moved
    assert abs(moved["y"] - 14.0) < 0.7, moved

    # --- the app's own gate must agree before we build ------------------
    assert ui.blocking() == [], ui.toast_texts()

    # --- download and inspect what the user actually receives -----------
    dl = _download(page, kicad=True)
    path = ui.downloads_dir / "project.zip"
    dl.save_as(path)
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
# Flow 2, the chaos monkey: every control in a hostile order
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
# Flow 3, the custom outline: the debounced POST /outline round-trip
# ===========================================================================
def test_custom_outline_survives_slider_spam(ui, logo):
    page = ui.page
    ui.show_panel("shape")

    # With no outline parts the board is the standard square and nothing is
    # fetched: there is no mode dropdown, parts alone drive the outline.
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

    # Back to square by removing the only part: rings dropped, nothing
    # outstanding; deleting the last part is the "switch back".
    page.locator("#shapeopts .item .del").first.click(timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.shape.elements.length === 0")
    ui.wait_outline()
    assert ui.js("() => customActive()") is False

    ui.assert_clean("flow 3 custom outline")


# ===========================================================================
# Placement: + Add LED must place a unit or say why not.  Never nothing.
# ===========================================================================
def test_add_led_refuses_out_loud_and_honestly_when_the_board_is_full(ui):
    """The refusal path is a contract: it must speak, it must not except, and
    it must be TRUE: no orientation the app is willing to use may still have
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
    The stack sits at the top-left of the work area by design now, so canvas
    overlap is accepted; what must hold is that no CARD sits on a discrete
    control, and that the container between and below the cards stays
    pointer-transparent -- the first overlay version made FRONT unclickable."""
    page = ui.page

    # Provoke TWO different standing warnings: refreshWarnings() re-raises
    # them on every draw, and the pointer-transparency probe below needs the
    # gap between two cards to aim at.
    ui.drop_all_pins()
    ui.wait_toast(r"no 3V3 or GND pin")
    ui.show_panel("text")
    page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    ui.js("() => { state.texts[0].text = '\\u2603 snow';"
          " renderTextList(); draw(); }")
    ui.wait_toast(r"characters")

    for w, h in WIDTH_SWEEP:
        ui.set_viewport(w, h)
        hits = ui.overlaps_controls()
        assert hits == [], f"a toast card covered {hits} at {w}x{h}"
        # Geometry is the guarantee; a real click is the proof it is the right
        # geometry.  CONTROL_TIMEOUT, not the 30 s default: a covered control
        # must fail this test in seconds rather than hang the suite.
        page.click('#viewtabs div[data-view="front"]', timeout=CONTROL_TIMEOUT)
        page.wait_for_function("() => view === 'front'", timeout=ELEMENT_TIMEOUT)
        ui.set_view("both")
        # The container itself must not eat clicks: the gap BETWEEN two cards
        # shows the container, and a click there has to reach whatever lies
        # underneath (the canvas), not the message layer.
        gap_clear = ui.js(
            """() => {
                const cards = [...document.querySelectorAll('#toasts .toast')];
                if (cards.length < 2) return 'need-two-cards';
                const a = cards[0].getBoundingClientRect();
                const b = cards[1].getBoundingClientRect();
                const el = document.elementFromPoint(
                    a.left + a.width / 2, (a.bottom + b.top) / 2);
                return !el || !el.closest('#toasts');
            }"""
        )
        assert gap_clear is True, (
            f"the toast container swallowed a click between its cards at "
            f"{w}x{h}: {gap_clear}"
        )

    # ...and the warning is still standing, i.e. this was not a vacuous pass.
    assert ui.has_toast(r"no 3V3 or GND pin"), ui.toast_texts()
    ui.assert_clean("warning vs controls")


def test_a_new_message_is_visible_even_when_older_ones_fill_the_dock(ui):
    """Out of sight is the same as unsaid.  With more standing warnings than
    the dock can show, the answer to what the user just did must still land
    where they can read it -- otherwise Download appears to do nothing."""
    page = ui.page
    ui.set_viewport(*NARROW)

    # Overfill the dock.  Identical problems collapse into one card now, so
    # the fill needs DISTINCT problem classes: unprintable characters, text
    # off the board edge, both at once, and the missing power rail.
    ui.show_panel("text")
    for _ in range(5):
        page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 5")
    ui.js("() => {"
          " state.texts[0].text = state.texts[1].text = '\\u2603 snow';"
          " state.texts[2].text = state.texts[3].text = 'off the edge';"
          " state.texts[2].x = state.texts[3].x = -6;"
          " state.texts[4].text = '\\u2603 off too'; state.texts[4].x = -6;"
          " renderTextList(); draw(); }")
    ui.drop_all_pins()
    ui.wait_state("document.querySelectorAll('#toasts .toast').length >= 4")

    # How many messages fit is a layout detail (toast padding, how many
    # warnings the app shows at once), so squeeze the window until the dock
    # genuinely overflows rather than assuming one size does it.
    overflows = "() => { const d = document.getElementById('toasts');" \
                " return d.scrollHeight > d.clientHeight + 1; }"
    for height in (NARROW[1], 640, 560, 480, 420):
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


def test_identical_standing_warnings_collapse_into_one_toast(ui):
    """N copies of the same warning bury the board the stack sits over
    without saying anything the first copy didn't (dogfood F3): the same
    problem on several parts must arrive as ONE card naming all of them."""
    page = ui.page
    ui.show_panel("text")
    for _ in range(3):
        page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 3")
    # The same problem three times, spread across both faces: the grouping
    # key is the problem, not the card or the side it lives on.
    ui.js("() => { state.texts.forEach(t => t.text = '\\u2603 snow');"
          " state.texts[2].side = 'back';"
          " renderTextList(); draw(); }")
    ui.wait_toast(r"Text 1")

    cards = ui.js(
        "() => [...document.querySelectorAll('#toasts .toast.warn')]"
        ".map(t => t.textContent).filter(t => /characters/.test(t))")
    assert len(cards) == 1, (
        f"the same unprintable-characters problem arrived as {len(cards)} "
        f"cards instead of one: {ui.toast_texts()}"
    )
    assert re.search(r"Text 1.*Text 2.*Text 3", cards[0]), (
        f"the grouped card must still name every affected text: {cards[0]!r}"
    )
    ui.assert_clean("warning grouping")


# A marker is drawn from the same numbers the app routes and lays out with,
# so it either lands on its item or it is somewhere else entirely; the
# tolerance only absorbs the float arithmetic on the way through.
_MARK_TOL_MM = 0.05


def test_a_standing_warning_marks_the_part_its_message_names(ui):
    """A message saying WHAT is wrong but not WHERE leaves the user dragging
    parts at random across a 20 mm board.  Every standing warning about
    something on the board carries a marker, and that marker sits on the part
    the message names -- not on the one beside it."""
    ui.show_panel("leds")
    while len(ui.leds()) < 3 and ui.add_led():
        pass
    assert len(ui.leds()) == 3, "the case needs three units to tell apart"
    ui.show_panel("text")
    for _ in range(2):
        ui.page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 2")
    # Three problems at once, none of them on the first item of its kind, on
    # the default face, or in the default package: an index, a side or a size
    # taken from the wrong place puts the marker somewhere the user can see.
    ui.js("""() => {
      const bad = state.leds[2];
      bad.size = '1206'; bad.rot = 30;
      bad.x = PAD_PAIRS.tl.at[0]; bad.y = PAD_PAIRS.tl.at[1];
      state.texts.forEach(t => { t.text = 'BADGE'; t.side = 'back'; });
      state.texts[1].x = outlineBounds()[2];
      state.pins = ALL_PINS.filter(
        n => PADS.find(p => p[4] === n)[2] !== 'GND');
      renderLedList(); renderTextList(); draw();
    }""")
    ui.wait_toast(r"no GND pin left")

    warns = ui.js("""() => designWarnings.map(w => ({
      key: w.key, kind: w.kind, i: w.i,
      pts: w.mark ? [...w.mark.polys.flat(), ...w.mark.segs.flat(),
                     ...w.mark.dots] : null}))""")
    places = ui.js("""() => ({
      led: state.leds.map(L => [L.x, L.y]),
      text: state.texts.map(t => { const b = textBBox(t);
        return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]; })})""")
    keys = {w["key"] for w in warns}
    assert {"w:led-fit-2", "w:text-1", "w:pins"} <= keys, (
        f"the case never provoked the three problems it is about: "
        f"{sorted(keys)}"
    )
    for w in warns:
        if w["kind"] not in places or w["i"] < 0:
            continue
        assert w["pts"], f"{w['key']} says what is wrong but marks nowhere"
        cx = sum(p[0] for p in w["pts"]) / len(w["pts"])
        cy = sum(p[1] for p in w["pts"]) / len(w["pts"])
        here = places[w["kind"]]
        near = min(range(len(here)),
                   key=lambda j: math.dist((cx, cy), here[j]))
        assert near == w["i"], (
            f"{w['key']} is about {w['kind']} {w['i'] + 1} but its marker "
            f"sits on {w['kind']} {near + 1}"
        )

    # The connector has no card and no outline to point at, so its marker has
    # to stand on the pads that went missing -- not on the ones still there.
    pads = ui.js("""() => PADS.map(p => ({x: p[0], y: p[1], label: p[2],
                                          pin: p[4], kept: pinOn(p[4])}))""")
    missing = ui.js("() => powerMissing()")
    dots = next(w["pts"] for w in warns if w["key"] == "w:pins")
    gone = [p for p in pads if not p["kept"] and p["label"] in missing]
    assert len(dots) == len(gone), (
        f"{len(gone)} {missing} pad(s) were dropped but the connector "
        f"warning marks {len(dots)} of them"
    )
    for dot in dots:
        pad = min(pads, key=lambda p: math.dist(dot, (p["x"], p["y"])))
        assert not pad["kept"] and pad["label"] in missing, (
            f"the connector marker stands on pin {pad['pin']} "
            f"({pad['label']}), which the board still has; it must stand on "
            f"the {missing} pad the design lost"
        )
    ui.assert_clean("warning markers")


def test_a_free_placed_units_warning_marks_the_part_that_is_in_the_way(ui):
    """With the parts dragged apart, the box around them is mostly empty
    board.  Marking that box tells the user a quarter of their badge is
    wrong and leaves them guessing which of five pieces to move -- so the
    marker outlines the piece that is actually on the header, and the
    message names it."""
    from shapely.geometry import Point, Polygon

    ui.show_panel("leds")
    # Resistor dragged down onto the bottom-left pair, LED left where it is:
    # the two ends of the unit are ~14 mm apart.
    ui.js("""() => {
      const L = state.leds[0];
      L.side = 'front'; L.x = 3.5; L.y = 4.2; L.novia = false;
      L.adv = {rx: -0.5, ry: 14.0, rrot: 0, lrot: 0, vx: 8.0, vy: 6.0};
      renderLedList(); draw();
    }""")
    ui.wait_toast(r"connector pad pair")

    got = ui.js("""() => {
      const L = state.leds[0], w = designWarnings.find(x => x.key === 'w:led-fit-0');
      return {polys: w && w.mark ? w.mark.polys : null, msg: w ? w.msg : null,
              led: [L.x, L.y], res: [L.x + L.adv.rx, L.y + L.adv.ry],
              envelope: unitPoly(L)};
    }""")
    assert got["polys"], "the unit is refused but nothing on the board says where"
    marked = [Polygon(q) for q in got["polys"]]
    assert Polygon(got["envelope"]).contains(Point(*got["led"])), (
        "vacuous: the envelope no longer spans the LED end of this unit")
    assert any(m.contains(Point(*got["res"])) for m in marked), (
        f"the resistor is the part on the pads, and the marker misses it: "
        f"{got['msg']}")
    assert not any(m.contains(Point(*got["led"])) for m in marked), (
        "the marker covers the LED end of the unit, 14 mm from the pads it "
        "is complaining about: it is outlining the box, not the copper")
    assert "resistor" in got["msg"], (
        f"the message does not name the part that is in the way: {got['msg']}")
    ui.assert_clean("free-placed marker")


def test_the_marker_for_a_run_that_cannot_route_covers_the_leg_that_failed(ui):
    """"a trace bend runs too close to other copper" is unactionable on a run
    with several bends.  The marker has to cover the leg the router actually
    rejected, so the user can see which bend to drag clear."""
    ui.show_panel("leds")
    # Via-less routing is off by default, and a hand-placed bend parked on the
    # connector pads is the ordinary way one of these runs stops routing.
    ui.js("""() => {
      const L = state.leds[0];
      L.novia = true;
      L.nodes = [[PAD_PAIRS.tl.at[0], PAD_PAIRS.tl.at[1]]];
      renderLedList(); draw();
    }""")
    ui.wait_toast(r"too close to other copper")

    run = ui.js("""() => {
      const L = state.leds[0], r = noviaRoute(L);
      const w = designWarnings.find(x => x.key === 'w:led-novia-0');
      return {tight: !!r.tight, start: r.pts[0], pad: r.pad,
              bend: L.nodes[0], segs: w && w.mark ? w.mark.segs : null};
    }""")
    assert run["tight"], "the case never made a run that fails to route"
    assert run["segs"] and len(run["segs"]) == 1, (
        f"the run that cannot route marks {run['segs']} copper: the message "
        "names a bend the board never points at"
    )
    ends = run["segs"][0]
    assert len(ends) == 2, f"a marked leg is two points, not {ends}"
    corners = [run["start"], run["bend"], run["pad"]]
    for end in ends:
        assert any(math.dist(end, c) <= _MARK_TOL_MM for c in corners), (
            f"the marked leg runs to {end}, which is neither end of the run "
            f"nor the bend on it: {corners}"
        )
    assert any(math.dist(end, run["bend"]) <= _MARK_TOL_MM for end in ends), (
        "the marked leg does not touch the bend the user placed, so the "
        "message and the marker are about different copper"
    )
    ui.assert_clean("route marker")


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
#: this the parity test passes vacuously: on a plain square with no obstacles
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
    they have drifted before: the JS kept subtracting BRIDGE_INSET along the
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
            f"but the preview {'refuses' if drawn is None else 'routes'}; the "
            "user is shown a bridge that will not exist, or none where one will")
        if board is None:
            continue
        d = max(abs(a - b) for pb, pd in zip(board, drawn) for a, b in zip(pb, pd))
        if d >= 1e-6:
            off_by[s] = (board[1], drawn[1], d)

    assert not off_by, "\n".join(
        f"from {s} the board runs its bridge to {tuple(round(v, 3) for v in b)} "
        f"but the canvas draws it to {tuple(round(v, 3) for v in d)}, {gap:.4f} mm out"
        for s, (b, d, gap) in off_by.items())


# ---------------------------------------------------------------------------
# Preview/generator parity: the other hand-maintained duplicates
#
# `bridgeRoute` above is not the only algorithm that exists twice.  These are
# the rest of the pairs that decide what the user sees against what the fab
# gets, exercised the same way: call the app's own function in the page, call
# the generator's in-process, and compare.  Each test names the pair it guards
# so a drift report says which copy to go and look at.
#
# The comparison runs at 1e-6 mm.  That is not a numerical-agreement fudge: the
# board file itself is written to 4 decimal places (`pcb._n`), so 1e-6 mm is a
# hundred times finer than anything that can reach KiCad, while still leaving
# room for float re-association between the two languages.
# ---------------------------------------------------------------------------
_PARITY_TOL = 1e-6

_SIZES = ("0603", "0805", "1206", "1.8mm", "3mm", "5x2mm")
_LAYOUTS = ("stacked", "inline")
#: 37 deg is deliberate: every multiple of 90 takes `rotOff`/`pcb._r`'s exact
#: branch, so a matrix of right angles never reaches the trig one at all.
_ROTS = (0, 90, 180, 270, 37)
#: Advanced placement moves the resistor and via off the layout and spins each
#: part on its own centre: the branch that builds `bbox` from real copper
#: rather than the package table.  Leaving it out makes half of `geomOf` dead.
_ADV = (None, {"rx": 2.0, "ry": -1.5, "rrot": 30, "lrot": 45, "vx": -2.2, "vy": 1.1})

#: An outline with slanted edges and a hole, so containment and clamping are
#: not decided by the standard square's axis-aligned arithmetic.
_HEX = [[(3.0, 0.3), (17.3, 0.3), (20.0, 10.2), (17.3, 20.0), (3.0, 20.0),
         (0.3, 10.2)]]
#: The hole is small on purpose: at 6 mm the ring left between it and the board
#: edge is narrower than a 1206 stacked unit, and most of the matrix then has
#: nowhere legal to stand at all.
_DONUT = [[(0.16, 0.16), (20.16, 0.16), (20.16, 20.16), (0.16, 20.16)],
          [(8.5, 8.5), (11.5, 8.5), (11.5, 11.5), (8.5, 11.5)]]


def _js_led(**kw):
    """One entry of `state.leds`, as the editor stores it."""
    led = {"x": 10.0, "y": 10.0, "color": "red", "side": "front", "rot": 0,
           "layout": "stacked", "size": "0805", "reverse": False,
           "novia": False, "nodes": [], "farled": False, "adv": None,
           "clk": False, "cnodes": []}
    led.update(kw)
    return led


def _py_led(d):
    """The same unit as `pcb.Led`, so one design drives both implementations."""
    from minibadge_designer import pcb

    term = None
    t = d.get("term")
    if isinstance(t, dict):
        if "pad" in t:
            term = ("pad", str(t["pad"]))
        elif "unit" in t:
            term = ("unit", int(t["unit"]))
    return pcb.Led(x=d["x"], y=d["y"], color=d["color"], side=d["side"],
                   rot=d["rot"], layout=d["layout"], size=d["size"],
                   reverse=d["reverse"], novia=d["novia"],
                   nodes=tuple(tuple(n) for n in d["nodes"]),
                   anodes=tuple(tuple(n) for n in d.get("anodes") or ()),
                   vnodes=tuple(tuple(n) for n in d.get("vnodes") or ()),
                   term=term, farled=d["farled"], adv=d["adv"],
                   clk=d.get("clk", False),
                   cnodes=tuple(tuple(n) for n in d.get("cnodes") or ()))


def _unit_matrix(sides=("front",), rots=_ROTS, advs=_ADV):
    """Every package crossed with every layout, mount, rotation and placement."""
    import itertools

    return [_js_led(layout=lay, size=size, reverse=rev, side=side, rot=rot,
                    adv=adv)
            for lay, size, rev, side, rot, adv
            in itertools.product(_LAYOUTS, _SIZES, (False, True), sides, rots,
                                 advs)]


def _clamped(leds, safe=None):
    """Pre-clamp the matrix so a parity test measures one algorithm at a time.

    The generator clamps inside every entry point; the editor clamps on the
    way in and stores the result.  Feeding both an already-clamped centre
    keeps a clamping bug from showing up as a copper-geometry failure.
    """
    from minibadge_designer import pcb

    out = []
    for d in leds:
        x, y = pcb.clamp_led_obj(_py_led(d), safe)
        out.append(dict(d, x=x, y=y))
    return out


def _describe(d):
    return (f"{d['layout']}/{d['size']}"
            f"{'/reverse' if d['reverse'] else ''}/{d['side']}/rot{d['rot']}"
            f"{'/adv' if d['adv'] else ''}")


def _gap(a, b):
    """Largest coordinate difference between two equal-shaped point lists."""
    return max((abs(u - v) for pa, pb in zip(a, b) for u, v in zip(pa, pb)),
               default=0.0)


_SET_DESIGN = """([leds, pins, rings, clk]) => {
    state.leds = leds;
    state.pins = pins ? pins : ALL_PINS.slice();
    Object.assign(state.clk, {jumper: true, x: null, y: null, rot: 0,
                              side: 'front', via: true,
                              nodes: [], v3nodes: [], v3pin: null}, clk || {});
    if (rings) {
        state.shape.mode = 'custom';
        state.shape.elements = [{kind: 'rect', op: 'add'}];
        state.shape.rings = rings;
        state.shape.ringsRev = (state.shape.ringsRev || 0) + 1;
    } else {
        state.shape.mode = 'square';
        state.shape.elements = [];
        state.shape.rings = null;
    }
}"""


#: Slide each unit along each ray until the canvas stops calling the spot solid
#: board, then bisect 50 times.  What comes back is the very last placement
#: the editor would let a user drop a unit on: the only place the client's
#: 0.555 mm and the generator's 0.55 mm can be told apart.
_EDGE_OF_ACCEPTANCE = """([leds, seeds, dirs]) => leds.map(L => {
    // A unit only has a boundary to find if some spot on the board suits it at
    // all: a 5 mm bar does not fit beside this outline's cut-out in every
    // orientation, and pushing off a spot it never occupied proves nothing.
    const start = seeds.map(([x, y]) => ({...L, x, y})).find(unitInsideBoard);
    if (!start) return [];
    const out = [];
    for (const [dx, dy] of dirs) {
        const at = t => ({...start, x: start.x + dx * t, y: start.y + dy * t});
        let lo = 0, hi = 0.25;
        while (hi < 32 && unitInsideBoard(at(hi))) { lo = hi; hi *= 2; }
        if (hi >= 32) continue;                 // this ray never leaves the board
        for (let i = 0; i < 50; i++) {
            const mid = (lo + hi) / 2;
            if (unitInsideBoard(at(mid))) lo = mid; else hi = mid;
        }
        const p = at(lo);
        out.push([p.x, p.y]);
    }
    return out;
})"""


def _set_design(ui, leds, pins=None, rings=None, clk=None):
    """Install a design in the page without driving the canvas.

    These tests are about two implementations of one formula agreeing, so the
    units are written straight into `state`; dragging them into place would
    add flakiness without adding evidence, which is the same reason the bridge
    test above reaches its function through an injected script.
    """
    ui.js(_SET_DESIGN, [leds, pins, [[list(p) for p in r] for r in rings]
                        if rings else None, clk])


@pytest.mark.browser
def test_the_previewed_unit_sits_where_the_generated_one_sits(ui):
    """The canvas puts every pad, via and hole where the board file puts it.

    `geomOf` in index.html and `pcb.led_geometry` are two copies of the unit
    layout table.  Everything downstream reads from it (the drawn part, the
    art keepouts, the bridge start, the via-less trace), so a drift here is
    not a wrong number, it is a badge whose resistor, via or reverse-mount
    hole is somewhere other than the picture the user approved.
    """
    from minibadge_designer import pcb

    keys = {"res": "res", "ledK": "led_k", "ledA": "led_a", "resIn": "res_in",
            "resOut": "res_out", "viaF": "via_front", "viaB": "via_back",
            "bbox": "bbox"}
    leds = _unit_matrix()
    drawn = ui.js("(Ls) => Ls.map(geomOf)", leds)
    off_by = []
    for d, js in zip(leds, drawn):
        board = pcb.led_geometry(_py_led(d))
        if js["pkg"] != board["pkg"]:
            off_by.append(f"{_describe(d)}: canvas builds a {js['pkg']} unit, "
                          f"the board builds a {board['pkg']} one")
            continue
        if abs(js["hole"] - board["hole"]) > _PARITY_TOL:
            off_by.append(f"{_describe(d)}: canvas routes a {js['hole']:.3f} mm "
                          f"reverse hole, the board routes {board['hole']:.3f} mm")
        for jk, pk in keys.items():
            gap = _gap([js[jk]], [board[pk]])
            if gap > _PARITY_TOL:
                off_by.append(
                    f"{_describe(d)}: {pk} is at "
                    f"{tuple(round(v, 4) for v in board[pk])} on the board but "
                    f"{tuple(round(v, 4) for v in js[jk])} on the canvas, "
                    f"{gap:.4f} mm out")

    assert not off_by, (
        f"{len(off_by)} differences across {len(leds)} units between where "
        "the canvas draws a unit and where the board builds it; geomOf() and "
        "pcb.led_geometry have drifted:\n"
        + "\n".join(off_by[:12]))
    ui.assert_clean("unit geometry parity")


@pytest.mark.browser
@pytest.mark.parametrize("rings", [None, _HEX], ids=["square", "hex"])
def test_a_dragged_unit_stops_where_the_board_would_stop_it(ui, rings):
    """A unit dragged off the edge settles at the same centre in both.

    `clampLedFor`/`unitSafe` and `pcb.clamp_led_obj`/`pcb.unit_safe` decide how
    close to the board edge a unit may sit.  If they drift the user drags a
    unit to the rim, sees it stop, and the generator quietly moves it somewhere
    else, or lets it hang over the edge and DRC rejects the board.  Custom
    outlines are the interesting half: there the safe rect follows the
    outline's bounding box rather than the standard square.
    """
    from minibadge_designer import pcb

    safe = pcb.unit_safe(pcb.BadgeSpec(outline=rings))
    # Well outside, exactly on the rim, and comfortably inside: only the first
    # two exercise the clamp at all, and the third proves it leaves a legal
    # centre alone rather than snapping everything to one spot.
    targets = [(-8.0, -8.0), (28.0, 28.0), (0.0, 10.0), (10.0, 0.0),
               (19.9, 1.2), (1.2, 19.9), (10.0, 10.0), (6.5, 13.5)]
    leds = [dict(d, x=x, y=y) for d in _unit_matrix(rots=(0, 90, 37))
            for x, y in targets]
    _set_design(ui, leds, rings=rings)
    drawn = ui.js("(Ls) => Ls.map(L => clampLedFor(L, L.x, L.y))", leds)

    off_by = []
    for d, js in zip(leds, drawn):
        board = pcb.clamp_led_obj(_py_led(d), safe)
        gap = _gap([js], [board])
        if gap > _PARITY_TOL:
            off_by.append(
                f"{_describe(d)} dragged to ({d['x']}, {d['y']}): the canvas "
                f"parks it at {tuple(round(v, 4) for v in js)}, the board at "
                f"{tuple(round(v, 4) for v in board)}, {gap:.4f} mm out")

    assert not off_by, (
        f"{len(off_by)} of {len(leds)} drags settle in different places on the "
        "canvas and on the board; clampLedFor()/unitSafe() and "
        "pcb.clamp_led_obj/pcb.unit_safe have drifted:\n"
        + "\n".join(off_by[:12]))
    ui.assert_clean("clamp parity")


@pytest.mark.browser
def test_art_is_carved_around_a_unit_the_same_way_it_is_on_the_board(ui):
    """The keepout the canvas erases art with is the one the generator uses.

    `unitCopperPieces` and `pcb.unit_copper_pieces` are the labelled quads that
    say how close artwork may come to a unit's pads, via, traces, reverse hole
    and through-hole silk.  Drift means the user sees a logo hugging an LED and
    downloads a board where that logo is eaten; or, the expensive direction,
    sees it clear and gets copper art shorting a pad.
    """
    from minibadge_designer import pcb

    leds = _clamped(_unit_matrix(sides=("front", "back")))
    drawn = ui.js("(Ls) => Ls.map(L => { state.leds = [L];"
                  " return unitCopperPieces(L); })", leds)
    off_by = []
    for d, js in zip(leds, drawn):
        board = pcb.unit_copper_pieces(_py_led(d))
        if [p[0] for p in js] != [p[0] for p in board]:
            off_by.append(
                f"{_describe(d)}: the canvas keeps art off {[p[0] for p in js]} "
                f"but the board keeps it off {[p[0] for p in board]}")
            continue
        for (label, jq), (_, bq) in zip(js, board):
            gap = _gap(jq, bq)
            if gap > _PARITY_TOL:
                off_by.append(f"{_describe(d)}: the {label} keepout is "
                              f"{gap:.4f} mm out between canvas and board")

    assert not off_by, (
        f"{len(off_by)} keepout pieces across {len(leds)} units are carved "
        "differently in the preview than on the board; unitCopperPieces() "
        "and pcb.unit_copper_pieces have drifted:\n" + "\n".join(off_by[:12]))
    ui.assert_clean("art keepout parity")


@pytest.mark.browser
def test_a_far_side_leds_keepout_is_carved_per_face_in_both(ui):
    """A far-side LED's per-face keepout matches between canvas and board.

    `unitCopperPieces` / `pcb.unit_copper_pieces` take a face argument for
    far-side ("LED on the other side") units, whose copper is split across
    the board: the LED face keeps only its pads, via and hole, while on the
    resistor face the departed pads shrink to their two via barrels. Drift
    here re-opens the ghost-pad bug on one side only: the preview erases a
    window over copper the board keeps, or shows pad-shaped slabs the
    board no longer ships.
    """
    from minibadge_designer import pcb

    leds = _clamped([_js_led(farled=True, layout=lay, size=size, side=side,
                             rot=rot)
                     for lay in _LAYOUTS for size in ("0603", "1206")
                     for side in ("front", "back") for rot in (0, 90, 37)])
    off_by = []
    for face in ("front", "back"):
        drawn = ui.js("([Ls, face]) => Ls.map(L => { state.leds = [L];"
                      " return unitCopperPieces(L, face); })", [leds, face])
        for d, js in zip(leds, drawn):
            board = pcb.unit_copper_pieces(_py_led(d), face=face)
            if [p[0] for p in js] != [p[0] for p in board]:
                off_by.append(
                    f"{_describe(d)} on {face}: canvas keeps {[p[0] for p in js]} "
                    f"but the board keeps {[p[0] for p in board]}")
                continue
            for (label, jq), (_, bq) in zip(js, board):
                gap = _gap(jq, bq)
                if gap > _PARITY_TOL:
                    off_by.append(f"{_describe(d)} on {face}: the {label} "
                                  f"piece is {gap:.4f} mm out")
    assert not off_by, (
        f"{len(off_by)} far-side per-face pieces differ between preview and "
        "board:\n" + "\n".join(off_by[:12]))
    ui.assert_clean("far-side per-face keepout parity")


@pytest.mark.browser
def test_the_previewed_perimeter_bridges_match_the_generated_ones(ui):
    """The bridge decision the canvas makes is the one the board ships.

    `allBridges` and `pcb.unit_bridges` both decide, per unit and layer,
    whether a thin power feed routes to the perimeter ring, and when either
    copy answers None, the webapp reserves the fat 2 mm window corridor for
    that unit instead. The lower-level `bridgeRoute` parity test feeds both
    copies empty obstacle lists, so it cannot see this layer: which of the
    unit's own pieces count as obstacles on which copper layer (a back unit's
    SMD pads are not copper on F.Cu at all). Drift here lies in the expensive
    direction: the user sees a hairline bridge and a window hugging their
    unit, then downloads a board with a corridor of pour across the window;
    or the reverse, a corridor drawn over art the board leaves alone.
    """
    from minibadge_designer import pcb

    leds = _clamped(_unit_matrix(sides=("front", "back")))
    off_by = []
    # The whole matrix in the no-window world: every contact of every unit
    # reaches its plane, so BOTH sides must cut nothing. A stray grey line
    # across a whole plane was the complaint that made this rule fine-grained;
    # here it is pinned across every package, layout, mount, rotation and
    # hand-placement. (The severed world goes through the real webapp in
    # test_a_severed_units_bridges_ship_exactly_as_previewed: raw hand-built
    # ArtLayers skip the keepout carving the webapp applies to windows, so
    # comparing against pcb.unit_bridges alone would judge the preview
    # against a board no design can ship.)
    drawn = ui.js("([Ls]) => Ls.map(L => { state.leds = [L];"
                  " state.art = []; return allBridges()[0]; })", [leds])
    for d, js in zip(leds, drawn):
        board = pcb.unit_bridges(pcb.BadgeSpec(leds=[_py_led(d)]))[0]
        for jkey, bkey in (("F", "F.Cu"), ("B", "B.Cu")):
            drawn_br = js.get(jkey)
            jseg = drawn_br["pts"] if drawn_br else None
            bseg = board.get(bkey)
            if jseg is not None or bseg is not None:
                off_by.append(
                    f"{_describe(d)} on {bkey}: the "
                    f"{'canvas draws' if jseg else 'board routes'} a bridge "
                    "for a contact the whole plane already reaches")

    assert not off_by, (
        f"{len(off_by)} bridge decisions across {len(leds)} units differ "
        "from 'a whole plane needs no trace to itself'; allBridges() or "
        "pcb.unit_bridges is cutting copper with no job:\n"
        + "\n".join(off_by[:12]))
    ui.assert_clean("bridge parity")


@pytest.mark.browser
def test_a_severed_units_bridges_ship_exactly_as_previewed(ui, client):
    """The bridges drawn for a windowed unit are the segments the zip carries.

    A window over a unit severs its contacts from the pour, and then a bridge
    per severed contact is the repair. Preview and board reach that answer by
    different roads -- a raster flood against the display's carved windows on
    one side, shapely fill components on the other -- and this walks the
    whole road: the very params the editor would post, through /generate, to
    the segments in the shipped file. Every drawn bridge leg must ship, and
    nothing but the unit's own traces may ship beyond them: an extra segment
    is a trace the user was never shown and cannot move.
    """
    import io
    import json
    import zipfile

    import invariants

    from minibadge_designer import pcb

    cases = [(d, "through")
             for d in _clamped([_js_led(layout=lay, size="0805", side=side,
                                        rot=rot, reverse=rev)
                                for lay in ("inline", "stacked")
                                for side in ("front", "back")
                                for rot, rev in ((0, False), (37, True))])]
    # One-face windows sever one face only, and the two sides reach that
    # answer through different code (the board picks layers per art.window,
    # the canvas builds one flood mask per face) -- a back-only window over a
    # back unit shipped its GND bridge while the preview showed nothing.
    cases += [(d, w) for d in _clamped([
                  _js_led(layout="inline", size="0805", side=side)
                  for side in ("front", "back")])
              for w in ("front", "back")]
    assert len(cases) == 12, "the severed-world matrix lost cases"
    any_bridge = False
    for d, wside in cases:
        window = {"kind": "circle", "material": "bare", "side": wside,
                  "cx": 10.16, "cy": 10.16, "wmm": 17.0}
        got = ui.js("""([L, win]) => {
            state.leds = [L]; state.art = [win];
            renderLedList(); renderArtList(); draw();
            const br = allBridges()[0];
            const own = [unitTracePts(state.leds[0], 'a').pts];
            const r = noviaRoute(state.leds[0]);
            if (!r && !L.novia) own.push(unitTracePts(state.leds[0], 'v').pts);
            if (r) own.push(r.pts);
            return { br, own,
                     params: designFormData().get('params') };
        }""", [d, window])
        params = json.loads(got["params"])
        params["name"] = "sever"
        resp = client.post("/generate", data={"params": json.dumps(params)},
                           content_type="multipart/form-data")
        assert resp.status_code == 200, (_describe(d), resp.get_json())
        root = invariants._parse_sexp(zipfile.ZipFile(io.BytesIO(
            resp.data)).read("sever/sever.kicad_pcb").decode())
        segs = {"F": [], "B": []}
        for gseg in invariants._kids(root, "segment"):
            a = invariants._kid(gseg, "start")
            b = invariants._kid(gseg, "end")
            ly = str(invariants._val(gseg, "layer"))[0]
            segs[ly].append(((float(a[1]) - pcb.ORIGIN, float(a[2]) - pcb.ORIGIN),
                             (float(b[1]) - pcb.ORIGIN, float(b[2]) - pcb.ORIGIN)))
        own_legs = [(tuple(p), tuple(q)) for pts in got["own"]
                    for p, q in zip(pts, pts[1:])]
        # Board files round coordinates to 1e-4 mm, so the float-exact
        # _PARITY_TOL can never match a shipped segment; a micron of slack
        # is still a thousandth of the trace width.
        tol = 1e-3

        def matches(leg, cand):
            (ax, ay), (bx, by) = leg
            (cx2, cy2), (dx2, dy2) = cand
            straight = max(abs(ax - cx2), abs(ay - cy2),
                           abs(bx - dx2), abs(by - dy2))
            flipped = max(abs(ax - dx2), abs(ay - dy2),
                          abs(bx - cx2), abs(by - cy2))
            return min(straight, flipped) < tol

        for jkey in ("F", "B"):
            br = got["br"].get(jkey)
            legs = ([(tuple(p), tuple(q))
                     for p, q in zip(br["pts"], br["pts"][1:])]
                    if br else [])
            if legs:
                any_bridge = True
            shipped = list(segs[jkey])
            # A layer with no bridge has no legs by design; the any_bridge
            # assert below proves the matrix still exercises the other case.
            for leg in legs:  # style-ok: E-VACUOUS-LOOP a bridge-less layer has zero legs to walk; the any_bridge assert after the loop fails if every case came back empty
                hit = next((c for c in shipped if matches(leg, c)), None)
                assert hit is not None, (
                    f"{_describe(d)}: the preview draws a {jkey}-face bridge "
                    f"leg {leg} that the shipped board does not carry")
                shipped.remove(hit)
            # The unit's own traces may ship SPLIT (a reverse unit's anode
            # trace skips its routed hole), so a leftover is "own copper"
            # when it lies along an own polyline, not only when it equals a
            # whole leg of one.
            def on_own(seg):
                def near(pt):
                    px2, py2 = pt
                    for (ax, ay), (bx, by) in own_legs:
                        vx, vy = bx - ax, by - ay
                        L2 = vx * vx + vy * vy
                        t = 0 if not L2 else max(0, min(
                            1, ((px2 - ax) * vx + (py2 - ay) * vy) / L2))
                        if ((px2 - ax - t * vx) ** 2
                                + (py2 - ay - t * vy) ** 2) < tol ** 2:
                            return True
                    return False
                return near(seg[0]) and near(seg[1])

            leftovers = [c for c in shipped if not on_own(c)]
            assert not leftovers, (
                f"{_describe(d)}: the shipped board carries {len(leftovers)} "
                f"{jkey}-face segment(s) beyond the unit's own traces and the "
                f"drawn bridges, e.g. {leftovers[0]} -- copper the user was "
                "never shown and cannot move")
    assert any_bridge, (
        "no case drew a bridge at all: the window fixture stopped severing, "
        "so this test compared nothing")
    ui.assert_clean("severed bridge shipping")
@pytest.mark.browser
def test_a_bridge_announces_itself_when_a_window_severs_the_unit(ui):
    """The trace that appears for a severed unit says why it appeared.

    A grey line materialising across the board unexplained reads as a bug --
    it was reported as one, twice. So the line only exists when a contact
    genuinely has no path to its plane, and the moment it appears the editor
    says which LED, what for, and that it can be dragged. No window, no
    line, no message.
    """
    page = ui.page
    ui.js("""() => {
        state.leds = [{x: 10.16, y: 10.16, color: 'red', side: 'front', rot: 0,
                       layout: 'stacked', size: '0805', reverse: false,
                       novia: false, nodes: [], farled: false, adv: null,
                       clk: false, cnodes: []}];
        state.art = []; renderLedList(); renderArtList(); draw();
    }""")
    assert ui.js("() => allBridges()[0]") == {}, (
        "a unit alone on a whole plane grew a bridge; nothing here severed it")
    assert not ui.has_toast(r"no path to the power plane"), (
        "the editor warned about a severed contact before anything was severed")

    # Cover the unit with a through window: both contacts lose their plane.
    ui.js("""() => {
        state.art = [{kind: 'circle', material: 'bare', side: 'through',
                      cx: 10.16, cy: 10.16, wmm: 17.0, palette: [],
                      overrides: []}];
        renderArtList(); draw();
    }""")
    ui.wait_state("Object.keys(allBridges()[0]).length === 2")
    ui.wait_toast(r"LED 1: its .* has no path to the power plane")

    # Take the window away and the bridges go with it -- the plane is whole
    # again, so the trace has no job (this is the complaint that built the
    # rule: a trace on copper the pad already reaches is just a line showing).
    ui.js("() => { state.art = []; renderArtList(); draw(); }")
    ui.wait_state("Object.keys(allBridges()[0]).length === 0")
    ui.assert_clean("bridge notice")
@pytest.mark.browser
def test_a_back_labels_box_is_not_carved_out_of_the_front_window(ui):
    """The window shows through where the OTHER face's label sits.

    D1/R1 print on the face their part is mounted on. The window keepout for
    a label used to be applied to both faces' cuts, so a through window wore
    a label-shaped hole in the middle of the drawing on the face the label
    never touches -- visible as an exclusion bite in the artwork, and shipped
    that way too (the board test owns the shipped half; this pins the pixels
    the user actually looks at).
    """
    ui.js("""() => {
        state.leds = [{x: 10.16, y: 10.5, color: 'red', side: 'back', rot: 0,
                       layout: 'inline', size: '0805', reverse: false,
                       novia: false, nodes: [], farled: false, adv: null,
                       clk: false, cnodes: []}];
        state.art = [{kind: 'circle', material: 'bare', side: 'through',
                      cx: 10.16, cy: 10.5, wmm: 17.0, palette: [],
                      overrides: []}];
        renderLedList(); renderArtList(); draw();
    }""")
    lab = ui.js("() => { const l = refdesLayout()[0];"
                " return { face: l.face, x: l.at[0], y: l.at[1] }; }")
    assert lab and lab["face"] == "back", (
        "fixture drift: the back unit's first label is not on the back")

    # The pixel at the label's spot, on each view, with the labels on and off:
    # toggling them must change the BACK view (label ink + kept mask) and
    # change NOTHING on the front (no ink there, so no carve either).
    def px(side, on):
        return ui.js("""([x, y, side, on]) => {
            state.refdes = on; rebuildAllArt(); draw();
            const cv = side === 'back' ? cvB : cvF;
            const sx = side === 'back' ? cv.width - VIEW.tx - x * SCALE
                                       : VIEW.tx + x * SCALE;
            return [...cv.getContext('2d').getImageData(
                Math.round(sx), Math.round(VIEW.ty + y * SCALE), 1, 1).data];
        }""", [lab["x"], lab["y"], side, on])

    front_on, front_off = px("front", True), px("front", False)
    back_on, back_off = px("back", True), px("back", False)
    assert any(abs(a - b) > 8 for a, b in zip(back_on, back_off)), (
        "toggling part labels changes nothing at the label's own spot on the "
        "back view: either the label is not drawn or the fixture misses it")
    # max() refuses an empty read outright, so a blank pixel fetch cannot
    # pass as "nothing changed".
    assert max(abs(a - b) for a, b in zip(front_on, front_off)) <= 8, (
        f"the FRONT view at the back label's spot changes with the labels "
        f"({front_on} vs {front_off}): the label is carving a hole in the "
        "window on the face it does not print on")
    ui.assert_clean("label face carve")


@pytest.mark.browser
@pytest.mark.parametrize("labels", [False, True],
                         ids=["captions-off", "captions-on"])
def test_the_preview_blocks_the_same_spots_the_connector_pads_block(ui, labels):
    """A spot the canvas calls free is one the generator will not shove.

    `padConflict` and `pcb.pad_conflict` both ask whether a unit's rotated
    footprint lands on a kept connector pad pair.  The canvas refuses the drop;
    the generator slides the unit away (`resolve_pad_overlap`).  If they
    disagree the user places a unit against the header, and the board comes
    back with it somewhere else; or worse, the canvas allows what the
    generator then has to move, silently.

    Dropped pins are the case worth having: dropping a pair frees its corner,
    and the two implementations have to free the same corner. So are the pin
    captions: while they print, a unit also has to stay out of the 0.5 mm band
    they occupy, and both sides have to hand that band back together.
    """
    from minibadge_designer import pcb

    pinsets = [None, ("1", "2", "7", "8"), ("9", "10"), ("2", "15"), ()]
    # A lattice that straddles all four corner keepouts and the free strips
    # between them, so both true and false answers are exercised everywhere.
    spots = [(x, y) for x in (1.2, 2.6, 4.2, 5.6, 10.0, 15.0, 17.8, 19.2)
             for y in (1.2, 2.6, 4.2, 10.0, 16.2, 17.6, 19.2)]
    # Hand-placed parts are the case the rule now turns on: their envelope
    # spans board neither the pads nor the copper occupy, so canvas and board
    # have to agree about the copper, not about the box around it.
    spread = {"rx": 5.5, "ry": 5.0, "rrot": 0, "lrot": 0, "vx": -4.0, "vy": -3.0}
    base = _unit_matrix(rots=(0, 90, 37), advs=(None, _ADV[1], spread))[::3]
    disagree, said_yes = [], 0
    for pins in pinsets:
        leds = _clamped([dict(d, x=x, y=y) for d in base for x, y in spots])
        _set_design(ui, leds, list(pins) if pins is not None else None)
        drawn = ui.js("([Ls, on]) => { state.pinlabels = on;"
                      " return Ls.map(L => { state.leds = [L];"
                      " return padConflict(L); }); }", [leds, labels])
        for d, js in zip(leds, drawn):
            board = pcb.pad_conflict(_py_led(d), pcb.ALL_PINS if pins is None
                                     else pins, None, labels)
            said_yes += bool(board)
            if js != board:
                disagree.append(
                    f"{_describe(d)} at ({d['x']:.2f}, {d['y']:.2f}) with pins "
                    f"{'all' if pins is None else pins} and captions "
                    f"{'on' if labels else 'off'}: the canvas says "
                    f"{'blocked' if js else 'free'}, the board says "
                    f"{'blocked' if board else 'free'}")

    assert said_yes, (
        "no probe in the lattice landed on a connector pad, so this test "
        "proved nothing; move the spots back over the corners")
    assert not disagree, (
        f"{len(disagree)} placements are judged differently by the canvas and "
        "the board; padConflict() and pcb.pad_conflict have drifted:\n"
        + "\n".join(disagree[:12]))
    ui.assert_clean("pad conflict parity")


@pytest.mark.browser
def test_the_previewed_via_less_trace_takes_the_route_the_board_routes(ui):
    """The via-less power trace is drawn along the copper that gets built.

    `noviaRouteRaw` and `pcb.novia_route` are the longest duplicated algorithm
    in the app: hazard collection, a visibility graph, Dijkstra, and a 45-degree
    mitre pass, written twice and expected to pick the identical path down to
    the tie-breaks.  The trace is real copper on the badge and the preview is
    the only place a user ever sees it, so a drift ships a board whose power
    run goes somewhere they never looked at: across a pad, or nowhere at all.

    The matrix has to include a crowded board: on an empty one the straight
    shot clears and neither implementation's graph search ever runs.
    """
    from minibadge_designer import pcb

    # Every package on both faces and both layouts, at a right angle and at an
    # oblique one.  A front through-hole unit is the branch that returns a
    # single point and no trace at all, so the TH sizes are not decoration.
    cases = [([_js_led(size=size, layout=lay, side=side, rot=rot, novia=True)],
              None)
             for size in _SIZES for lay in _LAYOUTS
             for side in ("front", "back") for rot in (0, 37)]
    # Which pins survive decides which pad the run chases and which pads become
    # hazards; both implementations have to break the distance tie the same way.
    cases += [([_js_led(size=size, side=side, novia=True)], pins)
              for size in ("0805", "3mm") for side in ("front", "back")
              for pins in (("1", "2", "9", "10"), ("7", "8"))]
    # A crowded board is what makes the graph search run: on an empty one the
    # straight shot clears and the Dijkstra half of both copies is never
    # reached.  Hand-placed bends are the other branch, where the user's own
    # nodes win and only the corners between them are mitred.
    crowded = [
        _js_led(x=6.0, y=8.0, novia=True),
        _js_led(x=6.0, y=12.0, rot=90, layout="inline", size="1206", novia=True),
        _js_led(x=14.0, y=10.0, rot=180, size="3mm", side="back", novia=True),
    ]
    cases += [(crowded, pins) for pins in (None, ("1", "2", "9", "10"))]
    cases += [([_js_led(novia=True, nodes=[[5.0, 5.0], [3.0, 3.0]])], None)]
    # A chosen destination must be honoured identically on both sides: a far
    # pad instead of the nearest, a chain onto another unit's same-net pad, a
    # loop (both must refuse it by falling back to auto), and a wrong-net pad
    # (both must ignore it). A drift here ships a run to a pad the preview
    # never showed.
    cases += [
        ([_js_led(x=6.0, y=6.0, novia=True, term={"pad": "16"})], None),
        ([_js_led(x=6.0, y=6.0, novia=True, term={"unit": 1}),
          _js_led(x=14.0, y=13.0, novia=True)], None),
        ([_js_led(x=6.0, y=6.0, novia=True, term={"unit": 1}),
          _js_led(x=14.0, y=13.0, novia=True, term={"unit": 0})], None),
        ([_js_led(x=10.0, y=13.0, side="back", novia=True,
                  term={"pad": "7"})], None),
        ([_js_led(x=6.0, y=6.0, novia=True, term={"pad": "7"})], None),
        ([_js_led(x=6.0, y=6.0, novia=True, term={"unit": 1},
                  nodes=[[10.0, 10.0]]),
          _js_led(x=14.0, y=13.0, novia=True)], None),
    ]

    off_by, routed = [], 0
    for design, pins in cases:
        leds = _clamped(design)
        _set_design(ui, leds, list(pins) if pins is not None else None)
        drawn = ui.js("(Ls) => Ls.map((_, i) =>"
                      " noviaRouteRaw(state.leds[i]))", leds)
        units = [_py_led(d) for d in leds]
        for i, d in enumerate(leds):
            keep = pcb.ALL_PINS if pins is None else pins
            board = pcb.novia_route(
                units[i], keep, None, units,
                term=pcb.novia_term(units[i], units, keep, None))
            js = drawn[i]
            if (board is None) != (js is None):
                off_by.append(
                    f"{_describe(d)} pins {'all' if pins is None else pins}: "
                    f"the board {'refuses' if board is None else 'routes'} "
                    f"but the canvas "
                    f"{'refuses' if js is None else 'routes'}")
                continue
            if board is None:
                continue
            routed += 1
            if len(js["pts"]) != len(board["pts"]):
                off_by.append(
                    f"{_describe(d)} pins {'all' if pins is None else pins}: "
                    f"the board bends the run {len(board['pts'])} times, "
                    f"the canvas draws {len(js['pts'])}")
                continue
            if bool(js.get("tight")) != bool(board.get("tight")):
                off_by.append(
                    f"{_describe(d)} pins {'all' if pins is None else pins}: "
                    "only one of the two flags this run as too tight, so "
                    "the warning the user sees does not match the copper")
            gap = _gap(js["pts"], board["pts"])
            if gap > _PARITY_TOL:
                off_by.append(
                    f"{_describe(d)} pins {'all' if pins is None else pins}: "
                    f"the run is {gap:.4f} mm out: board "
                    f"{[tuple(round(v, 3) for v in p) for p in board['pts']]}, "
                    f"canvas {[tuple(round(v, 3) for v in p) for p in js['pts']]}")

    assert routed, ("no case produced a route, so this test proved nothing; "
                    "check that the units still have novia set")
    assert not off_by, (
        f"{len(off_by)} differences across {routed} via-less runs between "
        "the copper drawn and the copper built; noviaRouteRaw() and "
        "pcb.novia_route have drifted:\n"
        + "\n".join(off_by[:10]))
    ui.assert_clean("via-less route parity")


@pytest.mark.browser
def test_the_previewed_clk_supply_takes_the_route_the_board_routes(ui):
    """A blinking unit's supply trace (and the jumper's link to pin 9) is
    drawn along the copper that gets built.

    clkRouteRaw / clkLink and pcb.clk_route / pcb.clk_link are the CLK third
    of the twice-written router; a back unit's supply additionally flows
    through noviaRouteRaw's supply branch. A drift ships a badge whose blink
    hookup crosses copper the preview never showed, or lands the run on a
    pad the user never picked.

    The matrix moves off every default: both hookup styles, both faces, a
    non-0805 package, an oblique rotation, a dragged and quarter-turned
    jumper (which drags the rail via and both run targets with it), a
    hand-bent supply run, and a unit that is via-less AND blinking.
    """
    from minibadge_designer import pcb

    cases = [
        # (leds, clk state) -- jumper hookup, front / back / both, off-default
        ([_js_led(x=6.0, y=6.0, size="0603", rot=90, clk=True)], {}),
        ([_js_led(x=14.0, y=12.0, side="back", layout="inline", clk=True)], {}),
        ([_js_led(x=6.0, y=6.0, rot=37, clk=True),
          _js_led(x=14.0, y=12.0, side="back", clk=True)], {}),
        # direct-trace hookup: the runs chase pin 9 itself
        ([_js_led(x=6.0, y=6.0, clk=True),
          _js_led(x=14.0, y=12.0, side="back", size="1206", clk=True)],
         {"jumper": False}),
        # the jumper dragged and stood on end: targets, hazards and the rail
        # via all move with it
        ([_js_led(x=13.0, y=6.0, clk=True),
          _js_led(x=13.5, y=13.5, side="back", clk=True)],
         {"x": 5.0, "y": 10.0, "rot": 90}),
        # hand-placed bends on the supply run win outright on both sides
        ([_js_led(x=6.0, y=6.0, clk=True, cnodes=[[4.0, 12.0], [7.0, 15.0]])],
         {}),
        # via-less AND blinking: the GND run and the supply run coexist
        ([_js_led(x=6.0, y=6.0, clk=True, novia=True),
          _js_led(x=14.0, y=12.0, clk=True)], {}),
        # the jumper mounted on the BACK: pads in the GND pour's layer, the
        # front unit crossing through the rail via, the link on B.Cu
        ([_js_led(x=6.0, y=6.0, clk=True),
          _js_led(x=14.0, y=12.0, side="back", clk=True)], {"side": "back"}),
        # traced steady hookup (back jumper, via off) with a far-face
        # blinker: the rail via still feeds it, and the 3V3 link routes
        ([_js_led(x=6.0, y=6.0, clk=True)], {"side": "back", "via": False}),
        # the jumper's own links bent by hand, on the traced hookup
        ([_js_led(x=7.0, y=8.0, side="back", clk=True)],
         {"side": "back", "via": False,
          "nodes": [[4.0, 14.0]], "v3nodes": [[14.0, 16.0]]}),
        # the 3V3 endpoint dragged to the pin the auto route would NOT pick
        ([_js_led(x=6.5, y=6.0, side="back", clk=True)],
         {"side": "back", "via": False, "x": 9.0, "y": 10.0, "v3pin": "15"}),
    ]

    off_by, routed = [], 0
    for design, clk_state in cases:
        leds = _clamped(design)
        _set_design(ui, leds, None, clk=clk_state)
        drawn = ui.js(
            "(Ls) => Ls.map((_, i) => {"
            "  const L = state.leds[i];"
            "  return { supply: L.side === 'back' ? noviaRouteRaw(L)"
            "                                     : clkRouteRaw(L),"
            "           gnd: L.side === 'back' ? null : noviaRouteRaw(L) };"
            "})", leds)
        link_js = ui.js("() => clkLink()")
        v3_js = ui.js("() => clkV3Link()")
        units = [_py_led(d) for d in leds]
        spec = pcb.BadgeSpec(
            leds=units,
            clk_jumper=clk_state.get("jumper", True) is not False,
            jumper=((clk_state["x"], clk_state["y"])
                    if "x" in clk_state else None),
            jumper_rot=clk_state.get("rot", 0),
            jumper_side=clk_state.get("side", "front"),
            jumper_via=clk_state.get("via", True) is not False,
            jumper_nodes=tuple(tuple(n) for n in clk_state.get("nodes", [])),
            jumper_v3nodes=tuple(tuple(n)
                                 for n in clk_state.get("v3nodes", [])),
            jumper_v3pin=clk_state.get("v3pin"))
        clk = pcb.clk_info(spec)
        assert clk is not None, "the case never armed CLK; it proves nothing"
        for i, unit in enumerate(units):
            if unit.side == "back":
                board = pcb.novia_route(
                    unit, spec.pins, None, units,
                    term=pcb.novia_term(unit, units, spec.pins, None, clk),
                    clk=clk)
            else:
                board = pcb.clk_route(unit, spec.pins, None, units, clk=clk)
            js = drawn[i]["supply"]
            for tag, bd, jd in (
                ("supply", board, js),
                ("gnd", pcb.novia_route(
                    unit, spec.pins, None, units,
                    term=pcb.novia_term(unit, units, spec.pins, None, clk),
                    clk=clk) if unit.side != "back" else None,
                 drawn[i]["gnd"]),
            ):
                if (bd is None) != (jd is None):
                    off_by.append(
                        f"{_describe(leds[i])} {tag}: the board "
                        f"{'refuses' if bd is None else 'routes'} but the "
                        f"canvas {'refuses' if jd is None else 'routes'}")
                    continue
                if bd is None:
                    continue
                routed += 1
                if bool(jd.get("tight")) != bool(bd.get("tight")):
                    off_by.append(
                        f"{_describe(leds[i])} {tag}: only one side flags "
                        "this run as too tight, so the warning the user sees "
                        "does not match the copper")
                if len(jd["pts"]) != len(bd["pts"]):
                    off_by.append(
                        f"{_describe(leds[i])} {tag}: the board bends the "
                        f"run {len(bd['pts'])} times, the canvas draws "
                        f"{len(jd['pts'])}")
                    continue
                gap = _gap(jd["pts"], bd["pts"])
                if gap > _PARITY_TOL:
                    off_by.append(
                        f"{_describe(leds[i])} {tag}: the run is "
                        f"{gap:.4f} mm out")
        for tag, board_link, js_link in (
            ("pin-9 link", pcb.clk_link(units, spec.pins, None, None, clk),
             link_js),
            ("3V3 link", pcb.clk_v3_link(units, spec.pins, None, None, clk),
             v3_js),
        ):
            if (board_link is None) != (js_link is None):
                off_by.append(f"only one side routes the jumper's {tag}")
            elif board_link is not None:
                routed += 1
                if len(js_link["pts"]) != len(board_link["pts"]):
                    off_by.append(
                        f"jumper {tag}: the board bends it "
                        f"{len(board_link['pts'])} times, the canvas draws "
                        f"{len(js_link['pts'])}")
                else:
                    gap = _gap(js_link["pts"], board_link["pts"])
                    if gap > _PARITY_TOL:
                        off_by.append(
                            f"jumper {tag}: the run is {gap:.4f} mm out")

    assert routed, ("no case produced a CLK run, so this test proved "
                    "nothing; check that the units still have clk set")
    assert not off_by, (
        f"{len(off_by)} differences across {routed} CLK runs between the "
        "copper drawn and the copper built; the CLK routers have drifted:\n"
        + "\n".join(off_by[:10]))
    ui.assert_clean("CLK route parity")


@pytest.mark.browser
def test_clk_traces_follow_the_jumper_when_it_is_dragged(ui):
    """Dragging the jumper takes every supply trace (and the rail via) with
    it, and the routes drawn after release end exactly on its new pads.

    The regression the first CLK build shipped: the route cache's mid-drag
    shortcut reused each unit's previous path, and the last drag frame
    stored that stale path under the final signature, so after release the
    preview kept showing runs to the jumper's OLD home forever -- copper
    the download would never build.
    """
    page = ui.page
    leds = [_js_led(x=6.0, y=6.0, clk=True),
            _js_led(x=14.0, y=12.0, side="back", clk=True)]
    _set_design(ui, leds)
    home = ui.js("() => clkInfo().jumper")
    assert ui.js("() => clkRoute(state.leds[0]).pts.at(-1)") == home[:2], (
        "the front supply run does not even start on the jumper's centre "
        "pad; the drag below would prove nothing")

    sx, sy = ui.board_to_client(home[0], home[1], "front")
    tx, ty = ui.board_to_client(home[0] - 3.0, home[1] - 5.0, "front")
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(tx, ty, steps=8)
    page.mouse.up()

    moved = ui.js("() => clkInfo().jumper")
    assert (moved[0], moved[1]) != (home[0], home[1]), (
        "the drag did not move the jumper at all, so this test proved "
        "nothing; did the hit test lose the jumper?")
    assert ui.js("() => clkRoute(state.leds[0]).pts.at(-1)") == moved[:2], (
        "the front unit's supply trace still ends somewhere other than the "
        "jumper's new centre pad; the preview shows copper the download "
        "does not build")
    assert (ui.js("() => noviaRoute(state.leds[1]).pts.at(-1)")
            == ui.js("() => clkInfo().via")), (
        "the back unit's supply trace did not follow the rail via to the "
        "jumper's new home")
    ui.assert_clean("jumper drag re-route")


@pytest.mark.browser
def test_a_clk_supply_trace_grows_bends_without_free_placement(ui):
    """Hovering a CLK unit's supply trace offers the "+", and the dropped
    bend drags and deletes -- with "Move parts freely" OFF.

    The other trace bends are deliberately gated behind free placement, but
    the supply run is copper the blink option itself created (like the
    jumper's links); a user who never opens Advanced still has to be able
    to steer it. Shipped broken once: the gate hid the "+" and, separately,
    the "c" trace was missing from the bend hit-test list entirely, so even
    free placement could not grab an existing bend.
    """
    page = ui.page
    # A back-side blinker, like the report: its supply run is the noviaRoute
    # supply branch. adv stays null on purpose.
    leds = [_js_led(x=10.0, y=4.0, side="back", layout="inline", clk=True)]
    _set_design(ui, leds)
    mid = ui.js("""() => {
      const r = noviaRoute(state.leds[0]);
      const k = Math.max(0, Math.floor(r.pts.length / 2) - 1);
      return [(r.pts[k][0] + r.pts[k+1][0]) / 2,
              (r.pts[k][1] + r.pts[k+1][1]) / 2];
    }""")
    sx, sy = ui.board_to_client(mid[0], mid[1], "back")
    page.mouse.move(sx, sy)
    assert ui.js("() => nodeHint && !nodeHint.jumper && nodeHint.trace") == "c", (
        "no '+' offered over the supply trace without free placement; the "
        "user cannot steer the copper the blink option added")
    page.mouse.down()
    tx, ty = ui.board_to_client(mid[0] + 2.0, mid[1] + 1.5, "back")
    page.mouse.move(tx, ty, steps=5)
    page.mouse.up()
    got = ui.js("""() => ({
      n: (state.leds[0].cnodes || []).length,
      sel: selected && selected.kind, trace: selected && selected.trace,
      manual: !!noviaRoute(state.leds[0]).manual })""")
    assert got == {"n": 1, "sel": "lednode", "trace": "c", "manual": True}, (
        f"the bend did not take: {got}; the trace ignored the user's hand")
    # Double-click removal was retired (it fought the click-to-select and
    # the grab on the same dot): a double-click on the bend must leave it
    # alone, and Delete on the selected dot is the one removal path.
    page.mouse.dblclick(tx, ty)
    assert ui.js("() => (state.leds[0].cnodes || []).length") == 1, (
        "double-click removed (or duplicated) the bend; that gesture is "
        "retired and must be inert on a bend")
    page.keyboard.press("Delete")
    assert not ui.js("() => (state.leds[0].cnodes || []).length"), (
        "Delete did not remove the selected bend")
    assert not ui.js("() => !!noviaRoute(state.leds[0]).manual"), (
        "the route still counts itself hand-shaped after its last bend went")
    ui.assert_clean("clk supply bend without adv")


@pytest.mark.browser
def test_trace_bends_stay_reachable_on_a_custom_outline(ui):
    """On a custom-shape board the "+" still appears over a trace, and the
    click drops a bend instead of grabbing the shape part underneath.

    A custom outline's shape element sits under EVERY trace by construction;
    shipped broken once: the hint yielded to it, so off a part's body no
    bend could ever be added -- every click just dragged the board shape.
    Away from a trace the shape must still drag normally.
    """
    page = ui.page
    ui.js("""() => {
      state.shape.elements = [{kind: "rect", op: "add", cx: 10.16, cy: 10.16,
                               w: 19, h: 19, rot: 0}];
      state.leds = [{x: 6.5, y: 6.0, color: "red", side: "front", rot: 0,
                     layout: "stacked", size: "0805", reverse: false,
                     novia: false, farled: false, adv: null, clk: true,
                     cnodes: [[13.0, 12.0]]}];
      Object.assign(state.clk, {jumper: true, x: null, y: null, rot: 0,
                                side: 'front', via: true,
                                nodes: [], v3nodes: [], v3pin: null});
      renderLedList(); draw();
    }""")
    # The leg whose midpoint sits farthest from the jumper: the final leg
    # dives into the jumper's pads, and midpoints inside its dead zone no
    # longer offer the "+" -- by design, so a click there moves the part
    # (test_clicking_a_jumper_pad_grabs_the_jumper_and_never_drops_a_bend).
    mid = ui.js("""() => {
      const r = clkRoute(state.leds[0]);
      const [jx, jy] = clkInfo().jumper;
      let best = null, away = -1;
      for (let k = 0; k + 1 < r.pts.length; k++) {
        const m = [(r.pts[k][0] + r.pts[k+1][0]) / 2,
                   (r.pts[k][1] + r.pts[k+1][1]) / 2];
        const d = Math.hypot(m[0] - jx, m[1] - jy);
        if (d > away) { away = d; best = m; }
      }
      return best;
    }""")
    sx, sy = ui.board_to_client(mid[0], mid[1], "front")
    page.mouse.move(sx, sy)
    assert ui.js("() => nodeHint && nodeHint.trace") == "c", (
        "no '+' offered over the trace on a custom outline; the shape part "
        "under it swallowed the hint and bends are unreachable")
    page.mouse.down()
    tx, ty = ui.board_to_client(mid[0] + 1.5, mid[1] - 1.5, "front")
    page.mouse.move(tx, ty, steps=4)
    page.mouse.up()
    got = ui.js("""() => ({
      bends: (state.leds[0].cnodes || []).length,
      shape: [state.shape.elements[0].cx, state.shape.elements[0].cy] })""")
    assert got["bends"] == 2, (
        f"the click did not add a bend: {got}; it grabbed something else")
    assert got["shape"] == [10.16, 10.16], (
        f"the click dragged the board shape to {got['shape']} instead of "
        "adding a bend; the outline walked away under the user's cursor")
    # Away from any trace, the shape itself still drags.
    ax, ay = ui.board_to_client(15.5, 5.0, "front")
    page.mouse.move(ax, ay)
    page.mouse.down()
    bx, by = ui.board_to_client(14.5, 6.0, "front")
    page.mouse.move(bx, by, steps=4)
    page.mouse.up()
    moved = ui.js("() => [state.shape.elements[0].cx, state.shape.elements[0].cy]")
    assert moved != [10.16, 10.16], (
        "the shape no longer drags at all; the bend fix overcorrected")
    ui.assert_clean("bends on a custom outline")


#: Every hand-placed bend in the design, one number.  A click that was meant
#: to grab the jumper but fell on a bend "+" leaves its mark here.
_BEND_COUNT = ("() => (state.clk.nodes || []).length"
               " + (state.clk.v3nodes || []).length"
               " + state.leds.reduce((n, L) => n + (L.cnodes || []).length"
               "     + (L.nodes || []).length + (L.anodes || []).length"
               "     + (L.vnodes || []).length, 0)")

#: Midpoints of every routed CLK leg, and whether the bend "+" is offered
#: there: proof the dead zone did not swallow the hint everywhere.
_HINT_STILL_OFFERED = """() => {
  const legs = [];
  const grab = r => { if (r && r.pts) for (let k = 0; k + 1 < r.pts.length; k++)
    legs.push([(r.pts[k][0] + r.pts[k+1][0]) / 2,
               (r.pts[k][1] + r.pts[k+1][1]) / 2]); };
  grab(clkLink()); grab(clkV3Link());
  for (const L of state.leds) if (L.clk)
    grab(L.side === "back" ? noviaRoute(L) : clkRoute(L));
  return {legs: legs.length,
          offered: legs.filter(([x, y]) => traceNodeHint(x, y)).length};
}"""

#: The worst places to click: for each CLK leg midpoint, the grabbable point
#: on the jumper nearest to it (its grab box is |ux|<2.4, |uy|<1.2 in the
#: local frame; 2.3/1.1 stays safely inside).  Kept when the midpoint is
#: within the 1.6 mm hint radius, because there the "+" competes with the
#: grab even though the pointer -- not the midpoint -- is on the body.
_FRINGE_CLICKS = """() => {
  const ci = clkInfo();
  const [jx, jy, rot] = ci.jumper;
  const legs = [];
  const grab = r => { if (r && r.pts) for (let k = 0; k + 1 < r.pts.length; k++)
    legs.push([(r.pts[k][0] + r.pts[k+1][0]) / 2,
               (r.pts[k][1] + r.pts[k+1][1]) / 2]); };
  grab(clkLink()); grab(clkV3Link());
  for (const L of state.leds) if (L.clk)
    grab(L.side === "back" ? noviaRoute(L) : clkRoute(L));
  const pts = [];
  for (const [mxx, myy] of legs) {
    const [ux, uy] = rotOff(mxx - jx, myy - jy, -rot);
    const gx = Math.max(-2.3, Math.min(2.3, ux));
    const gy = Math.max(-1.1, Math.min(1.1, uy));
    if (Math.hypot(ux - gx, uy - gy) < 1.6) {
      const [dx, dy] = rotOff(gx, gy, rot);
      pts.push([jx + dx, jy + dy]);
    }
  }
  return pts;
}"""


@pytest.mark.browser
def test_clicking_a_jumper_pad_grabs_the_jumper_and_never_drops_a_bend(ui):
    """A click on any of the jumper's three pads selects the jumper -- it
    never lands on a bend "+" -- while the "+" is still offered along the
    same traces away from the body.

    Several traces end on the jumper by construction (its pin-9 and 3V3
    links, a back blinker's supply run into the rail via), so their first-leg
    midpoints crowd the body.  Shipped broken once: hovering the pads showed
    the "+", and the click meant to drag the jumper instead recorded a
    permanent hand-bend in the trace -- the user's intent silently rewritten
    into copper they never asked for, with the jumper stranded where it was.
    """
    cases = [
        # the reported repro: default jumper, front face, rot 0; the trace
        # crowding the body is the back unit's supply run into the rail via
        ([_js_led(x=14.0, y=12.0, side="back", clk=True)], {}),
        # everything off default: jumper dragged, quarter-turned and mounted
        # on the BACK with the traced 3V3 hookup, an oblique front unit; the
        # crowding traces are the jumper's own links, in a rotated frame
        ([_js_led(x=6.0, y=6.0, rot=37, clk=True)],
         {"x": 12.0, "y": 10.0, "rot": 90, "side": "back", "via": False}),
    ]
    for leds, clk_state in cases:
        _set_design(ui, _clamped(leds), None, clk=clk_state)
        label = _describe(leds[0]) + f" jumper={clk_state or 'default'}"
        side = clk_state.get("side", "front")
        pads = ui.js("() => clkInfo().pads.map(p => [p[0], p[1], p[2]])")
        assert len(pads) == 3, f"{label}: the jumper lost a pad: {pads}"
        bends_before = ui.js(_BEND_COUNT)
        fringe = ui.js(_FRINGE_CLICKS)
        assert fringe, (
            f"{label}: no CLK leg midpoint competes with the grab box, so "
            "the fringe half of this test is vacuous; move the parts until "
            "a trace crowds the jumper again")
        spots = list(pads) + [("fringe", x, y) for x, y in fringe]
        assert len(spots) >= 4, f"{label}: 3 pads + >=1 fringe spot, got {spots}"
        for name, px, py in spots:
            ui.click_mm(px, py, side=side)
            sel = ui.selected()
            assert sel and sel["kind"] == "jumper", (
                f"{label}: clicking the {name} spot at ({px:.2f},{py:.2f}) "
                f"selected {sel and sel['kind']} instead of the jumper; the "
                "click meant to move the part went somewhere else")
        assert ui.js(_BEND_COUNT) == bends_before, (
            f"{label}: clicking the pads silently added "
            f"{ui.js(_BEND_COUNT) - bends_before} hand-bend(s) to a trace; "
            "that copper ships on the board and the user never asked for it")
        offered = ui.js(_HINT_STILL_OFFERED)
        assert offered["offered"] > 0, (
            f"{label}: no '+' anywhere along {offered['legs']} routed legs; "
            "the dead zone overcorrected and bends are unreachable")
    ui.assert_clean("jumper pads vs bend hint")


@pytest.mark.browser
def test_the_previewed_internal_traces_bend_the_same_copper_the_board_builds(ui):
    """A free-place unit's internal traces are drawn along the polyline the
    board emits, hand-placed bends included.

    unitTracePts and pcb.unit_trace_pts are written twice like the via-less
    router above, and their bends are real copper right between the parts
    the user just placed: a drift draws a trace across laminate the fab
    pours over, or hides one that is really there.
    """
    from minibadge_designer import pcb

    cases = [  # off the defaults: back side, rotations, non-0805 sizes
        _js_led(size="1206", rot=90,
                adv={"rx": -6.0, "ry": -3.0, "rrot": 45, "lrot": 0,
                     "vx": 3.0, "vy": 6.0},
                anodes=[[6.0, 6.0]], vnodes=[[7.0, 9.5], [9.0, 12.0]]),
        _js_led(side="back", size="0603", rot=180, layout="inline",
                adv={"rx": 5.0, "ry": 4.0, "rrot": 0, "lrot": 90,
                     "vx": -4.0, "vy": -5.0},
                anodes=[[14.0, 6.0], [12.0, 5.0]], vnodes=[[6.0, 13.0]]),
        # No bends: the straight two-point contract must agree too.
        _js_led(rot=270, adv={"rx": -5.0, "ry": 3.0, "rrot": 90, "lrot": 0,
                              "vx": 5.0, "vy": -3.0}),
    ]
    off_by, compared = [], 0
    for d in cases:
        leds = _clamped([d])
        _set_design(ui, leds)
        for which in ("a", "v"):
            js = ui.js("([w]) => unitTracePts(state.leds[0], w).pts", [which])
            board = pcb.unit_trace_pts(_py_led(leds[0]), which)
            if len(js) != len(board):
                off_by.append(f"{_describe(d)} trace {which}: the board bends "
                              f"it {len(board)} points, the canvas {len(js)}")
                continue
            compared += 1
            gap = _gap(js, board)
            if gap > _PARITY_TOL:
                off_by.append(
                    f"{_describe(d)} trace {which} is {gap:.4f} mm out: board "
                    f"{[tuple(round(v, 3) for v in p) for p in board]}, canvas "
                    f"{[tuple(round(v, 3) for v in p) for p in js]}")
    assert compared, ("no trace compared, so this test proved nothing; "
                      "check unitTracePts is still reachable")
    assert not off_by, (
        f"{len(off_by)} internal traces differ between the copper drawn and "
        "the copper built; unitTracePts and pcb.unit_trace_pts have "
        "drifted:\n" + "\n".join(off_by))
    ui.assert_clean("internal trace parity")


@pytest.mark.browser
def test_a_clicked_trace_bend_shows_selected_and_delete_removes_only_it(ui):
    """Clicking a bend handle selects that one dot, and Delete removes
    exactly it, never a neighbour.

    The selection drives the accent ring the user aims at; if Delete acts on
    a stale or wrong slot, the badge ships a trace the user believes they
    re-routed. Off the defaults on purpose: a back-side 1206 bending its
    pad-to-via stub, the trace that only free placement exposes at all.
    """
    leds = [_js_led(side="back", size="1206",
                    adv={"rx": -6.0, "ry": -3.0, "rrot": 0, "lrot": 0,
                         "vx": 4.0, "vy": 5.0},
                    vnodes=[[6.0, 13.0], [13.0, 16.0]])]
    _set_design(ui, leds)
    ui.click_mm(13.0, 16.0, side="back")
    sel = ui.selected()
    assert sel and sel["kind"] == "lednode", f"clicking a bend selected {sel}"
    assert sel.get("trace") == "v" and sel.get("node") == 1, \
        f"the wrong dot is selected: {sel}"
    ui.page.keyboard.press("Delete")
    vn = ui.js("() => state.leds[0].vnodes")
    assert vn == [[6.0, 13.0]], \
        f"Delete should remove only the selected bend, left {vn}"
    assert ui.selected() is None, "a deleted bend must not stay selected"
    ui.assert_clean("bend select and delete")


@pytest.mark.browser
@pytest.mark.parametrize("rings", [_HEX, _DONUT], ids=["hex", "donut"])
def test_the_preview_never_offers_a_spot_the_board_would_move_the_unit_off(ui, rings):
    """Every spot the canvas accepts on a custom outline is one the board keeps.

    `unitInsideBoard` is the client's copy of the generator's containment test
    (`outline.buffer(-0.55).contains(unit_footprint)`, webapp.py).  It is
    deliberately one-directional (0.555 mm against the server's 0.55 mm), so
    the canvas may refuse a spot the generator would have taken, but must never
    accept one the generator refuses: that direction is a unit the user placed
    over a cut-out, silently relocated somewhere else in the download.

    A hole in the outline is the case that matters; a convex shape is satisfied
    by the bounding box alone.

    The probes are the canvas's *own* acceptance boundary, found by sliding
    each unit outward until `unitInsideBoard` flips and bisecting.  A lattice
    of round numbers cannot test this rule: the whole margin in dispute is
    5 µm wide, so a grid of 3 mm probes stays green even with the client's
    clearance cut to 0.5 mm (measured, before this test was rewritten).
    """
    from shapely.geometry import Polygon

    from minibadge_designer import pcb

    poly = Polygon(rings[0], rings[1:])
    solid = poly.buffer(-0.55)
    safe = pcb.unit_safe(pcb.BadgeSpec(outline=rings))
    # Somewhere solid to start from, then push toward whatever edge is nearest:
    # the cut-out for the donut, the slanted sides for the hex.
    seeds = [(1.5 + 0.5 * i, 1.5 + 0.5 * j) for i in range(35) for j in range(35)]
    dirs = [(1, 0), (-1, 0), (0, 1), (0, -1), (0.6, 0.8), (-0.6, -0.8),
            (0.6, -0.8), (-0.6, 0.8)]
    leds = _unit_matrix(rots=(0, 37))
    _set_design(ui, leds, rings=rings)
    edge = ui.js(_EDGE_OF_ACCEPTANCE, [leds, seeds, dirs])

    probed, lies = 0, []
    for d, row in zip(leds, edge):
        for spot in row:
            if spot is None:
                continue   # this unit never left solid board along this ray
            here = dict(d, x=spot[0], y=spot[1])
            if _gap([pcb.clamp_led_obj(_py_led(here), safe)], [spot]) > _PARITY_TOL:
                continue   # the safe rect stopped it first; that is the clamp
                           # test's rule, not this one's
            probed += 1
            if not solid.contains(pcb.unit_footprint(_py_led(here), safe)):
                lies.append(f"{_describe(d)}: the canvas still accepts "
                            f"({spot[0]:.4f}, {spot[1]:.4f}), where the "
                            "generator relocates the unit")

    assert probed >= len(leds), (
        f"only {probed} boundary spots came back for {len(leds)} units, so the "
        "5 µm margin this rule is about was barely tested; either the seed "
        "lattice no longer lands on this outline, or the safe rect is now "
        "clamping units before the outline gets a say")
    assert not lies, (
        f"{len(lies)} of {probed} spots at the edge of what the canvas accepts "
        "are spots the generator moves the unit away from, so the download "
        "will not match the preview; unitInsideBoard() and the webapp's "
        "outline.buffer(-0.55) containment test have drifted:\n"
        + "\n".join(lies[:12]))
    ui.assert_clean("solid-board parity")


@pytest.mark.browser
def test_art_is_kept_off_the_pin_captions_the_same_way_in_both(ui):
    """The band of art the canvas carves out for a caption is the printed one.

    `captionBoxes` and `pcb.caption_boxes` are the keepout around the silk that
    names each connector pin.  The preview erases art inside it, and
    `textOverParts` refuses to build a design whose text lands in it, so a
    drift both hides art the fab will print over the caption and warns about
    text that is actually fine.
    """
    from minibadge_designer import pcb

    off_by = []
    for pins in (None, ("1", "2", "7", "8"), ("2", "7", "9", "16"), ("1",)):
        # Captions are opt-in now, and reserve nothing while they are off --
        # which is the state this parity question is only interesting in.
        drawn = ui.js("(p) => { state.pins = p ? p : ALL_PINS.slice();"
                      " state.pinlabels = true; return captionBoxes(); }",
                      list(pins) if pins is not None else None)
        board = pcb.caption_boxes(pcb.ALL_PINS if pins is None else pins, True)
        label = "all" if pins is None else pins
        if len(drawn) != len(board):
            off_by.append(f"pins {label}: the canvas carves {len(drawn)} "
                          f"caption boxes, the board carves {len(board)}")
            continue
        for js, b in zip(drawn, board):
            gap = max(abs(u - v) for u, v in zip(js, b))
            if gap > _PARITY_TOL:
                off_by.append(
                    f"pins {label}: caption keepout is "
                    f"{tuple(round(v, 3) for v in b)} on the board but "
                    f"{tuple(round(v, 3) for v in js)} on the canvas, "
                    f"{gap:.4f} mm out")

    ui.assert_clean("caption keepout parity")
    assert not off_by, (
        "art is carved away from the pin captions differently in the preview "
        "than on the board; captionBoxes() and pcb.caption_boxes have "
        "drifted:\n" + "\n".join(off_by[:8]))


# ===========================================================================
# The fab Gerber download (the ⬇ Gerbers button)
# ===========================================================================
@pytest.mark.kicad
@pytest.mark.needs("kicad")
def test_fab_download_waits_for_the_authors_warning_to_be_acknowledged(ui):
    """Ticking the fab package opens the author's caveat as a gate: declining it
    downloads nothing, and only "I understand" releases the package.

    That zip goes straight to a board house, so this is the one moment the
    app can say "give it a once-over in KiCad first" before real money is
    spent.  A notice that appears next to an already-started download warns
    nobody; the acknowledgement has to come first.

    The gate used to hang off a Gerbers button of its own.  There is one Download
    button now and the fab package is a tick inside it, which is exactly the kind
    of rearrangement that quietly drops a gate: the warning has to fire for the
    tick, and it has to fire whether the fab package is downloaded alone or
    alongside the KiCad project.
    """
    page = ui.page
    ui.show_panel("leds")
    assert ui.add_led() is True
    idx = len(ui.leds()) - 1
    # Off the defaults the plot path reads: the new unit mounts on the back.
    ui.card("ledlist", idx).locator("select.s").select_option(
        "back", timeout=ELEMENT_TIMEOUT)
    ui.wait_state(f"state.leds[{idx}].side === 'back'")
    assert ui.blocking() == [], ui.toast_texts()

    # Every download this page ever starts lands here; the declined path
    # below asserts against the whole list, not a race-prone instant.
    # The download event only fires once the server has finished plotting
    # (~2 s), so "no download yet" right after a decline proves nothing: a
    # wrongly-started export would still be in flight.  The request log is
    # the honest oracle: the POST is issued in the same task chain as the
    # acknowledgement, so on a decline it must never appear at all.
    downloads = []
    page.on("download", lambda d: downloads.append(d))
    gerber_posts = []
    page.on("request", lambda r: gerber_posts.append(r.url)
            if r.url.endswith("/gerbers") or r.url.endswith("/bundle") else None)
    dialog_open = "document.getElementById('fabwarn').classList.contains('open')"

    def ask_for_gerbers(*, with_kicad):
        """Open the picker, tick the fab package, press Download."""
        page.click("#download", timeout=ELEMENT_TIMEOUT)
        page.wait_for_selector("#dlbox.open", timeout=CONTROL_TIMEOUT)
        for key, want in (("kicad", with_kicad), ("gerbers", True),
                          ("design", False)):
            box = page.locator(f"#dl-{key}")
            if box.is_checked() != want:
                box.click(timeout=CONTROL_TIMEOUT)
        page.click("#dlgo", timeout=ELEMENT_TIMEOUT)

    def expect_dialog(should_be_open, why):
        # Give the UI its beat, then assert, so a missing (or lingering)
        # dialog reads as an assertion naming the defect, not a raw timeout.
        want = dialog_open if should_be_open else f"!{dialog_open}"
        try:
            page.wait_for_function(f"() => {want}", timeout=ELEMENT_TIMEOUT)
        except PWTimeout:
            pass
        assert ui.js(f"() => {dialog_open}") is should_be_open, why

    # --- declining the warning downloads nothing -------------------------
    ask_for_gerbers(with_kicad=False)
    expect_dialog(True, "ticking the fab package skipped the author's warning")
    assert downloads == [], (
        "the fab zip started downloading before the warning was answered")
    page.click("#fabcancel", timeout=ELEMENT_TIMEOUT)
    expect_dialog(False, "declining the warning left the dialog up")
    # Half a second is generous: a wrongly-released export issues its POST
    # in the same microtask chain as the dialog resolving.
    page.wait_for_timeout(500)
    assert gerber_posts == [] and downloads == [], (
        "declining the author's warning still started the fab export; the "
        "gate is decoration")

    # --- and again with the board alongside it, since that is a second path --
    ask_for_gerbers(with_kicad=True)
    expect_dialog(True, "the warning is skipped when the fab package rides along "
                        "with the KiCad project")
    page.click("#fabcancel", timeout=ELEMENT_TIMEOUT)
    expect_dialog(False, "declining the warning left the dialog up")
    page.wait_for_timeout(500)
    assert gerber_posts == [] and downloads == [], (
        "declining the warning still started the download when the fab package "
        "was one piece of several")

    # --- acknowledging it releases the fab package ------------------------
    ask_for_gerbers(with_kicad=False)
    expect_dialog(True, "the warning must gate every fab download, not one")
    with page.expect_download(timeout=GENERATE_TIMEOUT) as dl:
        page.click("#fabok", timeout=ELEMENT_TIMEOUT)
    expect_dialog(False, "acknowledging the warning left the dialog up")
    assert dl.value.suggested_filename.endswith("-gerbers.zip"), (
        dl.value.suggested_filename)
    path = ui.downloads_dir / "fab.zip"
    dl.value.save_as(path)
    with zipfile.ZipFile(path) as zf:
        exts = {n.rsplit(".", 1)[-1].lower() for n in zf.namelist()}
    # The full board-house contract lives in test_webapp.py; here it is
    # enough that the browser received the fab package, not the project zip.
    assert {"gtl", "gbl", "drl"} <= exts, exts
    assert len(gerber_posts) == 1, (
        f"one acknowledgement must release exactly one export, saw "
        f"{len(gerber_posts)} POSTs to /gerbers")
    ui.assert_clean("fab gerber gate flow")


@pytest.mark.browser
def test_the_trace_endpoint_drags_only_onto_valid_targets(ui):
    """Dragging a via-less trace's endpoint can only land it somewhere legal
    (a same-net connector pad or another unit's same-net pad), and
    double-clicking it returns the run to the automatic nearest pad.

    The endpoint is real copper: a drop in open space, on a wrong-net pad,
    or into a chain loop would ship a trace that powers nothing. The move
    handler snaps to the nearest of a pre-validated candidate list, so the
    invariant to hold is "term is always exactly one of the candidates the
    validator offered".
    """
    page = ui.page
    leds = [_js_led(x=7.0, y=7.0, novia=True),
            _js_led(x=14.0, y=14.0, color="blue", novia=True)]
    _set_design(ui, leds)

    def endpoint(i):
        return ui.js(f"() => noviaRoute(state.leds[{i}]).pts.at(-1)")

    def drag_endpoint(frm, to):
        sx, sy = ui.board_to_client(frm[0], frm[1], "front")
        dx, dy = ui.board_to_client(to[0], to[1], "front")
        page.mouse.move(sx, sy)
        page.mouse.down()
        page.mouse.move(dx, dy, steps=6)
        mid = ui.js("() => drag && drag.cands ? drag.cands.length : 0")
        page.mouse.up()
        return mid

    # Drop near the far GND pad (16): the endpoint snaps to that pad, and the
    # candidate list existed while the drag was live (that is what the user
    # sees highlighted).
    n_cands = drag_endpoint(endpoint(0), (19.05, 19.05))
    assert n_cands >= 4, (
        f"only {n_cands} destinations were on offer mid-drag; the same-net "
        "pads or the sibling unit are missing from the highlight set")
    assert ui.js("() => state.leds[0].term") == {"pad": "16"}
    assert endpoint(0) == [19.05, 19.05]

    # Drop onto LED 2's cathode pad: the run chains onto the sibling.
    target = ui.js("""() => {
      const t = geomOf(state.leds[1]);
      return unitPoint(state.leds[1], t.ledK[0], t.ledK[1]);
    }""")
    drag_endpoint(endpoint(0), target)
    assert ui.js("() => state.leds[0].term") == {"unit": 1}
    assert not ui.js("() => noviaRoute(state.leds[0]).tight"), (
        "the chained run should route clear: the target pad must not count "
        "as a hazard")

    # While LED 1 ends on LED 2, LED 2 must not be offered LED 1 back: a
    # loop feeds nothing, so it never appears among the candidates. Its three
    # same-net connector pads must still be there; an empty list here would
    # mean the validator broke, not that the loop was excluded.
    cands = ui.js("() => noviaTermTargets(state.leds[1]).map(c => c.term)")
    assert len(cands) >= 3, f"LED 2 lost its connector-pad destinations: {cands}"
    assert not any(c.get("unit") == 0 for c in cands), cands

    # Double-click the endpoint: back to the automatic nearest pad.
    ex, ey = endpoint(0)
    cx, cy = ui.board_to_client(ex, ey, "front")
    page.mouse.dblclick(cx, cy)
    assert ui.js("() => state.leds[0].term ?? null") is None
    ui.assert_clean("trace endpoint drag")


# ===========================================================================
# First-session defaults (dogfood findings F1/F2/F5/F6)
# ===========================================================================
def test_the_first_session_defaults_never_greet_the_user_with_a_warning(ui):
    """A fresh design's defaults compose cleanly: the starter LED sits on
    the back (the standard minibadge build: glowing through a window at the
    host badge), a newly added text lands clear of parts instead of on top
    of them, an emptied LED panel says what to do next, and text answers the
    same double-click-to-rotate gesture as everything else on the canvas.

    The text/panel/gesture checks were top dogfood findings; the back-side
    LED default is a deliberate owner decision that reversed the dogfood-era
    front default.
    """
    page = ui.page
    # The starter LED defaults to the back face.
    assert ui.js("() => state.leds[0].side") == "back", (
        "LED units should default to the back side")

    # A new text lands on solid board AND clear of the starter LED: no
    # self-inflicted warning. The app's own predicates are the oracle.
    ui.show_panel("text")
    page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    page.locator("#textlist .item input.tx").first.fill("hello badge")
    page.wait_for_timeout(300)
    assert ui.js("() => textOverParts(state.texts[0])") is False, (
        "new text spawned on top of an existing part; the first thing the "
        "user sees after typing is a warning the app caused itself")
    assert ui.js("() => textOnSolidBoard(state.texts[0])") is True

    # Text rotates with the same gesture as LEDs and art.
    t = ui.js("() => [state.texts[0].x, state.texts[0].y]")
    x, y = ui.board_to_client(t[0], t[1], "front")
    page.mouse.dblclick(x, y)
    page.wait_for_timeout(200)
    assert ui.js("() => state.texts[0].rot") == 90, (
        "double-click rotates LEDs and art but not text")

    # An emptied LED panel guides instead of going blank.
    ui.show_panel("leds")
    ui.remove_card("ledlist", 0)
    ui.wait_state("state.leds.length === 0")
    hint = ui.js("() => document.getElementById('ledlist').innerText.trim()")
    assert hint, "the emptied LED panel is a blank void with no next step"
    ui.assert_clean("first-session defaults")


# ===========================================================================
# Stacking order: the parts are on top of the board, on the canvas as in life
# ===========================================================================
@pytest.mark.browser
@pytest.mark.parametrize(
    "cover",
    [
        # The board-shape part: on a custom outline it covers the whole board,
        # which is what made this reachable everywhere at once.
        "shapeel",
        # An artwork layer spread over the same ground.
        "art",
    ],
)
def test_a_far_side_units_ghost_is_grabbed_before_the_board_under_it(ui, cover):
    """Dragging a component never drags the board out from under it.

    A unit mounted on the far face is drawn as a dashed ghost and is meant to
    be draggable from either view.  Both the custom-outline part and a
    full-board art layer sit under every ghost, so if either wins the hit
    test the user reaches for an LED on a custom-shaped board and moves the
    entire outline (or a logo) instead -- a destructive answer to an ordinary
    drag, and one that is only obvious after the board redraws.
    """
    ui.custom_square_board(30)

    # One unit on the BACK face: from the FRONT view it is only a ghost, so
    # this is the weakest case for the component and the strongest for
    # whatever is underneath it.
    ui.js(
        """(cover) => {
            state.leds.length = 0;
            state.leds.push({x: 10.16, y: 6.0, color: 'red', side: 'back',
                             rot: 0, layout: 'inline', size: '0805',
                             reverse: false, novia: false, farled: false,
                             adv: null});
            state.art.length = 0;
            if (cover === 'art') {
                state.art.push({kind: 'rect', material: 'silk', side: 'front',
                                cx: 10.16, cy: 10.16, wmm: 26, h: 26, rot: 0,
                                overrides: []});
            }
            renderLedList(); renderArtList(); draw();
        }""",
        cover,
    )
    ui.wait_state("state.leds.length === 1")

    before = ui.js("() => [state.leds[0].x, state.leds[0].y]")
    el_before = ui.js("() => state.shape.elements.map(e => [e.cx, e.cy])")
    art_before = ui.js("() => state.art.map(a => [a.cx, a.cy])")

    # The ghost really is over the thing that must not win, or the drag would
    # prove nothing at all.
    assert ui.js("([x, y]) => unitHit(state.leds[0], x, y)", before), (
        "the probe point is not on the unit; this drag would test nothing")

    ui.drag_mm(before, (before[0] + 4.0, before[1]), side="front")

    assert ui.js("() => selected && selected.kind") == "led", (
        f"the drag grabbed {ui.js('() => selected && selected.kind')!r} "
        f"instead of the unit standing on top of the {cover}")
    after = ui.js("() => [state.leds[0].x, state.leds[0].y]")
    assert after[0] - before[0] > 2.0, (
        f"the ghost did not follow the pointer: {before} -> {after}")
    assert ui.js("() => state.shape.elements.map(e => [e.cx, e.cy])") == el_before, (
        "dragging a component moved the board outline")
    assert ui.js("() => state.art.map(a => [a.cx, a.cy])") == art_before, (
        "dragging a component moved an artwork layer")
    ui.assert_clean("ghost over board grab")


#: How much of a part's own ink must still be visible once decoration is
#: added beneath it.  Not 100 %: a 40 %-alpha dashed outline has a few pixels
#: that composite over pale silk to the silk colour by coincidence.
_PART_STAYS_VISIBLE = 0.95


@pytest.mark.browser
@pytest.mark.parametrize("under", ["art", "text"])
def test_decoration_added_under_a_part_never_rubs_the_part_out(ui, under):
    """Artwork and text are printed ON the board; the parts sit on top of it.

    The interesting case is a unit mounted on the FAR face.  Its copper is
    carved out of this face's decoration already, but the dashed ghost that
    says "a part stands here" is not -- and the ghost is what the user
    reaches for to drag it.  Decoration painted over it leaves the user
    dragging something they cannot see, which is a lying preview: the class
    of failure DRC can never catch.

    Measured as a relationship, so no colour or coordinate is asserted: count
    the pixels the part inks on a bare board, then require nearly all of them
    to still differ from the decoration-only render once the decoration is
    added underneath.  "Nearly" because the ghost outline is a 40 %-alpha
    dash, and a handful of its pixels composite over pale silk to exactly the
    silk colour by coincidence.  Calibrated on this board: 646-648 of 648
    ghost pixels survive with the parts painted last, and 417 of 648 with the
    text pass moved back after them, so the bar sits far from both.
    """
    ui.js(
        """(under) => {
            state.leds.length = 0; state.art.length = 0; state.texts.length = 0;
            // Back face: from the front view this unit is only its ghost.
            const led = {x: 10.16, y: 10.16, color: 'red', side: 'back',
                         rot: 0, layout: 'inline', size: '0805',
                         reverse: false, novia: false, farled: false,
                         adv: null};
            state.leds.push(led);
            window.__led = {...led};
            window.__patch = unitBBox(led);   // the whole unit footprint
            window.__deco = under === 'art'
                ? {kind: 'rect', material: 'silk', side: 'front', cx: 10.16,
                   cy: 10.16, wmm: 14, h: 14, rot: 0, overrides: []}
                : {x: 10.16, y: 10.16, text: 'MMMMMMMMM', size: 7,
                   side: 'front', font: 'archivo', material: 'silk', rot: 0};
            state.leds.length = 0;
            renderLedList(); draw();
        }""",
        under,
    )
    # A web font inks nothing until it has loaded.
    ui.page.wait_for_function("() => document.fonts.status === 'loaded'",
                              timeout=ELEMENT_TIMEOUT)

    shot = """(mode) => {
        const [x0, y0, x1, y1] = window.__patch;
        state.leds.length = 0; state.art.length = 0; state.texts.length = 0;
        if (mode.includes('deco')) {
            if (window.__deco.kind) state.art.push(window.__deco);
            else state.texts.push(window.__deco);
        }
        if (mode.includes('part')) state.leds.push({...window.__led});
        renderLedList(); renderArtList(); renderTextList(); draw();
        const px = Math.round(VIEW.tx + x0 * SCALE);
        const py = Math.round(VIEW.ty + y0 * SCALE);
        const w = Math.max(1, Math.round((x1 - x0) * SCALE));
        const h = Math.max(1, Math.round((y1 - y0) * SCALE));
        return [...cvF.getContext('2d').getImageData(px, py, w, h).data];
    }"""
    bare = ui.js(shot, "bare")
    part_only = ui.js(shot, "part")
    deco_only = ui.js(shot, "deco")
    both = ui.js(shot, "deco+part")

    pixels = range(0, len(bare), 4)
    ghost = [i for i in pixels if bare[i:i + 3] != part_only[i:i + 3]]
    assert ghost, "the unit inks nothing over its own footprint; nothing to test"
    assert [i for i in pixels if bare[i:i + 3] != deco_only[i:i + 3]], (
        f"the {under} inks nothing over the unit's footprint, so it could "
        "not hide the part even if it were drawn on top")

    survived = [i for i in ghost if deco_only[i:i + 3] != both[i:i + 3]]
    assert len(survived) >= _PART_STAYS_VISIBLE * len(ghost), (
        f"adding {under} under the unit rubbed out "
        f"{len(ghost) - len(survived)} of the {len(ghost)} pixels the unit "
        f"draws ({100 * len(survived) / len(ghost):.1f}% left): the {under} "
        "is painted over the part, and the user is left dragging something "
        "the preview does not show")
    ui.assert_clean(f"part over {under}")


# ===========================================================================
# Self-inflicted problems: the app must not create the error it then reports
# ===========================================================================
#: An outline with a wide slot cut out of the lower half, where the text
#: placer's second-choice spot lives.  Holes are the case a part-avoiding
#: placer misses, because a hole is not a part.
_SLOTTED = [[(0.16, 0.16), (20.16, 0.16), (20.16, 20.16), (0.16, 20.16)],
            [(4.0, 14.0), (16.0, 14.0), (16.0, 17.0), (4.0, 17.0)]]


@pytest.mark.browser
def test_a_newly_added_text_never_lands_on_a_hole_in_the_board(ui):
    """"+ Add text" picks a spot; that spot has to be one the user can keep.

    The placer already avoided parts.  It judged the spot with a four-letter
    stand-in, though, so on a board with a hole it could pick a place where
    the stand-in fits and a real word does not: the user typed one word and
    was told their text hangs over the board, having never chosen the
    position, with the download blocked until they moved it themselves.
    """
    # A front unit over the top of the board, so the placer's first choice is
    # taken and it has to consider the spots further down -- one of which is
    # the slot.
    _set_design(ui, [_js_led(x=10.16, y=5.0, side="front")], rings=_SLOTTED)
    ui.wait_state("state.leds.length === 1")

    ui.show_panel("text")
    ui.page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    ui.page.locator("#textlist .item input.tx").first.fill("DOGFOOD")
    ui.page.wait_for_timeout(300)

    where = ui.js("() => [state.texts[0].x, state.texts[0].y]")
    assert ui.js("() => textOnSolidBoard(state.texts[0])"), (
        f"the app parked its own new text at {where}, which is not on solid "
        "board; the user is blamed for a position they never chose")
    assert not ui.blocking(), (
        f"adding text and typing one word left the design unbuildable: "
        f"{ui.blocking()}")
    ui.assert_clean("new text placement")


@pytest.mark.browser
@pytest.mark.parametrize("width", [1600, 1280, 960, 700])
def test_every_fabrication_choice_shows_its_longest_option(ui, width):
    """A dropdown the user cannot read is a control they cannot use.

    Mask colour, finish and via tenting share one row in a rail that narrows
    with the window.  Three columns cannot hold all three longest options at
    the narrow end, so the row wraps; whichever way it lays out, the widest
    option of every select must still fit inside its box.
    """
    ui.set_viewport(width, 900)
    ui.show_panel("shape")
    clipped = ui.js(
        """() => {
            const bad = [];
            for (const id of ['mask', 'finish', 'tenting']) {
                const el = document.getElementById(id);
                if (!el || !el.offsetParent) continue;
                const cs = getComputedStyle(el);
                const c = document.createElement('canvas').getContext('2d');
                c.font = cs.fontSize + ' ' + cs.fontFamily;
                let widest = 0, worst = '';
                for (const o of el.options) {
                    const w = c.measureText(o.text).width;
                    if (w > widest) { widest = w; worst = o.text; }
                }
                const room = el.clientWidth - parseFloat(cs.paddingLeft)
                                            - parseFloat(cs.paddingRight);
                // 12 px is a conservative allowance for the native arrow.
                if (widest > room - 12) {
                    bad.push({id, worst, needs: Math.round(widest + 12),
                              has: Math.round(room)});
                }
            }
            return bad;
        }""")
    assert not clipped, (
        f"at {width}px these fabrication dropdowns clip their longest "
        f"option: {clipped}")
    ui.assert_clean(f"fab row at {width}px")


# ---------------------------------------------------------------------------
# text metrics: the preview's box versus the ink the board actually gets
# ---------------------------------------------------------------------------
#: How far the editor's box may sit OUTSIDE the ink the board prints, mm.  The
#: editor no longer estimates: it lays the string out from the per-character
#: metrics the server ships for that face (textpoly.char_metrics, served at
#: /fonts/<key>.metrics.json) using text_geometry's own rules, so the only
#: residue is text_geometry's closing `simplify(0.005)`, which can drop the one
#: vertex that reached furthest, plus the 0.01-font-unit outward rounding the
#: table is shipped with (1.7 um at the largest text the UI offers).  That
#: bounds the disagreement at ~0.0085 mm; measured worst case over the sweep
#: below is 0.0057 mm in Python and 0.0045 mm through the browser.  0.015 mm is
#: under a thirteenth of the 0.2 mm silk-to-edge budget it feeds, so a box that
#: passes here cannot cost the user a millimetre of usable board.
#:
#: It used to be a FRACTION of the string width, 0.08, because the editor
#: measured strings with the browser's own rasteriser and the two disagreed by
#: up to 6.1% of the width (Pacifico "gjpqy", whose "g" and "o" the browser
#: draws from that face's default alternates while textpoly takes the plain cmap
#: glyph).  On a 17 mm string that tolerance was 1.36 mm per side, and the app
#: padded its fit check by the same amount: it refused a text that had
#: millimetres of real clearance.  A tolerance that scales with the string is
#: the wrong shape for a box that is now exact.
TEXT_BOX_TOL_MM = 0.015
#: How far the board's ink may fall outside the editor's box, mm.  This is the
#: direction that ships a design the user never saw -- silk clipped away at the
#: fab while the preview looked fine -- and it is structurally zero: every ink
#: extent in the table is rounded OUTWARD, and the editor walks the string with
#: the same advances text_geometry does.  Measured 0.000000 mm over 1442 Python
#: cases and 624 browser cases, so this is float noise money, not tolerance:
#: 1e-3 mm is twelve orders of magnitude above double-precision residue on
#: millimetre coordinates and still a fiftieth of the smallest thing a fab
#: prints.
TEXT_INK_ESCAPE_MM = 0.001

#: Strings chosen for what they stress: digits (no descenders, and Press Start
#: 2P raises them off the baseline), a mixed-case phrase with descenders, one
#: wide cap, a run of pure descenders, all caps, near-zero-width glyphs, a kern
#: pair, punctuation whose advance dwarfs its ink, a pair the browser would
#: happily draw as one ligature glyph, blanks at both ends (the ink is centred
#: on the INK, not the advance), and a character no bundled face has, which
#: text_geometry skips with a half-em gap while the browser draws it from a
#: fallback face.
_METRIC_STRINGS = ["418", "made by half", "W", "gjpqy", "MINIBADGE", "iIl1",
                   "Wg", ".", "fi ffl", " g ", "\u0416\u0416", "A\u0416B"]
#: Both ends of the size range the UI offers plus the ordinary middle.  Nothing
#: here should depend on size -- every metric scales linearly -- so a case that
#: fails at one size and passes at another is the finding, not the noise.
_METRIC_SIZES = [0.6, 1.5, 12.0, 119.0]


@pytest.mark.browser
def test_the_editors_text_box_is_the_ink_the_board_will_actually_get(ui):
    """What the editor measures a string as, and what the fab prints, are the
    same box in every bundled typeface.

    The editor sizes a face by CSS pixels, which the browser scales by the EM;
    the board sizes it by CAP HEIGHT (`textpoly.text_geometry`).  The ratio
    between the two is a property of the individual font -- the bundled faces
    run 0.348 to 1.000 -- and the editor assumed 0.700 for all of them.  So it
    drew Press Start 2P 43% oversized and Special Elite at half size, and then
    decided whether the text fitted the board from that wrong width: a real
    design was refused for "hanging over the board edge" with four millimetres
    of clearance on both sides, and no test in the suite could see it.

    The same class of error hides in the vertical band (a baseline off by a
    fraction of the cap height), in kerning and ligatures (the server applies
    neither, walking the string one glyph at a time) and in centring (the
    server centres the ink, not the advance).  All of it shows up as this one
    box disagreeing, so this is where it is measured -- against the server's
    own geometry, per face, not against a recorded number.

    The editor closes that gap by not guessing: the server ships each face's
    per-character ink extents (`textpoly.char_metrics`) and the editor lays the
    string out from those, by text_geometry's own rules.  So this test now holds
    it to an ABSOLUTE tolerance a hundredth of the old one, and to a far tighter
    one in the direction that ships clipped ink.  Loosening either constant back
    to a fraction of the string width would restore the 1.36 mm phantom margin
    that refused the design this test was written for.
    """
    from minibadge_designer import textpoly

    fonts = list(textpoly.FONTS)
    assert len(fonts) > 1, (
        "one bundled face cannot show a per-face scale error; the defect this "
        "test exists for was invisible in the two faces whose cap ratio "
        "happens to be 0.700")

    # Load every face into the page and wait for the real outlines to arrive.
    # Measured against a fallback face every comparison below is meaningless,
    # and NOT stable: `document.fonts.check()` is the obvious wait and the wrong
    # one -- it answers "can this be rendered without loading anything new",
    # which a fallback satisfies, so it returns true before a single .ttf has
    # arrived. It passed alone and failed 83 of 384 cases in a full-file run,
    # on whichever two faces the server was slowest to serve. Ask the font set
    # what it actually holds instead.
    # Two arrivals per face, and the box depends on the SECOND one: ensureFont
    # fetches the outlines for the canvas and, separately, the per-character
    # metrics the box is computed from.  Waiting only for the outlines measures
    # the browser's own rasteriser through measuredRun100 -- the fallback path,
    # padded by 8% -- and calls it exact.
    ui.js("(keys) => keys.forEach(k => ensureFont(k))", fonts)
    ui.page.wait_for_function(
        """(keys) => keys.every(k => {
               if (!FONT_INK[k] || !FONT_INK[k].chars) return false;
               for (const f of document.fonts) {
                   if (f.family === `bm-${k}` && f.status === 'loaded') return true;
               }
               return false;
           })""",
        arg=fonts, timeout=UPLOAD_TIMEOUT)

    cases = [[s, size, f] for f in fonts
             for s in _METRIC_STRINGS for size in _METRIC_SIZES]
    # One round trip: 528 separate evaluate() calls would dominate the runtime.
    # `exact` comes back with each box because it decides how much the app pads
    # its own fit check by, and a box that is only as good as the browser's
    # rasteriser must not be measured as though it were the server's own.
    boxes = ui.js(
        """(cs) => cs.map(([text, size, font]) => {
               const t = {text, size, font, x: 0, y: 0, rot: 0};
               return [textLocalBox(t), textInk(t).run.exact];
           })""", cases)

    # The comparison is a loop, and a loop over nothing passes: if the page
    # answers with fewer boxes than cases (an exception inside the map, a
    # renamed textLocalBox) the sweep silently covers zero strings.
    pairs = list(zip(cases, boxes))
    assert len(boxes) == len(cases), (
        f"asked the editor to measure {len(cases)} strings and got "
        f"{len(boxes)} answers; an exception inside the map leaves every check "
        "below sweeping a shorter list than it says it does")
    assert all(exact for _box, exact in boxes), (
        "the editor measured "
        f"{sum(1 for _b, e in boxes if not e)} of {len(boxes)} strings with the "
        "browser's own rasteriser instead of the metrics the server shipped for "
        "that face. Every comparison below then measures the fallback path, "
        "which is allowed to be 6% out and is padded to match -- the exact path "
        "is the one the user's fit check runs on")
    assert len(pairs) > 100, (
        f"asked the editor to measure {len(cases)} strings across "
        f"{len(fonts)} faces and got {len(boxes)} boxes back; a sweep this "
        "short is not sweeping the parameter the defect hid in")

    bad, inked = [], 0
    for (text, size, font), (box, _exact) in pairs:
        geom = textpoly.text_geometry(text, font, size)
        if geom is None:
            # Nothing in the string is in this face (the Cyrillic pair, in ten
            # of the twelve).  The board gets no ink, so the editor must claim
            # none either: a box drawn round glyphs a fallback face supplied is
            # a box round ink that will not exist.
            assert box[2] - box[0] == 0, (
                f"{font} prints nothing at all for {text!r}, yet the editor "
                f"gives it a {box[2] - box[0]:.3f} mm wide box; it is measuring "
                "a face the board will not use")
            continue
        inked += 1
        sx0, sy0, sx1, sy1 = geom.bounds
        # The server's baseline sits at +size/2 from the text's centre and its
        # ink is centred on x=0, which is the frame textLocalBox reports in.
        # Each entry is how far the INK reaches past that edge of the box, so
        # positive is ink the editor did not know about and negative is box the
        # ink never fills.  The two are not symmetric: an oversized box costs
        # the user usable board, an undersized one ships silk the fab clips off
        # a design whose preview looked perfect, and only the second is a board
        # nobody previewed.
        off = {
            "left": box[0] - sx0, "right": sx1 - box[2],
            "top": box[1] - sy0, "bottom": sy1 - box[3],
        }
        for edge, v in off.items():
            room = TEXT_INK_ESCAPE_MM if v > 0 else TEXT_BOX_TOL_MM
            if abs(v) > room:
                bad.append({"font": font, "text": text, "size": size,
                            "side": edge, "off_mm": round(v, 5),
                            "allowed_mm": room,
                            "editor": [round(c, 3) for c in box],
                            "board": [round(c, 3) for c in (sx0, sy0, sx1, sy1)]})

    assert inked > 100, (
        f"only {inked} of {len(cases)} strings put ink on the board at all; the "
        "edge comparison below covers those and nothing else")
    assert not bad, (
        f"{len(bad)} of {inked * 4} string edges measure differently in the "
        f"editor than they print on the board. Positive off_mm means the ink "
        f"reaches OUTSIDE the editor's box (text ships clipped while the "
        f"preview looks fine); negative means the box is bigger than the ink "
        f"(good designs get refused as overhanging). Worst first: "
        f"{sorted(bad, key=lambda d: -abs(d['off_mm']))[:4]}")
    ui.assert_clean("text metrics sweep")


#: How far the ink on the board may differ from the box the editor drew, mm,
#: end to end: through the metrics fetch, the fit check, /generate and the art
#: clip.  Every stage in that chain is exact to ~0.005 mm (text_geometry's
#: closing simplify) and the board file is written to four decimals, so 0.02 mm
#: is four times the residue and still a tenth of the 0.2 mm silk-to-edge
#: budget -- far too tight for a whole clip margin to hide in.
TEXT_ROUNDTRIP_MM = 0.02


@pytest.mark.browser
@pytest.mark.parametrize("text,font,key", [
    # A string wider than the pad rows, nudged sideways: the horizontal limit.
    ("MINIBADGE", "pressstart", "ArrowLeft"),
    # Pacifico is the face whose default alternates the old measured box got
    # wrong, and "g" hangs a descender below the baseline, so nudging this one
    # upward tests the vertical limit against the ink's TOP edge.
    ("Wg", "pacifico", "ArrowUp"),
])
def test_a_text_the_editor_accepts_arrives_with_all_of_its_ink(ui, text, font, key):
    """Text the editor says fits comes back from /generate whole.

    The editor and the server each decide, separately, how close to the board
    edge a string's ink may go: the editor to refuse the download, the server to
    clip the polygons it writes.  When those two numbers disagree the user is
    told one thing and sent another -- and they did.  The editor allowed ink
    0.2 mm from the edge while the art pipeline clipped text at the 0.5 mm
    ARTWORK frame, so a text pushed outward lost a 0.3 mm strip off every glyph
    that reached the edge, in the file that went to the fab, with nothing
    anywhere saying so.  No check on either side alone can see that: the box can
    be exactly right and the download still short.

    The text is walked to the edge the way a user walks it there -- arrow-key
    nudges until the editor objects, then one step back -- so it ends up inside
    the last 0.2 mm of what the editor allows, which is exactly the band a
    too-tight server clip eats.
    """
    ui.show_panel("text")
    ui.page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    card = ui.page.locator("#textlist .item").first
    card.locator("input.tx").fill(text)
    card.locator("select.fnt").select_option(font)
    # 2.1 mm caps: big enough that this string's ink reaches the board edge
    # while still fitting between them, so the edge rule is what stops it. The
    # UI default of 1.5 mm clears the edge from every position and would test
    # nothing at all.
    card.locator("input.szn").fill("2.1")
    card.locator("input.szn").dispatch_event("change")
    # The box is computed from the face's per-character metrics; before those
    # arrive the editor measures the browser's own glyphs and pads 8%, which is
    # a different rule than the one under test.
    ui.page.wait_for_function(
        "(f) => FONT_INK[f] && FONT_INK[f].chars", arg=font, timeout=UPLOAD_TIMEOUT)

    # Select on the canvas, which also takes focus off the text field: arrow
    # keys move a caret in there and a text layer out here.
    where = ui.js("() => [state.texts[0].x, state.texts[0].y]")
    ui.click_mm(where[0], where[1], side="front")
    ui.wait_state("selected && selected.kind === 'text'")
    fits = "() => textOnSolidBoard(state.texts[0])"
    assert ui.js(fits), (
        f"a {font} {text!r} at 2.1 mm does not fit the board where the app put "
        "it, so there is nothing to walk to the edge")
    for _ in range(60):
        ui.page.keyboard.press(key)
        if not ui.js(fits):
            break
    else:
        raise AssertionError(
            f"60 nudges of {key} never reached a position the editor refuses; "
            "this text never got near the edge, so the clip under test never "
            "applied")
    ui.page.keyboard.press({"ArrowLeft": "ArrowRight", "ArrowUp": "ArrowDown"}[key])
    assert not ui.blocking(), (
        f"one step back from the edge the editor still refuses to build its own "
        f"text at {ui.js('() => [state.texts[0].x, state.texts[0].y]')}: "
        f"{ui.blocking()}")
    box = ui.js("() => textBBox(state.texts[0])")

    path = ui.downloads_dir / "roundtrip.zip"
    _download(ui.page, kicad=True).save_as(path)
    with zipfile.ZipFile(path) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".kicad_pcb"))
        board = zf.read(name).decode()

    from minibadge_designer import pcb

    pts = [(float(x) - pcb.ORIGIN, float(y) - pcb.ORIGIN)
           for blk in re.findall(
               r"\(gr_poly \(pts (.*?)\) \(stroke[^\n]*?\(layer \"F\.SilkS\"\)",
               board)
           for x, y in re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", blk)]
    assert len(pts) > 20, (
        f"the download carries only {len(pts)} silk vertices for {text!r}, so "
        "the comparison below has almost no ink to compare")
    ink = (min(x for x, _y in pts), min(y for _x, y in pts),
           max(x for x, _y in pts), max(y for _x, y in pts))

    # Two guarantees at once, and they pull against each other: the ink has to
    # stay off the routed edge (silk touching Edge.Cuts is a violation KiCad
    # fails the board for) and it has to be ALL there (a clip tighter than the
    # editor's own rule is a slice taken out of the user's string).
    edge = pcb.OUTLINE
    inside = min(ink[0] - edge[0], ink[1] - edge[1],
                 edge[2] - ink[2], edge[3] - ink[3])
    assert inside > 0, (
        f"silk ink reaches {-inside:.4f} mm past the board outline; KiCad fails "
        f"that as silkscreen clipped by board edge. ink={ink}")
    lost = max(ink[0] - box[0], ink[1] - box[1], box[2] - ink[2], box[3] - ink[3])
    assert lost < TEXT_ROUNDTRIP_MM, (
        f"the board's silk stops {lost:.4f} mm inside the box the editor drew, "
        f"so that much of every glyph at the edge was clipped out of the "
        f"download the user sends to the fab. editor box={box}, board ink={ink}")
    ui.assert_clean("text round trip")


# ---------------------------------------------------------------------------
# restoring a saved design: the autosave has to hand back what it took
# ---------------------------------------------------------------------------
@pytest.mark.browser
@pytest.mark.parametrize("kind", ["png", "svg"])
def test_a_saved_design_with_artwork_restores_the_artwork(ui, logo_png, logo_svg,
                                                          kind):
    """A design that went into the autosave with a picture comes back with it.

    `designJSON()` stores an uploaded image as a `data:` URL and `dataUrlFile()`
    turns it back into a File on the way in.  It did that with
    `fetch(dataUrl)` -- and this app serves `connect-src 'self' blob:`, which
    makes that fetch a blocked request.  So restoring ANY design containing a
    picture threw `TypeError: Failed to fetch` and dropped every artwork layer:
    the user pressed Restore, the bar went away, and their work did not come
    back.  It shipped for four days because `dataUrlFile` predates the policy
    and nothing tied the two together.

    What let it ship was a coverage hole, not a missing instrument.  The restore
    runs inside a `try/catch`, so no exception reached `window.onerror` and
    nothing looked like a crash -- but the browser logs the policy refusal as a
    console error, which the `page` fixture here does collect and does fail on.
    So `ui.assert_clean()` would have caught this the first time any test
    restored a design holding a picture.  None ever did.  That is the hole this
    test fills, and it is why the assertions below are functional: a test that
    leaned only on the error capture would pass again the moment the failure
    became quiet.  Relatedly, the policy's own test
    (`test_resource_limits.py::test_the_content_policy_still_allows_what_the_page
    _actually_loads`) enumerates the directives by hand, and a list of what
    someone remembered is not a check on what the page does.

    Both upload kinds run because they take different paths back in: a PNG is
    decoded straight to a raster, while an SVG is re-rasterised through an
    object URL, so a decoder that only handled one would still lose half the
    designs.
    """
    upload = ({"name": "logo.png", "mimeType": "image/png", "buffer": logo_png}
              if kind == "png" else
              {"name": "logo.svg", "mimeType": "image/svg+xml", "buffer": logo_svg})

    ui.show_panel("art")
    ui.page.set_input_files("#artfile", files=[upload], timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    saved = ui.js("() => JSON.parse(JSON.stringify(designJSON()))")
    assert saved["art"] and saved["art"][0].get("srcData"), (
        "the design was saved without the picture's bytes, so this test would "
        "be asserting that nothing comes back from nothing")

    outcome = ui.page.evaluate(
        """async (d) => {
            try { await restoreDesign(d); return {ok: true}; }
            catch (e) { return {ok: false, err: String(e)}; }
        }""", saved)

    # Asserted before any wait on the restored state: a wait first turns a clean
    # "the restore threw" into a timeout on an unrelated predicate, which is a
    # slower failure that names the wrong thing.
    assert outcome["ok"], (
        f"restoring a design that holds one {kind.upper()} threw "
        f"{outcome.get('err')!r}; every artwork layer in it is gone and the "
        "user is looking at an empty board")
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    # The layer has to arrive usable, not merely counted: the drawing and the
    # download both read `img`, so a layer without one is a layer in name only.
    assert ui.js("() => !!(state.art[0].img && state.art[0].img.width > 0)"), (
        "the artwork layer came back with no decoded image behind it")
    after = ui.js("() => JSON.parse(JSON.stringify(designJSON()))")
    assert after["art"][0]["srcData"] == saved["art"][0]["srcData"], (
        "the restored layer carries different bytes than were saved, so the "
        "next save would drift further from the picture the user uploaded")
    ui.assert_clean(f"restore a design holding one {kind}")


# ---------------------------------------------------------------------------
# design files: saving work to a file and opening it again
# ---------------------------------------------------------------------------
#: What a design file must announce about itself before the loader trusts a byte
#: of it. Stated here, not imported from the page: a check that reads the same
#: constants the code reads cannot notice them changing, and a file written by
#: today's build has to stay readable by tomorrow's.
DESIGN_FILE_FORMAT = "minibadge-design"
DESIGN_FILE_VERSION = 2


def _design_file(pg) -> str:
    """The text `⬇ Save design` would write, without going near the filesystem."""
    return pg.evaluate("() => designFileText()")


def _download(page, *, kicad=False, gerbers=False, design=False):
    """Open the one Download button's picker, tick exactly these, download.

    Every test that used to click Download and get a zip comes through here: the
    button opens a picker now, and the default ticks (board + design file) would
    otherwise hand the test a bundle under a different name.
    """
    page.click("#download", timeout=ELEMENT_TIMEOUT)
    page.wait_for_selector("#dlbox.open", timeout=CONTROL_TIMEOUT)
    for key, want in (("kicad", kicad), ("gerbers", gerbers), ("design", design)):
        box = page.locator(f"#dl-{key}")
        if box.is_checked() != want:
            box.click(timeout=CONTROL_TIMEOUT)
    with page.expect_download(timeout=GENERATE_TIMEOUT) as caught:
        page.click("#dlgo", timeout=ELEMENT_TIMEOUT)
    return caught.value


def _save_design(page, *, kicad=False, gerbers=False):
    """Download through the picker with the design file ticked; return the file.

    Saving is no longer a button of its own -- it is a tick inside the one
    Download button -- so every test that used to click Save comes through here.
    """
    return _download(page, kicad=kicad, gerbers=gerbers, design=True)


def _open_design(pg, text, name="d.minibadge.json", *, clean=True):
    """Hand `loadDesignFile` a file and return its outcome, never hanging.

    `clean` clears `dirty` first, the way saving does. A load with unsaved work
    on screen stops to ask, and a test that did not expect the question would
    wait on a dialog nobody clicks -- which is the one thing this file forbids
    (see the module docstring). The question has its own test.

    The in-page race is the second half of that promise: if a load ever fails to
    settle again, this fails in five seconds with a message, instead of stopping
    the suite for as long as pytest is willing to wait.
    """
    return pg.evaluate(
        """async (arg) => {
            if (arg.clean) dirty = false;
            const f = new File([arg.text], arg.name, {type: 'application/json'});
            return await Promise.race([
                loadDesignFile(f),
                new Promise(r => setTimeout(
                    () => r({error: 'HUNG: loadDesignFile never settled'}), 5000)),
            ]);
        }""",
        {"text": text if isinstance(text, str) else json.dumps(text),
         "name": name, "clean": clean})


def _write_design(tmp_path, doc, name="d.minibadge.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


_UPLOADS_AND_BOARD = """async () => {
    const fd = designFormData();
    const files = [];
    for (const [k, v] of fd.entries()) {
        if (!(v instanceof File)) continue;
        const d = await crypto.subtle.digest('SHA-256', await v.arrayBuffer());
        files.push([k, v.name, v.size, [...new Uint8Array(d)]
            .map(b => b.toString(16).padStart(2, '0')).join('')]);
    }
    const r = await fetch('/generate', {method: 'POST', body: designFormData()});
    if (!r.ok) return {files, error: (await r.json()).error};
    const raw = new Uint8Array(await r.arrayBuffer());
    let b64 = '';
    for (let i = 0; i < raw.length; i += 0x8000) {
        b64 += String.fromCharCode(...raw.subarray(i, i + 0x8000));
    }
    return {files, zip: btoa(b64)};
}"""


def _uploads_and_board(pg):
    """What the user is actually handed: every uploaded byte, and the board.

    Not a comparison of two JSON blobs -- a design file is allowed to be
    canonicalised on the way through (absent fields becoming their defaults),
    and a test that failed on that would be measuring the format's tidiness
    rather than the promise.  The promise is that the BOARD does not change, so
    the zip is compared entry by entry.  Entry contents rather than the zip's
    own bytes, because the archive carries mtimes.
    """
    got = pg.evaluate(_UPLOADS_AND_BOARD)
    assert "error" not in got, (
        f"the design would not build, so this test cannot compare boards: "
        f"{got.get('error')}")
    zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(got["zip"])))
    return {
        "files": got["files"],
        "board": {n: hashlib.sha256(zf.read(n)).hexdigest()
                  for n in sorted(zf.namelist()) if not n.endswith("/")},
    }


@pytest.mark.browser
def test_a_saved_design_file_reopens_as_the_same_board(ui, logo, tmp_path):
    """Work saved to a file and opened later builds the same board.

    Before this existed the only way to keep a design was the browser's own
    autosave, which one tab can hold at a time and any cleared site data
    removes; the KiCad zip the app offers is a board, not a design -- no art
    layers, no text objects, no uploaded pictures -- so it cannot be reopened
    to keep working, and the app told users it could.

    The assertion is deliberately not "the two files match".  A file is free to
    be canonicalised on the way through; what the user is promised is that the
    BOARD does not change, so what is compared is the generator's whole input:
    the params string and a digest of every uploaded byte.  Measured on this
    design, the zip that comes out is identical file for file.
    """
    ui.show_panel("art")
    ui.page.set_input_files("#artfile", files=[logo], timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    ui.js("""() => {
        $('name').value = 'keepsake';
        state.mask = 'purple'; $('mask').value = 'purple';
        state.finish = 'hasl'; $('finish').value = 'hasl';
        state.pins = ['2', '7', '9', '15'];
        state.texts = [{x: 10.16, y: 16.4, text: 'made by half', size: 1.6,
                        side: 'back', font: 'blackops', material: 'copper', rot: 14}];
        ensureFont('blackops');
        renderPinGrid(); renderTextList(); renderArtList(); draw();
    }""")
    ui.page.wait_for_timeout(400)
    before = _uploads_and_board(ui.page)

    download = _save_design(ui.page)
    assert download.suggested_filename == "keepsake.minibadge.json", (
        f"the design saved as {download.suggested_filename!r}; a design and the "
        "board zip beside it have to arrive under the same name or a folder of "
        "them cannot be told apart")
    saved = tmp_path / download.suggested_filename
    download.save_as(str(saved))
    doc = json.loads(saved.read_text())
    assert doc.get("$format") == DESIGN_FILE_FORMAT, (
        "the file does not identify itself, so no loader can tell it from any "
        f"other JSON: {list(doc)[:6]}")
    assert doc.get("formatVersion") == DESIGN_FILE_VERSION, (
        f"saved as format {doc.get('formatVersion')!r}, expected "
        f"{DESIGN_FILE_VERSION}; a file with no version it can be migrated from "
        "is a file that stops opening the next time the schema moves")

    fresh = ui.page.context.new_page()
    try:
        fresh.goto(ui.page.url, timeout=LOAD_TIMEOUT)
        fresh.wait_for_function("() => ready === true", timeout=LOAD_TIMEOUT)
        fresh.evaluate("""() => {
            const bar = document.getElementById('restorebar');
            if (bar) bar.remove(); // the autosave's offer is not what is under test
        }""")
        fresh.set_input_files("#designfile", str(saved), timeout=ELEMENT_TIMEOUT)
        fresh.wait_for_function("() => state.art.length === 1", timeout=UPLOAD_TIMEOUT)
        fresh.wait_for_timeout(600)
        after = _uploads_and_board(fresh)
    finally:
        fresh.close()

    assert after["files"] == before["files"], (
        "the pictures the board is built from changed across a save and open:\n"
        f"  before {before['files']}\n  after  {after['files']}")
    changed = [n for n in set(before["board"]) | set(after["board"])
               if before["board"].get(n) != after["board"].get(n)]
    assert not changed, (
        f"reopening the saved design builds a different board: {changed} differ "
        "inside the zip. What the user gets back is not what they saved")
    ui.assert_clean("save a design file and open it again")


@pytest.mark.browser
def test_a_saved_design_file_reopens_byte_for_byte(ui, tmp_path):
    """A design that has been opened once saves the same bytes every time after.

    A format that drifts on every trip through the app cannot be trusted with a
    year-old file: each open would add or lose a little and nobody could tell a
    real change from the format breathing.

    The comparison starts at the SECOND save on purpose.  The first one is
    written straight from the editor, where a field the user never touched is
    simply absent; the loader fills those in with the defaults the app was
    already behaving as if they had (`clk: false`, `nodes: []`).  So the first
    save canonicalises, and every save after it is a fixed point -- which is the
    property that matters, because from then on any difference is a real one.
    """
    ui.show_panel("text")
    ui.page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    ui.page.locator("#textlist .item input.tx").first.fill("fixed point")
    ui.js("""() => {
        state.leds.push({x: 13.5, y: 12.5, color: 'green', side: 'back', rot: 270,
                         layout: 'stacked', size: '0603', reverse: false,
                         novia: false, farled: false, clk: false, adv: null});
        state.art.push({kind: 'star', material: 'glow', side: 'front',
                        fname: 'Star', cx: 6, cy: 6, wmm: 4, h: 4, rot: 30,
                        sides: 5, mode: 'threshold', threshold: 128,
                        invert: false, flip: false, palette: [], overrides: [],
                        caches: null, empty: false});
        renderLedList(); renderArtList(); draw();
    }""")
    ui.page.wait_for_timeout(300)

    # one pass through the loader to canonicalise, then the two saves compared
    assert _open_design(ui.page, _design_file(ui.page), "fp.minibadge.json").get("ok"), (
        "the app would not reopen the file it had just written")
    ui.wait_state("state.texts.length === 1", timeout=UPLOAD_TIMEOUT)
    ui.page.wait_for_timeout(400)
    second = _design_file(ui.page)
    assert _open_design(ui.page, second, "fp2.minibadge.json").get("ok"), (
        "a canonical design file would not reopen")
    ui.page.wait_for_timeout(400)
    third = _design_file(ui.page)

    # savedAt differs by design; the design itself must not.
    a, b = json.loads(second)["design"], json.loads(third)["design"]
    drift = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    assert not drift, (
        f"a design changed just by being opened and saved again: {drift}. "
        f"before={ {k: a.get(k) for k in drift} } after={ {k: b.get(k) for k in drift} }")
    ui.assert_clean("open and re-save a design file")


#: Files that are not designs, or are designs with something unusable in them.
#: Every one of these used to do damage: the first four silently wiped the open
#: design and reported success, the null members wedged the editor permanently
#: (`draw()` threw on `t.text.trim()` from then on, so the canvas stopped
#: updating and only a reload recovered), and the unreadable-picture cases hung
#: forever because the image loader's error path never called its callback.
_BAD_FILES = [
    ("truncated-json", '{"$format": "minibadge-design", "formatVersion": 2, "des'),
    ("bare-number", "42"),
    ("bare-array", "[]"),
    ("bare-string", '"hello"'),
    ("empty-object", "{}"),
    ("no-format-tag", '{"formatVersion": 2, "design": {"name": "x"}}'),
    ("wrong-format-tag", '{"$format": "something-else", "formatVersion": 2, "design": {}}'),
    ("design-not-an-object", '{"$format": "minibadge-design", "formatVersion": 2, "design": 5}'),
    ("no-version", '{"$format": "minibadge-design", "design": {"name": "x"}}'),
    ("picture-is-not-a-picture", '{"$format": "minibadge-design", "formatVersion": 2,'
     ' "design": {"art": [{"kind": "image", "fname": "bad.png",'
     ' "srcData": "data:image/png;base64,bm90IGFuIGltYWdlIGF0IGFsbA=="}]}}'),
    ("data-url-with-no-comma", '{"$format": "minibadge-design", "formatVersion": 2,'
     ' "design": {"art": [{"kind": "image", "fname": "x.png",'
     ' "srcData": "data:image/png;base64"}]}}'),
]


@pytest.mark.browser
@pytest.mark.parametrize("label,text", _BAD_FILES, ids=[c[0] for c in _BAD_FILES])
def test_a_design_file_that_cannot_be_opened_leaves_the_open_design_alone(
        ui, tmp_path, label, text):
    """Trying a file is safe: if it cannot be opened, nothing on screen moves.

    That is the whole reason a load is worth having.  Without it the only way to
    find out whether a file is a design is to lose the design you already had --
    and losing it was silent: a file that parsed as `42` cleared the board and
    the status line said the load had succeeded.

    Four things are asserted, and the last is the one that catches the worst of
    them: the editor still WORKS afterwards.  A design carrying `text: null` used
    to leave every later `draw()` throwing, so the canvas froze mid-session with
    no message and no way back but a reload.
    """
    ui.show_panel("text")
    ui.page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    ui.page.locator("#textlist .item input.tx").first.fill("KEEP ME")
    ui.page.wait_for_timeout(250)
    before = ui.js("() => JSON.stringify(designJSON())")

    outcome = _open_design(ui.page, text, "bad.minibadge.json")

    assert not outcome.get("ok"), (
        f"{label}: the app accepted a file that is not a design it can open")
    assert outcome.get("error"), (
        f"{label}: the load failed and said nothing; the user is left looking at "
        "a board that did not change with no idea why")
    assert ui.js("() => JSON.stringify(designJSON())") == before, (
        f"{label}: the open design changed even though the file was refused. "
        "Trying a file must never cost the user the work they already had")
    still_works = ui.js("""() => {
        try {
            draw(); renderTextList(); renderArtList(); renderLedList();
            renderShapeOpts(); blockingProblems(); designFormData();
            return true;
        } catch (e) { return String(e); }
    }""")
    assert still_works is True, (
        f"{label}: the editor is wedged after the refusal -- {still_works}. "
        "Every later frame throws, so the canvas stops updating and only a "
        "reload recovers")
    ui.assert_clean(f"refuse a {label} design file")


@pytest.mark.browser
def test_a_design_file_from_a_newer_designer_is_refused_by_name(ui, tmp_path):
    """A file from a build we do not understand is refused whole, and says so.

    Loading the parts today's build happens to recognise would silently discard
    whatever the newer one added: the user would be editing a partial copy of
    their own design without being told which parts survived.
    """
    doc = {"$format": DESIGN_FILE_FORMAT, "formatVersion": DESIGN_FILE_VERSION + 1,
           "design": {"name": "from-the-future", "texts": []}}
    before = ui.js("() => JSON.stringify(designJSON())")
    outcome = _open_design(ui.page, doc, "future.minibadge.json")
    assert not outcome.get("ok"), "a design from a newer build was opened anyway"
    assert re.search(r"newer version", outcome["error"]), (
        f"the refusal does not say the file is from a newer build: {outcome['error']!r}")
    assert str(DESIGN_FILE_VERSION + 1) in outcome["error"], (
        f"the refusal does not name the format it could not read: {outcome['error']!r}")
    assert ui.js("() => JSON.stringify(designJSON())") == before
    ui.assert_clean("refuse a design file from a newer build")


#: Designs as three earlier builds of this app wrote them, and what each one has
#: to become. Expectations are stated here, independently of MIGRATIONS: a check
#: that read the migration table could not notice the table being wrong.
_OLD_SCHEMAS = [
    # Before per-pin control, a design stored which ROWS were kept; a row means
    # both of its corner pairs.
    ("rows-not-pins", {"rows": {"top": True, "bottom": False}},
     lambda ui: ui.js("() => state.pins") == ["1", "2", "7", "8"]),
    # "reverse" used to be a layout; it is now a flag on top of "stacked", and it
    # forces the 1206 footprint because the hole needs that pad spacing.
    ("reverse-as-a-layout",
     {"leds": [{"x": 10.16, "y": 10.16, "color": "red", "layout": "reverse",
                "size": "1206"}]},
     lambda ui: ui.js("() => [state.leds[0].layout, state.leds[0].reverse]")
                == ["stacked", True]),
    # The square/custom dropdown is gone. A design set to "square" could still be
    # carrying outline parts that were invisible on screen; honour what the save
    # showed, not what it stored.
    ("square-with-stranded-parts",
     {"shape": {"mode": "square", "smooth": 0.12,
                "elements": [{"kind": "circle", "op": "add", "cx": 10, "cy": 10,
                              "w": 8, "h": 8, "rot": 0}]}},
     lambda ui: ui.js("() => state.shape.elements.length") == 0),
]


@pytest.mark.browser
@pytest.mark.parametrize("label,design,check", _OLD_SCHEMAS,
                         ids=[c[0] for c in _OLD_SCHEMAS])
def test_a_design_saved_by_an_older_build_opens_the_way_it_looked(
        ui, label, design, check):
    """A design from an earlier schema opens as the board it was, not as junk.

    Three schema changes have already happened, and every one of them was
    handled by sniffing the saved shape -- with the version field stamped `1`
    throughout, so `1` in the wild means any of three layouts.  That cannot be
    undone; what it can be is the last time.  These cases pin the three
    migrations to the boards they produce, so the ladder that replaced the
    sniffing is checked against outcomes rather than against itself.
    """
    doc = {"$format": DESIGN_FILE_FORMAT, "formatVersion": 1, "design": design}
    outcome = _open_design(ui.page, doc, "old.minibadge.json")
    assert outcome.get("ok"), (
        f"{label}: a design from an older build would not open: {outcome}")
    assert check(ui), (
        f"{label}: it opened, but not as the board it was. state now: "
        f"pins={ui.js('() => state.pins')} "
        f"leds={ui.js('() => state.leds.map(L => [L.layout, L.reverse])')} "
        f"parts={ui.js('() => state.shape.elements.length')}")
    ui.assert_clean(f"open an old-schema design ({label})")


#: Real designs carrying something the editor cannot use. These are NOT refused:
#: a file is usually mostly good, and refusing all of it over one bad member
#: costs the user everything. They load with what fits and say what was left out.
#: Each one used to do damage: a null text field wedged the editor permanently,
#: because `draw()` called `t.text.trim()` on every frame from then on.
_SALVAGEABLE = [
    ("texts-not-a-list", {"texts": "nope"}, "text layers"),
    ("text-field-null", {"texts": [{"text": None, "x": 5, "y": 5}]}, None),
    ("null-text-member", {"texts": [None, {"text": "kept", "x": 5, "y": 5}]},
     "text layers"),
    ("null-art-member", {"art": [None]}, "artwork layers"),
    ("led-list-is-a-number", {"leds": 7}, "LED units"),
]


@pytest.mark.browser
@pytest.mark.parametrize("label,design,noted", _SALVAGEABLE,
                         ids=[c[0] for c in _SALVAGEABLE])
def test_a_design_file_with_an_unusable_part_loads_the_rest_and_says_so(
        ui, label, design, noted):
    """One bad layer costs the user that layer, not the whole file, and never
    silence.

    Silence is the failure being fixed: a file whose text list had become a
    string used to load as a design with no text at all, reporting success, so
    the user's only clue was the absence of something they had written.  And a
    null in the wrong place did worse than vanish -- it left every later frame
    throwing, so the canvas froze with no message at all.
    """
    doc = {"$format": DESIGN_FILE_FORMAT, "formatVersion": DESIGN_FILE_VERSION,
           "design": design}
    outcome = _open_design(ui.page, doc, "partial.minibadge.json")

    assert outcome.get("ok"), (
        f"{label}: a design with one unusable part was refused whole, which "
        f"costs the user everything else in the file: {outcome}")
    if noted:
        assert any(noted in n for n in outcome.get("notes") or []), (
            f"{label}: the {noted} that could not be loaded were dropped without "
            f"a word about it; notes were {outcome.get('notes')!r}")
    still_works = ui.js("""() => {
        try {
            draw(); renderTextList(); renderArtList(); renderLedList();
            renderShapeOpts(); blockingProblems(); designFormData();
            return true;
        } catch (e) { return String(e); }
    }""")
    assert still_works is True, (
        f"{label}: the editor is wedged after loading it -- {still_works}. Every "
        "later frame throws, so the canvas stops updating and only a reload "
        "recovers")
    # And the design has to be shippable, not merely on screen.
    assert isinstance(ui.js("() => designFormData().get('params')"), str), (
        f"{label}: the loaded design cannot even be serialised for download")
    ui.assert_clean(f"load a design with an unusable {label}")


#: The app's own limits, restated. Mirrors of webapp.MAX_ART / MAX_TEXTS /
#: MAX_LEDS and index.html's MAX_SHAPE_ELS: written literally so that raising one
#: on either side without the other goes red here rather than shipping a preview
#: that promises more than the zip contains.
CAPS = {"art": 8, "texts": 24, "leds": 64, "elements": 12}


@pytest.mark.browser
def test_a_design_file_cannot_ask_for_more_layers_than_the_board_gets(ui):
    """A file over the limits loads at the limits, and says what it dropped.

    The server keeps the first MAX_* of each list and silently discards the rest,
    so a file with forty artwork layers used to put forty on the canvas and eight
    on the board: the preview promised artwork the zip never contained, which is
    exactly the failure the "+ Image…" button refuses an over-cap layer to
    prevent.  The load path went straight past that guard.
    """
    design = {
        "art": [{"kind": "circle", "material": "silk", "cx": 5, "cy": 5, "wmm": 3}
                for _ in range(CAPS["art"] + 32)],
        "texts": [{"text": f"t{i}", "x": 5, "y": 5} for i in range(CAPS["texts"] + 176)],
        "leds": [{"x": 5, "y": 5, "color": "red"} for _ in range(CAPS["leds"] + 236)],
        "shape": {"mode": "custom", "smooth": 0.12,
                  "elements": [{"kind": "circle", "op": "add", "cx": 10, "cy": 10,
                                "w": 4, "h": 4} for _ in range(CAPS["elements"] + 48)]},
    }
    doc = {"$format": DESIGN_FILE_FORMAT, "formatVersion": DESIGN_FILE_VERSION,
           "design": design}
    outcome = _open_design(ui.page, doc, "greedy.minibadge.json")
    assert outcome.get("ok"), f"an over-large design would not open at all: {outcome}"

    got = ui.js("""() => ({art: state.art.length, texts: state.texts.length,
                          leds: state.leds.length,
                          elements: state.shape.elements.length})""")
    assert got == CAPS, (
        f"the editor is holding {got}, the board can carry {CAPS}. Whatever is "
        "over the limit is dropped from the download, so the preview would be "
        "showing work the fab never receives")
    # The download has to agree with the canvas, which is the actual promise.
    shipped = ui.js("""() => {
        const p = JSON.parse(designFormData().get('params'));
        return {art: (p.art || []).length, texts: (p.texts || []).length,
                leds: (p.leds || []).length,
                elements: ((p.shape || {}).elements || []).length};
    }""")
    assert shipped == got, (
        f"the canvas shows {got} but the download would carry {shipped}")
    for what in ("artwork layers", "text layers", "LED units", "board-shape parts"):
        assert any(what in n for n in outcome["notes"]), (
            f"nothing told the user their {what} were dropped; notes were "
            f"{outcome['notes']!r}")
    ui.assert_clean("load an over-large design file")


@pytest.mark.browser
def test_opening_a_second_design_while_the_first_is_still_loading_keeps_only_the_second(
        ui, logo):
    """Two loads at once end as the second design, not a mixture of both.

    Measured before the loader was split in two: the final design carried the
    second file's name and mask with FOUR artwork layers -- one from it and three
    from the first -- in scrambled order, and both loads reported success.  The
    old restore mutated `state` between its awaits, so any two runs interleaved.
    Nobody would author that on purpose, but a double-click on Open is enough.
    """
    ui.show_panel("art")
    ui.page.set_input_files("#artfile", files=[logo], timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    heavy = json.loads(_design_file(ui.page))          # carries a real picture
    heavy["design"]["name"] = "FIRST"
    light = {"$format": DESIGN_FILE_FORMAT, "formatVersion": DESIGN_FILE_VERSION,
             "design": {"name": "SECOND", "mask": "red", "art": [],
                        "texts": [{"text": "second", "x": 5, "y": 5}]}}

    result = ui.page.evaluate(
        """async (arg) => {
            dirty = false;
            const mk = d => new File([JSON.stringify(d)], 'x.minibadge.json',
                                     {type: 'application/json'});
            // deliberately not awaited in order: both in flight at once
            const a = loadDesignFile(mk(arg.first));
            const b = loadDesignFile(mk(arg.second));
            const [ra, rb] = await Promise.all([a, b]);
            return {first: ra, second: rb, name: $('name').value,
                    mask: state.mask, art: state.art.length,
                    texts: state.texts.map(t => t.text)};
        }""", {"first": heavy, "second": light})

    assert result["name"] == "SECOND" and result["mask"] == "red", (
        f"the design that won is {result['name']}/{result['mask']}, not the one "
        "opened last")
    assert result["art"] == 0 and result["texts"] == ["second"], (
        f"the two files merged: {result['art']} artwork layers and texts "
        f"{result['texts']} is neither file on its own")
    assert not result["first"].get("ok"), (
        "both loads reported success, so nothing in the app knows which design "
        "is on screen")
    ui.assert_clean("two design files opened at once")


@pytest.mark.browser
def test_a_crafted_colour_in_a_design_file_cannot_reach_the_art_panel_as_markup(
        ui, logo):
    """A palette colour out of a design file is three numbers, never markup.

    Those numbers are interpolated into a `style` attribute inside an innerHTML
    template, and `cssRGB`'s own comment says why it clamps them: "the first
    'load a design file' or share-link feature would make a crafted rgb entry
    able to close the attribute and add an event handler."  This is that feature.
    The guard was written in advance; this is the test it asked for.
    """
    ui.show_panel("art")
    ui.page.set_input_files("#artfile", files=[logo], timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    doc = json.loads(_design_file(ui.page))
    doc["design"]["art"][0]["mode"] = "palette"
    doc["design"]["art"][0]["palette"] = [
        {"rgb": ['255); background:url("javascript:alert(1)"', 0, 0], "material": "silk"},
        {"rgb": ['0,0,0)" onmouseover="alert(1)', 1, 2], "material": "copper"},
    ]
    assert _open_design(ui.page, doc, "xss.minibadge.json").get("ok"), (
        "the crafted design would not open, so this test proves nothing about "
        "what happens when it does")

    checked = ui.js("""() => {
        renderArtList();
        const panel = document.getElementById('artlist');
        const chips = [...panel.querySelectorAll('.chip')];
        return {
            rgb: state.art[0].palette.map(q => q.rgb),
            styles: chips.map(c => c.getAttribute('style')),
            handlers: chips.filter(c => c.getAttributeNames()
                .some(n => n.startsWith('on'))).length,
            markup: /onmouseover|javascript:/i.test(panel.innerHTML),
        };
    }""")
    # Both crafted entries have to still be there: a palette that was dropped
    # entirely would satisfy every check below without proving anything, and
    # "the swatches vanished" is not the guarantee being tested.
    assert len(checked["rgb"]) == 2 and len(checked["styles"]) == 2, (
        f"expected the two crafted swatches to load and render; got "
        f"{len(checked['rgb'])} colours and {len(checked['styles'])} swatches, so "
        "the clamp below was never exercised")
    assert all(all(isinstance(v, int) and 0 <= v <= 255 for v in rgb)
               for rgb in checked["rgb"]), (
        f"a palette colour survived as something other than three bytes: {checked['rgb']}")
    for style in checked["styles"]:
        assert re.fullmatch(r"background:rgb\(\d{1,3},\d{1,3},\d{1,3}\)", style or ""), (
            f"a swatch's style attribute is not a plain colour: {style!r}")
    assert checked["handlers"] == 0, "a swatch came out of the file with an event handler"
    assert not checked["markup"], (
        "the art panel's markup contains script the design file put there")
    ui.assert_clean("open a design file with a crafted palette colour")


@pytest.mark.browser
def test_the_zip_download_no_longer_claims_to_have_saved_the_design(ui):
    """Only saving a design file clears the unsaved-work warning.

    The KiCad zip holds a board: no art layers, no text objects, no uploaded
    pictures.  It cannot be reopened to keep working, so treating a zip download
    as "saved" armed the close-tab prompt to stay quiet on work that only existed
    on screen.  A design file really does hold the design, so that is what clears
    it.
    """
    ui.show_panel("text")
    ui.page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    ui.page.locator("#textlist .item input.tx").first.fill("unsaved")
    ui.page.wait_for_timeout(250)
    assert ui.js("() => dirty") is True, (
        "typing into a text layer did not mark the design unsaved, so the rest "
        "of this test would prove nothing")

    zipped = ui.page.evaluate(
        """async () => {
            const r = await fetch('/generate', {method: 'POST', body: designFormData()});
            if (r.ok) await r.arrayBuffer();
            return r.ok;
        }""")
    assert zipped, "the board would not generate, so the zip half is untested"
    assert ui.js("() => dirty") is True, (
        "generating the board cleared the unsaved-work flag. The zip contains no "
        "design, so the next close would discard the user's work without asking")

    _save_design(ui.page)
    assert ui.js("() => dirty") is False, (
        "saving a design file left the design marked unsaved, so the app now "
        "warns about work that is safely in a file")
    ui.assert_clean("zip versus design-file save")


@pytest.mark.browser
@pytest.mark.parametrize("answer", ["cancel", "continue"])
def test_opening_a_file_over_unsaved_work_asks_first(ui, answer):
    """Unsaved work is not replaced without being asked about.

    Opening a design replaces the one on screen, and there is no undo for that.
    The question only appears when there is something to lose -- work that is not
    in a file yet -- and Cancel has to leave the board exactly as it was, because
    a dialog whose safe answer still costs you something is worse than no dialog.
    """
    ui.show_panel("text")
    ui.page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    ui.page.locator("#textlist .item input.tx").first.fill("MINE")
    ui.page.wait_for_timeout(250)
    assert ui.js("() => dirty") is True, "nothing unsaved, so nothing to ask about"
    before = ui.js("() => JSON.stringify(designJSON())")

    doc = {"$format": DESIGN_FILE_FORMAT, "formatVersion": DESIGN_FILE_VERSION,
           "design": {"name": "THEIRS", "mask": "red",
                      "texts": [{"text": "theirs", "x": 5, "y": 5}]}}
    # Not awaited: the load is parked on the dialog until it is answered.
    ui.page.evaluate(
        """(doc) => {
            window.__load = loadDesignFile(
                new File([JSON.stringify(doc)], 'theirs.minibadge.json',
                         {type: 'application/json'}));
        }""", doc)
    ui.page.wait_for_selector("#askbox.open", timeout=CONTROL_TIMEOUT)

    if answer == "cancel":
        ui.page.click("#askno", timeout=CONTROL_TIMEOUT)
        outcome = ui.page.evaluate("() => window.__load")
        assert outcome.get("cancelled"), f"Cancel did not cancel the load: {outcome}"
        assert ui.js("() => JSON.stringify(designJSON())") == before, (
            "declining the question still replaced the design; there is no undo "
            "for that and the user said no")
        assert ui.js("() => dirty") is True, (
            "the work is still only on screen, so it is still unsaved")
    else:
        ui.page.click("#askyes", timeout=CONTROL_TIMEOUT)
        outcome = ui.page.evaluate("() => window.__load")
        assert outcome.get("ok"), f"accepting the question did not load the file: {outcome}"
        assert ui.js("() => [$('name').value, state.mask, state.texts.map(t => t.text)]") \
            == ["THEIRS", "red", ["theirs"]], "the file was accepted but not applied"
    assert ui.page.locator("#askbox.open").count() == 0, (
        "the dialog is still open after being answered")
    ui.assert_clean(f"answer the replace question with {answer}")


# ---------------------------------------------------------------------------
# one Download button, with the pieces ticked
# ---------------------------------------------------------------------------
#: (ticks, what arrives). The names matter as much as the contents: a folder of
#: these has to be tellable apart, and the fab package has to stay uploadable
#: without being repacked, which is why it nests as its own zip rather than as a
#: subfolder inside the bundle.
_DOWNLOAD_CASES = [
    ("board only", dict(kicad=True), "keeper.zip",
     ["keeper/BOM.csv", "keeper/README.txt", "keeper/keeper.kicad_pcb",
      "keeper/keeper.kicad_pro"]),
    ("design only", dict(design=True), "keeper.minibadge.json", None),
    ("board and design", dict(kicad=True, design=True), "keeper-bundle.zip",
     ["keeper.minibadge.json", "keeper/BOM.csv", "keeper/README.txt",
      "keeper/keeper.kicad_pcb", "keeper/keeper.kicad_pro"]),
]


@pytest.mark.browser
@pytest.mark.parametrize("label,ticks,fname,contents", _DOWNLOAD_CASES,
                         ids=[c[0] for c in _DOWNLOAD_CASES])
def test_the_download_button_delivers_exactly_what_was_ticked(
        ui, label, ticks, fname, contents):
    """One button, and you get the pieces you asked for -- no more, no less.

    Three buttons used to live in that corner (KiCad project, Gerbers, Save
    design) and a user had to already know which of them kept their work; two of
    the three could not be reopened at all.  One button with three ticks says it
    instead.  What is asserted here is the part a rearrangement breaks quietly:
    that a tick actually changes what lands, and that one tick still gets the
    plain artifact rather than a zip wrapped around a zip.
    """
    ui.js("() => { $('name').value = 'keeper'; draw(); }")
    ui.page.wait_for_timeout(200)
    download = _download(ui.page, **ticks)
    assert download.suggested_filename == fname, (
        f"{label}: arrived as {download.suggested_filename!r}, expected {fname!r}")
    path = ui.downloads_dir / download.suggested_filename
    download.save_as(path)
    if contents is None:
        doc = json.loads(path.read_text())
        assert doc.get("$format") == DESIGN_FILE_FORMAT, (
            f"{label}: a design file was asked for and something else arrived")
    else:
        with zipfile.ZipFile(path) as zf:
            assert sorted(zf.namelist()) == contents, (
                f"{label}: the zip holds {sorted(zf.namelist())}")
    ui.assert_clean(f"download {label}")


@pytest.mark.browser
def test_the_download_picker_refuses_to_download_nothing(ui):
    """With no piece ticked there is nothing to download, and the button says so.

    Better than a click that appears to work and produces an empty zip, or a
    request the server answers with an error the user never asked for.
    """
    ui.page.click("#download", timeout=ELEMENT_TIMEOUT)
    ui.page.wait_for_selector("#dlbox.open", timeout=CONTROL_TIMEOUT)
    for key in ("kicad", "gerbers", "design"):
        box = ui.page.locator(f"#dl-{key}")
        if box.is_checked():
            box.click(timeout=CONTROL_TIMEOUT)
    assert ui.page.locator("#dlgo").is_disabled(), (
        "Download is still clickable with nothing selected")
    assert "Nothing selected" in ui.page.inner_text("#dlnote"), (
        f"the dialog does not say why: {ui.page.inner_text('#dlnote')!r}")
    ui.page.click("#dlcancel", timeout=CONTROL_TIMEOUT)
    assert ui.page.locator("#dlbox.open").count() == 0
    ui.assert_clean("download picker with nothing ticked")


@pytest.mark.browser
def test_the_download_picker_remembers_what_was_ticked_last_time(ui):
    """The ticks survive a reload.

    Someone ordering five badges picks the same combination five times; a picker
    that resets every visit is a picker that gets in the way. Kept in
    localStorage, so it is per-browser and costs nothing if it is missing.
    """
    ui.page.click("#download", timeout=ELEMENT_TIMEOUT)
    ui.page.wait_for_selector("#dlbox.open", timeout=CONTROL_TIMEOUT)
    for key, want in (("kicad", False), ("gerbers", True), ("design", True)):
        box = ui.page.locator(f"#dl-{key}")
        if box.is_checked() != want:
            box.click(timeout=CONTROL_TIMEOUT)
    ui.page.click("#dlcancel", timeout=CONTROL_TIMEOUT)

    ui.page.reload(timeout=LOAD_TIMEOUT)
    ui.page.wait_for_function("() => ready === true", timeout=LOAD_TIMEOUT)
    ui.dismiss_restore_bar()
    ui.page.click("#download", timeout=ELEMENT_TIMEOUT)
    ui.page.wait_for_selector("#dlbox.open", timeout=CONTROL_TIMEOUT)
    ticked = ui.js("() => ['kicad','gerbers','design'].filter(k => $('dl-'+k).checked)")
    assert ticked == ["gerbers", "design"], (
        f"the picker came back with {ticked} ticked, not what was chosen last time")
    ui.page.click("#dlcancel", timeout=CONTROL_TIMEOUT)
    ui.assert_clean("download picker across a reload")


@pytest.mark.browser
def test_dropping_a_design_file_on_the_page_opens_it(ui):
    """A design file dragged anywhere onto the page opens it.

    The Open button is a small target for something the whole window can accept,
    and dropping a file is what anyone who has used a design tool before tries
    first. The overlay has to appear while the file is over the window and go
    away again whether the drop happens or the drag leaves, because an overlay
    that stays covers the app it is inviting you to use.
    """
    doc = {"$format": DESIGN_FILE_FORMAT, "formatVersion": DESIGN_FILE_VERSION,
           "design": {"name": "dropped-in", "mask": "red",
                      "texts": [{"text": "dropped", "x": 10.16, "y": 10.16}]}}
    handle = ui.page.evaluate_handle(
        """(arg) => {
            const dt = new DataTransfer();
            dt.items.add(new File([JSON.stringify(arg)], 'dropped.minibadge.json',
                                  {type: 'application/json'}));
            return dt;
        }""", doc)
    ui.page.dispatch_event("body", "dragenter", {"dataTransfer": handle})
    assert ui.page.locator("#dropzone.on").count() == 1, (
        "nothing showed that the page would take the file")
    ui.page.dispatch_event("body", "dragleave", {"dataTransfer": handle})
    assert ui.page.locator("#dropzone.on").count() == 0, (
        "dragging the file away left the overlay covering the app")

    ui.page.dispatch_event("body", "dragenter", {"dataTransfer": handle})
    ui.page.dispatch_event("body", "drop", {"dataTransfer": handle})
    # Waited for, then asserted: a bare wait on the state would report a drop
    # that did nothing as a timeout on a predicate, which names the symptom
    # rather than the failure.
    try:
        ui.wait_state("state.texts.length === 1 && state.texts[0].text === 'dropped'",
                      timeout=UPLOAD_TIMEOUT)
    except PWTimeout:
        pass
    assert ui.js("() => state.texts.map(t => t.text)") == ["dropped"], (
        "dropping a design file on the page did not open it; the design on "
        f"screen is {ui.js('() => state.texts.map(t => t.text)')}")
    assert ui.page.locator("#dropzone.on").count() == 0, (
        "the overlay stayed up after the drop")
    assert ui.js("() => [$('name').value, state.mask]") == ["dropped-in", "red"], (
        "the dropped design did not fully apply")
    ui.assert_clean("drop a design file")


@pytest.mark.browser
def test_dropping_a_picture_says_where_pictures_go(ui):
    """An image dropped on the page is not silently refused as "not a design".

    Someone with a logo in hand will drop it here, and "that is not a design
    file" would be technically true and useless: artwork and board outlines are
    both one click away, so the refusal names them.
    """
    handle = ui.page.evaluate_handle(
        """() => {
            const dt = new DataTransfer();
            dt.items.add(new File(['\\x89PNG not really'], 'logo.png',
                                  {type: 'image/png'}));
            return dt;
        }""")
    before = ui.js("() => JSON.stringify(designJSON())")
    ui.page.dispatch_event("body", "dragenter", {"dataTransfer": handle})
    ui.page.dispatch_event("body", "drop", {"dataTransfer": handle})
    ui.wait_toast(r"is a picture, not a design file")
    told = [t for t in ui.toast_texts() if "picture" in t][0]
    assert "Art" in told and "Shape" in told, (
        f"the message does not say where a picture should go: {told!r}")
    assert ui.js("() => JSON.stringify(designJSON())") == before, (
        "dropping a picture changed the design")
    ui.assert_clean("drop a picture")


# ===========================================================================
# Snapping (the SNAP toggle in the top bar; on by default)
#
# Snapping decides where a part is actually built: a drag that lands 0.3 mm
# from the board's centre line builds a badge whose art is 0.3 mm off centre,
# and a guide line drawn through a part that is somewhere else is a preview
# telling the user a lie about the board they will get.  Both are invisible to
# DRC -- an off-centre part is perfectly manufacturable -- so the editor is the
# only place they can be caught.
# ===========================================================================
def _add_text_on(ui, side):
    """One text layer, on the face asked for, with ink in it so it is grabbable
    on the canvas.  `side='back'` is the deliberate move off the default: the
    BACK view is mirrored, and mirrored board coordinates are where a
    hand-rolled snap could quietly work on one face only."""
    ui.show_panel("text")
    ui.page.click("#addtext", timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.texts.length === 1")
    ui.page.locator("#textlist .item input.tx").first.fill("SNAP", timeout=ELEMENT_TIMEOUT)
    ui.card("textlist", 0).locator("select.s").select_option(side, timeout=ELEMENT_TIMEOUT)
    ui.wait_state(f"state.texts[0].side === '{side}'")
    return ui.js("() => [state.texts[0].x, state.texts[0].y]")


def _grabbable_led(ui):
    """The starter unit's anchor and the face it is mounted on."""
    led = ui.leds()[0]
    return [led["x"], led["y"]], led["side"]


def _anchor_to_centre(ui, anchor, side):
    """How far the selected item's visible centre sits from the point a drag
    grabs it by.  A drag aims the ANCHOR, so a test that wants the CENTRE to
    end up somewhere has to aim that much short of it -- which is exactly the
    correction the app itself has to make."""
    ui.click_mm(anchor[0], anchor[1], side=side)
    centre = ui.visible_centre()
    assert centre, "nothing is selected, so there is no box to centre"
    return [centre[0] - anchor[0], centre[1] - anchor[1]]


@pytest.mark.browser
@pytest.mark.parametrize("kind", ["text", "led"])
def test_snapping_lands_a_dragged_part_exactly_on_the_board_centre(ui, kind):
    """With SNAP on, a drag that ends NEAR a centre line lands ON it.

    "Near" is the whole point: the user aims by hand and the app is supposed to
    finish the job.  The contrast case in the same test is the same gesture with
    SNAP off, which must land where the cursor was and nowhere else -- a snap
    that cannot be turned off is worse than none, because 0.2 mm adjustments
    stop existing.

    Both faces are covered (a text on the BACK, the unit on its own face): the
    back view is mirrored, so a snap computed on screen coordinates instead of
    board coordinates would pass on the front alone.
    """
    if kind == "text":
        start, side = _add_text_on(ui, "back"), "back"
        read = lambda: ui.js("() => [state.texts[0].x, state.texts[0].y]")
    else:
        start, side = _grabbable_led(ui)
        read = lambda: [ui.leds()[0]["x"], ui.leds()[0]["y"]]

    # Aim the item's CENTRE a hair off the board centre: inside the pull,
    # outside exactness.  For a unit that is a couple of millimetres away from
    # where the cursor holds it, which is the whole point of this test.
    off = _anchor_to_centre(ui, start, side)
    aim = (SQUARE_CENTRE_MM - off[0] + 0.14, SQUARE_CENTRE_MM - off[1] - 0.11)

    ui.set_snap(False)
    ui.drag_mm(start, aim, side=side)
    free_centre = ui.visible_centre()
    assert abs(free_centre[0] - SQUARE_CENTRE_MM) > SNAP_EXACT_MM, (
        f"snapping is OFF but the part still centred exactly: {free_centre}")
    assert math.hypot(free_centre[0] - SQUARE_CENTRE_MM - 0.14,
                      free_centre[1] - SQUARE_CENTRE_MM + 0.11) < 0.7, (
        f"with snapping off the part must follow the cursor; centre {free_centre}")

    ui.set_snap(True)
    ui.drag_mm(read(), aim, side=side)
    snapped = ui.visible_centre()
    assert len(snapped) == 2, f"a centre is two numbers; got {snapped}"
    for axis, got in enumerate(snapped):
        assert abs(got - SQUARE_CENTRE_MM) < SNAP_EXACT_MM, (
            f"axis {axis} of the item's own centre did not land on the board "
            f"centre: {snapped} (the anchor it is stored by is at {read()})")
    # The app's own idea of the board centre must be the one the test used, or
    # the number above is only self-consistent.
    bounds = ui.js("() => outlineBounds()")
    assert abs((bounds[0] + bounds[2]) / 2 - SQUARE_CENTRE_MM) < 0.01, (
        f"the default board is not the standard square any more: {bounds}")
    ui.assert_clean(f"snap a {kind} to the board centre")


@pytest.mark.browser
def test_a_snapped_drag_with_nothing_to_line_up_with_lands_on_the_grid(ui):
    """Away from every centre line, a snapped drag still lands on a round
    number: the 0.5 mm grid the SNAP button promises.  Off, the same drag keeps
    whatever fraction of a millimetre the cursor had.

    This is the half of snapping that has no guide line to look at, so state is
    the only witness.
    """
    start = _add_text_on(ui, "front")
    # Not a grid multiple, and clear of every centre on the board -- the LED
    # unit's own centre included, which sits well left of the LED itself.
    aim = (14.37, 4.88)
    targets = ui.js("() => snapTargets(null)[0].map(t => t.at)")
    assert len(targets) >= 2, (
        f"a board with a unit and a text has at least its own centre and the "
        f"unit's to offer; got {targets}")
    for c in targets:
        assert abs(c - aim[0]) > 1.0, (
            f"the aim point is {abs(c - aim[0]):.2f} mm from a real snap target "
            f"at {c}: this test would measure that snap, not the grid")

    ui.set_snap(False)
    ui.drag_mm(start, aim, side="front")
    free = ui.js("() => [state.texts[0].x, state.texts[0].y]")
    free_centre = ui.visible_centre()
    off_grid = [abs(v / SNAP_GRID_MM - round(v / SNAP_GRID_MM)) for v in free_centre]
    assert max(off_grid) > SNAP_EXACT_MM, (
        f"snapping is OFF but both axes still landed on the grid: {free_centre}")

    ui.set_snap(True)
    ui.drag_mm_hold(free, aim, side="front")
    mid_drag_guides = ui.snap_guides()
    ui.release()
    assert not mid_drag_guides, (
        f"a guide line appeared, so this landing was an alignment and not the "
        f"grid: {mid_drag_guides}")
    snapped = ui.visible_centre()
    assert len(snapped) == 2, f"a centre is two numbers; got {snapped}"
    for axis, got in enumerate(snapped):
        steps = got / SNAP_GRID_MM
        assert abs(steps - round(steps)) < SNAP_EXACT_MM, (
            f"axis {axis} is not on the {SNAP_GRID_MM} mm grid: {snapped}")
    assert math.hypot(snapped[0] - aim[0], snapped[1] - aim[1]) < 0.7, (
        f"the grid snap moved the text further than one grid step: {snapped} vs {aim}")
    ui.assert_clean("snap a text to the grid")


@pytest.mark.browser
def test_holding_alt_places_a_part_off_the_snap_lines(ui):
    """Alt is the escape hatch: the one gesture it is held for ignores the
    toggle, so a part can be nudged just off a centre line without hunting for
    the button.  Without it, snapping on means some positions are unreachable.
    """
    start = _add_text_on(ui, "front")
    aim = (SQUARE_CENTRE_MM + 0.13, SQUARE_CENTRE_MM + 0.13)

    ui.set_snap(True)
    ui.drag_mm_with(start, aim, side="front", modifiers=("Alt",))
    landed = ui.visible_centre()
    assert abs(landed[0] - SQUARE_CENTRE_MM) > SNAP_EXACT_MM, (
        f"Alt did not suspend snapping; the text snapped anyway: {landed}")
    assert math.hypot(landed[0] - aim[0], landed[1] - aim[1]) < 0.7, (
        f"Alt-dragging must still follow the cursor; aimed {aim}, got {landed}")
    assert not ui.snap_guides(), (
        "a guide was left on screen for a drag that did not snap")

    # And the toggle itself survives: Alt suspends, it does not switch off.
    assert ui.js("() => snapOn") is True, "Alt turned the toggle off for good"
    assert ui.snap_pressed() == {"aria": "true", "lit": True}, (
        "the SNAP button stopped showing the mode the drags are using")
    ui.assert_clean("alt-drag with snapping on")


@pytest.mark.browser
def test_a_snap_guide_only_marks_a_line_the_part_really_landed_on(ui):
    """A guide line is a claim about the board: "this part is on this line".

    A snapped position still has to survive the clamping each kind does -- a
    unit will not enter another unit -- so the position the snap aimed at is
    not always the position the part got.  Drawing the line anyway would make
    the preview promise an alignment the fab board will not have.

    Two units share a column here, so the y snap is refused by collision while
    the x snap goes through: exactly the case where an unfiltered guide lies.
    """
    ui.show_panel("leds")
    assert ui.add_led(), "the board must take a second unit for this test"
    leds = ui.leds()
    mover, target = leds[0], leds[1]
    ui.set_snap(True)
    # Aim at the other unit's row and column, from just off both.
    ui.drag_mm_hold((mover["x"], mover["y"]),
                    (target["x"] + 0.12, target["y"] + 0.12),
                    side=mover["side"])
    guides = ui.snap_guides()
    centre = ui.visible_centre()
    ui.release()

    assert guides, (
        "a drag onto another unit's column produced no guide at all, so this "
        "test could not see whether guides tell the truth")
    for g in guides:
        at_part = centre[0] if g["axis"] == "x" else centre[1]
        assert abs(at_part - g["at"]) < 0.03, (
            f"a {g['axis']} guide is drawn at {g['at']} but the part sits at "
            f"{at_part}: the preview claims an alignment the board will not have")
    ui.assert_clean("guides during a blocked snap")


@pytest.mark.browser
def test_snapping_turns_a_rotation_drag_in_whole_15_degree_steps(ui):
    """The knob is the only way to rotate on the canvas, and free-hand angles
    are the reason silk text arrives 37° askew.  With SNAP on the knob steps
    15°; with it off the same sweep keeps the angle the user drew.
    """
    _add_text_on(ui, "front")
    # Park it low on the board first: the toast that every toggle click raises
    # floats over the TOP of the canvas, and a rotate knob under it is a knob
    # the mouse cannot reach -- a harness hazard, not a snapping one.
    ui.js("() => { state.texts[0].x = 10.16; state.texts[0].y = 15.5;"
          " state.texts[0].auto = false; draw(); }")
    ui.click_mm(*ui.js("() => [state.texts[0].x, state.texts[0].y]"), side="front")
    assert ui.selected() and ui.selected()["kind"] == "text", (
        "the text was not selected, so no rotate knob exists to drag")

    def sweep_to(degrees):
        """Grab the knob and swing it `degrees` clockwise from straight up,
        around the selection's own centre."""
        si = ui.js("() => JSON.parse(JSON.stringify(selectionInfo()))")
        box = si["sb"]
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        knob = si.get("knob") or ((box[0] + box[2]) / 2,
                                 box[1] - 16 / ui.js("() => SCALE"))
        arm = math.hypot(knob[0] - centre[0], knob[1] - centre[1])
        rad = math.radians(degrees - 90)
        ui.drag_mm(knob,
                   (centre[0] + arm * math.cos(rad), centre[1] + arm * math.sin(rad)),
                   side="front")
        return ui.js("() => state.texts[0].rot")

    ui.set_snap(False)
    free = sweep_to(37)
    assert free % 15 != 0, (
        f"snapping is OFF but the angle still came out a 15° multiple: {free}°")

    ui.set_snap(True)
    stepped = sweep_to(37)
    assert stepped % 15 == 0, f"snapping did not step the rotation: {stepped}°"
    assert min(abs(stepped - 37), abs(stepped - 37 + 360)) <= 15, (
        f"the snapped angle is not the nearest step to the gesture: {stepped}°")
    ui.assert_clean("snap a rotation drag")


@pytest.mark.browser
def test_the_first_session_arrives_with_snapping_on_and_says_so(ui):
    """Snapping is the default, and the button has to agree with it.

    A button that reads OFF while drags snap (or the reverse) makes every
    placement a guess: the user aims for a spot and the app moves the part
    somewhere else with no visible reason.  Nothing is written to storage
    until the user actually chooses, so a later change of default is not
    frozen into every browser that ever opened the app.
    """
    assert ui.js("() => snapOn") is True, "the first session did not start snapped"
    assert ui.snap_pressed() == {"aria": "true", "lit": True}, (
        "the SNAP button does not show the mode the drags are using")
    assert ui.js("() => localStorage.getItem('bm-snap')") is None, (
        "the default was written to storage, freezing it for this browser")
    ui.page.click("#snapbtn", timeout=CONTROL_TIMEOUT)
    ui.wait_state("snapOn === false")
    assert ui.js("() => localStorage.getItem('bm-snap')") == "0", (
        "turning snapping off did not record the choice, so it will not survive")
    assert ui.snap_pressed() == {"aria": "false", "lit": False}, (
        "the button still reads pressed after being switched off")
    ui.assert_clean("read and flip the snapping default")


@pytest.mark.browser
@pytest.mark.parametrize("part", ["ledres", "ledvia"])
def test_a_free_placed_part_lines_up_on_its_own_units_led(ui, part):
    """With free placement on, each part of a unit drags separately -- so each
    one snaps separately, and what it wants to line up with is usually the LED
    it belongs to.

    Offering only whole-unit centres here would make the one alignment that
    matters (resistor and via squared up on their own LED) the one alignment
    the user has to do by eye, on parts about a millimetre across.
    """
    ui.show_panel("leds")
    # Free placement, with the resistor and via parked well off both of the
    # LED's own axes so a snap has somewhere to travel.  The LED itself is put
    # on a deliberately OFF-grid column: on a round number the 0.5 mm grid
    # would land the part in the same place and the test would pass without
    # the LED ever being a target.
    ui.js("""() => {
        const L = state.leds[0];
        L.x = 7.37;
        L.adv = {rx: 3.2, ry: 2.4, vx: -2.8, vy: -2.0, rrot: 0, lrot: 0};
        renderLedList(); draw();
    }""")
    led = ui.leds()[0]
    body = [led["x"], led["y"]]
    off_grid = abs(body[0] / SNAP_GRID_MM - round(body[0] / SNAP_GRID_MM))
    assert off_grid > 0.1, (
        f"the LED landed on the snap grid at x={body[0]}, so lining up on it "
        "cannot be told apart from the grid fallback")
    where = ui.js(
        "(k) => { const L = state.leds[0], g = geomOf(L);"
        " return k === 'ledres' ? unitPoint(L, g.res[0], g.res[1])"
        "                       : unitPoint(L, g.viaF[0], g.viaF[1]); }", part)

    ui.set_snap(True)
    # Aim a hair off the LED's own column, keeping the row well clear so only
    # one axis can snap: a two-axis landing would not say which target won.
    ui.drag_mm(where, (body[0] + 0.12, where[1] - 1.6), side=led["side"])
    guides = ui.snap_guides()
    after = ui.js(
        "(k) => { const L = state.leds[0], g = geomOf(L);"
        " return k === 'ledres' ? unitPoint(L, g.res[0], g.res[1])"
        "                       : unitPoint(L, g.viaF[0], g.viaF[1]); }", part)

    # 0.03 mm covers the 0.05 mm rounding a free-placed offset goes through.
    assert abs(after[0] - body[0]) < 0.03, (
        f"the {part} did not line up on its own LED: part at {after}, LED at {body}")
    assert abs(after[1] - where[1]) > 0.5, (
        f"the {part} never moved, so nothing was tested: {where} -> {after}")
    # The LED itself must not have been dragged along; only one part moves.
    now = ui.leds()[0]
    assert [now["x"], now["y"]] == body, (
        f"dragging the {part} moved the LED too: {body} -> {[now['x'], now['y']]}")
    assert not guides, "guides outlive the gesture that drew them"
    ui.assert_clean(f"snap a free-placed {part} onto its LED")


# ===========================================================================
# Telling the user when an SVG's paint cannot be used exactly
#
# `svgart` uses an SVG's own vector paths only while every paint is one flat
# colour; a gradient or a pattern sends the layer down the raster tracer
# instead.  That fallback is fine for the badge -- traced edges land within
# ~0.04 mm of the artwork -- but it used to be invisible, while the art panel
# went on saying the paths were used exactly.  Nothing on the server tells the
# browser which path a layer took, so the panel decides for itself, and the
# only thing that makes that honest is agreeing with the parser.
# ===========================================================================
_GRADIENT_DEFS = ('<defs><linearGradient id="g">'
                  '<stop offset="0" stop-color="#fff"/>'
                  '<stop offset="1" stop-color="#888"/></linearGradient></defs>')

#: (label, SVG body) -- the four ways the answer can go, including both ways
#: it can go wrong. `unused-defs` and `style-attribute` are the cases a naive
#: check gets backwards: one has a gradient element and no gradient paint, the
#: other has gradient paint and no gradient attribute.
_PAINT_CASES = [
    ("flat-fills",
     '<circle cx="50" cy="50" r="40" fill="#111"/>'
     '<circle cx="50" cy="40" r="20" fill="#fff"/>'),
    ("unused-defs",
     _GRADIENT_DEFS + '<circle cx="50" cy="50" r="40" fill="#111"/>'),
    ("fill-attribute",
     _GRADIENT_DEFS + '<circle cx="50" cy="50" r="40" fill="url(#g)"/>'),
    ("style-attribute",
     _GRADIENT_DEFS + '<circle cx="50" cy="50" r="40" style="fill:url(#g)"/>'),
]


@pytest.mark.browser
@pytest.mark.parametrize("label,body", _PAINT_CASES, ids=[c[0] for c in _PAINT_CASES])
def test_the_art_panel_promises_exact_vector_paths_only_when_svgart_agrees(
        ui, make_svg, label, body):
    """The panel's claim about an upload matches what the parser will do to it.

    Two implementations of one rule: `svgart.svg_color_regions` decides it on
    the server, `svgApproxReason` in the browser decides it at upload time
    from the parsed document. Whichever way they disagree the user is misled
    -- promised exact paths they will not get, or warned off a file that was
    going to be traced exactly -- so the test asserts them equal rather than
    asserting either one's answer.
    """
    from minibadge_designer import svgart

    svg = make_svg(body)
    try:
        svgart.svg_color_regions(svg)
        server_is_exact = True
    except ValueError:
        server_is_exact = False

    ui.show_panel("art")
    ui.page.set_input_files(
        "#artfile", files=[{"name": f"{label}.svg", "mimeType": "image/svg+xml",
                            "buffer": svg}], timeout=ELEMENT_TIMEOUT)
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    reason = ui.js("() => state.art[0].approx || ''")
    panel_is_exact = not reason

    assert panel_is_exact == server_is_exact, (
        f"{label}: the parser would {'use' if server_is_exact else 'refuse'} "
        f"these paths exactly and the panel says "
        f"{'exact' if panel_is_exact else reason!r}; one of the two is lying "
        "to the user about the artwork on their badge")

    # And the news has to reach the user, not just the layer object.
    note = ui.card("artlist", 0).locator(".approxnote")
    if server_is_exact:
        assert note.count() == 0, (
            f"{label}: the card warns about paint the parser accepts")
    else:
        assert note.count() == 1 and "traced" in note.inner_text().lower(), (
            f"{label}: nothing on the card says the paths were not used "
            f"exactly (found {note.count()} notes)")
        assert ui.has_toast("traced from a rendered copy"), (
            f"{label}: the upload said nothing at the moment it happened: "
            f"{ui.toast_texts()}")
    ui.assert_clean()


# ===========================================================================
# What the editor tells you before the download refuses you
#
# Every unbuildable state in this app is meant to be visible before Download:
# blockingProblems() plus a canvas marker.  Two things escaped that contract —
# an image bigger than the server will decode (accepted here, refused there),
# and a width the board cannot fit (kept in the box, thrown away on the way to
# the canvas).  A third thing, the carve, is not an error at all: it is a rule
# the app applies to everyone's artwork and never mentioned anywhere.
# ===========================================================================
def _add_art(ui, upload):
    ui.show_panel("art")
    ui.page.set_input_files("#artfile", files=[upload], timeout=ELEMENT_TIMEOUT)


@pytest.mark.browser
def test_an_image_the_server_will_not_decode_is_refused_where_it_is_chosen(
        ui, make_png):
    """An upload too big to build is turned away at the moment it is picked.

    The browser decodes the file to draw it, so it knows the pixel count before
    the layer exists — the same number `logo.MAX_INPUT_PIXELS` refuses. Letting
    it in meant the editor drew a layer, reported no problems, and then /bundle
    answered 400 on the one click the user could not undo by editing.
    """
    over = make_png(size=(5200, 5200))     # 27 Mpx, just over the 24 Mpx cap
    under = make_png(size=(2000, 2000))    # 4 Mpx, comfortably inside it

    _add_art(ui, {"name": "over.png", "mimeType": "image/png", "buffer": over})
    # Wait for the upload to RESOLVE either way -- a layer or a message -- so a
    # regression fails on the assertion below rather than on a wait timeout.
    ui.page.wait_for_function(
        """() => state.art.length > 0
             || [...document.querySelectorAll('#toasts .toast')]
                  .some(t => /megapixel/.test(t.textContent))""",
        timeout=UPLOAD_TIMEOUT)
    assert ui.js("() => state.art.length") == 0, (
        "a layer was created for an image the server will refuse; the editor "
        "is promising a board it cannot build")
    msg = next(t for t in ui.toast_texts() if "megapixel" in t)
    for owed in ("5200", "27", "24"):
        assert owed in msg, (
            f"the refusal does not say {owed}: it has to name the size, the "
            f"limit and the gap, or the user cannot act on it — got {msg!r}")

    # The contrast case: a big-but-usable image must still be accepted, or this
    # guard is just a smaller cap than the one it mirrors.
    _add_art(ui, {"name": "under.png", "mimeType": "image/png", "buffer": under})
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    ui.assert_clean()


@pytest.mark.browser
def test_artwork_wider_than_the_board_is_drawn_wide_and_says_it_overhangs(ui,
                                                                          logo):
    """A width past the board draws at that width, and the card says so.

    Artwork used to be fitted down to the board, with a note explaining that
    the typed number had been ignored. That made the one thing you most need a
    big image for impossible: lining a picture up with a board profile, where
    the interesting part is on the board and the rest hangs off. Art is placed
    at the size asked for now, clipped at the edge, and the standing note says
    which of those is happening.
    """
    _add_art(ui, logo)
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    page = ui.page

    def note():
        found = page.eval_on_selector_all(
            "#artlist .clampnote:not([hidden])",
            "els => els.map(e => e.textContent.replace(/\\s+/g, ' '))")
        return found[0] if found else None

    def set_width(mm):
        box = page.query_selector("#artlist input.wdn")
        box.fill(str(mm))
        box.dispatch_event("change")
        page.wait_for_timeout(150)

    set_width(14)
    assert note() is None, (
        "a layer that fits inside the board is annotated as overhanging; the "
        "note has to mean something when it appears")
    set_width(60)
    drawn = ui.js("() => artDims(state.art[0]).w")
    assert abs(drawn - 60) < 0.01, (
        f"60 mm of artwork was drawn at {drawn:.1f} mm; the board is not "
        "allowed to resize it any more")
    said = note()
    assert said, ("60 mm of artwork on a 20 mm board hangs well off it and the "
                  "card says nothing about it")
    assert f"{drawn:.1f}" in said, (
        f"the note must state the size actually drawn ({drawn:.1f} mm): {said!r}")
    assert "clip" in said.lower(), (
        f"the note must say what happens to the part that hangs over: {said!r}")
    # And it goes away again, so it tracks the design rather than latching.
    set_width(12)
    assert note() is None, "the note outlived the condition it describes"
    ui.assert_clean()


@pytest.mark.browser
@pytest.mark.parametrize("material,shown", [("silk", True), ("copper", True),
                                            ("glow", False), ("cut", False)])
def test_the_art_card_says_ink_keeps_off_pads_when_the_layer_paints_ink(
        ui, logo, material, shown):
    """The carve is stated on the layer it applies to.

    Silkscreen and copper are trimmed where pads, captions and parts sit, and
    the starting design already carries a unit — so the first badge anyone
    builds can arrive with a bite out of its logo. Nothing in the UI or in any
    of the ten tips said so; the user just saw damage. Windows and cuts are not
    ink and are not carved, so they must NOT claim they are.
    """
    _add_art(ui, logo)
    ui.wait_state("state.art.length === 1", timeout=UPLOAD_TIMEOUT)
    page = ui.page
    # threshold mode gives the layer one material select to drive
    page.query_selector("#artlist select.mode").select_option("threshold")
    page.wait_for_timeout(200)
    page.query_selector("#artlist select.m").select_option(material)
    page.wait_for_timeout(250)

    lines = page.eval_on_selector_all(
        "#artlist .item .sub",
        "els => els.map(e => e.textContent.replace(/\\s+/g, ' '))")
    says = [ln for ln in lines if "keep clear" in ln or "cannot sit on solder" in ln]
    assert bool(says) is shown, (
        f"material={material}: the carve note is "
        f"{'missing' if shown else 'claimed'} — lines were {lines}")
    if shown:
        for owed in ("pads", "part"):
            assert owed in says[0], (
                f"the note has to name what the ink keeps off ({owed}): {says[0]!r}")
    ui.assert_clean()


@pytest.mark.browser
def test_the_illustrated_help_can_be_reached_without_a_mouse(ui):
    """Every `?` tip is reachable by keyboard.

    These tips are the app's whole explanation mechanism — the carve, the
    materials, the pin rules all live in them — and they were skipped by the
    tab order outright (`tabindex="-1"` on all twelve). Escape already closes
    them, so nothing else about the flow needed changing.
    """
    page = ui.page
    page.evaluate("() => { document.body.setAttribute('tabindex', '-1');"
                  " document.body.focus(); }")
    reached = None
    for i in range(1, 13):
        page.keyboard.press("Tab")
        page.wait_for_timeout(40)
        if page.evaluate("() => document.activeElement.classList.contains('qm')"):
            reached = i
            break
    assert reached, ("no help button took focus in twelve tabs from the "
                     "document start; the tips are mouse-only")
    # #tippop is position:fixed, so offsetParent is null even while it shows:
    # measure what the user sees instead.
    shown = """() => { const d = document.querySelector('#tippop');
        if (!d) return false;
        const b = d.getBoundingClientRect();
        return getComputedStyle(d).display !== 'none' && b.width > 0 && b.height > 0; }"""
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    assert page.evaluate(shown), (
        "the focused help button did not open its tip on Enter")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert not page.evaluate(shown), "Escape did not close the tip"
    ui.assert_clean()


#: Controls that change the room a unit claims *after* the CLK hookup is on,
#: which is the half `relocateJumper` was never wired to. Crowding the board
#: so that NO legal spot is left was tried as a fourth case and dropped: the
#: same handler relocates the unit first, so the jumper never ends up
#: stranded that way and the case proved nothing the three below do not.
_RESHAPE = [("package", ".pk", "1206"), ("layout", ".l", "inline"),
            ("side", ".s", "front")]


def _built_jumper(client, params, slug="jumperparity"):
    """Where the BOARD puts the CLK jumper, for a design the page just built.

    Returns ``(position, None)`` or ``(None, error)``: a refusal is an answer
    too -- it means the download promised nothing -- and the caller decides
    which of those the guarantee allows.
    """
    import io
    import json
    import zipfile

    import invariants

    params = dict(params, name=slug)
    resp = client.post("/generate", data={"params": json.dumps(params)})
    if resp.status_code != 200:
        return None, resp.get_json().get("error", "")
    board = invariants.Board(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        f"{slug}/{slug}.kicad_pcb").decode())
    return next(((x, y) for _n, _l, x, y, ref, _f in board.footprints
                 if ref == "JP1"), None), None


@pytest.mark.browser
@pytest.mark.parametrize("what,sel,value", _RESHAPE,
                         ids=[r[0] for r in _RESHAPE])
def test_the_previewed_clk_jumper_is_the_one_the_board_gets(ui, client, what,
                                                            sel, value):
    """The preview never shows a CLK jumper the download will not build.

    The jumper is placed by the app, not by the user, and the server always
    yields it when a unit is in the way -- units are placed first. The canvas
    mirrors that walk in `relocateJumper`, but it was only wired to the events
    that turn the hookup ON. Change the unit *afterwards* -- its package,
    layout or side, or the rail-via tick -- and nothing re-ran it: the canvas
    kept drawing the jumper at its old spot while the server moved it, so the
    badge shipped with a via the user was never shown, sitting in the middle
    of the board. Reported from exactly that: "it seems to be generating
    unneeded ground vias when the LED is on the back".

    Driven through the real controls on purpose. Writing `state` and calling
    `draw()` -- the way the parity tests above set their designs -- bypasses
    the handlers this guards, so it would pass on the broken code.

    Either position agrees with the board, or the download is refused with a
    reason: a refusal promises nothing, and "no room for the CLK jumper" is
    what the server says when the walk finds nowhere legal.
    """
    import json

    page = ui.page
    page.click("#tab-leds")
    # Setup only: one blinking unit on the back, parked over the jumper's home
    # so that any growth of it collides. The controls do the rest.
    ui.js("""(led) => {
        state.leds = [led]; renderLedList(); draw();
    }""", _js_led(x=10.0, y=15.0, side="back", size="0805", layout="stacked"))
    row = "#ledlist .item:first-child"
    page.locator(f"{row} .clk").check()          # this path already yielded
    before = ui.js("() => clkInfo().jumper")

    page.select_option(f"{row} {sel}", value)    # the path under test

    drawn = ui.js("() => clkInfo().jumper")
    built, refused = _built_jumper(
        client, json.loads(ui.js('() => designFormData().get("params")')))

    if refused is not None:
        # Believed unreachable through the UI (the unit is relocated first),
        # so this is a guard rather than a covered branch: if it ever does
        # happen, the editor has to have said so before the click.
        assert ui.js("() => blockingProblems().map(p => p[0])"), (
            f"changing the {what} left the server unable to build the board "
            f"({refused!r}) while the editor reported no blocking problem at "
            "all: Download fails with no warning")
        return
    assert built, "the board carries no CLK jumper at all"
    gap = max(abs(drawn[0] - built[0]), abs(drawn[1] - built[1]))
    assert gap <= 0.01, (
        f"after changing the {what}, the preview draws the CLK jumper at "
        f"{(round(drawn[0], 2), round(drawn[1], 2))} and the board builds it "
        f"at {(round(built[0], 2), round(built[1], 2))} -- {gap:.2f} mm apart "
        f"(it started at {(round(before[0], 2), round(before[1], 2))}); its "
        "rail via moves with it, so the user approves a board with a via "
        "somewhere they never saw one")


@pytest.mark.browser
def test_a_new_board_shape_takes_the_previewed_jumper_with_it(ui, client):
    """Cutting the board under the CLK jumper moves it in the preview too.

    Same guarantee as the reshape cases above, reached the other way: the
    jumper is placed by the app, and a new outline can take the board out from
    under it (a cut over its corner, a silhouette that no longer reaches it).
    `/outline`'s reply already relocates stranded LEDs; the jumper was left
    where it was, so the canvas drew it on board that no longer existed while
    the server put it somewhere else and built THAT -- with its rail via, a
    via the user never saw.
    """
    import json

    page = ui.page
    ui.js("""(led) => { state.leds = [led]; renderLedList(); draw(); }""",
          _js_led(x=6.0, y=6.0, clk=True))
    home = ui.js("() => clkInfo().jumper")

    # A cut across the bottom strip, which is where the jumper lives.
    ui.js("""() => {
        state.shape.elements = [{kind: 'rect', op: 'cut', cx: 10.16, cy: 19.2,
                                 w: 12, h: 3.5, rot: 0}];
        requestOutline(true);
    }""")
    page.wait_for_function("() => state.shape.rings !== null",
                           timeout=GENERATE_TIMEOUT)

    drawn = ui.js("() => clkInfo().jumper")
    assert (drawn[0], drawn[1]) != (home[0], home[1]), (
        "the cut removed the board under the jumper and the preview left it "
        "there; nothing below would tell us whether the two agree by accident")
    built, refused = _built_jumper(
        client, json.loads(ui.js('() => designFormData().get("params")')),
        slug="jumpershape")
    if refused is not None:
        assert ui.js("() => blockingProblems().map(p => p[0])"), (
            f"the shape left the server unable to build the board ({refused!r}) "
            "while the editor reported no blocking problem: Download fails "
            "with no warning")
        return
    assert built, "the board carries no CLK jumper at all"
    gap = max(abs(drawn[0] - built[0]), abs(drawn[1] - built[1]))
    assert gap <= 0.01, (
        f"after the cut the preview draws the CLK jumper at "
        f"{(round(drawn[0], 2), round(drawn[1], 2))} and the board builds it "
        f"at {(round(built[0], 2), round(built[1], 2))} -- {gap:.2f} mm apart")


@pytest.mark.browser
def test_a_perimeter_bridge_can_be_bent_and_the_board_follows(ui, client):
    """The bridge is editable like every other trace, and shaped in the file.

    It used to be the one piece of routed copper nobody could touch: no bends,
    and not drawn in the editor at all, so a ground trace appeared in the 3D
    view running from a unit to the board edge that its owner had never been
    shown. This drives the real gesture -- hover the trace, click the "+" it
    offers, drag the handle -- and then asks the board where the trace went.
    """
    import json

    page = ui.page
    # A back unit under a bare window, which is what makes its rail pad's
    # bridge exist at all (pcb.unit_bridges only cuts one where a window
    # could sever the pad).
    ui.js("""(led) => {
        state.leds = [led];
        state.art = [{kind: 'rect', material: 'bare', side: 'through',
                      cx: 10.16, cy: 10.16, wmm: 15, h: 15, rot: 0}];
        state.texts = []; renderLedList(); draw();
    }""", _js_led(x=10.0, y=10.0, side="back", size="0805", layout="stacked"))

    before = ui.js("() => { const b = allBridges()[0].B; return b && b.pts; }")
    assert before and len(before) == 2, (
        f"the unit's GND bridge is not the plain two-point run to start with "
        f"({before}); the drag below would prove nothing")

    # Hover the middle of the run, which is where the "+" is offered, then
    # click it: that is how every other trace gets its first bend.
    mid = [(before[0][0] + before[1][0]) / 2, (before[0][1] + before[1][1]) / 2]
    hx, hy = ui.board_to_client(mid[0], mid[1], "back")
    page.mouse.move(hx, hy)
    # Read it rather than waiting on it: the hover handler runs inline on the
    # event, so "no hint" is an answer, and a wait would turn this into a
    # timeout -- which reads as a broken test rather than a lost feature.
    hint = ui.js("() => nodeHint && ({trace: nodeHint.trace, side: nodeHint.side})")
    assert hint, (
        "hovering the middle of the bridge offered no '+' at all, so the "
        "trace cannot be bent: it is the one piece of routed copper on the "
        "board its owner cannot shape")
    assert hint["trace"] == "b" and hint["side"] == "back", (
        f"the hover offered {hint}, not a bend on this unit's back-face "
        "bridge: the trace is not editable from the view it runs on")
    page.mouse.down()
    page.mouse.up()
    bends = ui.js("() => state.leds[0].bnodes || []")
    assert len(bends) == 1, f"clicking the + added {bends}, not one bend"

    # Drag it somewhere clear of the unit and check the board agrees.
    tx, ty = ui.board_to_client(bends[0][0], 15.0, "back")
    sx, sy = ui.board_to_client(bends[0][0], bends[0][1], "back")
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(tx, ty, steps=6)
    page.mouse.up()

    after = ui.js("() => { const b = allBridges()[0].B; return b && {pts: b.pts, ok: b.ok}; }")
    assert after and len(after["pts"]) > 2, (
        f"the bridge is still unbent after the drag: {after}")
    params = json.loads(ui.js('() => designFormData().get("params")'))
    params["name"] = "bentbridge"
    resp = client.post("/generate", data={"params": json.dumps(params)})
    if not after["ok"]:
        assert resp.status_code != 200, (
            "the canvas says the bend cannot be routed but the server built "
            "the board anyway")
        return
    assert resp.status_code == 200, resp.get_json()
    import io
    import re
    import zipfile
    board = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "bentbridge/bentbridge.kicad_pcb").decode()
    legs = [tuple(round(float(v) - 100.0, 3) for v in m) for m in re.findall(
        r'\(segment \(start ([\d.]+) ([\d.]+)\) \(end ([\d.]+) ([\d.]+)\)'
        r'[^\n]+\(layer "B\.Cu"\) \(net 2\)', board)]
    assert len(legs) == len(after["pts"]) - 1, (
        f"the preview draws {len(after['pts']) - 1} legs of bent bridge and "
        f"the board carries {len(legs)}")
    assert legs, "the board carries no GND copper for the bent bridge at all"
    for (ax, ay), (leg) in zip(after["pts"], legs):
        assert abs(ax - leg[0]) < 0.01 and abs(ay - leg[1]) < 0.01, (
            f"a leg starts at {leg[:2]} on the board and {(ax, ay)} in the "
            "preview: the bent trace the user shaped is not the one built")


@pytest.mark.browser
def test_the_previewed_part_labels_are_the_ones_the_board_prints(ui, client):
    """Every D1 the canvas draws is a D1 the generator places, in the same spot.

    `refdesLayout` and `pcb.refdes_layout` are two copies of one placement
    search -- away from the unit's other part, then past the pads, then the
    diagonals, each candidate tested against the copper, the ink and the board
    edge, first clear one wins. A drift is not cosmetic: the canvas would show
    a label beside a part that the board prints somewhere else, or does not
    print at all.

    Compared unit by unit against `pcb` rather than against a downloaded
    board, for the same reason the bridge parity test does: a crowded design
    goes through the placement pipeline on the way to /generate, so the parts
    themselves move and the labels legitimately follow them. The board's half
    of the promise -- the ink reaching the silkscreen at all, clear of every
    mask opening -- is what the invariant battery and the DRC corpus check.
    """
    import json

    import invariants
    from minibadge_designer import pcb

    leds = _clamped(_unit_matrix(sides=("front", "back")))
    drawn = ui.js("(Ls) => Ls.map(L => { state.leds = [L]; state.art = [];"
                  " return refdesLayout().map(l => [l.ref, l.face, l.at[0], l.at[1]]); })",
                  leds)   # raw, not rounded: the tolerance below is tighter
                          # than four decimals, so rounding here reads as drift
    assert any(drawn), "the canvas drew no part labels at all, so nothing is compared"

    off_by = []
    for d, js in zip(leds, drawn):
        spec = pcb.BadgeSpec(leds=[_py_led(d)])
        board = [(lab["ref"], lab["face"], lab["at"])
                 for lab in pcb.refdes_layout(spec)]
        if len(js) != len(board):
            off_by.append(f"{_describe(d)}: canvas places {len(js)} label(s), "
                          f"the generator {len(board)}")
            continue
        for (jref, jface, jx, jy), (bref, bface, (bx, by)) in zip(js, board):
            if jref != bref or jface != bface:
                off_by.append(f"{_describe(d)}: canvas draws {jref} on the "
                              f"{jface}, the generator places {bref} on the "
                              f"{bface}")
            elif max(abs(jx - bx), abs(jy - by)) > _PARITY_TOL:
                off_by.append(
                    f"{_describe(d)}: {jref} is drawn at "
                    f"{(jx, jy)} and placed at {(round(bx, 4), round(by, 4))}")
    assert not off_by, (
        f"{len(off_by)} part-label placements across {len(leds)} units differ "
        "between preview and generator:\n" + "\n".join(off_by[:10]))

    # Switched off, neither the canvas nor the download shows one.
    import io
    import zipfile
    ui.js("(L) => { state.leds = [L]; state.refdes = false; rebuildAllArt();"
          " draw(); }", _js_led(x=10.0, y=10.0))
    assert ui.js("() => refdesLayout().length") == 0, (
        "the canvas still draws part labels with the switch off")
    params = json.loads(ui.js('() => designFormData().get("params")'))
    params["name"] = "nolabels"
    resp = client.post("/generate", data={"params": json.dumps(params)})
    assert resp.status_code == 200, resp.get_json()
    board = invariants.assert_parses(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "nolabels/nolabels.kicad_pcb").decode())
    assert not invariants._printed_references(board), (
        "the preview stopped drawing part labels and the board still prints "
        f"{[r for r, *_ in invariants._printed_references(board)]}")


#: (kind, how to install one, how to read the list back). Every selectable
#: thing on the board, because "copy" that works on units and silently does
#: nothing on a text is worse than no copy at all.
_CLIP_KINDS = [
    ("led", """() => { state.leds = [{x: 10, y: 6, color: 'blue', side: 'back',
        rot: 90, layout: 'inline', size: '0603', reverse: false, novia: false,
        nodes: [], farled: false, adv: null, clk: false, cnodes: []}];
        renderLedList(); draw(); setSelection('led', 0, 'back'); }""",
     "() => state.leds.map(L => [L.x, L.y, L.rot, L.layout, L.size, L.side])"),
    ("text", """() => { state.texts = [{x: 5, y: 5, text: 'zap', size: 2,
        side: 'back', rot: 0, font: 'kicad', material: 'silk'}];
        renderTextList(); draw(); setSelection('text', 0, 'back'); }""",
     "() => state.texts.map(t => [t.text, t.size, t.side])"),
    ("art", """() => { state.art = [{kind: 'circle', material: 'copper',
        side: 'front', cx: 6, cy: 6, wmm: 5, h: 5, rot: 0, mode: 'threshold',
        palette: [], overrides: []}];
        renderArtList(); draw(); setSelection('art', 0, 'front'); }""",
     "() => state.art.map(a => [a.kind, a.material, a.wmm])"),
]


@pytest.mark.browser
@pytest.mark.parametrize("kind,install,read", _CLIP_KINDS,
                         ids=[k[0] for k in _CLIP_KINDS])
def test_anything_selected_can_be_copied_and_pasted(ui, kind, install, read):
    """Copy and paste put a second one on the board, the same as the first.

    Real clipboard events, driven with the real shortcut: that is what makes a
    copy survive into another tab, and it is the only version of this feature
    worth having -- an app-private buffer would leave a user wondering why the
    paste they just did in the other window produced nothing.

    The copy has to arrive with the properties it was copied WITH. A unit that
    comes back rotated (because the free-spot search was allowed to turn it to
    make it fit) is not a copy of anything the user asked for.
    """
    page = ui.page
    ui.js(install)
    before = ui.js(read)
    assert len(before) == 1, f"the {kind} fixture did not install: {before}"

    page.keyboard.press("ControlOrMeta+c")
    page.keyboard.press("ControlOrMeta+v")
    after = ui.js(read)
    assert len(after) == 2, (
        f"copy then paste left {len(after)} {kind}(s) on the board, not two: "
        f"{after}")
    if kind == "led":
        # x/y move on purpose (a copy under its original is invisible);
        # everything about the unit's shape must not.
        assert after[1][2:] == before[0][2:], (
            f"the pasted unit came back as {after[1][2:]} where the copied one "
            f"was {before[0][2:]}")
        assert (after[1][0], after[1][1]) != (before[0][0], before[0][1]), (
            "the pasted unit landed exactly on top of the original, where "
            "nobody can see or grab it")
    else:
        assert after[1] == before[0], (
            f"the pasted {kind} is {after[1]} and the copied one was "
            f"{before[0]}")
    assert ui.js("() => selected && [selected.kind, selected.index]") == [kind, 1], (
        "the paste left the ORIGINAL selected, so a second paste would copy "
        "the wrong thing and Delete would remove it")
    ui.assert_clean(f"copy and paste a {kind}")


@pytest.mark.browser
def test_cut_takes_the_object_with_it_and_paste_puts_it_back(ui):
    """Cut removes what it copied, and the copy is still on the clipboard.

    Cut that only deleted would be a worse Delete; cut that only copied would
    lose the user's object the first time they tried to move one between
    designs.
    """
    page = ui.page
    ui.js("""() => { state.texts = [{x: 5, y: 5, text: 'zap', size: 2,
        side: 'front', rot: 0, font: 'kicad', material: 'silk'}];
        renderTextList(); draw(); setSelection('text', 0, 'front'); }""")
    page.keyboard.press("ControlOrMeta+x")
    assert ui.js("() => state.texts.length") == 0, "cut left the text behind"
    page.keyboard.press("ControlOrMeta+v")
    back = ui.js("() => state.texts.map(t => [t.text, t.size])")
    assert back == [["zap", 2]], f"paste after cut produced {back}"
    ui.assert_clean("cut and paste a text")


@pytest.mark.browser
def test_a_part_label_can_be_dragged_switched_off_and_reset(ui, client):
    """The label goes where it is dragged, off when told, back when reset.

    Three gestures on one piece of ink, and each has to reach the board: drag
    it, select it and press Delete to switch that one part's label off, and
    double-click it to hand the spot back to the automatic search. Driven
    through the canvas rather than through `state`, because the whole feature
    IS the gesture.
    """
    import io
    import json
    import zipfile

    import invariants

    page = ui.page
    ui.js("""(led) => { state.leds = [led]; state.art = []; state.texts = [];
                        renderLedList(); draw(); }""",
          _js_led(x=10.0, y=6.0, side="front", size="0805", layout="stacked"))
    home = ui.js("() => refdesLayout().find(l => l.ref === 'D1')")
    assert home and not home["hand"], (
        f"D1 has no automatic label to start from ({home})")

    sx, sy = ui.board_to_client(home["at"][0], home["at"][1], "front")
    tx, ty = ui.board_to_client(home["at"][0] + 3.0, home["at"][1], "front")
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(tx, ty, steps=8)
    page.mouse.up()
    moved = ui.js("() => refdesLayout().find(l => l.ref === 'D1')")
    assert moved["hand"] and abs(moved["at"][0] - (home["at"][0] + 3.0)) < 0.3, (
        f"the drag left D1 at {moved['at']} (hand={moved['hand']}); it was "
        f"aimed 3 mm right of {home['at']}")

    # ... and the board prints it there.
    params = json.loads(ui.js('() => designFormData().get("params")'))
    params["name"] = "movedlabel"
    resp = client.post("/generate", data={"params": json.dumps(params)})
    assert resp.status_code == 200, resp.get_json()
    board = invariants.assert_parses(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "movedlabel/movedlabel.kicad_pcb").decode())
    printed = {ref: at for ref, _layer, at, _bx
               in invariants._printed_references(board)}
    assert "D1" in printed, f"the board prints {sorted(printed)}, not D1"
    assert max(abs(printed["D1"][0] - moved["at"][0]),
               abs(printed["D1"][1] - moved["at"][1])) < 0.01, (
        f"the preview shows D1 at {moved['at']} and the board prints it at "
        f"{printed['D1']}")

    # Double-click: back to the automatic spot.
    dx, dy = ui.board_to_client(moved["at"][0], moved["at"][1], "front")
    page.mouse.dblclick(dx, dy)
    back = ui.js("() => refdesLayout().find(l => l.ref === 'D1')")
    assert back and not back["hand"], (
        f"double-click left D1 hand-placed at {back and back['at']}")

    # Select it and press Delete: that ONE label goes, its sibling stays.
    cx, cy = ui.board_to_client(back["at"][0], back["at"][1], "front")
    page.mouse.click(cx, cy)
    assert ui.js("() => selected && [selected.kind, selected.which]") \
        == ["refdes", "led"], "clicking the ink did not select the label"
    page.keyboard.press("Delete")
    left = ui.js("() => refdesLayout().map(l => l.ref)")
    assert left == ["R1"], (
        f"Delete on D1's label left {left}; it must switch that one label off "
        "and nothing else")
    assert ui.js("() => state.leds.length") == 1, (
        "Delete on a label deleted the part under it")
    ui.assert_clean("drag, reset and switch off a part label")


@pytest.mark.browser
def test_the_clk_rail_via_can_be_dragged_and_the_board_follows(ui, client):
    """The jumper's via is placeable, and the preview never lies about it.

    The via was wherever the jumper's own frame put it, which on a crowded
    board is not necessarily where its owner wants a hole. Dragging it moves
    real copper -- the barrel and the stub that feeds it -- so the canvas
    refuses spots the board could not carry, and what it does accept has to
    come out of /generate in the same place.
    """
    import io
    import json
    import re
    import zipfile

    page = ui.page
    ui.js("""(led) => { state.leds = [led]; state.art = []; state.texts = [];
                        renderLedList(); draw(); }""",
          _js_led(x=10.0, y=6.0, side="back", size="0805", clk=True))
    home = ui.js("() => clkInfo().via")
    assert home, "this design has no rail via to drag"

    sx, sy = ui.board_to_client(home[0], home[1], "front")
    tx, ty = ui.board_to_client(home[0], home[1] - 2.0, "front")
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(tx, ty, steps=8)
    page.mouse.up()
    moved = ui.js("() => clkInfo().via")
    assert abs(moved[1] - (home[1] - 2.0)) < 0.3, (
        f"the drag left the via at {moved}, aimed 2 mm above {home}")

    params = json.loads(ui.js('() => designFormData().get("params")'))
    params["name"] = "movedvia"
    resp = client.post("/generate", data={"params": json.dumps(params)})
    assert resp.status_code == 200, resp.get_json()
    text = zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "movedvia/movedvia.kicad_pcb").decode()
    built = [(round(float(a) - 100.0, 3), round(float(b) - 100.0, 3))
             for a, b in re.findall(r"\(via \(at ([\d.]+) ([\d.]+)\)", text)]
    assert any(max(abs(vx - moved[0]), abs(vy - moved[1])) < 0.01
               for vx, vy in built), (
        f"the preview shows the rail via at {moved} and the board drills "
        f"{built}")

    # An illegal spot is not taken: dragging it onto a connector pad leaves it
    # where it was rather than shipping a short.
    px, py = ui.board_to_client(1.27, 1.27, "front")
    vx, vy = ui.board_to_client(moved[0], moved[1], "front")
    page.mouse.move(vx, vy)
    page.mouse.down()
    page.mouse.move(px, py, steps=10)
    page.mouse.up()
    after = ui.js("() => clkInfo().via")
    assert max(abs(after[0] - 1.27), abs(after[1] - 1.27)) > 1.0, (
        f"the via was dropped on a connector pad at {after}")
    ui.assert_clean("drag the CLK rail via")


def _half_dark_png() -> bytes:
    """A wide image, dark in its RIGHT half only.

    Which half of it lands on the board is the whole assertion below, so the
    two halves have to be told apart by position alone.
    """
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(img).rectangle((200, 0, 399, 199), fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


#: (id, side, cx, wmm, expect ink on the board). A wide image with ink in its
#: RIGHT half only, so which half lands says whether the crop kept the right
#: window -- and the back cases are the ones that matter, because the canvas
#: mirrors a back layer when it PAINTS while the generator mirrors the image
#: before cropping.
_OVERHANG = [
    ("front-centred", "front", 10.16, 87.0, True),
    ("front-off-left", "front", -8.0, 40.0, True),
    ("back-centred", "back", 10.16, 87.0, True),
    ("back-off-left", "back", -8.0, 40.0, False),
    ("back-off-right", "back", 28.0, 40.0, True),
]


@pytest.mark.browser
@pytest.mark.parametrize("label,side,cx,wmm,inked", _OVERHANG,
                         ids=[c[0] for c in _OVERHANG])
def test_art_pushed_off_the_board_previews_the_half_the_board_gets(
        ui, client, label, side, cx, wmm, inked):
    """The preview paints the part of an overhanging image that prints.

    Art is no longer fitted to the board, so a layer can be far bigger than the
    board and hang off it -- which is the point: that is how a picture is lined
    up with a board profile. Both sides then have to agree about WHICH part of
    it lands, and the two arrive there differently: the canvas mirrors a back
    layer when it paints, the generator mirrors the image before it crops. Get
    that backwards and the preview shows one half of the drawing while the
    board prints the other.
    """
    import io
    import json
    import zipfile

    import invariants
    from minibadge_designer import pcb

    page = ui.page
    png = _half_dark_png()
    page.click("#tab-art")
    page.set_input_files("#artfile", {"name": "wide.png", "mimeType": "image/png",
                                      "buffer": png})
    page.wait_for_function("() => state.art.length === 1 && state.art[0].img",
                           timeout=UPLOAD_TIMEOUT)
    ui.js("""([side, cx, w]) => {
        const a = state.art[0];
        a.side = side; a.cx = cx; a.cy = 10.16; a.wmm = w;
        a.material = 'silk'; a.mode = 'threshold';
        rebuildArt(a); renderArtList(); draw();
    }""", [side, cx, wmm])

    drawn = ui.js("() => !!(state.art[0].caches && state.art[0].caches.silk)")
    params = json.loads(ui.js('() => designFormData().get("params")'))
    params["name"] = "oh"
    resp = client.post("/generate", data={
        "params": json.dumps(params), "art0": (io.BytesIO(png), "wide.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    root = invariants._parse_sexp(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "oh/oh.kicad_pcb").decode())
    silk = [(float(q[1]) - pcb.ORIGIN, float(q[2]) - pcb.ORIGIN)
            for g in invariants._kids(root, "gr_poly")
            if str(invariants._val(g, "layer")).endswith("SilkS")
            for q in invariants._kids(invariants._kid(g, "pts"), "xy")]

    assert bool(silk) == inked, (
        f"{label}: the board {'prints nothing' if not silk else 'prints ink'} "
        f"where the dark half of the image should {'land' if inked else 'miss'}")
    assert drawn == bool(silk), (
        f"{label}: the preview {'paints' if drawn else 'paints nothing'} and "
        f"the board {'prints' if silk else 'prints nothing'} -- one of them is "
        "showing the wrong half of the drawing")
    if silk:
        xs = [x for x, _y in silk]
        assert min(xs) >= -0.01 and max(xs) <= 20.33, (
            f"{label}: ink runs {min(xs):.2f}..{max(xs):.2f}, off the board")
    ui.assert_clean(f"overhanging art, {label}")
def _centre_stripe_png() -> bytes:
    """A wide image with one dark stripe down its middle.

    Symmetric on purpose: a back layer is painted mirrored, and a stripe that
    survives the mirror lets the front and back cases share one assertion
    about WHERE the ink lands.
    """
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(img).rectangle((180, 0, 219, 199), fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


#: Sample a band of the board off one canvas.  Mirrors the back canvas the
#: same way the app's boardCoords() does.
_BAND = """([side, x0, x1, y0, y1]) => {
    const cv = side === 'back' ? cvB : cvF;
    const left = side === 'back' ? cv.width - VIEW.tx - x1 * SCALE
                                 : VIEW.tx + x0 * SCALE;
    const w = Math.max(1, Math.round((x1 - x0) * SCALE));
    const h = Math.max(1, Math.round((y1 - y0) * SCALE));
    return [...cv.getContext('2d').getImageData(
        Math.round(left), Math.round(VIEW.ty + y0 * SCALE), w, h).data];
}"""

#: (id, layer side, sample while still holding the button)
_ART_MOVES = [
    ("front-mid-drag", "front", True),
    ("front-released", "front", False),
    ("back-released", "back", False),
]


@pytest.mark.browser
@pytest.mark.parametrize("label,side,mid", _ART_MOVES,
                         ids=[c[0] for c in _ART_MOVES])
def test_art_dragged_across_the_board_is_painted_where_it_is_dragged(
        ui, label, side, mid):
    """Dragging an art layer moves the picture, not just its outline.

    Only the part of a layer over the board is classified, so what is cached
    is a window onto the image -- and a move slides that window. If the cache
    is placed by where it was BUILT rather than by where the layer is now, the
    selection box tracks the mouse while the artwork stays behind at the old
    spot, and lining a drawing up with a board profile becomes guesswork.
    """
    page = ui.page
    page.click("#tab-art")
    page.set_input_files("#artfile", {"name": "stripe.png",
                                      "mimeType": "image/png",
                                      "buffer": _centre_stripe_png()})
    page.wait_for_function("() => state.art.length === 1 && state.art[0].img",
                           timeout=UPLOAD_TIMEOUT)
    start, end = 5.0, 15.0
    ui.js("""([side, cx]) => {
        state.leds.length = 0; state.texts.length = 0;
        const a = state.art[0];
        a.side = side; a.cx = cx; a.cy = 10.16; a.wmm = 40;
        a.material = 'silk'; a.mode = 'threshold';
        rebuildArt(a); renderLedList(); renderArtList(); draw();
    }""", [side, start])

    def band(x):
        """The pixels in a 4 mm-wide slice of the board around x."""
        return ui.js(_BAND, [side, x - 2.0, x + 2.0, 6.0, 14.0])

    # What the two slices look like with no art at all, so "inked" below can
    # mean "differs from the bare board" rather than a colour this test guesses.
    ui.js("() => { window.__parked = state.art.pop(); draw(); }")
    blank = (band(start), band(end))
    ui.js("() => { state.art.push(window.__parked);"
          " rebuildArt(window.__parked); draw(); }")

    def inked(now, base):
        return sum(1 for i in range(0, len(base), 4)
                   if any(abs(now[i + k] - base[i + k]) > 8 for k in range(3)))

    was_here = inked(band(start), blank[0])
    was_there = inked(band(end), blank[1])
    assert was_here > 0, (
        "the stripe inks nothing where the layer starts; nothing to test")
    assert was_there == 0, (
        f"the stripe already inks {was_there} pixels at the destination "
        "before the drag; the two bands are not telling the positions apart")

    if mid:
        ui.drag_mm_hold((start, 10.16), (end, 10.16), side=side)
    else:
        ui.drag_mm((start, 10.16), (end, 10.16), side=side)
    moved = ui.js("() => state.art[0].cx")
    assert abs(moved - end) < 1.5, (
        f"the drag left the layer at cx={moved:.2f}, not near {end}: it "
        "grabbed something other than the art, so nothing here was tested")

    now_here = inked(band(start), blank[0])
    now_there = inked(band(end), blank[1])
    if mid:
        ui.release()
    assert now_there > 0.5 * was_here, (
        f"{label}: the layer is at cx={moved:.2f} but the destination band "
        f"only inks {now_there} pixels against {was_here} at the old place -- "
        "the preview is not showing the artwork where the user dragged it")
    assert now_here < 0.2 * was_here, (
        f"{label}: the artwork still inks {now_here} of {was_here} pixels "
        "where the layer used to be, after being dragged away from there")
    ui.assert_clean(f"dragged art, {label}")
#: One row of board pixels off the FRONT canvas, with what it takes to turn a
#: column back into millimetres.
_ROW = """([y, x0, x1]) => {
    const px = Math.round(VIEW.tx + x0 * SCALE);
    const w = Math.max(1, Math.round((x1 - x0) * SCALE));
    return {mm0: (px - VIEW.tx) / SCALE, per: 1 / SCALE,
            data: [...cvF.getContext('2d').getImageData(
                px, Math.round(VIEW.ty + y * SCALE), w, 1).data]};
}"""

#: (id, how the user asks for the highlight)
_HIGHLIGHTS = ["threshold-row", "wand-hover", "override-chip"]


@pytest.mark.browser
@pytest.mark.parametrize("how", _HIGHLIGHTS, ids=_HIGHLIGHTS)
def test_the_highlight_lights_up_the_artwork_it_names(ui, how):
    """Hovering a control lights up the pixels that control changes.

    The highlight is a mask over the classification grid, and that grid now
    covers only the cropped window -- the part of the layer over the board.
    Painted over the whole placed layer instead, or seeded with a pick's
    whole-image fraction, it lights up somewhere else entirely: the user aims
    the wand at one region of the drawing and a different one lights up, so
    the material they choose lands on the wrong part of the badge.
    """
    page = ui.page
    stripe, band = 8.0, 10.16
    page.click("#tab-art")
    page.set_input_files("#artfile", {"name": "stripe.png",
                                      "mimeType": "image/png",
                                      "buffer": _centre_stripe_png()})
    page.wait_for_function("() => state.art.length === 1 && state.art[0].img",
                           timeout=UPLOAD_TIMEOUT)
    # Overhanging on BOTH sides, which is the only time the cropped window and
    # the placed layer differ -- and so the only time this can go wrong.
    ui.js("""([cx]) => {
        state.leds.length = 0; state.texts.length = 0;
        const a = state.art[0];
        a.side = 'front'; a.cx = cx; a.cy = 10.16; a.wmm = 40;
        a.material = 'silk'; a.mode = 'threshold';
        rebuildArt(a); renderLedList(); renderArtList(); draw();
    }""", [stripe])
    assert ui.js("() => state.art[0].cacheUV.u0 > 0"), (
        "the layer does not overhang, so the crop is the whole image and a "
        "mask placed over either rectangle would land in the same place")

    def row():
        r = ui.js(_ROW, [band, 0.0, 20.32])
        return r["data"], r["mm0"], r["per"]

    def changed(a, b, mm0, per):
        """Millimetre span of the columns where two scans differ."""
        cols = [i // 4 for i in range(0, len(a), 4)
                if any(abs(a[i + k] - b[i + k]) > 8 for k in range(3))]
        return (mm0 + cols[0] * per, mm0 + cols[-1] * per, set(cols)) if cols \
            else (None, None, set())

    if how == "override-chip":
        page.select_option("#artlist .wandm", "copper")
        page.click("#artlist .wandb")
        ui.click_mm(stripe, band)
        page.wait_for_selector("#artlist .ovchips .chip2")

    ui.js("() => { window.__parked = state.art.pop(); draw(); }")
    bare, mm0, per = row()
    ui.js("() => { state.art.push(window.__parked);"
          " rebuildArt(window.__parked); draw(); }")
    plain, _, _ = row()
    lo, hi, ink = changed(plain, bare, mm0, per)
    assert ink, "the layer inks nothing across the scan line; nothing to test"

    if how == "threshold-row":
        page.hover("#artlist .m")
    elif how == "wand-hover":
        page.select_option("#artlist .wandm", "copper")
        page.click("#artlist .wandb")
        x, y = ui.board_to_client(stripe, band, "front")
        page.mouse.move(x, y)
    else:
        page.hover("#artlist .ovchips .chip2")
    page.wait_for_function("() => artHL !== null", timeout=ELEMENT_TIMEOUT)
    lit_lo, lit_hi, lit = changed(row()[0], plain, mm0, per)

    assert lit, (
        f"{how}: nothing on the canvas changed when the highlight came on, so "
        "the user gets no feedback about what they are about to change")
    covered = len(lit & ink) / len(ink)
    assert covered > 0.7, (
        f"{how}: the highlight lights up {lit_lo:.2f}..{lit_hi:.2f} mm while "
        f"the artwork it names inks {lo:.2f}..{hi:.2f} mm -- only "
        f"{100 * covered:.0f}% of it overlaps, so it is pointing at the wrong "
        "part of the drawing")
    ui.assert_clean(f"art highlight, {how}")


def _solid_ink_png() -> bytes:
    """A solid dark square: every pixel of it prints, so where the ink stops
    is the keepout and nothing else."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (20, 20, 20)).save(buf, "PNG")
    return buf.getvalue()
#: Walk a ray from a pad centre into the board and report, in mm, where the
#: canvas first paints something the bare board does not.
_RAY = """([px, py, step, n]) => {
    const at = t => {
        const x = Math.round(VIEW.tx + px * SCALE);
        const y = Math.round(VIEW.ty + (py + t) * SCALE);
        return [...cvF.getContext('2d').getImageData(x, y, 1, 1).data];
    };
    const out = [];
    for (let i = 0; i <= n; i++) out.push(at(i * step));
    return out;
}"""


@pytest.mark.browser
@pytest.mark.parametrize("material", ["silk", "copper"],
                         ids=["silk", "copper"])
def test_the_preview_stops_artwork_beside_a_pad_where_the_board_does(
        ui, client, material):
    """Art crowds a connector pad by the same margin in both.

    How close ink may come to a pad is not one number: silk stops where the
    fab would clip it against the mask opening, copper stops at the pour's
    clearance from a powered pad. The preview draws one and the fab prints the
    other, so a drift here either hides artwork the board will carry or shows
    artwork that gets eaten on the way to the fab.
    """
    import io
    import json
    import zipfile

    import invariants
    from shapely.geometry import Point, Polygon

    from minibadge_designer import pcb

    pad = (16.51, 1.27)          # a top-row pad, ray running into the board
    step, count = 0.02, 150
    page = ui.page
    page.click("#tab-art")
    png = _solid_ink_png()
    page.set_input_files("#artfile", {"name": "ink.png", "mimeType": "image/png",
                                      "buffer": png})
    page.wait_for_function("() => state.art.length === 1 && state.art[0].img",
                           timeout=UPLOAD_TIMEOUT)
    ui.js("""([mat]) => {
        state.leds.length = 0; state.texts.length = 0;
        const a = state.art[0];
        a.side = 'front'; a.cx = 10.16; a.cy = 10.16; a.wmm = 22;
        a.material = mat; a.mode = 'threshold';
        rebuildArt(a); renderLedList(); renderArtList(); draw();
    }""", [material])
    lit = ui.js(_RAY, [pad[0], pad[1], step, count])
    ui.js("() => { window.__parked = state.art.pop(); draw(); }")
    bare = ui.js(_RAY, [pad[0], pad[1], step, count])
    ui.js("() => { state.art.push(window.__parked);"
          " rebuildArt(window.__parked); draw(); }")
    hits = [i for i, (a, b) in enumerate(zip(lit, bare))
            if any(abs(a[k] - b[k]) > 8 for k in range(3))]
    assert hits, (
        f"{material}: the preview paints nothing along the ray out of the pad, "
        "so there is no edge to compare")
    canvas_mm = hits[0] * step

    params = json.loads(ui.js('() => designFormData().get("params")'))
    params["name"] = "gap"
    resp = client.post("/generate", data={
        "params": json.dumps(params), "art0": (io.BytesIO(png), "ink.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    root = invariants._parse_sexp(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "gap/gap.kicad_pcb").decode())
    # "Copper" artwork is a mask OPENING over the pour, not a copper polygon:
    # what the user sees as gold is bare metal where the mask is missing.
    want = "F.SilkS" if material == "silk" else "F.Mask"
    polys = []
    for g in invariants._kids(root, "gr_poly"):
        if str(invariants._val(g, "layer")) != want:
            continue
        ring = [(float(q[1]) - pcb.ORIGIN, float(q[2]) - pcb.ORIGIN)
                for q in invariants._kids(invariants._kid(g, "pts"), "xy")]
        if len(ring) >= 3:
            polys.append(Polygon(ring))
    assert polys, f"{material}: the board carries no artwork on {want}"
    board_hits = [i for i in range(count + 1)
                  if any(p.contains(Point(pad[0], pad[1] + i * step))
                         for p in polys)]
    assert board_hits, (
        f"{material}: the board prints no artwork along the ray out of the pad")
    board_mm = board_hits[0] * step

    # Four samples of slack: the board's outline is a traced polygon whose
    # chords cut the corner off a circle, and the canvas is read at whole
    # pixels. A radius that actually moved is a fifth of a millimetre out.
    assert abs(canvas_mm - board_mm) <= 4 * step, (
        f"{material} artwork starts {canvas_mm:.2f} mm from the pad centre in "
        f"the preview and {board_mm:.2f} mm from it on the board: the two "
        "disagree about how close to a connector pad the user may draw")
    ui.assert_clean(f"pad gap parity, {material}")
@pytest.mark.browser
@pytest.mark.parametrize("axis", ["left", "right", "top"],
                         ids=["left", "right", "top"])
def test_the_preview_stops_artwork_at_the_edge_where_the_board_does(
        ui, client, axis):
    """Art runs as close to the routed edge in the preview as on the board.

    Lining a drawing up with a board profile puts the part of it the user
    cares about right against the edge, so the last fraction of a millimetre
    is the drawing. The preview used to paint ink to the cut while the board
    trimmed it back, which is the direction that loses artwork: the picture
    comes back from the fab with a slice shaved off that the editor showed
    whole.
    """
    import io
    import json
    import zipfile

    import invariants
    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union

    from minibadge_designer import pcb

    step, count = 0.02, 60
    page = ui.page
    page.click("#tab-art")
    png = _solid_ink_png()
    page.set_input_files("#artfile", {"name": "ink.png", "mimeType": "image/png",
                                      "buffer": png})
    page.wait_for_function("() => state.art.length === 1 && state.art[0].img",
                           timeout=UPLOAD_TIMEOUT)
    ui.js("""() => {
        state.leds.length = 0; state.texts.length = 0;
        const a = state.art[0];
        a.side = 'front'; a.cx = 10.16; a.cy = 10.16; a.wmm = 26;
        a.material = 'silk'; a.mode = 'threshold';
        rebuildArt(a); renderLedList(); renderArtList(); draw();
    }""")
    # A ray walking IN from just outside the edge, along a line that misses the
    # connector pads and their own keepouts.
    rays = {"left": (0.16, 6.0, 1, 0), "right": (20.16, 6.0, -1, 0),
            "top": (10.16, 0.16, 0, 1)}
    x0, y0, dx, dy = rays[axis]
    lit = ui.js("""([x, y, dx, dy, step, n]) => {
        const out = [];
        for (let i = 0; i <= n; i++) {
            const px = Math.round(VIEW.tx + (x + dx * i * step) * SCALE);
            const py = Math.round(VIEW.ty + (y + dy * i * step) * SCALE);
            out.push([...cvF.getContext('2d').getImageData(px, py, 1, 1).data]);
        }
        return out;
    }""", [x0, y0, dx, dy, step, count])
    ui.js("() => { window.__parked = state.art.pop(); draw(); }")
    bare = ui.js("""([x, y, dx, dy, step, n]) => {
        const out = [];
        for (let i = 0; i <= n; i++) {
            const px = Math.round(VIEW.tx + (x + dx * i * step) * SCALE);
            const py = Math.round(VIEW.ty + (y + dy * i * step) * SCALE);
            out.push([...cvF.getContext('2d').getImageData(px, py, 1, 1).data]);
        }
        return out;
    }""", [x0, y0, dx, dy, step, count])
    ui.js("() => { state.art.push(window.__parked);"
          " rebuildArt(window.__parked); draw(); }")
    hits = [i for i, (a, b) in enumerate(zip(lit, bare))
            if any(abs(a[k] - b[k]) > 8 for k in range(3))]
    assert hits, f"{axis}: the preview paints no artwork along this edge at all"
    canvas_mm = hits[0] * step

    params = json.loads(ui.js('() => designFormData().get("params")'))
    params["name"] = "edge"
    resp = client.post("/generate", data={
        "params": json.dumps(params), "art0": (io.BytesIO(png), "ink.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    root = invariants._parse_sexp(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "edge/edge.kicad_pcb").decode())
    ink = unary_union([
        Polygon([(float(q[1]) - pcb.ORIGIN, float(q[2]) - pcb.ORIGIN)
                 for q in invariants._kids(invariants._kid(g, "pts"), "xy")])
        for g in invariants._kids(root, "gr_poly")
        if str(invariants._val(g, "layer")) == "F.SilkS"])
    assert not ink.is_empty, f"{axis}: the board prints no artwork on this edge"
    board_hits = [i for i in range(count + 1)
                  if ink.contains(Point(x0 + dx * i * step, y0 + dy * i * step))]
    assert board_hits, f"{axis}: the board prints nothing along this ray"
    board_mm = board_hits[0] * step

    # Four samples of slack: the board's edge is a traced polygon whose chords
    # cut corners, and the canvas is read at whole pixels.
    assert abs(canvas_mm - board_mm) <= 4 * step, (
        f"{axis} edge: artwork starts {canvas_mm:.2f} mm in from the cut in "
        f"the preview and {board_mm:.2f} mm in on the board -- the two "
        "disagree about how close to the edge a drawing may be printed")
    ui.assert_clean(f"art edge parity, {axis}")
@pytest.mark.browser
def test_an_overhanging_svg_previews_the_half_the_board_gets(ui, client):
    """A vector drawing pushed off the board previews where it prints.

    The two sides get there by different routes: the browser renders the SVG
    and crops that raster to the board, the server walks the file's own paths
    and clips the geometry. A drawing hung half off the edge is where those
    can disagree about scale -- the vector path used to shrink it to fit while
    the preview overhung it -- and then the picture the user lines up against
    the outline is not the one the fab prints.
    """
    import io
    import json
    import zipfile

    import invariants

    from minibadge_designer import pcb

    # Dark right half only: which half lands is the whole assertion.
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
           b'<rect x="100" y="0" width="100" height="100" fill="#101010"/></svg>')
    page = ui.page
    page.click("#tab-art")
    page.set_input_files("#artfile", {"name": "half.svg",
                                      "mimeType": "image/svg+xml",
                                      "buffer": svg})
    page.wait_for_function("() => state.art.length === 1 && state.art[0].img",
                           timeout=UPLOAD_TIMEOUT)
    ui.js("""() => {
        state.leds.length = 0; state.texts.length = 0;
        const a = state.art[0];
        a.side = 'front'; a.cx = 2.0; a.cy = 10.16; a.wmm = 34;
        a.material = 'silk'; a.mode = 'threshold';
        rebuildArt(a); renderLedList(); renderArtList(); draw();
    }""")
    # The dark half spans the layer's right half: placed 34 mm wide centred at
    # 2.0 it runs 2.0..19.0, so the board should carry ink out to ~19 mm.
    drawn = ui.js("() => !!(state.art[0].caches && state.art[0].caches.silk)")
    params = json.loads(ui.js('() => designFormData().get("params")'))
    params["name"] = "vec"
    resp = client.post("/generate", data={
        "params": json.dumps(params), "art0": (io.BytesIO(svg), "half.svg"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    root = invariants._parse_sexp(zipfile.ZipFile(io.BytesIO(resp.data)).read(
        "vec/vec.kicad_pcb").decode())
    xs = [float(q[1]) - pcb.ORIGIN
          for g in invariants._kids(root, "gr_poly")
          if str(invariants._val(g, "layer")) == "F.SilkS"
          for q in invariants._kids(invariants._kid(g, "pts"), "xy")]
    assert drawn, "the preview painted nothing for an overhanging SVG"
    assert xs, "the board printed nothing for an overhanging SVG"
    # Shrunk to fit instead of clipped, the same placement would put the dark
    # half's right edge near 12 mm rather than out at the board's own edge.
    assert max(xs) > 17.0, (
        f"the vector drawing's ink stops at {max(xs):.2f} mm where the layer "
        "runs to 19 mm: it was resized to fit the board instead of clipped at "
        "the edge, and the preview showed the size it did not get")
    assert max(xs) <= 20.33, (
        f"vector ink runs to {max(xs):.2f} mm, past the board edge")
    ui.assert_clean("overhanging svg")
