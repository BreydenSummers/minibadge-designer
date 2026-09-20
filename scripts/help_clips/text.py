"""Record the "?" tip clip for the Text step (TIPS key `text`).

    .venv/bin/python scripts/help_clips/text.py [--workdir DIR] [--out static/help/text.webp] [--fps 8] [--quality 70]

Starts from scripts/help_fixtures/stage4-led.json (helmet, bare visor, red LED
on the back) with the Text panel open, and shows only this step, driven the way
a person drives it: + Add text, click into the field, type "half" one letter at
a time, pick Black Ops One, Side -> Back, size 2 mm, then Alt-drag the word on
the BACK canvas from its automatic spot on the forehead down to the chin at
(10.16, 14.9). Ends on a hold of the whole window with "half" on the chin.

The camera (see the skill's "The camera"): the clip opens already framed 1:1
on the Text card (`start_focused`), so + Add text, the typed letters, the font
and Side selects and the size field read at popover size; it pulls back to
the whole window when the word first appears on the front board and again
when Side -> Back moves it to the back board; it sits 1:1 on the back board
for the drag so "half" is large; and it ends on the whole window.

Measured while planning this clip (probe over textWarnings on the fixture):
- A fresh text lands at (10.16, 5.76), straight above the LED at (10.36, 10);
  Side -> Back and the size leave it there.
- "half" in Black Ops One at 2 mm overlaps the D1/R1 unit for every centre in
  y = 8..12.5 across the whole board width (the sides there are off the board,
  which adds the "hangs over the board edge" warning), so no drag from the
  forehead to the chin can avoid a brief "overlaps a part" warning toast;
  refreshWarnings() runs inside every draw(), and the drag draws on every
  pointermove. The straight vertical drag at x = 10.16 is the shortest crossing
  and never overhangs; it puts the band in the fastest (middle) part of the
  eased drag, so the toast shows for a few frames and clears on the drop. It
  sits in the toast stack at the bottom-right, away from the word.
- `#canvaszone` is a scroll container 75 px taller than its box, and
  `Capture.scroll_into_view` (inside `Recorder.drag_mm`) scrolls it by that
  much: that was the ~50 px lurch a reviewer saw in the first camera round.
  Both canvases fit the viewport here (cvB ends at y 1110 of 1150), so the
  drag below is a local one that never calls scrollIntoView.
- Loading the fixture posts the 6 s "Bridges were added" status toast in the
  same corner; like the LEDs clip, this one lets it expire before frame 0.
- Legality is judged by three async gates (customActive() && boardCarved(),
  FONT_INK['blackops'], the loaded document.fonts entry); blockingProblems()
  is only believed after all three.
"""

from __future__ import annotations

import argparse
import json
import math
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

FIXTURE = "stage4-led"
TEXT = "half"
TEXT_FONT = "blackops"
TEXT_SIZE = 2.0
TEXT_TO = (10.16, 14.9)       # the story's spot; legal for "half" at 1.3-2.2 mm
CARD = "#textlist .item"


# -- short gestures ------------------------------------------------------------
# Recorder.click() scales its move with distance (up to 1.2 s); on a 1:1 crop of
# one card the hand has nowhere far to go, and a 10 s budget cannot afford a
# second per control. These are the same gestures with the move time given.

def click_ms(rec: Recorder, selector: str, ms: int, settle_ms: int = 180,
             after_ms: int = 200) -> None:
    rec.move_to(*rec.center(selector), ms=ms)
    rec.hold(settle_ms)
    rec.press()
    if after_ms:
        rec.hold(after_ms)


def choose_ms(rec: Recorder, selector: str, value: str, ms: int,
              after_ms: int = 300) -> None:
    """A <select>: the cursor presses it, then the value changes (native popups
    never render headless)."""
    click_ms(rec, selector, ms, after_ms=120)
    rec.page.select_option(selector, value)
    rec.hold(after_ms)


def set_number_ms(rec: Recorder, selector: str, value: str, ms: int) -> None:
    """Click into a number field, select what is there, type, tab out."""
    click_ms(rec, selector, ms, after_ms=120)
    rec.page.locator(selector).first.select_text()
    rec.frame(120)
    rec.type_text(value)
    rec.page.keyboard.press("Tab")
    rec.hold(250)


def drag_local(rec: Recorder, frm, to, side: str, alt: bool, ms: int) -> None:
    """Recorder.drag_mm without its scrollIntoView: that call scrolls
    #canvaszone by 75 px and lurches the whole frame. Both canvases are inside
    the viewport in the "both" view, so nothing needs scrolling; the
    destination is still recomputed from the live canvas rect on every step."""
    x0, y0 = rec.cap.board_to_client(*frm, side)
    rec.move_to(x0, y0, ms=600)
    rec.hold(200)
    if alt:
        rec.page.keyboard.down("Alt")
    rec.page.mouse.down()
    rec.down_ms = rec.clock_ms
    rec.frame(100)
    steps = max(6, round(ms / STEP_MS))
    for i in range(1, steps + 1):
        u = (1 - math.cos(math.pi * i / steps)) / 2
        x1, y1 = rec.cap.board_to_client(*to, side)
        rec.mx, rec.my = x0 + (x1 - x0) * u, y0 + (y1 - y0) * u
        rec.page.mouse.move(rec.mx, rec.my)
        rec.frame(STEP_MS)
    rec.page.mouse.up()
    if alt:
        rec.page.keyboard.up("Alt")
    rec.hold(250)


def focus_board(rec: Recorder, side: str, pad: float = 70, ms: int = 700) -> None:
    """Camera 1:1 on one canvas's board: the outline's mm bounds mapped through
    the live canvas rect (the back view is mirrored, so take min/max), grown by
    `pad` px. No scrollIntoView, for the reason drag_local gives."""
    ob = rec.cap.js("() => outlineBounds()")
    pts = [rec.cap.board_to_client(x, y, side)
           for x in (ob[0], ob[2]) for y in (ob[1], ob[3])]
    x0, x1 = min(p[0] for p in pts) - pad, max(p[0] for p in pts) + pad
    y0, y1 = min(p[1] for p in pts) - pad, max(p[1] for p in pts) + pad
    rec.focus_centre((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0, ms)


SCROLL_JS = ("() => ({win: window.scrollY, zone: document.getElementById('canvaszone')"
             " ? document.getElementById('canvaszone').scrollTop : null})")


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.show_panel("text")          # the tip opens from this panel's "?"
    cap.set_view("both")
    cap.wait_state("customActive() && boardCarved()")
    # Let the fixture's 6 s "Bridges were added" status toast expire before the
    # first frame, as a person opening a saved design a moment later sees it.
    cap.wait_state("![...document.querySelectorAll('#toasts .toast')]"
                   ".some(t => /Bridges were added/.test(t.textContent))", timeout=9_000)
    scroll0 = cap.js(SCROLL_JS)
    # The hand starts on the panel, below where the card will appear; the clip
    # opens already framed 1:1 on the Text panel (the card grows into frame).
    rec.mx, rec.my = rec.center("#addtext", fx=0.5, fy=4.5)
    page.mouse.move(rec.mx, rec.my)
    rec.start_focused("#addtext", pad=60, include=["#textlist"])
    rec.mark("stage 4: helmet, bare visor, LED on the back (camera on the Text panel)")
    rec.hold(250)

    # 1. + Add text, then the word typed a letter at a time, on the card 1:1.
    click_ms(rec, "#addtext", ms=400, after_ms=200)
    cap.wait_state("state.texts.length === 1")
    rec.mark("+ Add text")
    click_ms(rec, f"{CARD} input.tx", ms=380, settle_ms=160, after_ms=100)
    rec.type_text(TEXT)
    cap.wait_state(f"state.texts[0].text === {TEXT!r}")
    rec.mark(f'typed "{TEXT}"')
    rec.hold(150)

    # Result beat: the word has appeared on the FRONT board, at its automatic
    # spot on the forehead; pull back so the viewer sees it arrive.
    rec.focus_full(ms=500)
    rec.settle_camera()
    rec.mark("full view: text on the front board")
    rec.hold(250)

    # 2. Font, then Side -> Back, on the card 1:1 (the camera eases in while
    # the hand comes back to the panel).
    rec.focus_on(CARD, pad=60, ms=550)
    choose_ms(rec, f"{CARD} select.fnt", TEXT_FONT, ms=550, after_ms=250)
    cap.wait_state(f"state.texts[0].font === '{TEXT_FONT}'")
    rec.mark("Black Ops One")
    choose_ms(rec, f"{CARD} select.s", "back", ms=380, after_ms=250)
    cap.wait_state("state.texts[0].side === 'back'")
    rec.mark("Side: Back")

    # Result beat: the word has moved to the BACK board.
    rec.focus_full(ms=500)
    rec.settle_camera()
    rec.mark("full view: text now on the back board")
    rec.hold(250)

    # 3. Size 2 mm, typed, on the card 1:1.
    rec.focus_on(CARD, pad=60, ms=550)
    set_number_ms(rec, f"{CARD} input.szn", f"{TEXT_SIZE:g}", ms=550)
    cap.wait_state(f"Math.abs(state.texts[0].size - {TEXT_SIZE}) < 0.01")
    rec.mark("size 2 mm")

    # 4. On the BACK canvas, 1:1 on the board, Alt-drag the word straight down
    # from the forehead to the chin (same-side items win the hit test, so a
    # back text is grabbed on the back view; Alt because the spot is off
    # SNAP's grid). The camera eases onto the board while the hand travels.
    t = cap.js("() => ({x: state.texts[0].x, y: state.texts[0].y})")
    focus_board(rec, "back", pad=70, ms=700)
    rec.mark(f"drag from ({t['x']:.2f}, {t['y']:.2f}) on the back canvas (camera on the board)")
    drag_local(rec, (t["x"], t["y"]), TEXT_TO, side="back", alt=True, ms=800)
    cap.wait_state(f"Math.abs(state.texts[0].x - {TEXT_TO[0]}) < 0.3"
                   f" && Math.abs(state.texts[0].y - {TEXT_TO[1]}) < 0.3")
    rec.mark("dropped on the chin")

    # The three gates before believing the legality check.
    cap.wait_state("customActive() && boardCarved()")
    cap.wait_state(f"!!FONT_INK['{TEXT_FONT}']")
    page.evaluate(f"() => document.fonts.load('16px bm-{TEXT_FONT}')")
    cap.wait_state(f"[...document.fonts].some(f => f.family === 'bm-{TEXT_FONT}' && f.status === 'loaded')")
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    toasts = cap.js("() => [...document.querySelectorAll('#toasts .toast')]"
                    ".map(e => e.className + ': ' + e.textContent.slice(0, 80))")
    scroll1 = cap.js(SCROLL_JS)
    # End: the whole window, the hand resting where it dropped the word.
    rec.focus_full(ms=500)
    rec.settle_camera()
    rec.hold(1500)
    rec.mark("end")
    return {"blocking_problems": problems, "toasts_at_end": toasts,
            "scroll_before": scroll0, "scroll_after": scroll1,
            "text": cap.js("() => ({...state.texts[0]})")}


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
    ap.add_argument("--out", default=str(HELP_DIR / "text.webp"))
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="tip-text-"))
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
    print("blocking problems at the end:", result["blocking_problems"])
    print("toasts at the end:", result["toasts_at_end"])
    print("scroll before/after:", result["scroll_before"], result["scroll_after"])
    print("text at the end:", result["text"])
    print("webp:", out, stats)
    print("contact sheet:", sheet)
    t = result["text"]
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems: {result['blocking_problems']}")
    if (abs(t["x"] - TEXT_TO[0]) > 0.3 or abs(t["y"] - TEXT_TO[1]) > 0.3
            or abs(t["size"] - TEXT_SIZE) > 0.01 or t["font"] != TEXT_FONT or t["side"] != "back"):
        raise SystemExit(f"text did not end as the story says: {t}")


if __name__ == "__main__":
    main()
