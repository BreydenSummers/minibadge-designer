"""Record the "?" tip clip for TIPS.download ("Step 6: the finished board").

    .venv/bin/python scripts/help_clips/download.py [--workdir DIR] [--out minibadge_designer/static/help/final.webp]

The last step of the helmet story, on the shared Recorder (scripts/
help_recorder.py): the finished badge (scripts/help_fixtures/stage5-text.json)
sits on both canvases. The cursor presses "⬇ Download" in the top bar and the
picker opens with its three pieces (KiCad project, Gerber fab package, Design
file); the cursor rests on the Gerber row, ticks it (the project stays ticked),
and holds so the viewer can read the menu and its "You get one zip…" note.
Escape closes the picker WITHOUT downloading: the "Download" confirm is never
pressed, so no fab warning opens and nothing is fetched. Then the cursor
presses the 3D tab, the "building model…" spinner shows as one capped frame,
and once the model is in, one slow orbit drag turns it over, the way the README
take does (help_build_gif.finish). Ends holding on the model.

If kicad-cli is missing the 3D view reports an error instead of a model; the
clip then ends on the open picker and the result says so - never on a faked
model.

Timing: a tip clip runs 4-10 s and renders at 360 px in the popover, so 660 px
frames. `--fps 6` folds every other motion frame into its neighbour when the
webp needs to come in under the ~300 KB soft target; choreography is unchanged.
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

FIXTURE = "stage5-text"
TEXT_FONT = "blackops"        # the fixture's text; its ink gate must be awaited
SPINNER_SHOWN_MS = 1000       # the kicad-cli export, shown as one held frame
GERBER_ROW = "#dlbox label.dlopt:has(#dl-gerbers) b"   # the row's title text


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    downloads: list[str] = []
    page.on("download", lambda d: downloads.append(d.suggested_filename))

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
    # some connector pads. Bridges were added...") that expires ~6 s later. It
    # sits at the window's bottom-right, exactly over the 3D stage's "building
    # model..." chip (measured 2026-09-20), so the take waits for it to expire
    # in real time before the first frame: a person on a finished board would
    # not have it on screen. The wait is real; nothing is hidden.
    cap.wait_state("!document.querySelector('#toasts .toast')", timeout=15_000)

    # Start the hand in the top bar so the first move is short and readable.
    ox, oy = rec.center("#openbtn", fx=0.5, fy=2.4)
    rec.mx, rec.my = ox, oy
    page.mouse.move(ox, oy)
    rec.mark("stage 5: the finished helmet")
    rec.hold(300)

    # 1. Download: the picker opens, nothing is fetched yet.
    rec.click("#download", settle_ms=200, after_ms=250)
    cap.wait_state("$('dlbox').classList.contains('open')")
    rec.mark("Download menu open")
    initial = cap.js("() => ({kicad: $('dl-kicad').checked, gerbers: $('dl-gerbers').checked,"
                     " design: $('dl-design').checked})")

    # 2. Rest on the Gerber row, then tick it. The project stays ticked.
    rec.move_to(*rec.center(GERBER_ROW, fx=0.35), ms=700)
    rec.hold(600)
    rec.mark("hover Gerber fab package")
    rec.press()
    cap.wait_state("$('dl-gerbers').checked === true && $('dl-kicad').checked === true")
    rec.mark("Gerber fab package ticked")
    ticked = cap.js("() => ({kicad: $('dl-kicad').checked, gerbers: $('dl-gerbers').checked,"
                    " design: $('dl-design').checked, note: $('dlnote').textContent})")
    rec.hold(1000)

    # 3. Escape closes the picker; the confirm button is never pressed.
    page.keyboard.press("Escape")
    cap.wait_state("!$('dlbox').classList.contains('open') && !$('fabwarn').classList.contains('open')")
    rec.hold(200)
    rec.mark("menu closed (Esc), nothing downloaded")

    # 4. The 3D view: spinner as one capped frame, then the model.
    rec.move_to(*rec.center("#tab-3d"), ms=650)
    rec.hold(200)
    rec.press()
    rec.hold(120)
    cap.wait_state("mode3d === true")
    rec.frame(SPINNER_SHOWN_MS)     # "building model…" while kicad-cli exports
    rec.mark("3D tab: building model")
    cap.wait_state("!m3d.busy && (!!m3d.url || !!m3d.err)", timeout=240_000)
    err = cap.js("() => m3d.err ? String(m3d.err) : null")
    orbit = None
    if not err:
        cap.wait_state("$('d3mv').loaded === true", timeout=60_000)
        rec.hold(250)
        rec.mark("model loaded")
        mv = rec.center("#d3mv")
        rec.move_to(mv[0] + 60, mv[1] + 20, ms=450)
        rec.hold(150)
        rec.orbit_drag(-170, 0, bump=-40, ms=1200)
        orbit = rec.orbit()
        rec.mark(f"orbit drag, {orbit}")
    else:
        rec.mark(f"3D view FAILED: {err}")
    rec.hold(1500)
    rec.mark("end")
    return {"blocking_problems": problems, "menu_initial": initial, "menu_ticked": ticked,
            "downloads": downloads, "model3d_error": err, "orbit": orbit,
            "mode3d": cap.js("() => mode3d"),
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
    ap.add_argument("--fps", type=int, default=12, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=82)
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
    if result["downloads"]:
        raise SystemExit(f"the take downloaded something: {result['downloads']}")
    if not (result["menu_ticked"]["gerbers"] and result["menu_ticked"]["kicad"]):
        raise SystemExit(f"menu state wrong: {result['menu_ticked']}")
    if result["model3d_error"]:
        print("WARNING: the 3D view failed; the clip ends without the model:",
              result["model3d_error"], file=sys.stderr)


if __name__ == "__main__":
    main()
