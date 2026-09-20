"""Record the "?" tip clip for TIPS.shape ("Step 1: a helmet-shaped board").

    .venv/bin/python scripts/help_clips/shape.py [--workdir DIR] [--out minibadge_designer/static/help/parts.webp]

One step of the helmet story, on the shared Recorder (scripts/help_recorder.py):
a blank app with the soldermask already black, the Shape panel open. The
cursor presses "+ Silhouette…" and helmet.png becomes the board; the
threshold slider is dragged to 220 (128 punches the vents out); the width is
typed as 18 mm; the clip ends holding on the helmet-shaped board on both
canvases. Same gestures and waits as the shape step of the README take
(scripts/help_build_gif.py), which is the reference.

Camera: the Silhouette click is a card-only 1:1 crop; the threshold drag and
the width entry frame the union of the control and the FRONT board (~0.85x),
so the vents are seen closing while the slider moves (review, round two: a
panel-only crop hid the result); the result holds are the full window.

The LED is moved in SETUP to LED_SPOT (11.5, 10), FRONT: stage1-shape.json's
spot, legal with its via connected at 220 on 16 mm and on 18 mm, so the
outline never relocates it ("Moved LED(s) onto solid board" was the round-two
finding) and the pins tip opens on the same part. Two LED messages are the
app's real answer and cannot be placed away (measured 2026-09-20):

- At threshold 128 no layout or rotation of the 10.3 x 2.8 mm LED unit fits
  the vented helmet anywhere (0.25 mm sweep, inline and stacked, rot 0/90),
  so the unit is boxed red with "LED 1: no solid board under this unit" until
  the slider passes ~163. That is what "128 loses the vents" costs, and the
  tip text says so.
- Every slider step and keystroke nulls the outline for the 250 ms debounce
  plus the round trip; in that window unitInsideBoard() falls back to true
  and allBridges() floods the client-side composition, which reports both
  rails unreachable for EVERY spot at EVERY threshold 129-219, so "LED 1: its
  GND via has no path ... a trace was added" fires and lingers 6 s of wall
  time, though the server outline that follows needs no trace. The take waits
  (wall clock, no frames) for the dock to hold no LED message before each
  result beat, so the result holds show the board the app settles on. That
  toast is a dogfood finding, not a placement problem.

The take fails if the LED moved, if "Moved LED(s)" ever appeared, if any
other LED message appeared, or if an LED message is still up at the end.

Timing: a tip clip runs 4-10 s and renders at 360 px in the popover, so
660 px frames. The Recorder shoots ~12 fps during motion; `--fps 6` halves
that on disk (every other motion frame folded into its neighbour) when the
webp needs to come in under the ~300 KB soft target. Frame count, not
choreography, is what changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from help_recorder import (
    HELMET,
    HELP_DIR,
    STEP_MS,
    TIP_H,
    TIP_W,
    GifCapture,
    Recorder,
    assemble_webp,
    contact_sheet,
)

SHAPE_THRESHOLD = 220         # 128 punches the vents out of the outline
SHAPE_W = 18.0
LED_SPOT = (11.5, 10.0)       # front; stage1-shape.json's spot: legal, via connected, at 220 and at 18 mm


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    import time
    wall: list[tuple[float, int, str]] = []   # (wall ms, clock ms, label) per mark
    _mark = rec.mark
    def mark(label):
        _mark(label); wall.append((time.time() * 1000, rec.clock_ms, label))
    rec.mark = mark

    def wait_led_toasts_clear(timeout_ms: int = 9_000) -> None:
        """Wall-clock wait, no frames: the dock holds no LED message. The
        interim reconnect status lives 6 s; see the docstring."""
        cap.wait_state("![...$('toasts').children].some(el => /\\bLED\\b/i.test(el.textContent))",
                       timeout=timeout_ms)
    # Setup, before the first frame: black mask (the story's), Shape panel.
    cap.show_panel("shape")
    # The LED starts on the FRONT, as in the stage fixtures every later tip
    # opens on, so the part does not jump sides between Step 1 and the pins tip.
    # It also sits at LED_SPOT, stage1-shape.json's spot, legal at 220 and at 18 mm
    # so the outline never relocates it (the docstring has what 128 does to it).
    page.evaluate(f"() => {{ $('mask').value = 'black'; state.mask = 'black';"
                  f" state.leds.forEach(L => {{ L.side = 'front'; L.x = {LED_SPOT[0]}; L.y = {LED_SPOT[1]}; }});"
                  " rebuildAllArt(); renderLegend(); renderLedList(); draw(); }")
    cap.wait_state("state.mask === 'black' && activePanel === 'shape' && state.leds.every(L => L.side === 'front')")
    # Every toast text the take raises, caught by a MutationObserver on the
    # dock (childList + text) so a message that lived between two frames is
    # still on record; main() fails the take on any LED message but the two
    # the docstring allows, and on one still up at the end.
    page.evaluate("""() => {
      window.__toastLog = [];
      const dock = $('toasts');
      const seen = new Set();
      const note = () => { for (const el of dock.children) { const t = el.textContent.trim();
        if (t && !seen.has(t)) { seen.add(t); window.__toastLog.push([Date.now(), t]); } } };
      new MutationObserver(note).observe(dock, {childList: true, subtree: true, characterData: true});
      note();
    }""")
    # Start the hand near the panel so the first move is short and readable.
    rec.mx, rec.my = rec.center("#eladdimg", fx=0.5, fy=2.6)
    page.mouse.move(rec.mx, rec.my)
    rec.mark("blank square board, black mask")
    rec.hold(500)

    # 1. Silhouette: the click is required or the upload is a silent no-op.
    # The camera closes in on the Board shape card for the panel beats; the
    # crop still reaches the front board, so the result lands in frame too.
    rec.focus_on("#eladdimg", pad=90)
    rec.click("#eladdimg", after_ms=150)
    page.set_input_files("#shapefile", str(HELMET))
    cap.wait_state("state.shape.mode === 'custom' && state.shape.elements.length === 1", timeout=20_000)
    rec.wait_outline_shown()
    # Result beat: pull back so both boards are in frame as the helmet lands.
    rec.focus_full(ms=700)
    rec.settle_camera()
    rec.mark("helmet silhouette uploaded")
    rec.hold(600)

    # 2. Threshold 220: dragged along the live track, then landed exactly.
    # The camera frames the slider AND the front board (union of their live
    # boxes, grown to the output aspect), so the vents close on screen.
    rec.focus_on("#shapeopts input.eth", pad=60, include=["#cvF"])
    rec.drag_slider("#shapeopts input.eth", SHAPE_THRESHOLD)
    cap.wait_state(f"state.shape.elements[0].threshold >= {SHAPE_THRESHOLD - 6}"
                   f" && state.shape.elements[0].threshold <= {SHAPE_THRESHOLD + 6}")
    page.evaluate(f"() => {{ const e = state.shape.elements[0]; if (e.threshold !== {SHAPE_THRESHOLD}) {{"
                  f" e.threshold = {SHAPE_THRESHOLD}; const s = document.querySelector('#shapeopts input.eth');"
                  f" s.value = {SHAPE_THRESHOLD}; s.dispatchEvent(new Event('input')); }} }}")
    rec.wait_outline_shown()
    wait_led_toasts_clear()
    # Result beat: the vents close on the board.
    rec.focus_full(ms=700)
    rec.settle_camera()
    rec.mark("threshold 220")
    rec.hold(500)

    # 3. Width 18 mm, typed; the whole Width row and the board in frame again.
    rec.focus_on("#shapeopts input.ewdn", pad=60, include=["#shapeopts input.ewd", "#cvF"])
    rec.set_number("#shapeopts input.ewdn", f"{SHAPE_W:g}")
    cap.wait_state(f"Math.abs(state.shape.elements[0].w - {SHAPE_W}) < 0.01")
    rec.wait_outline_shown()
    rec.mark("board width 18 mm")

    # The gates before believing the board: carved outline, then legality.
    cap.wait_state("customActive() && boardCarved()")
    cap.js("() => draw()")
    wait_led_toasts_clear()
    problems = cap.js("() => blockingProblems()")
    n_el = cap.js("() => state.shape.elements.length")
    # Pull back to the whole window for the result, the hand drifting off the
    # panel onto the board as a person's would.
    rec.focus_full(ms=800)
    rec.move_to(*cap.board_to_client(10.16, 4.0, "front"), ms=800)
    rec.hold(1300)
    rec.mark("end")
    toasts = cap.js("() => window.__toastLog")
    toasts_at_end = cap.js("() => [...$('toasts').children].map(el => el.textContent.trim())")
    led = cap.js("() => state.leds.map(L => ({x: L.x, y: L.y, side: L.side,"
                 " inside: unitInsideBoard(L), pad: padConflict(L)}))")
    return {"blocking_problems": problems, "elements": n_el,
            "threshold": cap.js("() => state.shape.elements[0].threshold"),
            "width": cap.js("() => state.shape.elements[0].w"),
            "mask": cap.js("() => state.mask"),
            "toasts": toasts, "toasts_at_end": toasts_at_end, "leds": led, "wall_marks": wall}


def thin(frames: list[tuple[Path, int]], fps: int) -> list[tuple[Path, int]]:
    """Fold motion frames (STEP_MS holds) into their neighbour to hit `fps`.

    Holds longer than a motion step are kept as they are; only the ~12 fps
    motion reel is decimated, so the choreographed time is unchanged."""
    if fps >= round(1000 / STEP_MS):
        return frames
    keep_every = max(1, round((1000 / STEP_MS) / fps))
    out: list[tuple[Path, int]] = []
    run = 0  # position inside the current run of motion frames
    for p, d in frames:
        if d <= STEP_MS + 1:
            if run % keep_every and out:
                out[-1] = (out[-1][0], out[-1][1] + d)   # fold into the kept one
            else:
                out.append((p, d))
            run += 1
        else:
            run = 0
            out.append((p, d))
    return out


def webp_duration_ms(path: Path) -> tuple[int, int]:
    """(frames, total ms) read from the ANMF chunks. Pillow reports 0 ms per
    frame when it reads an animated webp back, so assemble_webp's duration_ms
    cannot be trusted; the container's own frame headers can."""
    import struct
    b = path.read_bytes()
    i, n, total = 12, 0, 0
    while i + 8 <= len(b):
        tag, size = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        if tag == b"ANMF":
            n += 1
            total += struct.unpack("<I", b[i + 20:i + 23] + b"\0")[0]
        i += 8 + size + (size & 1)
    return n, total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=str(HELP_DIR / "parts.webp"))
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="tip-shape-"))
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("f*.png"):
        old.unlink()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = None
    with GifCapture(workdir=workdir) as cap:
        rec = Recorder(cap, workdir, out_w=TIP_W, out_h=TIP_H)
        try:
            result = record(rec)
        finally:
            print("marks:")
            for ms, label in rec.marks:
                print(f"  {ms / 1000:5.2f}s  {label}")
            print("frames shot:", len(rec.frames), "page events:", rec.events)
    if result is None:
        raise SystemExit("the take did not finish; nothing written")
    frames = thin(rec.frames, args.fps)
    stats = assemble_webp(frames, out, quality=args.quality)
    stats["frames_in_file"], stats["duration_ms"] = webp_duration_ms(out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    (workdir / "result.json").write_text(json.dumps(
        {"result": result, "stats": stats, "marks": rec.marks,
         "frames": [(p.name, d) for p, d in rec.frames]}, default=str, indent=1))
    print("state at the end:", result)
    print("webp:", out, stats)
    print("contact sheet:", sheet)
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems: {result['blocking_problems']}")
    if result["elements"] != 1:
        raise SystemExit(f"expected one shape element, got {result['elements']}")
    # The two LED messages the app cannot help (docstring) are allowed; any
    # other one, and any still up at the end, fails the take.
    allowed = ("LED 1: no solid board under this unit", "LED 1: its GND via has no path")
    led_toasts = [t for _, t in result["toasts"] if "led" in t.lower()]
    stray = [t for t in led_toasts if not t.startswith(allowed)]
    if stray:
        raise SystemExit(f"an unexpected LED toast appeared during the take: {stray}")
    lingering = [t for t in result["toasts_at_end"] if "led" in t.lower()]
    if lingering:
        raise SystemExit(f"an LED toast is still up on the last frame: {lingering}")
    for L in result["leds"]:
        if (L["x"], L["y"], L["side"]) != (*LED_SPOT, "front") or not L["inside"] or L["pad"]:
            raise SystemExit(f"the LED did not stay put and legal at {LED_SPOT} front: {L}")


if __name__ == "__main__":
    main()
