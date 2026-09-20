"""Record the "?" tip clip for TIPS.pins: untick the top row of connector pins.

    .venv/bin/python scripts/help_clips/pins.py [--workdir DIR] [--out minibadge_designer/static/help/pins.webp]
                                                [--fps 8] [--quality 70]

One control, one gesture, one outcome (see .claude/skills/help-recordings):
the helmet board from scripts/help_fixtures/stage1-shape.json, but with all
eight connector pins ticked again, so the crown carries the top row of pads.
The camera opens 1:1 on the Shape panel's Connector pins grid (with the note
under it), so the boxes and their names (VBAT GND 3V3 GND / CLK NC 3V3 GND)
read at the popover's size. The cursor unticks pads 1, 2, 7, 8 one at a time,
slides down to the note under the grid for a moment (no click), then the
camera pulls back to the whole window and holds on both canvases with the top
row of pads gone. The clip ends on that full view.

The fixture already has only the bottom row (9/10/15/16). Setup, before the
first frame, lets the fixture's "bridges were added" toast expire, puts every
pin the grid offers back into state.pins the way the checkbox handler would
(renderPinGrid, relocateStrandedLeds, refreshWarnings, requestOutline, draw)
and waits for the outline, so the take starts from a real app state and shows
only its own step.

Timing: a tip clip runs 4-10 s and renders at 360 px in the popover, so
660 px frames. The Recorder shoots ~12 fps during motion; `--fps 8` (the
default) folds every third motion frame into its neighbour on disk, so the
choreographed time is unchanged and only the frame count drops. A camera clip
lands 450-550 KB at 8 fps / q70; that is the budget, not a target to beat by
trading legibility.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from help_recorder import (
    HELP_DIR,
    STEP_MS,
    TIP_H,
    TIP_W,
    GifCapture,
    Recorder,
    assemble_webp,
    contact_sheet,
)

FIXTURE = "stage1-shape"
TOP_PINS = ("1", "2", "7", "8")        # the row the story unticks
BOTTOM_PINS = ("9", "10", "15", "16")  # 3V3 + GND, kept
TOAST_GONE = "!document.querySelector('#toasts .toast')"

RESTORE_ALL_PINS = """() => {
  const ids = [...document.querySelectorAll('#pingrid input[data-pin]')].map(i => i.dataset.pin);
  state.pins = ALL_PINS.filter(n => ids.includes(n));
  renderPinGrid();
  relocateStrandedLeds();
  refreshWarnings();
  requestOutline(true);
  draw();
  return state.pins;
}"""


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.wait_state("customActive() && boardCarved()")
    # Loading the fixture posts the "bridges were added" status toast (6 s
    # info). Let it expire before the first frame; nothing is shown meanwhile.
    cap.wait_state(TOAST_GONE, timeout=9_000)
    cap.show_panel("shape")
    pins = cap.js(RESTORE_ALL_PINS)
    if len(pins) != 8:
        raise AssertionError(f"grid offers {pins}, expected eight pins")
    cap.wait_state("state.pins.length === 8")
    cap.wait_outline()
    cap.js("() => { renderShapeOpts(); renderPinGrid(); draw(); }")
    # Re-ticking the pins re-outlines the board; if that posts a toast, let
    # it go too, so no toast sits over the board in the result beat.
    cap.wait_state(TOAST_GONE, timeout=9_000)
    page.locator("#pingrid").scroll_into_view_if_needed()
    b = rec.box("#pingrid")
    if not (0 <= b["y"] and b["y"] + b["height"] <= page.viewport_size["height"]):
        raise AssertionError(f"#pingrid off-viewport: {b}")

    # The camera opens already on the pins card (start_focused: focus_on()
    # alone would ease FROM the full view, and frame 0 shot at clock 0 would
    # be the whole window followed by a jump; seen in a webp read-back). The
    # hand rests just under the note, so the first move to pin 1 is short and
    # the first untick lands inside 0.8 s.
    rec.start_focused("#pingrid", pad=70, include=["#pinnote"])
    rec.mx, rec.my = rec.center("#pinnote", fx=0.55, fy=3.2)
    page.mouse.move(rec.mx, rec.my)
    rec.mark("all eight pins ticked; crown carries the top row")
    rec.hold(150)

    for i, n in enumerate(TOP_PINS):
        sel = f"#pingrid input[data-pin='{n}']"
        # The first move is the one the reviewer clocked; keep it short.
        rec.move_to(*rec.center(sel), ms=360 if i == 0 else None)
        rec.hold(180)
        rec.press()
        cap.wait_state(f"!(state.pins || []).includes('{n}')")
        rec.mark(f"pin {n} unticked")
        rec.hold(220)
    rec.wait_outline_shown()
    cap.wait_state("state.pins.length === 4 && ['9','10','15','16'].every(n => state.pins.includes(n))")

    # A short beat on the note under the grid, still at 1:1 so it reads. No
    # click. Each re-outline posted the 6 s "bridges were added" status toast
    # at the window's bottom-right, which is off this crop but sits on the
    # back board's lower pads in the full view; it is waited out here, while
    # the hand rests on the note, and the rest is shown as one short hold (a
    # static frame, so nothing the viewer sees is compressed away). Then pull
    # back so both boards are in frame with the top row gone.
    rec.move_to(*rec.center("#pinnote", fx=0.35), ms=420)
    rec.mark("hover the note under the kept 3V3 + GND row")
    cap.wait_state(TOAST_GONE, timeout=9_000)
    rec.hold(250)
    rec.focus_full(ms=700)
    rec.settle_camera()
    rec.mark("top row gone from both canvases")
    rec.hold(1500)
    rec.mark("end")

    problems = cap.js("() => blockingProblems()")
    toasts = cap.js("() => [...document.querySelectorAll('#toasts .toast')].map(t => t.textContent)")
    return {"blocking_problems": problems, "pins": cap.js("() => state.pins"),
            "pinnote": cap.js("() => $('pinnote').textContent"), "toasts_at_end": toasts}


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
    ap.add_argument("--out", default=str(HELP_DIR / "pins.webp"))
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="pins-tip-"))
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
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems at the end: {result['blocking_problems']}")
    if sorted(result["pins"], key=int) != sorted(BOTTOM_PINS, key=int):
        raise SystemExit(f"pins at the end: {result['pins']}")
    if result["toasts_at_end"]:
        raise SystemExit(f"a toast sits over the result beat: {result['toasts_at_end']}")
    frames = thin(rec.frames, args.fps)
    stats = assemble_webp(frames, out, quality=args.quality)
    stats["frames_in_file"], stats["duration_ms"] = webp_duration_ms(out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    (workdir / "result.json").write_text(json.dumps(
        {"result": result, "stats": stats, "marks": rec.marks,
         "frames": [(p.name, d) for p, d in rec.frames]}, default=str, indent=1))
    print("pins at the end:", result["pins"], "| note:", result["pinnote"],
          "| toasts:", result["toasts_at_end"])
    print("webp:", out, stats)
    print("contact sheet:", sheet)


if __name__ == "__main__":
    main()
