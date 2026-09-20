"""Record the helmet badge being designed start to finish, as an animated GIF.

    .venv/bin/python scripts/help_build_gif.py [--workdir DIR] [--out docs/helmet-build.gif]

This is the README's hero image: one continuous take of the real app, driven
the way a person would drive it, from the blank square board to the finished
badge in the 3D view. It reuses the help-example harness (scripts/help_capture.py:
private server, Chromium, the 1320x1150 dark viewport, mm->client conversion)
and adds two things the harness does not have.

**A human hand.** Playwright's mouse teleports; a person does not. Every move
here is an eased (cosine ease-in-out), slightly curved path whose duration
scales with distance (~0.5-1.2 s), with a short settle before each click and a
pause after each action so the viewer can read what happened. Text is typed a
character at a time. Uploads still go through set_input_files on the hidden
input, but the cursor travels to and presses the visible button first, so the
recording reads as the person having picked a file.

**A visible cursor.** Headless Chromium draws no pointer, and screenshots never
include one. So the recorder tracks the pointer position it commanded, grabs a
screenshot after every commanded step (~12 per second of motion, one per hold),
and paints a standard white-with-black-outline arrow at that position on each
frame with Pillow, plus a fading ring for ~250 ms after each mousedown. Frame
durations are the choreographed step times, so the GIF plays at the pace the
gestures were designed at; server waits (outline, 3D export) are shown as a
single held frame capped at ~1.5 s, because a 40 s kicad-cli export is not
something anybody wants to watch.

Encoding: frames go to disk as they are shot, then one global 256-colour
palette is built from a spread of them and every frame is quantized against it
with no dithering. Sharing the palette is what lets Pillow's GIF writer store
only the changed rectangle per frame and merge identical holds, which is how
40 s of a mostly static UI fits in a few megabytes.

Traps hit while building this (all real):
- The shape upload is a silent no-op unless #eladdimg is clicked first (the
  art upload has no such rule). The harness memory records this; it is easy to
  forget when scripting the "human" click anyway.
- The threshold slider is a real <input type=range>; a mouse drag along it
  works and re-requests the outline, but the destination x must be computed
  from the slider's live box, which moves when the card above it grows.
- Text legality is judged by three async gates (customActive() &&
  boardCarved(), FONT_INK['blackops'], the loaded document.fonts entry); the
  final blockingProblems() check waits for all three or it lies.
- Snap pulls a drop onto the centre lines / 0.5 mm grid; the canonical LED and
  text spots are not on it, so those two drags hold Alt, as a person would.
- Native <select> popups never render headless. The cursor presses the select
  and the value then changes via select_option; it reads fine at 12 fps.
- **Google Chrome (channel="chrome") closes the page after ~370 screenshots**,
  per browser process, silently: no crash event, just "Target page ... has
  been closed" on whatever call came next. Neither --disable-gpu nor software
  ANGLE helps, and a fresh page in the same browser inherits the count. A
  40 s take at 12 fps is ~450 frames, so this recorder needs Playwright's own
  Chromium (bundled build or headless shell), which took 450+ in a row and
  renders the 3D view. help_capture.Capture falls back to the Chrome channel
  when the bundled build for the installed Playwright is missing; GifCapture
  below looks for ANY cached bundled build first and only then Chrome, with a
  warning, because a take on Chrome will die at frame ~370.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

from help_capture import VIEW_H, VIEW_W, Capture

HELMET = REPO / "scripts" / "help_fixtures" / "helmet.png"


class GifCapture(Capture):
    """Capture, but launched on a browser that survives 450+ screenshots."""

    @staticmethod
    def _launch_options():
        cache = Path.home() / "Library" / "Caches" / "ms-playwright"
        opts: list[dict] = [{}]  # the bundled build for this Playwright, if installed
        for pat in ("chromium_headless_shell-*/*/chrome-headless-shell",
                    "chromium-*/*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                    "chromium-*/*/chrome"):
            for exe in sorted(cache.glob(pat), reverse=True):
                if exe.is_file():
                    opts.append({"executable_path": str(exe)})
        opts.append({"channel": "chrome"})
        return opts

    def __enter__(self):
        import help_capture
        from playwright.sync_api import sync_playwright
        self._server = help_capture._Server()
        self._pw = sync_playwright().start()
        last = None
        for kwargs in self._launch_options():
            try:
                self._browser = self._pw.chromium.launch(**kwargs)
            except Exception as exc:  # noqa: BLE001 - any launch failure
                last = exc
                continue
            if kwargs.get("channel") == "chrome":
                print("WARNING: recording on Google Chrome; its headless mode closes the "
                      "page after ~370 screenshots. Run `playwright install chromium`.",
                      file=sys.stderr)
            break
        else:
            raise RuntimeError(f"no launchable chromium: {last}")
        ctx = self._browser.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=1)
        self.page = ctx.new_page()
        self.page.goto(self._server.url, timeout=help_capture.LOAD_TIMEOUT)
        self.page.wait_for_function(
            "() => typeof ready !== 'undefined' && ready === true",
            timeout=help_capture.LOAD_TIMEOUT)
        return self

OUT_W = 720
OUT_H = round(VIEW_H * OUT_W / VIEW_W)
SCALE = OUT_W / VIEW_W
FPS = 12
STEP_MS = round(1000 / FPS)
WAIT_SHOWN_MS = 1500          # a server wait is shown as at most this long

# The story's canonical spots (scripts/help_fixtures/stage*.json).
SHAPE_THRESHOLD = 220         # 128 punches the vents out of the outline
SHAPE_W = 18.0
ART_W = 18.0
LED_TO = (10.3625, 10.0)      # back, behind the visor (stage4-led.json)
TEXT = "made by half"
TEXT_FONT = "blackops"
TEXT_SIZE = 1.3
TEXT_TO = (10.16, 14.9)       # stage5-text.json; warning-free under all gates
VISOR_UV = (0.4988, 0.4891)   # stage3-wand.json override, in art image space


class Recorder:
    """Drives the app like a person and keeps the frame reel."""

    def __init__(self, cap: Capture, workdir: Path, seed: int = 7):
        self.cap = cap
        self.page = cap.page
        self.workdir = workdir
        self.frames: list[tuple[Path, int]] = []   # (png path, duration ms)
        self.mx, self.my = VIEW_W * 0.55, VIEW_H * 0.55
        self.down_ms: int | None = None
        self.clock_ms = 0
        self.rng = random.Random(seed)
        self.marks: list[tuple[int, str]] = []
        self.events: list[str] = []
        self.page.on("crash", lambda: self.events.append("PAGE CRASH"))
        self.page.on("pageerror", lambda e: self.events.append(f"pageerror: {str(e)[:160]}"))
        self.page.on("close", lambda: self.events.append("PAGE CLOSED"))
        cap._browser.on("disconnected", lambda: self.events.append("BROWSER DISCONNECTED"))
        self.page.on("console", lambda m: self.events.append(f"console[{m.type}]: {m.text[:160]}"))
        self.page.context.on("page", lambda pg: self.events.append(f"NEW PAGE: {pg.url}"))
        self.page.mouse.move(self.mx, self.my)

    # -- frames ----------------------------------------------------------------
    def mark(self, label: str) -> None:
        self.marks.append((self.clock_ms, label))

    def frame(self, dur_ms: int) -> None:
        png = self.page.screenshot()
        im = Image.open(BytesIO(png)).convert("RGB")
        im = im.resize((OUT_W, OUT_H), Image.LANCZOS)
        self._paint_cursor(im)
        path = self.workdir / f"f{len(self.frames):04d}.png"
        im.save(path, compress_level=1)
        self.frames.append((path, dur_ms))
        self.clock_ms += dur_ms
        if self.down_ms is not None and self.clock_ms - self.down_ms > 260:
            self.down_ms = None

    def _paint_cursor(self, im: Image.Image) -> None:
        x, y = self.mx * SCALE, self.my * SCALE
        d = ImageDraw.Draw(im, "RGBA")
        if self.down_ms is not None:
            age = (self.clock_ms - self.down_ms) / 260
            r = 6 + 12 * age
            a = int(200 * (1 - age))
            d.ellipse((x - r, y - r, x + r, y + r), outline=(255, 255, 255, a), width=2)
        # Classic arrow, ~18 px tall at frame scale, tip at the hot spot.
        pts = [(0, 0), (0, 17), (4.2, 13.2), (7.4, 19.4), (10.2, 18.1),
               (7.0, 12.0), (12.5, 12.0)]
        poly = [(x + px, y + py) for px, py in pts]
        d.polygon(poly, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
        d.line(poly + [poly[0]], fill=(0, 0, 0, 255), width=1)

    def hold(self, ms: int) -> None:
        self.frame(ms)

    # -- the hand ---------------------------------------------------------------
    def move_to(self, x: float, y: float, ms: int | None = None) -> None:
        dist = math.hypot(x - self.mx, y - self.my)
        if ms is None:
            ms = int(min(1200, max(450, 350 + dist * 0.9)))
        steps = max(4, round(ms / STEP_MS))
        x0, y0 = self.mx, self.my
        # A gentle bow off the straight line, sign chosen once per move.
        bow = min(26, dist * 0.06) * self.rng.choice((-1, 1))
        nx, ny = (-(y - y0) / dist, (x - x0) / dist) if dist > 1 else (0, 0)
        for i in range(1, steps + 1):
            u = i / steps
            e = (1 - math.cos(math.pi * u)) / 2
            s = math.sin(math.pi * u) * bow
            self.mx = x0 + (x - x0) * e + nx * s
            self.my = y0 + (y - y0) * e + ny * s
            self.page.mouse.move(self.mx, self.my)
            self.frame(STEP_MS)
        self.mx, self.my = x, y
        self.page.mouse.move(x, y)

    def box(self, selector: str):
        b = self.page.locator(selector).first.bounding_box()
        if not b:
            raise AssertionError(f"{selector} has no box - is its panel shown?")
        return b

    def center(self, selector: str, fx: float = 0.5, fy: float = 0.5):
        b = self.box(selector)
        return b["x"] + b["width"] * fx, b["y"] + b["height"] * fy

    def press(self) -> None:
        self.page.mouse.down()
        self.down_ms = self.clock_ms
        self.frame(80)
        self.page.mouse.up()
        self.frame(80)

    def click(self, selector: str, settle_ms: int = 220, after_ms: int = 350) -> None:
        self.move_to(*self.center(selector))
        self.hold(settle_ms)
        self.press()
        if after_ms:
            self.hold(after_ms)

    def click_at(self, x: float, y: float, settle_ms: int = 220, after_ms: int = 350) -> None:
        self.move_to(x, y)
        self.hold(settle_ms)
        self.press()
        if after_ms:
            self.hold(after_ms)

    def choose(self, selector: str, value: str, after_ms: int = 450) -> None:
        """A <select>: the cursor presses it, then the value changes."""
        self.click(selector, after_ms=120)
        self.page.select_option(selector, value)
        self.hold(after_ms)

    def type_text(self, text: str) -> None:
        for ch in text:
            self.page.keyboard.type(ch)
            self.frame(self.rng.randint(70, 130))

    def set_number(self, selector: str, value: str) -> None:
        """Click into a number field, select what is there, type, tab out."""
        self.click(selector, after_ms=120)
        # Select-all shortcuts do not reach a number input headless; select
        # the text the way a double-click would, then overtype it.
        self.page.locator(selector).first.select_text()
        self.frame(120)
        self.type_text(value)
        self.page.keyboard.press("Tab")
        self.hold(300)

    def drag_slider(self, selector: str, value: float) -> None:
        """Drag an <input type=range> to a value, recomputing from its live box."""
        b = self.box(selector)
        lo = float(self.page.locator(selector).first.get_attribute("min"))
        hi = float(self.page.locator(selector).first.get_attribute("max"))
        cur = float(self.page.locator(selector).first.input_value())
        pad = 8  # thumb radius; the track runs between the thumb centres
        def xat(v):
            b2 = self.box(selector)
            return b2["x"] + pad + (b2["width"] - 2 * pad) * (v - lo) / (hi - lo)
        self.move_to(xat(cur), b["y"] + b["height"] / 2)
        self.hold(200)
        self.page.mouse.down()
        self.down_ms = self.clock_ms
        steps = 14
        x0 = self.mx
        for i in range(1, steps + 1):
            u = (1 - math.cos(math.pi * i / steps)) / 2
            self.mx = x0 + (xat(value) - x0) * u
            self.page.mouse.move(self.mx, self.my)
            self.frame(STEP_MS)
        self.page.mouse.up()
        self.hold(250)

    def drag_mm(self, frm, to, side="front", alt=False, ms=1100) -> None:
        """Drag between board coordinates; the destination is recomputed from
        the live canvas rect on every step (a warning banner reflows it)."""
        self.cap.scroll_into_view(side)
        x0, y0 = self.cap.board_to_client(*frm, side)
        self.move_to(x0, y0)
        self.hold(250)
        if alt:
            self.page.keyboard.down("Alt")
        self.page.mouse.down()
        self.down_ms = self.clock_ms
        self.frame(100)
        steps = max(6, round(ms / STEP_MS))
        for i in range(1, steps + 1):
            u = (1 - math.cos(math.pi * i / steps)) / 2
            x1, y1 = self.cap.board_to_client(*to, side)
            self.mx, self.my = x0 + (x1 - x0) * u, y0 + (y1 - y0) * u
            self.page.mouse.move(self.mx, self.my)
            self.frame(STEP_MS)
        self.page.mouse.up()
        if alt:
            self.page.keyboard.up("Alt")
        self.hold(300)

    def wait_shown(self, predicate: str, timeout_ms: int = 30_000) -> None:
        """Wait on app state; show the wait as one held frame, capped."""
        t0 = time.monotonic()
        self.cap.wait_state(predicate, timeout=timeout_ms)
        waited = int((time.monotonic() - t0) * 1000)
        if waited > 150:
            self.frame(min(waited, WAIT_SHOWN_MS))

    def wait_outline_shown(self) -> None:
        t0 = time.monotonic()
        self.cap.wait_outline()
        waited = int((time.monotonic() - t0) * 1000)
        self.frame(min(max(waited, 300), WAIT_SHOWN_MS))


# -- the take ------------------------------------------------------------------

def record(rec: Recorder, from_design: dict | None = None, stop_after: int = 99) -> dict:
    cap, page = rec.cap, rec.page
    if from_design is not None:
        # Debug/tuning path: skip straight to the ending on a finished design.
        page.evaluate("async (d) => { await restoreDesign(d); draw(); }", from_design)
        cap.wait_outline()
        cap.js("() => { renderArtList(); renderLedList(); renderTextList(); renderShapeOpts(); draw(); }")
        cap.show_panel("text")
        rec.hold(600)
        return finish(rec, problems=cap.js("() => blockingProblems()"))
    rec.mark("blank square board")
    rec.hold(1200)

    # 1. Board shape from the helmet silhouette, on a black mask.
    cap.show_panel("shape")
    rec.choose("#mask", "black")
    cap.wait_state("state.mask === 'black'")
    rec.mark("black soldermask")
    rec.click("#eladdimg", after_ms=150)
    page.set_input_files("#shapefile", str(HELMET))
    cap.wait_state("state.shape.mode === 'custom' && state.shape.elements.length === 1", timeout=20_000)
    rec.wait_outline_shown()
    rec.mark("helmet silhouette uploaded")
    rec.hold(600)
    rec.drag_slider("#shapeopts input.eth", SHAPE_THRESHOLD)
    cap.wait_state(f"state.shape.elements[0].threshold >= {SHAPE_THRESHOLD - 6}"
                   f" && state.shape.elements[0].threshold <= {SHAPE_THRESHOLD + 6}")
    # Land exactly on the canonical value if the thumb stopped a notch off.
    page.evaluate(f"() => {{ const e = state.shape.elements[0]; if (e.threshold !== {SHAPE_THRESHOLD}) {{"
                  f" e.threshold = {SHAPE_THRESHOLD}; const s = document.querySelector('#shapeopts input.eth');"
                  f" s.value = {SHAPE_THRESHOLD}; s.dispatchEvent(new Event('input')); }} }}")
    rec.wait_outline_shown()
    rec.mark("threshold 220")
    rec.set_number("#shapeopts input.ewdn", f"{SHAPE_W:g}")
    rec.wait_outline_shown()
    rec.mark("board width 18 mm")
    rec.hold(500)
    if stop_after <= 1:
        return finish(rec, problems=cap.js("() => blockingProblems()"))

    # 2. The same picture as artwork, by colour.
    rec.click("#tab-art", after_ms=250)
    cap.wait_state("activePanel === 'art'")
    rec.click("#addart", after_ms=150)
    page.set_input_files("#artfile", str(HELMET))
    rec.wait_shown("state.art.length === 1 && state.art[0].palette && state.art[0].palette.length > 0", 20_000)
    rec.mark("artwork uploaded, by colour")
    rec.hold(700)
    # White is background, not ink.
    rec.choose("#artlist select.pm[data-j='1']", "ignore")
    cap.wait_state("state.art[0].palette[1].material === 'ignore'")
    rec.set_number("#artlist input.wdn", f"{ART_W:g}")
    cap.wait_state(f"Math.abs(state.art[0].wmm - {ART_W}) < 0.01")
    rec.mark("art width 18 mm, white ignored")
    rec.hold(500)
    # The visor becomes a glow window: wand, one connected region.
    rec.choose("#artlist select.wandm", "glow", after_ms=250)
    rec.click("#artlist .wandb", after_ms=250)
    cap.wait_state("!!picking")
    a = cap.js("() => ({cx: state.art[0].cx, cy: state.art[0].cy, w: state.art[0].wmm,"
               " h: state.art[0].wmm * state.art[0].ih / state.art[0].iw})")
    vx = a["cx"] + (VISOR_UV[0] - 0.5) * a["w"]
    vy = a["cy"] + (VISOR_UV[1] - 0.5) * a["h"]
    cap.scroll_into_view("front")
    rec.click_at(*cap.board_to_client(vx, vy, "front"), after_ms=200)
    rec.wait_shown("state.art[0].overrides.length === 1 && state.art[0].overrides[0].material === 'glow'", 15_000)
    rec.mark("visor picked as glow window")
    rec.hold(900)
    if stop_after <= 2:
        return finish(rec, problems=cap.js("() => blockingProblems()"))

    # 3. The red LED goes behind the visor, on the back.
    rec.click("#tab-leds", after_ms=250)
    cap.wait_state("activePanel === 'leds'")
    led = cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y, side: state.leds[0].side, color: state.leds[0].color})")
    if led["side"] != "back":
        rec.choose("#ledlist .item select.s", "back")
        cap.wait_state("state.leds[0].side === 'back'")
    if led["color"] != "red":
        rec.choose("#ledlist .item select.c", "red")
        cap.wait_state("state.leds[0].color === 'red'")
    rec.drag_mm((led["x"], led["y"]), LED_TO, side="back", alt=True)
    cap.wait_state(f"Math.abs(state.leds[0].x - {LED_TO[0]}) < 0.3 && Math.abs(state.leds[0].y - {LED_TO[1]}) < 0.3")
    rec.mark("LED behind the visor (back)")
    rec.hold(800)
    if stop_after <= 3:
        return finish(rec, problems=cap.js("() => blockingProblems()"))

    # 4. Stencil text on the back.
    rec.click("#tab-text", after_ms=250)
    cap.wait_state("activePanel === 'text'")
    rec.click("#addtext", after_ms=200)
    cap.wait_state("state.texts.length === 1")
    rec.click("#textlist .item input.tx", after_ms=100)
    rec.type_text(TEXT)
    cap.wait_state(f"state.texts[0].text === {TEXT!r}")
    rec.hold(300)
    rec.choose("#textlist .item select.fnt", TEXT_FONT)
    cap.wait_state(f"state.texts[0].font === '{TEXT_FONT}'")
    rec.choose("#textlist .item select.s", "back")
    cap.wait_state("state.texts[0].side === 'back'")
    rec.set_number("#textlist .item input.szn", f"{TEXT_SIZE:g}")
    cap.wait_state(f"Math.abs(state.texts[0].size - {TEXT_SIZE}) < 0.01")
    if stop_after <= 4:
        return finish(rec, problems=cap.js("() => blockingProblems()"))
    t = cap.js("() => ({x: state.texts[0].x, y: state.texts[0].y})")
    rec.drag_mm((t["x"], t["y"]), TEXT_TO, side="back", alt=True, ms=1300)
    cap.wait_state(f"Math.abs(state.texts[0].x - {TEXT_TO[0]}) < 0.3 && Math.abs(state.texts[0].y - {TEXT_TO[1]}) < 0.3")
    if stop_after <= 5:
        return finish(rec, problems=cap.js("() => blockingProblems()"))
    # The three gates before believing the legality check.
    cap.wait_state("customActive() && boardCarved()")
    cap.wait_state(f"!!FONT_INK['{TEXT_FONT}']")
    page.evaluate(f"() => document.fonts.load('16px bm-{TEXT_FONT}')")
    cap.wait_state(f"[...document.fonts].some(f => f.family === 'bm-{TEXT_FONT}' && f.status === 'loaded')")
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    rec.mark("text placed on the back")
    rec.hold(900)
    return finish(rec, problems)


def finish(rec: Recorder, problems) -> dict:
    cap, page = rec.cap, rec.page
    # 5. The 3D view. The back-canvas drags scrolled the canvas zone, and the
    # view tabs scroll with it; a person would wheel back up first.
    cap.set_view("both")
    rec.move_to(*cap.board_to_client(10.16, 10.16, "front"))
    for _ in range(6):
        page.mouse.wheel(0, -250)
        rec.frame(STEP_MS)
    rec.hold(300)
    (rec.workdir / "design.json").write_text(__import__("json").dumps(cap.js("() => designJSON()")))
    rec.click("#tab-3d", after_ms=200)
    cap.wait_state("mode3d === true")
    rec.frame(WAIT_SHOWN_MS)  # the "building model…" spinner
    cap.wait_state("!m3d.busy && (!!m3d.url || !!m3d.err)", timeout=240_000)
    err = cap.js("() => m3d.err ? String(m3d.err) : null")
    if not err:
        cap.wait_state("$('d3mv').loaded === true", timeout=60_000)
        rec.hold(600)
        # A slow orbit, the way a person turns a model over.
        mv = rec.center("#d3mv")
        rec.move_to(mv[0] + 60, mv[1] + 20)
        rec.hold(200)
        page.mouse.down()
        rec.down_ms = rec.clock_ms
        steps = 22
        x0, y0 = rec.mx, rec.my
        for i in range(1, steps + 1):
            u = (1 - math.cos(math.pi * i / steps)) / 2
            rec.mx, rec.my = x0 - 170 * u, y0 - 40 * math.sin(math.pi * u)
            page.mouse.move(rec.mx, rec.my)
            rec.frame(STEP_MS)
        page.mouse.up()
    rec.mark("3D view" + (f" FAILED: {err}" if err else ""))
    rec.hold(2500)
    return {"blocking_problems": problems, "model3d_error": err,
            "design": cap.js("() => designJSON()")}


# -- assembly ----------------------------------------------------------------------

def assemble(frames: list[tuple[Path, int]], out: Path) -> dict:
    n = len(frames)
    picks = [frames[i][0] for i in sorted({round(k * (n - 1) / 23) for k in range(24)})]
    strip = Image.new("RGB", (OUT_W, OUT_H * len(picks)))
    for i, p in enumerate(picks):
        strip.paste(Image.open(p), (0, OUT_H * i))
    master = strip.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    imgs = []
    for p, _ in frames:
        imgs.append(Image.open(p).convert("RGB").quantize(palette=master, dither=Image.Dither.NONE))
    durs = [d for _, d in frames]
    imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=durs,
                 loop=0, optimize=False, disposal=1)
    with Image.open(out) as g:
        count = getattr(g, "n_frames", 1)
        total = 0
        for i in range(count):
            g.seek(i)
            total += g.info.get("duration", 0)
    return {"bytes": out.stat().st_size, "frames_written": count,
            "frames_shot": n, "duration_ms": total, "size": (OUT_W, OUT_H)}


def contact_sheet(frames: list[tuple[Path, int]], out: Path, k: int = 16) -> Path:
    """k frames evenly spaced in TIME (not index), 4 per row, timestamped."""
    total = sum(d for _, d in frames)
    targets = [total * (i + 0.5) / k for i in range(k)]
    chosen, acc, j = [], 0, 0
    for p, d in frames:
        while j < k and acc + d > targets[j]:
            chosen.append((p, acc)); j += 1
        acc += d
    while len(chosen) < k:
        chosen.append((frames[-1][0], total))
    cw, ch = OUT_W // 2, OUT_H // 2
    sheet = Image.new("RGB", (cw * 4, ch * ((k + 3) // 4)), "black")
    d = ImageDraw.Draw(sheet)
    for i, (p, t) in enumerate(chosen):
        im = Image.open(p).resize((cw, ch), Image.LANCZOS)
        x, y = (i % 4) * cw, (i // 4) * ch
        sheet.paste(im, (x, y))
        d.rectangle((x, y, x + 62, y + 16), fill="black")
        d.text((x + 3, y + 2), f"{t / 1000:5.1f}s", fill="white")
    sheet.save(out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=str(REPO / "docs" / "helmet-build.gif"))
    ap.add_argument("--stop-after", type=int, default=99,
                    help="jump to the 3D ending after step N (1 shape, 2 art, 3 LED); bisecting aid")
    ap.add_argument("--from-design", default=None,
                    help="skip to the ending on this designJSON file (tuning aid)")
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="helmet-gif-"))
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("f*.png"):
        old.unlink()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = None
    with GifCapture(workdir=workdir) as cap:
        rec = Recorder(cap, workdir)
        try:
            fd = __import__("json").loads(Path(args.from_design).read_text()) if args.from_design else None
            result = record(rec, fd, stop_after=args.stop_after)
        finally:
            print("marks:")
            for ms, label in rec.marks:
                print(f"  {ms / 1000:5.1f}s  {label}")
            print("frames shot:", len(rec.frames), "page events:", rec.events)
    if result is None:
        raise SystemExit("the take did not finish; nothing written")
    stats = assemble(rec.frames, out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    print("blocking problems at the end:", result["blocking_problems"])
    print("3D error:", result["model3d_error"])
    print("gif:", out, stats)
    print("contact sheet:", sheet)


if __name__ == "__main__":
    main()
