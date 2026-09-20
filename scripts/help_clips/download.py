"""Record the "?" tip clip for TIPS.download ("Step 6: the finished board").

    .venv/bin/python scripts/help_clips/download.py [--workdir DIR] [--out minibadge_designer/static/help/final.webp]

The last step of the helmet story, on the shared Recorder (scripts/
help_recorder.py): the finished badge (scripts/help_fixtures/stage5-text.json)
sits on both canvases. The cursor presses "⬇ Download" in the top bar; the
camera closes in on the picker at 1:1; the cursor rests on the Gerber row and
ticks it (the project and design file stay ticked), then presses the dialog's
own "Download". Because the fab package is in, the board-house caveat opens
next; the camera moves onto it and the cursor presses "I understand". The
export REALLY runs (kicad-cli plots the Gerbers, a few seconds, shown as one
capped frame of the "Generating…" toast); the browser accepts the bundle zip
through Playwright's download event, and the clip holds on what the app shows
afterwards: the dialogs gone and the "Done — …" toast. That toast lingers 6 s
over the spot where the 3D stage's "building model…" chip appears, so the
take waits for it to expire in real time (no frames) before the next gesture.

Then the 3D view: the cursor presses the 3D tab, the spinner shows as one
capped frame, and once the model is in, one slow drag turns the board over so
its BACK is on screen (the LED D1, its resistor R1 and "half"; measured
2026-09-20: azimuth alone never shows it, the camera has to drop below the
board plane, phi ~125-130 deg). Then the camera closes in on the LAYER OPACITY
panel, the Soldermask slider is dragged to 0, and the camera pulls back to the
model for the last hold. The headless render shows the black mask as grey;
that is the app.

If kicad-cli is missing the export fails and the 3D view reports an error; the
clip then ends on the caveat dialog with the cursor on "I understand" and the
result says so - never on a faked model or a faked toast.

Timing: the reviewer's beat list (tick, confirm, caveat, outcome, back of the
model, slider) runs longer than the 4-10 s of a one-gesture tip; every hold is
at the floor of "long enough to read". 660 px frames; `--fps 8` folds motion
frames into their neighbours; choreography is unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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

FIXTURE = "stage5-text"
TEXT_FONT = "blackops"        # the fixture's text; its ink gate must be awaited
EXPORT_SHOWN_MS = 800         # the kicad-cli Gerber plot, shown as one held frame
SPINNER_SHOWN_MS = 800        # the kicad-cli GLB export, shown as one held frame
GERBER_ROW = "#dlbox label.dlopt:has(#dl-gerbers) b"   # the row's title text
MASK_SLIDER = "#d3layers input[data-layer=soldermask]"
DONE_TOAST = ("[...document.querySelectorAll('#toasts .toast')]"
              ".some(t => /^Done|^Download failed/.test(t.textContent))")
# One drag that lands the camera under the board, azimuth ~180 deg from the
# default: ~0.33 deg per px each way (help_recorder.orbit_drag), from the
# default orbit 20deg 65deg.
ORBIT_DX, ORBIT_DY = -480, -190


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    downloads: list = []
    page.on("download", lambda d: downloads.append(d))

    cap.load_fixture(FIXTURE)
    cap.set_view("both")
    cap.show_panel("text")          # the step the story just finished
    # The three gates before believing the legality check (the fixture's text
    # is judged by the font's ink, which loads asynchronously).
    cap.wait_state("customActive() && boardCarved()")
    cap.wait_state(f"!!FONT_INK['{TEXT_FONT}']")
    page.evaluate(f"() => document.fonts.load('16px bm-{TEXT_FONT}')")
    cap.wait_state(f"[...document.fonts].some(f => f.family === 'bm-{TEXT_FONT}' && f.status === 'loaded')")
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    if problems:
        raise AssertionError(f"the fixture is not buildable: {problems}")
    # Restoring the fixture posts a transient status ("The shape doesn't reach
    # some connector pads. Bridges were added...") that expires ~6 s later; a
    # person on a finished board would not have it on screen. Real wait.
    cap.wait_state("!document.querySelector('#toasts .toast')", timeout=15_000)

    # Start the hand in the top bar so the first move is short and readable.
    ox, oy = rec.center("#openbtn", fx=0.5, fy=2.4)
    rec.mx, rec.my = ox, oy
    page.mouse.move(ox, oy)
    rec.mark("stage 5: the finished helmet")
    rec.hold(250)

    # 1. Download: the picker opens; the camera closes in on it at 1:1.
    rec.click("#download", settle_ms=200, after_ms=120)
    cap.wait_state("$('dlbox').classList.contains('open')")
    initial = cap.js("() => ({kicad: $('dl-kicad').checked, gerbers: $('dl-gerbers').checked,"
                     " design: $('dl-design').checked})")
    # The camera eases onto the card over the hand's next move (the skill's
    # rule: a focus change rides on frames, so follow it with a move).
    rec.focus_on("#dlbox .card", pad=40, ms=600)
    rec.mark("Download dialog open")

    # 2. Rest on the Gerber row, then tick it. The other ticks stay.
    rec.move_to(*rec.center(GERBER_ROW, fx=0.35), ms=650)
    rec.hold(400)
    rec.mark("hover Gerber fab package")
    rec.press()
    cap.wait_state("$('dl-gerbers').checked === true && $('dl-kicad').checked === true")
    ticked = cap.js("() => ({kicad: $('dl-kicad').checked, gerbers: $('dl-gerbers').checked,"
                    " design: $('dl-design').checked, note: $('dlnote').textContent})")
    rec.mark("Gerber fab package ticked")
    rec.hold(450)

    # 3. The dialog's own Download: with the fab package in, the board-house
    # caveat opens first. The camera moves onto it.
    rec.click("#dlgo", settle_ms=200, after_ms=0)
    cap.wait_state("$('fabwarn').classList.contains('open')")
    rec.focus_on("#fabwarn .card", pad=40, ms=500)
    rec.mark("board-house caveat open")

    # 4. "I understand": the export runs for real; the move carries the camera.
    rec.move_to(*rec.center("#fabok"), ms=550)
    rec.hold(250)
    rec.press()
    rec.mark("I understand clicked")
    cap.wait_state("!$('fabwarn').classList.contains('open') && !$('dlbox').classList.contains('open')")
    # The outcome is the status toast at the window's bottom-right; at the
    # popover's size a whole-window frame makes it an unreadable chip, so the
    # camera goes onto it at 1:1 (the back board's chin rides along). These
    # frames are shot in real time while kicad-cli plots, so they show the
    # app's own "Generating…" status first.
    cap.wait_state("!!document.querySelector('#toasts .toast')")
    rec.focus_on("#toasts .toast", pad=90, ms=600)
    rec.settle_camera()
    t0 = time.monotonic()
    cap.wait_state(DONE_TOAST, timeout=120_000)
    waited = int((time.monotonic() - t0) * 1000)
    if waited > 150:
        rec.frame(min(waited, EXPORT_SHOWN_MS))
    outcome = cap.js("() => [...document.querySelectorAll('#toasts .toast')].map(t => t.textContent)")
    rec.mark(f"outcome: {outcome}")
    rec.hold(1400)

    # The "Done" toast lingers 6 s exactly where the 3D stage's "building
    # model…" chip appears (both bottom-right). Let it expire for real, off
    # camera, so the spinner beat is not hidden under it.
    cap.wait_state("!document.querySelector('#toasts .toast')", timeout=15_000)

    # 5. The 3D view: spinner as one capped frame, then the model. The move
    # up to the rail tab carries the camera back to the whole window.
    rec.focus_full(ms=700)
    rec.move_to(*rec.center("#tab-3d"), ms=750)
    rec.hold(200)
    rec.press()
    rec.hold(100)
    cap.wait_state("mode3d === true")
    rec.frame(SPINNER_SHOWN_MS)     # "building model…" while kicad-cli exports
    rec.mark("3D tab: building model")
    cap.wait_state("!m3d.busy && (!!m3d.url || !!m3d.err)", timeout=240_000)
    err = cap.js("() => m3d.err ? String(m3d.err) : null")
    orbit = mask = None
    if not err:
        cap.wait_state("$('d3mv').loaded === true", timeout=60_000)
        rec.hold(250)
        rec.mark("model loaded (front up)")
        # One slow drag: to the left and upward, so the camera swings round
        # and drops under the board. The BACK comes up: D1, R1 and "half".
        mv = rec.center("#d3mv")
        rec.move_to(mv[0] + 200, mv[1] + 40, ms=500)
        rec.hold(150)
        rec.orbit_drag(ORBIT_DX, ORBIT_DY, ms=1300)
        orbit = rec.orbit()
        rec.mark(f"orbit drag, back up, {orbit}")
        rec.hold(1500)

        # 6. Soldermask to 0: the paint comes off the copper. Camera on the
        # LAYER OPACITY panel at 1:1 for the drag, then back to the model.
        rec.focus_on("#d3layers", pad=50, ms=600)
        rec.drag_slider(MASK_SLIDER, 0)
        cap.wait_state(f"document.querySelector('{MASK_SLIDER}').value === '0'")
        mask = cap.js(f"() => document.querySelector('{MASK_SLIDER}').value")
        rec.mark("soldermask opacity 0")
        rec.focus_full(ms=700)
        rec.settle_camera()
    else:
        rec.mark(f"3D view FAILED: {err}")
    rec.hold(1500)
    rec.mark("end")
    got = []
    for d in downloads:
        try:
            p = d.path()
            got.append({"name": d.suggested_filename, "bytes": Path(p).stat().st_size})
        except Exception as exc:  # noqa: BLE001 - report, never hide
            got.append({"name": d.suggested_filename, "error": str(exc)[:160]})
    return {"blocking_problems": problems, "menu_initial": initial, "menu_ticked": ticked,
            "outcome": outcome, "downloads": got, "model3d_error": err, "orbit": orbit,
            "soldermask": mask, "mode3d": cap.js("() => mode3d"),
            "stale": cap.js("() => $('d3stale').style.display")}


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
    ap.add_argument("--out", default=str(HELP_DIR / "final.webp"))
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="tip-download-"))
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
    if not (result["menu_ticked"]["gerbers"] and result["menu_ticked"]["kicad"]):
        raise SystemExit(f"menu state wrong: {result['menu_ticked']}")
    if not any("bytes" in d for d in result["downloads"]):
        raise SystemExit(f"the export did not arrive as a download: {result['downloads']}")
    if not any(str(t).startswith("Done") for t in result["outcome"]):
        raise SystemExit(f"the app did not report the download done: {result['outcome']}")
    if result["model3d_error"]:
        print("WARNING: the 3D view failed; the clip ends without the model:",
              result["model3d_error"], file=sys.stderr)


if __name__ == "__main__":
    main()
