"""Record the "?" tip clip for TIPS.shape ("Step 1: a helmet-shaped board").

    .venv/bin/python scripts/help_clips/shape.py [--workdir DIR] [--out minibadge_designer/static/help/parts.webp]

One step of the helmet story, on the shared Recorder (scripts/help_recorder.py):
a blank app with the soldermask already black, the Shape panel open. The
cursor presses "+ Silhouette…" and helmet.png becomes the board; the
threshold slider is dragged to 220 (128 punches the vents out); the width is
typed as 18 mm; the clip ends holding on the helmet-shaped board on both
canvases. Same gestures and waits as the shape step of the README take
(scripts/help_build_gif.py), which is the reference.

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


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    # Setup, before the first frame: black mask (the story's), Shape panel.
    cap.show_panel("shape")
    # The LED starts on the FRONT, as in the stage fixtures every later tip
    # opens on, so the part does not jump sides between Step 1 and the pins tip.
    page.evaluate("() => { $('mask').value = 'black'; state.mask = 'black';"
                  " state.leds.forEach(L => { L.side = 'front'; });"
                  " rebuildAllArt(); renderLegend(); renderLedList(); draw(); }")
    cap.wait_state("state.mask === 'black' && activePanel === 'shape' && state.leds.every(L => L.side === 'front')")
    # Start the hand near the panel so the first move is short and readable.
    rec.mx, rec.my = rec.center("#eladdimg", fx=0.5, fy=2.6)
    page.mouse.move(rec.mx, rec.my)
    rec.mark("blank square board, black mask")
    rec.hold(600)

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
    rec.hold(700)

    # 2. Threshold 220: dragged along the live track, then landed exactly.
    rec.focus_on("#shapeopts input.eth", pad=120)
    rec.drag_slider("#shapeopts input.eth", SHAPE_THRESHOLD)
    cap.wait_state(f"state.shape.elements[0].threshold >= {SHAPE_THRESHOLD - 6}"
                   f" && state.shape.elements[0].threshold <= {SHAPE_THRESHOLD + 6}")
    page.evaluate(f"() => {{ const e = state.shape.elements[0]; if (e.threshold !== {SHAPE_THRESHOLD}) {{"
                  f" e.threshold = {SHAPE_THRESHOLD}; const s = document.querySelector('#shapeopts input.eth');"
                  f" s.value = {SHAPE_THRESHOLD}; s.dispatchEvent(new Event('input')); }} }}")
    rec.wait_outline_shown()
    # Result beat: the vents close on the board.
    rec.focus_full(ms=700)
    rec.settle_camera()
    rec.mark("threshold 220")
    rec.hold(600)

    # 3. Width 18 mm, typed.
    rec.focus_on("#shapeopts input.ewdn", pad=120)
    rec.set_number("#shapeopts input.ewdn", f"{SHAPE_W:g}")
    cap.wait_state(f"Math.abs(state.shape.elements[0].w - {SHAPE_W}) < 0.01")
    rec.wait_outline_shown()
    rec.mark("board width 18 mm")

    # The gates before believing the board: carved outline, then legality.
    cap.wait_state("customActive() && boardCarved()")
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    n_el = cap.js("() => state.shape.elements.length")
    # Pull back to the whole window for the result, the hand drifting off the
    # panel onto the board as a person's would.
    rec.focus_full(ms=800)
    rec.move_to(*cap.board_to_client(10.16, 4.0, "front"), ms=800)
    rec.hold(1500)
    rec.mark("end")
    return {"blocking_problems": problems, "elements": n_el,
            "threshold": cap.js("() => state.shape.elements[0].threshold"),
            "width": cap.js("() => state.shape.elements[0].w"),
            "mask": cap.js("() => state.mask")}


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


if __name__ == "__main__":
    main()
