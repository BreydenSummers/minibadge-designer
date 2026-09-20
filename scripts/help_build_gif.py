"""Record the helmet badge being designed start to finish, as an animated GIF.

    .venv/bin/python scripts/help_build_gif.py [--workdir DIR] [--out docs/helmet-build.gif]

This is the README's hero image: one continuous take of the real app, driven
the way a person would drive it, from the blank square board to the finished
badge in the 3D view: black mask, the helmet silhouette as the board shape
(threshold 220, 18 mm), the top row of connector pins unticked, the same
picture as by-colour artwork (gold stripes copper, goggle rim silkscreen), the
magic wand turning the visor lens into BARE BOARD (the fixtures use a glow
window there; this take does not), the red LED on the back behind it, the
stencil text "half" on the back (Black Ops One, 2 mm at (10.16, 14.9), legal
under all three gates), then the 3D view: an orbit, the Soldermask and Board
opacity sliders dragged to 0 so the copper stands alone, a near top-down look
at the routing, and a low profile with a wheel zoom-in where the vias show as
pillars between the two copper layers. It reuses the help-example harness (scripts/help_capture.py:
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
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from help_recorder import (
    HELMET,
    STEP_MS,
    WAIT_SHOWN_MS,
    GifCapture,
    Recorder,
    assemble_gif,
    contact_sheet,
)

SHAPE_THRESHOLD = 220         # 128 punches the vents out of the outline
SHAPE_W = 18.0
ART_W = 18.0
LED_TO = (10.3625, 10.0)      # back, behind the visor (stage4-led.json)
TEXT = "half"
TEXT_FONT = "blackops"
TEXT_SIZE = 2.0               # "half" alone is short enough for 2 mm on the chin
TEXT_TO = (10.16, 14.9)       # the fixtures' spot; measured legal for "half" up to 2.2 mm
TOP_PINS = ("1", "2", "7", "8")  # the row the stage fixtures untick
VISOR_UV = (0.4988, 0.4891)   # the visor lens, in art image space (stage3-wand.json)


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
    rec.hold(1000)

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
    # Only the bottom row of connector pins is kept (stage fixtures: 9, 10, 15,
    # 16). The bottom row carries 3V3 + GND, so the badge stays powered.
    for n in TOP_PINS:
        rec.click(f"#pingrid input[data-pin='{n}']", settle_ms=180, after_ms=250)
        cap.wait_state(f"!(state.pins || []).includes('{n}')")
    rec.wait_outline_shown()
    cap.wait_state("state.pins.length === 4 && ['9','10','15','16'].every(n => state.pins.includes(n))")
    rec.mark("top-row pins unticked")
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
    # The visor lens becomes bare board: wand, one connected region. The rim
    # stays silkscreen and the body stays mask because the flood runs on the
    # 0.18 mm class grid and the rim is wide enough (>=10 px) to stop it.
    rec.choose("#artlist select.wandm", "bare", after_ms=250)
    rec.click("#artlist .wandb", after_ms=250)
    cap.wait_state("!!picking")
    a = cap.js("() => ({cx: state.art[0].cx, cy: state.art[0].cy, w: state.art[0].wmm,"
               " h: state.art[0].wmm * state.art[0].ih / state.art[0].iw})")
    vx = a["cx"] + (VISOR_UV[0] - 0.5) * a["w"]
    vy = a["cy"] + (VISOR_UV[1] - 0.5) * a["h"]
    cap.scroll_into_view("front")
    rec.click_at(*cap.board_to_client(vx, vy, "front"), after_ms=200)
    rec.wait_shown("state.art[0].overrides.length === 1 && state.art[0].overrides[0].material === 'bare'", 15_000)
    rec.mark("visor picked as bare board")
    rec.hold(700)
    if stop_after <= 2:
        return finish(rec, problems=cap.js("() => blockingProblems()"))

    # 3. The red LED goes on the back, behind the visor.
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
    rec.hold(700)
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
        rec.orbit_drag(-170, 0, bump=-40)
        rec.mark(f"3D view, orbit {rec.orbit()}")
        rec.hold(800)
        # 6. Peel the layers: mask off, then the board body, so the copper
        # pours, traces, vias and parts stand on their own.
        rec.drag_slider("#d3layers input[data-layer=soldermask]", 0)
        cap.wait_state("document.querySelector('#d3layers input[data-layer=soldermask]').value === '0'")
        rec.mark("soldermask opacity 0")
        rec.hold(1100)
        rec.drag_slider("#d3layers input[data-layer=board]", 0)
        cap.wait_state("document.querySelector('#d3layers input[data-layer=board]').value === '0'")
        rec.mark("board opacity 0")
        rec.hold(1100)
        # 7. Inspect the routing from nearly above (phi -> ~20 deg), squared
        # up (theta -> ~90 deg): pads -> resistor -> LED reads left to right.
        rec.move_to(mv[0] - 40, mv[1] - 60)
        rec.hold(200)
        rec.orbit_drag(-40, 135)
        rec.mark(f"top-down inspection, orbit {rec.orbit()}")
        rec.hold(1500)
        # 8. Then a near side-on profile (phi -> ~72 deg, measured: at 85+
        # the two copper sheets merge into one line and the vias vanish): the
        # vias become pillars between the copper layers, LED and pins standing
        # up. Two wheel notches zoom in (radius 0.06 -> ~0.053) so the 1.5 mm
        # stack reads at hero-image scale while the pins stay in frame (four
        # notches pushed the connector out of the picture).
        rec.orbit_drag(0, -155)
        for _ in range(2):
            page.mouse.wheel(0, -100)
            rec.frame(200)
        rec.mark(f"profile view, orbit {rec.orbit()}")
    else:
        rec.mark(f"3D view FAILED: {err}")
    rec.hold(2500)
    return {"blocking_problems": problems, "model3d_error": err,
            "design": cap.js("() => designJSON()")}


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
    stats = assemble_gif(rec.frames, out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    print("blocking problems at the end:", result["blocking_problems"])
    print("3D error:", result["model3d_error"])
    print("gif:", out, stats)
    print("contact sheet:", sheet)


if __name__ == "__main__":
    main()
