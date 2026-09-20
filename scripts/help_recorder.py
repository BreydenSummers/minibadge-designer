"""The shared recorder behind every help recording (see .claude/skills/help-recordings).

One browser session against a private app instance, a hand that moves like a
person's, a frame reel with the cursor painted in, and two encoders: GIF for
the README hero take, animated webp for the "?" tip clips. scripts/
help_build_gif.py is the reference user; each tip clip is a short script that
imports from here and never re-implements any of it.

**A human hand.** Playwright's mouse teleports; a person does not. Every move
is an eased (cosine ease-in-out), slightly curved path whose duration scales
with distance (~0.5-1.2 s), with a short settle before each click and a pause
after each action so the viewer can read what happened. Text is typed a
character at a time. Uploads go through set_input_files on the hidden input,
but the cursor travels to and presses the visible button first.

**A visible cursor.** Headless Chromium draws no pointer and screenshots never
include one, so the recorder tracks the pointer position it commanded, grabs a
screenshot after every commanded step (~12 per second of motion, one per hold),
and paints a white-with-black-outline arrow at that position on each frame,
plus a fading ring for ~250 ms after each mousedown. Frame durations are the
choreographed step times; server waits are shown as one held frame capped at
WAIT_SHOWN_MS.

**Browser.** Google Chrome (channel="chrome") silently closes the page after
~370 screenshots per process. GifCapture launches Playwright's cached bundled
Chromium by path first and only then Chrome, with a warning.

**Encoding.** GIF: one global 256-colour palette from a spread of frames, no
dithering, so Pillow stores only changed rectangles and merges identical
holds. Webp: independent full frames -- minimize_size/allow_mixed are FORBIDDEN;
their lossy deltas ghosted the previous step through every later frame on this
dark UI (shipped and user-reported 2026-09-02).
"""

from __future__ import annotations

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
HELP_DIR = REPO / "minibadge_designer" / "static" / "help"


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

#: Tip clips render at 360 px in the popover; 660 keeps text legible after that
#: downscale (the house width since the first illustrated tips).
TIP_W = 660
TIP_H = round(VIEW_H * TIP_W / VIEW_W)


class Recorder:
    """Drives the app like a person and keeps the frame reel."""

    def __init__(self, cap: Capture, workdir: Path, seed: int = 7,
                 out_w: int = OUT_W, out_h: int = OUT_H):
        self.cap = cap
        self.out_w, self.out_h = out_w, out_h
        self.scale = out_w / VIEW_W
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
        im = im.resize((self.out_w, self.out_h), Image.LANCZOS)
        self._paint_cursor(im)
        path = self.workdir / f"f{len(self.frames):04d}.png"
        im.save(path, compress_level=1)
        self.frames.append((path, dur_ms))
        self.clock_ms += dur_ms
        if self.down_ms is not None and self.clock_ms - self.down_ms > 260:
            self.down_ms = None

    def _paint_cursor(self, im: Image.Image) -> None:
        x, y = self.mx * self.scale, self.my * self.scale
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

    def orbit_drag(self, dx: float, dy: float, bump: float = 0, ms: int = 1800) -> None:
        """Turn the <model-viewer> camera with a real drag from the current
        pointer spot. Measured in this app: ~0.33 deg of azimuth per px of
        horizontal drag (rightwards = camera to the left), and ~0.33 deg of
        elevation per px vertically (dragging DOWN raises the camera toward
        top-down, phi -> 0; dragging UP lowers it toward the board plane,
        phi -> 90). `bump` adds a sine wobble across the drag."""
        self.page.mouse.down()
        self.down_ms = self.clock_ms
        steps = max(8, round(ms / STEP_MS))
        x0, y0 = self.mx, self.my
        for i in range(1, steps + 1):
            u = (1 - math.cos(math.pi * i / steps)) / 2
            self.mx = x0 + dx * u
            self.my = y0 + dy * u + bump * math.sin(math.pi * u)
            self.page.mouse.move(self.mx, self.my)
            self.frame(STEP_MS)
        self.page.mouse.up()
        self.frame(STEP_MS)

    def orbit(self) -> str:
        return self.cap.js("() => { const o = $('d3mv').getCameraOrbit();"
                           " return 'theta ' + Math.round(o.theta*180/Math.PI)"
                           " + ' phi ' + Math.round(o.phi*180/Math.PI); }")

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



# -- assembly ----------------------------------------------------------------------

def _frame_size(frames):
    with Image.open(frames[0][0]) as im:
        return im.size


def assemble_gif(frames: list[tuple[Path, int]], out: Path) -> dict:
    n = len(frames)
    picks = [frames[i][0] for i in sorted({round(k * (n - 1) / 23) for k in range(24)})]
    w, h = _frame_size(frames)
    strip = Image.new("RGB", (w, h * len(picks)))
    for i, p in enumerate(picks):
        strip.paste(Image.open(p), (0, h * i))
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
            "frames_shot": n, "duration_ms": total, "size": (w, h)}


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
    w, h = _frame_size(frames)
    cw, ch = w // 2, h // 2
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




def assemble_webp(frames: list[tuple[Path, int]], out: Path, quality: int = 82) -> dict:
    """Animated webp of INDEPENDENT full frames, identical holds merged.

    No minimize_size, no allow_mixed: those let the encoder store lossy deltas
    against the previous frame, and on this dark UI they ghosted the previous
    step's board through every later frame (shipped 2026-09-02). Quality 82,
    method 6 lands a 6 s clip around 200-300 KB at 660 px.
    """
    merged: list[tuple[Path, int]] = []
    last_bytes = None
    for p, d in frames:
        b = Image.open(p).convert("RGB").tobytes()
        if merged and b == last_bytes:
            merged[-1] = (merged[-1][0], merged[-1][1] + d)
        else:
            merged.append((p, d)); last_bytes = b
    imgs = [Image.open(p).convert("RGB") for p, _ in merged]
    durs = [d for _, d in merged]
    imgs[0].save(out, "WEBP", save_all=True, append_images=imgs[1:], duration=durs,
                 loop=0, quality=quality, method=6, lossless=False)
    with Image.open(out) as g:
        count = getattr(g, "n_frames", 1)
        total = 0
        for i in range(count):
            g.seek(i)
            total += g.info.get("duration", 0)
    w, h = _frame_size(frames)
    return {"bytes": out.stat().st_size, "frames_written": count,
            "frames_shot": len(frames), "duration_ms": total, "size": (w, h)}
