"""Capture harness for the illustrated help examples in static/help/.

The help assets are step-slideshow animated webps: 4-6 held frames of the real
app walking one step of the helmet-badge story, 660x575, dark theme, with one
magenta ring per frame around the control the frame is about. This module owns
everything that must be identical across assets - the server, the browser, the
viewport, fixture loading, mm->px conversion, the ring, and webp assembly - so
that two assets built months apart still look like the same documentation.

Used as a library by capture scripts (see .claude/agents/help-example-builder.md):

    from help_capture import Capture
    with Capture() as cap:
        cap.load_fixture("stage5-text")
        cap.highlight("#tab-leds")
        cap.shoot()                      # frame 1
        ...
        cap.assemble("clk.webp")

Run directly for a smoke test: python scripts/help_capture.py
"""

import json
import threading
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "scripts" / "help_fixtures"
HELP_DIR = REPO / "minibadge_designer" / "static" / "help"

# House style, fixed. The popover renders images 360 px wide; 660 keeps text
# legible after that downscale without shipping megabytes.
FRAME_W, FRAME_H = 660, 575
VIEW_W, VIEW_H = FRAME_W * 2, FRAME_H * 2  # shoot at 2x, downscale once
RING = "#e83e8c"
HOLD_MS, LAST_HOLD_MS = 1400, 2200

LOAD_TIMEOUT = 30_000
OUTLINE_TIMEOUT = 20_000
STATE_TIMEOUT = 15_000

# Exact inverse of the app's boardCoords(), recomputed at call time - SCALE
# changes with the outline, VIEW is the app's {tx, ty} pan offset, and canvas
# width flips on view change. Copied from tests/test_browser.py, the proven
# original.
_BOARD_TO_CLIENT = """([mx, my, side]) => {
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


class _Server:
    def __init__(self):
        from werkzeug.serving import make_server

        from minibadge_designer.webapp import app

        self._srv = make_server("127.0.0.1", 0, app, threaded=True)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._srv.server_port}"

    def close(self):
        self._srv.shutdown()
        self._thread.join(timeout=5)


class Capture:
    """One browser session against a private app instance, plus a frame reel."""

    def __init__(self, workdir=None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.frames = []
        self.steps = []  # Remotion storyboard: shot via step(), not shoot()
        self._server = None
        self._pw = None
        self._browser = None
        self.page = None

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._server = _Server()
        self._pw = sync_playwright().start()
        last = None
        # Bundled chromium first, then the system channels; whichever launches.
        for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
            try:
                self._browser = self._pw.chromium.launch(**kwargs)
                break
            except Exception as exc:  # noqa: BLE001 - any launch failure
                last = exc
        else:
            raise RuntimeError(f"no launchable chromium: {last}")
        ctx = self._browser.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=1)
        self.page = ctx.new_page()
        self.page.goto(self._server.url, timeout=LOAD_TIMEOUT)
        self.page.wait_for_function(
            "() => typeof ready !== 'undefined' && ready === true",
            timeout=LOAD_TIMEOUT)
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        if self._server:
            self._server.close()

    # -- app state -----------------------------------------------------------
    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def wait_state(self, predicate, timeout=STATE_TIMEOUT):
        self.page.wait_for_function(f"() => {predicate}", timeout=timeout)

    def wait_outline(self):
        """Settle predicate is 'the server answered', never a sleep."""
        self.page.wait_for_function(
            "() => !customActive()"
            " || (state.shape.rings && state.shape.rings.length)"
            " || state.shape.outlineEmpty === true",
            timeout=OUTLINE_TIMEOUT)

    def load_fixture(self, name):
        """Restore a designJSON fixture from scripts/help_fixtures/<name>.json."""
        d = json.loads((FIXTURES / f"{name}.json").read_text())
        self.page.evaluate(
            "async (d) => { await restoreDesign(d); draw(); }", d)
        self.wait_outline()
        self.js("() => { renderArtList(); renderLedList(); renderTextList();"
                " renderShapeOpts(); draw(); }")

    def dump_fixture(self, name):
        """Save the CURRENT design as a fixture (for building new stages)."""
        d = self.js("() => designJSON()")
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / f"{name}.json").write_text(json.dumps(d))
        return d

    # -- navigation ----------------------------------------------------------
    def show_panel(self, key):
        self.page.click(f"#tab-{key}")
        self.wait_state(f"activePanel === '{key}'")

    def set_view(self, v):
        self.js("(v) => setView(v)", v)

    def scroll_into_view(self, side="front"):
        self.js("(s) => (s === 'back' ? cvB : cvF)"
                ".scrollIntoView({block: 'center'})", side)

    # -- canvas geometry ------------------------------------------------------
    def board_to_client(self, mm_x, mm_y, side="front"):
        pt = self.js(_BOARD_TO_CLIENT, [mm_x, mm_y, side])
        if pt.get("hidden"):
            raise AssertionError(f"canvas {side} hidden - select its view tab")
        if not (0 <= pt["x"] <= pt["vw"] and 0 <= pt["y"] <= pt["vh"]):
            raise AssertionError(
                f"({mm_x},{mm_y}) on {side} maps off-viewport - scroll it in")
        return pt["x"], pt["y"]

    def click_mm(self, mm_x, mm_y, side="front"):
        self.scroll_into_view(side)
        x, y = self.board_to_client(mm_x, mm_y, side)
        self.page.mouse.click(x, y)

    def drag_mm(self, frm, to, side="front", steps=8):
        """Drag between board coordinates. The destination is recomputed from
        the live canvas rect on EVERY step: pointerdown runs showPanel() and a
        standing warning appearing above the canvas reflows it ~19 px mid-drag,
        so a destination computed once at the press lands ~1 mm short."""
        self.scroll_into_view(side)
        x0, y0 = self.board_to_client(*frm, side)
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        for i in range(1, steps + 1):
            x1, y1 = self.board_to_client(*to, side)
            self.page.mouse.move(x0 + (x1 - x0) * i / steps,
                                 y0 + (y1 - y0) * i / steps)
        self.page.mouse.up()

    # -- the magenta ring ------------------------------------------------------
    _RING_JS = """([x, y, w, h, color]) => {
        for (const el of document.querySelectorAll('.help-ring')) el.remove();
        const d = document.createElement('div');
        d.className = 'help-ring';
        d.style.cssText = `position:fixed;left:${x-8}px;top:${y-8}px;` +
            `width:${w+16}px;height:${h+16}px;border:3px solid ${color};` +
            `border-radius:10px;pointer-events:none;z-index:99999;`;
        document.body.appendChild(d);
    }"""

    def highlight(self, selector):
        """Ring a DOM control. One ring per frame; calling again moves it."""
        box = self.page.locator(selector).first.bounding_box()
        if not box:
            raise AssertionError(f"{selector} has no box - is its panel shown?")
        self.js(self._RING_JS,
                [box["x"], box["y"], box["width"], box["height"], RING])

    def highlight_mm(self, mm_x, mm_y, side="front", r_px=36):
        """Ring a spot on the board itself."""
        x, y = self.board_to_client(mm_x, mm_y, side)
        self.js(self._RING_JS,
                [x - r_px, y - r_px, 2 * r_px, 2 * r_px, RING])

    def clear_highlight(self):
        self.js("() => { for (const el of document.querySelectorAll"
                "('.help-ring')) el.remove(); }")

    # -- Remotion storyboard (the current pipeline) ---------------------------
    def step(self, caption, ring=None, ring_mm=None, side="front", r_px=36,
             hold_ms=None):
        """Record one storyboard step: a CLEAN screenshot plus metadata.

        The ring is NOT baked into the pixels - Remotion draws and animates it
        at render time. `ring` is a CSS selector (boxed ring around a control),
        `ring_mm` is (mm_x, mm_y) on the board (circular spot ring). At most
        one per step; a step needing two rings is two steps. `caption` is the
        short line shown in the chip (<= ~60 chars; it must survive one line
        at 660 px). hold_ms defaults to 1400, and render() bumps the last
        step to 2200 unless it was set explicitly.
        Assert your app state FIRST, exactly as with shoot()."""
        self.clear_highlight()  # a DOM ring must never appear in the pixels
        meta = None
        if ring is not None:
            box = self.page.locator(ring).first.bounding_box()
            if not box:
                raise AssertionError(f"{ring} has no box - is its panel shown?")
            meta = {"kind": "box", "x": box["x"], "y": box["y"],
                    "w": box["width"], "h": box["height"]}
        elif ring_mm is not None:
            x, y = self.board_to_client(*ring_mm, side)
            meta = {"kind": "spot", "x": x, "y": y, "r": r_px}
        path = self.workdir / f"step_{len(self.steps):02d}.png"
        self.page.screenshot(path=str(path))
        self.steps.append({"img": path, "caption": caption, "ring": meta,
                           "hold_ms": hold_ms})
        return path

    def render(self, asset_name, out_dir=None):
        """Render the recorded steps through Remotion (help_motion.py) into
        the house-style animated webp - or, for a .png asset with exactly one
        step, a styled still. Returns the output path."""
        import help_motion
        out_dir = Path(out_dir) if out_dir else self.workdir
        out = out_dir / asset_name
        if not self.steps:
            raise AssertionError("no steps recorded - call step() first")
        steps = [dict(s) for s in self.steps]
        for i, s in enumerate(steps):
            if s["hold_ms"] is None:
                s["hold_ms"] = LAST_HOLD_MS if i == len(steps) - 1 else HOLD_MS
        if out.suffix == ".png":
            if len(steps) != 1:
                raise AssertionError(".png assets are single-step stills")
            return help_motion.render_still(
                steps[0]["img"], out, caption=steps[0]["caption"])
        path, stats = help_motion.render_storyboard(
            steps, out, src_w=VIEW_W, src_h=VIEW_H)
        print(f"rendered {path}: {stats}")
        return path

    # -- frames (legacy Pillow slideshow; kept for comparison) -----------------
    def shoot(self, note=""):
        """Capture the viewport as the next frame. Assert your state FIRST."""
        path = self.workdir / f"frame_{len(self.frames):02d}.png"
        self.page.screenshot(path=str(path))
        self.frames.append((path, note))
        return path

    def assemble(self, asset_name, durations=None, out_dir=None):
        """Downscale the reel to 660x575 and write one animated webp (or, for
        a single frame, a still png). Returns the output path."""
        out_dir = Path(out_dir) if out_dir else self.workdir
        out = out_dir / asset_name
        imgs = [Image.open(p).convert("RGB").resize(
            (FRAME_W, FRAME_H), Image.LANCZOS) for p, _ in self.frames]
        if not imgs:
            raise AssertionError("no frames shot")
        if out.suffix == ".png":
            imgs[-1].save(out)
            return out
        if durations is None:
            durations = [HOLD_MS] * (len(imgs) - 1) + [LAST_HOLD_MS]
        imgs[0].save(out, save_all=True, append_images=imgs[1:], loop=0,
                     duration=durations, quality=80, method=6)
        return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(REPO))
    with Capture(workdir="/tmp") as cap:
        cap.highlight("#download")
        cap.shoot()
        p = cap.assemble("smoke.png")
        print(f"ok: {p}")
