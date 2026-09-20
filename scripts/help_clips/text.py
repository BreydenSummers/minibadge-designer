"""Record the "?" tip clip for the Text step (TIPS key `text`).

    .venv/bin/python scripts/help_clips/text.py [--workdir DIR] [--out static/help/text.webp]

Starts from scripts/help_fixtures/stage4-led.json (helmet, bare visor, red LED
on the back) with the Text panel open, and shows only this step, driven the way
a person drives it: + Add text, click into the field, type "half" one letter at
a time, pick Black Ops One, Side -> Back, size 2 mm, then Alt-drag the word on
the BACK canvas from its automatic spot on the forehead down to the chin at
(10.16, 14.9). Ends on a hold of the back view with "half" on the chin.

Measured while planning this clip (probe over textWarnings on the fixture):
- A fresh text lands at (10.16, 5.76), straight above the LED at (10.36, 10).
- "half" in Black Ops One at 2 mm overlaps the D1/R1 unit for every centre in
  y = 8..12.5 across the whole board width (the sides there are off the board),
  so no drag from the forehead to the chin can avoid a brief "overlaps a part"
  warning; refreshWarnings() runs inside every draw(), and the drag draws on
  every pointermove. The straight vertical drag is the shortest crossing and
  puts the band in the fastest (middle) part of the eased drag, so the warning
  shows for a few frames and clears on the drop. It sits in the toast stack at
  the bottom-right, away from the chin.
- The fixture carries a standing status toast ("The shape doesn't reach some
  connector pads. Bridges were added...") in that same corner; that is real app
  state for the helmet, so it is left alone.
- Legality is judged by three async gates (customActive() && boardCarved(),
  FONT_INK['blackops'], the loaded document.fonts entry); blockingProblems()
  is only believed after all three.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from help_recorder import (
    HELP_DIR,
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


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.show_panel("text")          # the tip opens from this panel's "?"
    cap.set_view("both")
    rec.mark("stage 4: helmet, bare visor, LED on the back")
    rec.hold(350)

    rec.click("#addtext", settle_ms=180, after_ms=200)
    cap.wait_state("state.texts.length === 1")
    rec.mark("+ Add text")
    rec.click("#textlist .item input.tx", settle_ms=160, after_ms=100)
    rec.type_text(TEXT)
    cap.wait_state(f"state.texts[0].text === {TEXT!r}")
    rec.mark(f'typed "{TEXT}"')
    rec.hold(150)

    rec.choose("#textlist .item select.fnt", TEXT_FONT, after_ms=280)
    cap.wait_state(f"state.texts[0].font === '{TEXT_FONT}'")
    rec.mark("Black Ops One")
    rec.choose("#textlist .item select.s", "back", after_ms=280)
    cap.wait_state("state.texts[0].side === 'back'")
    rec.mark("Side: Back")
    rec.set_number("#textlist .item input.szn", f"{TEXT_SIZE:g}")
    cap.wait_state(f"Math.abs(state.texts[0].size - {TEXT_SIZE}) < 0.01")
    rec.mark("size 2 mm")

    t = cap.js("() => ({x: state.texts[0].x, y: state.texts[0].y})")
    rec.mark(f"drag from ({t['x']:.2f}, {t['y']:.2f}) on the back")
    rec.drag_mm((t["x"], t["y"]), TEXT_TO, side="back", alt=True, ms=850)
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
    rec.hold(1300)
    rec.mark("end")
    return {"blocking_problems": problems, "toasts_at_end": toasts,
            "text": cap.js("() => ({...state.texts[0]})")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=str(HELP_DIR / "text.webp"))
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
                print(f"  {ms / 1000:5.1f}s  {label}")
            print("frames shot:", len(rec.frames), "page events:", rec.events)
    if result is None:
        raise SystemExit("the take did not finish; nothing written")
    stats = assemble_webp(rec.frames, out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    print("blocking problems at the end:", result["blocking_problems"])
    print("toasts at the end:", result["toasts_at_end"])
    print("text at the end:", result["text"])
    print("webp:", out, stats)
    print("contact sheet:", sheet)


if __name__ == "__main__":
    main()
