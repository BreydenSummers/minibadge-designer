"""Record the "?" tip clip for TIPS.pins: untick the top row of connector pins.

    .venv/bin/python scripts/help_clips/pins.py [--workdir DIR] [--out minibadge_designer/static/help/pins.webp]

One control, one gesture, one outcome (see .claude/skills/help-recordings):
the helmet board from scripts/help_fixtures/stage1-shape.json, but with all
eight connector pins ticked again, so the crown carries the top row of pads.
The cursor unticks pads 1, 2, 7, 8 one at a time in the Shape panel's
Connector pins grid; the pads leave both canvases as each box clears. Then
the hand sweeps the remaining bottom row and rests on the note under it (no
click): that row is the 3V3 + GND pair that keeps the badge powered.

The fixture already has only the bottom row (9/10/15/16). Setup, before the
first frame, puts every pin the grid offers back into state.pins the way the
checkbox handler would (renderPinGrid, relocateStrandedLeds, refreshWarnings,
requestOutline, draw) and waits for the outline, so the take starts from a
real app state and shows only its own step.
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
    cap.show_panel("shape")
    pins = cap.js(RESTORE_ALL_PINS)
    if len(pins) != 8:
        raise AssertionError(f"grid offers {pins}, expected eight pins")
    cap.wait_state("state.pins.length === 8")
    cap.wait_outline()
    cap.js("() => { renderShapeOpts(); renderPinGrid(); draw(); }")
    page.locator("#pingrid").scroll_into_view_if_needed()
    b = rec.box("#pingrid")
    if not (0 <= b["y"] and b["y"] + b["height"] <= page.viewport_size["height"]):
        raise AssertionError(f"#pingrid off-viewport: {b}")
    # Hand starts resting near the board, not on the grid.
    fx, fy = cap.board_to_client(10.16, 6.0, "front")
    rec.mx, rec.my = fx, fy
    page.mouse.move(fx, fy)
    rec.mark("all eight pins ticked; crown carries the top row")
    rec.hold(600)

    for n in TOP_PINS:
        rec.click(f"#pingrid input[data-pin='{n}']", settle_ms=180, after_ms=220)
        cap.wait_state(f"!(state.pins || []).includes('{n}')")
        rec.mark(f"pin {n} unticked")
    rec.wait_outline_shown()
    cap.wait_state("state.pins.length === 4 && ['9','10','15','16'].every(n => state.pins.includes(n))")
    rec.mark("top row gone from both canvases")
    rec.hold(500)

    # The row that stays: cross to its left pair, then rest on the note under
    # it. No click. (A three-stop sweep pushed the clip past 300 KB and 9.9 s.)
    rec.move_to(*rec.center("#pingrid label.pinbox:has(input[data-pin='9'])"))
    rec.hold(300)
    rec.move_to(*rec.center("#pinnote", fx=0.35))
    rec.mark("hover the kept 3V3 + GND row")
    rec.hold(1500)

    problems = cap.js("() => blockingProblems()")
    return {"blocking_problems": problems, "pins": cap.js("() => state.pins"),
            "pinnote": cap.js("() => $('pinnote').textContent")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=str(HELP_DIR / "pins.webp"))
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
                print(f"  {ms / 1000:5.1f}s  {label}")
            print("frames shot:", len(rec.frames), "page events:", rec.events)
    if result is None:
        raise SystemExit("the take did not finish; nothing written")
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems at the end: {result['blocking_problems']}")
    if sorted(result["pins"], key=int) != sorted(BOTTOM_PINS, key=int):
        raise SystemExit(f"pins at the end: {result['pins']}")
    stats = assemble_webp(rec.frames, out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    (workdir / "marks.json").write_text(json.dumps(rec.marks))
    print("pins at the end:", result["pins"], "| note:", result["pinnote"])
    print("webp:", out, stats)
    print("contact sheet:", sheet)


if __name__ == "__main__":
    main()
