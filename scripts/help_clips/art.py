"""Record the "art" tip clip: the helmet picture added as by-colour artwork.

    .venv/bin/python scripts/help_clips/art.py [--workdir DIR] [--out minibadge_designer/static/help/art.webp]

TIPS key `art` ("Step 2: paint it with materials"). Starts from the
stage1-shape fixture (helmet board, black mask, bottom pins only) with the Art
panel open. The cursor presses **+ Image**, the helmet PNG lands as artwork in
By color mode and the per-colour rows appear; the cursor hovers the gold row
and then the grey row so their pixels light up on the front canvas, switches
the white row to Ignore (the cheek pads go; the grey goggle frame and
frame vanish; the goggles are mask only, the stripes stay copper), sets the
width to 18 mm, and rests on the painted helmet. 8-11 s, 660 px wide, animated webp.

The camera (see the skill's "The camera"): 1:1 on the + Image button for the
press; the whole window while the picture lands on the front board and for
the two row hovers (the hover paints the row's pixels on the front canvas
and nothing on the row itself, and a 1:1 crop of the rows ends where the
canvas begins, so the hovers must be seen from the whole window); 1:1 on the
four colour rows for the two Ignore changes; the whole window again once the
goggle frame is gone; 1:1 on the Width field for the typing; the whole window
for the closing hold. A result off-camera did not happen for the viewer.

Uses the shared Recorder (scripts/help_recorder.py); nothing is re-implemented.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
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

ART_W = 18.0
FIXTURE = "stage1-shape"
ROWS = "#artlist .swrow"
ROW_PAD = 70         # around the four colour rows: chips, names and selects readable


def _is_white(rgb):
    return min(rgb) >= 240


def _is_gold(rgb):
    r, g, b = rgb
    return r > g > b and r - b >= 60


def _is_grey(rgb):
    # The helmet's rim is a light grey (201, 206, 214), well short of white.
    return max(rgb) - min(rgb) <= 30 and 60 <= sum(rgb) / 3 <= 235


def _focus_rows(rec: Recorder, n_rows: int, ms: int = 600) -> None:
    """Frame the colour rows at 1:1, first to last, padded."""
    rec.focus_on(f"{ROWS} >> nth=0", pad=ROW_PAD, ms=ms,
                 include=[f"{ROWS} >> nth={n_rows - 1}"])


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.show_panel("art")
    cap.wait_state("activePanel === 'art' && state.art.length === 0")
    # The fixture load posts step 1's toast ("bridges were added"); let it
    # expire for real (6 s wall clock, no frames shot) so the clip opens clean.
    cap.wait_state("$('toasts').children.length === 0", timeout=12_000)
    # Park the hand near the panel so the first move is short and readable.
    rec.mx, rec.my = rec.center("#addart", fx=0.5, fy=3.2)
    page.mouse.move(rec.mx, rec.my)
    rec.mark("helmet board, Art panel empty")
    rec.hold(300)

    # 1. + Image, pick the helmet PNG (hidden input; the button press reads as
    # the person having picked the file). The camera closes in on the button
    # over the move, so the label is legible when it is pressed.
    rec.focus_on("#addart", pad=90)
    rec.click("#addart", after_ms=150)
    rec.mark("+ Image pressed")
    page.set_input_files("#artfile", str(HELMET))
    rec.wait_shown("state.art.length === 1 && state.art[0].palette && state.art[0].palette.length > 0", 20_000)
    # Result beat: pull back so the picture is seen landing on the front board.
    rec.focus_full(ms=700)
    rec.settle_camera()
    rec.mark("artwork on the board, colour rows shown")
    rec.hold(500)

    palette = cap.js("() => state.art[0].palette.map(p => ({rgb: p.rgb, material: p.material}))")
    gold = next((j for j, p in enumerate(palette) if _is_gold(p["rgb"])), None)
    grey = next((j for j, p in enumerate(palette) if _is_grey(p["rgb"])), None)
    white = next((j for j, p in enumerate(palette) if _is_white(p["rgb"])), None)
    if white != 1:
        raise AssertionError(f"white row expected at data-j=1, palette is {palette}")
    if gold is None or grey is None:
        raise AssertionError(f"no gold/grey row found in palette {palette}")
    n_rows = len(palette)

    # 2. Hover the gold row, then the grey row: the pixels each controls light
    # up on the front canvas. Short dwells (~0.6 s each), at the whole window
    # so the lit pixels are on screen (the row itself shows nothing).
    rec.move_to(*rec.center(f"{ROWS} >> nth={gold}", fx=0.35), ms=650)
    cap.wait_state(f"!!artHL && artHL.type === 'palette' && artHL.j === {gold}")
    rec.mark("hover: gold row lit")
    rec.hold(300)
    rec.move_to(*rec.center(f"{ROWS} >> nth={grey}", fx=0.35), ms=300)
    cap.wait_state(f"!!artHL && artHL.type === 'palette' && artHL.j === {grey}")
    rec.mark("hover: grey row lit")
    rec.hold(300)

    # 3. White is background, not ink. The camera closes in on the four rows
    # over the move so the chips, names and selects read at 1:1.
    _focus_rows(rec, n_rows)
    rec.choose("#artlist select.pm[data-j='1']", "ignore", after_ms=250)
    cap.wait_state("state.art[0].palette[1].material === 'ignore'")
    rec.mark("white row -> Ignore")
    # Result beat: the cheeks are gone and the stripes are copper on the board;
    # the grey (goggle frame, mouth detail) stays white silkscreen for Step 3.
    # The hand leaves the rows on the way (down to the Width label, the next
    # control), or the grey row's hover keeps its pixels lit magenta through
    # the hold and the clean board is never seen.
    rec.focus_full(ms=700)
    rec.move_to(*rec.center("#artlist .sub:has-text('Width')", fx=0.3), ms=450)
    cap.wait_state("artHL === null")
    rec.settle_camera(250)
    rec.mark("white gone, stripes copper, grey silk")
    rec.hold(600)

    # 4. Width 18 mm, same as the board.
    rec.focus_on("#artlist input.wdn", pad=120)
    rec.set_number("#artlist input.wdn", f"{ART_W:g}")
    cap.wait_state(f"Math.abs(state.art[0].wmm - {ART_W}) < 0.01")
    rec.mark("width 18 mm")
    # Pull back to the whole window and park the hand off the card so the last
    # frames show the painted helmet, not a focused field.
    rec.focus_full(ms=700)
    rec.move_to(*cap.board_to_client(10.16, 4.0, "front"), ms=700)
    # The "Added helmet.png..." status has a 6 s timer; wait (wall clock, no
    # frames) for it to fade so the closing hold is the board alone.
    cap.wait_state("$('toasts').children.length === 0", timeout=12_000)
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    rec.mark("painted helmet")
    rec.hold(1300)
    rec.mark("end")
    return {"blocking_problems": problems, "palette": palette,
            "design": cap.js("() => designJSON()")}


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
    ap.add_argument("--out", default=str(HELP_DIR / "art.webp"))
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="art-clip-"))
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
    (workdir / "design.json").write_text(json.dumps(result["design"]))
    frames = thin(rec.frames, args.fps)
    stats = assemble_webp(frames, out, quality=args.quality)
    stats["frames_in_file"], stats["duration_ms"] = webp_duration_ms(out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    (workdir / "result.json").write_text(json.dumps(
        {"palette": result["palette"], "blocking_problems": result["blocking_problems"],
         "stats": stats, "marks": rec.marks,
         "frames": [(p.name, d) for p, d in rec.frames]}, default=str, indent=1))
    print("palette:", result["palette"])
    print("blocking problems at the end:", result["blocking_problems"])
    print("webp:", out, stats)
    print("contact sheet:", sheet)
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems: {result['blocking_problems']}")


if __name__ == "__main__":
    main()
