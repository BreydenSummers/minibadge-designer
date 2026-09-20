"""Record the "?" tip clip for `leds`: a red LED goes on the BACK, behind the visor.

    .venv/bin/python scripts/help_clips/leds.py [--workdir DIR] [--out minibadge_designer/static/help/leds.webp]

Starts from scripts/help_fixtures/stage3-wand.json (helmet, visor already bare
board, one red LED on the front) and shows only step 4 of the helmet story:
open the LEDs panel, set the LED's Side to Back, drag it on the BACK canvas to
(10.36, 10.0) behind the visor with Alt held (the spot is off SNAP's grid),
then drag its D1 label a few millimetres to show labels move too. Ends held on
the back view with the LED behind the visor. One continuous take on the shared
Recorder (scripts/help_recorder.py), 660 px wide, encoded as an animated webp
of independent full frames.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from help_recorder import (
    TIP_H,
    TIP_W,
    GifCapture,
    Recorder,
    assemble_webp,
    contact_sheet,
)

FIXTURE = "stage3-wand"
LED_TO = (10.3625, 10.0)      # back, behind the visor (stage4-led.json)
LABEL_MOVES = [(0.0, 2.6), (0.0, -2.6), (-2.4, 0.0), (2.4, 0.0),
               (0.0, 3.2), (0.0, -3.2), (-3.0, 0.0), (3.0, 0.0)]


def d1_label(cap) -> dict:
    """Where the app currently prints D1 (unit 0's LED label), and on which face."""
    return cap.js("() => { const l = refdesLayout().find(l => l.unit === 0 && l.which === 'led');"
                  " return l ? {x: l.at[0], y: l.at[1], face: l.face, hand: !!l.hand} : null; }")


def record(rec: Recorder) -> dict:
    cap = rec.cap
    cap.load_fixture(FIXTURE)
    cap.wait_state("customActive() && boardCarved()")
    # Loading the fixture posts the "bridges were added" status toast (6 s
    # info). Let it expire before the first frame, as a person opening a saved
    # design a moment later would see it; nothing is shown while we wait.
    cap.wait_state("![...document.querySelectorAll('#toasts .toast')]"
                   ".some(t => /Bridges were added/.test(t.textContent))", timeout=9_000)
    rec.mark("stage3: visor bare, LED still on the front")
    rec.hold(400)

    # 1. The LEDs panel (a shorter reach than click()'s distance-scaled move;
    # the first move in a clip should not eat a second of the budget).
    rec.move_to(*rec.center("#tab-leds"), ms=700)
    rec.hold(200)
    rec.press()
    rec.hold(200)
    cap.wait_state("activePanel === 'leds'")
    rec.mark("LEDs panel")

    # 2. Side -> Back (and red, if the fixture ever changes).
    led = cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y,"
                 " side: state.leds[0].side, color: state.leds[0].color})")
    if led["side"] != "back":
        rec.choose("#ledlist .item select.s", "back", after_ms=400)
        cap.wait_state("state.leds[0].side === 'back'")
        rec.mark("side: Back")
    if led["color"] != "red":
        rec.choose("#ledlist .item select.c", "red", after_ms=400)
        cap.wait_state("state.leds[0].color === 'red'")
        rec.mark("colour: red")

    # 3. Drag it on the BACK canvas behind the visor. Same-side items win the
    # hit test, so a back LED is grabbed on the back view; Alt because the
    # canonical spot is off SNAP's grid.
    led = cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y})")
    rec.mark("drag LED (back canvas)")
    rec.drag_mm((led["x"], led["y"]), LED_TO, side="back", alt=True, ms=900)
    cap.wait_state(f"Math.abs(state.leds[0].x - {LED_TO[0]}) < 0.3"
                   f" && Math.abs(state.leds[0].y - {LED_TO[1]}) < 0.3")
    rec.mark("LED behind the visor")
    rec.hold(600)

    # 4. The D1 label moves too: pick a nearby spot the app will honour
    # (refdesOkAt runs the real layout with the position in place) and drag
    # the ink there on the face it prints on.
    lab = d1_label(cap)
    label_moved = None
    if lab:
        for dx, dy in LABEL_MOVES:
            tx, ty = lab["x"] + dx, lab["y"] + dy
            ok = cap.js("([x, y]) => refdesOkAt(state.leds[0], 'led', x, y)", [tx, ty])
            if ok:
                label_moved = (tx, ty)
                break
    if label_moved:
        rec.mark("drag D1 label")
        rec.drag_mm((lab["x"], lab["y"]), label_moved, side=lab["face"], ms=800)
        cap.wait_state("!!state.leds[0].dlabel_at")
        after = d1_label(cap)
        rec.mark(f"D1 label at ({after['x']:.2f}, {after['y']:.2f}), hand={after['hand']}")
    else:
        rec.mark("no legal label spot found; label step skipped")

    cap.wait_state("customActive() && boardCarved()")
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    rec.mark("end: back view, LED behind the visor")
    rec.hold(1500)
    return {"blocking_problems": problems, "label_moved": label_moved,
            "label_after": d1_label(cap),
            "led": cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y,"
                          " side: state.leds[0].side, color: state.leds[0].color})")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=str(REPO / "minibadge_designer" / "static" / "help" / "leds.webp"))
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="leds-clip-"))
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
    print("result:", json.dumps({k: v for k, v in result.items()}, default=str))
    print("webp:", out, stats)
    print("contact sheet:", sheet)


if __name__ == "__main__":
    main()
