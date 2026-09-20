"""Record the "?" tip clip for `leds`: a red LED goes on the BACK, behind the visor.

    .venv/bin/python scripts/help_clips/leds.py [--workdir DIR] [--out minibadge_designer/static/help/leds.webp] [--fps 8] [--quality 70]

Starts from scripts/help_fixtures/stage3-wand.json (helmet, visor already bare
board, one red LED on the front) with two setup moves before frame 0: the LED
is parked on the FRONT at the forehead (11.5, 5.5), so the drag under the
visor is a real ~4.6 mm travel rather than the ~1 mm sidestep the fixture's
(11.5, 10) would give, and the LEDs panel is already open. Then it shows only
step 4 of the helmet story, in two clearly separate beats, as the tip text
puts it ("Set Side to Back, then, on the Back view, drag it under the visor"):
with the camera 1:1 on the LED card, set Side to Back and rest on it; pull
back so the LED is seen arriving on the back board; then, 1:1 on the BACK
board, drag the LED down (Alt held: the spot is off SNAP's grid) to
(10.36, 10.0) under the visor, and drag its D1 label a few millimetres to show
labels move too; one 1:1 look at the FRONT board, where only the via dot and
the dashed ghost show. The app's own reconnect-trace status message is left
to run, then waited out in real time so the closing full-window hold is
clean. One continuous take on the shared Recorder (scripts/help_recorder.py),
660 px wide, encoded as an animated webp of independent full frames; `--fps`
thins the ~12 fps motion reel on disk without changing the choreographed time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from help_recorder import (
    STEP_MS,
    TIP_H,
    TIP_W,
    GifCapture,
    Recorder,
    assemble_webp,
    contact_sheet,
)

FIXTURE = "stage3-wand"
LED_FROM = (11.5, 5.5)        # setup: front, at the forehead (legal on both faces)
LED_TO = (10.3625, 10.0)      # back, behind the visor (stage4-led.json)
LABEL_MOVES = [(0.0, 2.6), (0.0, -2.6), (-2.4, 0.0), (2.4, 0.0),
               (0.0, 3.2), (0.0, -3.2), (-3.0, 0.0), (3.0, 0.0)]
LED_CARD = "#ledlist .item"
SIDE_SELECT = f"{LED_CARD} select.s"
COLOR_SELECT = f"{LED_CARD} select.c"


def d1_label(cap) -> dict:
    """Where the app currently prints D1 (unit 0's LED label), and on which face."""
    return cap.js("() => { const l = refdesLayout().find(l => l.unit === 0 && l.which === 'led');"
                  " return l ? {x: l.at[0], y: l.at[1], face: l.face, hand: !!l.hand} : null; }")


def focus_board(rec: Recorder, side: str, pad: float = 70, ms: int = 700) -> None:
    """Camera 1:1 on one canvas's board: the outline's mm bounds mapped through
    the live canvas rect (the back view is mirrored, so take min/max), grown by
    `pad` px so the LED, its ghost and the label have room around the helmet."""
    rec.cap.scroll_into_view(side)
    ob = rec.cap.js("() => outlineBounds()")
    pts = [rec.cap.board_to_client(x, y, side)
           for x in (ob[0], ob[2]) for y in (ob[1], ob[3])]
    x0, x1 = min(p[0] for p in pts) - pad, max(p[0] for p in pts) + pad
    y0, y1 = min(p[1] for p in pts) - pad, max(p[1] for p in pts) + pad
    rec.focus_centre((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0, ms)


def record(rec: Recorder) -> dict:
    cap = rec.cap
    cap.load_fixture(FIXTURE)
    cap.wait_state("customActive() && boardCarved()")
    # SETUP, before frame 0: the fixture's LED sits at (11.5, 10), already at
    # visor height, so "drag it under the visor" would move it ~1 mm sideways
    # and read as nothing. Park it on the FRONT at the forehead instead (the
    # inline unit is a 10 mm strip, so the crown only has room right of the
    # centre line; (11.5, 5.5) is legal on both faces and the straight path
    # down to LED_TO crosses no illegal spot). The on-camera drag is then a
    # real ~4.6 mm travel down behind the visor. The take refuses a start
    # the app outlines red.
    ok = cap.js("([x, y]) => { const L = state.leds[0]; L.x = x; L.y = y; L.side = 'front';"
                " renderLedList(); draw();"
                " return unitInsideBoard(L) && !padConflict(L) && blockingProblems().length === 0; }",
                list(LED_FROM))
    if not ok:
        raise SystemExit(f"setup: the LED is not legal at {LED_FROM} on the front")
    # The clip opens on the LEDs panel; the rail click is not its subject.
    cap.show_panel("leds")
    # Loading the fixture posts the "bridges were added" status toast (6 s
    # info). Let every toast expire before the first frame, as a person
    # opening a saved design a moment later would see it; nothing is shown
    # while we wait.
    cap.wait_state("document.querySelectorAll('#toasts .toast').length === 0", timeout=9_000)
    rec.mark("stage3 + LEDs panel: visor bare, LED on the FRONT forehead (full view)")
    rec.hold(350)

    # 1. Beat one: Side -> Back, the camera 1:1 on the LED card so the select
    # and its value read at popover size. Then rest on it, so "set Side to
    # Back" and "drag it under the visor" are two things, not one blur.
    led = cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y,"
                 " side: state.leds[0].side, color: state.leds[0].color})")
    rec.focus_on(LED_CARD, pad=40, ms=600)
    if led["color"] != "red":
        rec.choose(COLOR_SELECT, "red", after_ms=400)
        cap.wait_state("state.leds[0].color === 'red'")
        rec.mark("colour: red")
    if led["side"] != "back":
        rec.choose(SIDE_SELECT, "back", after_ms=550)
        cap.wait_state("state.leds[0].side === 'back'")
        rec.mark("Side: Back (held on the card)")

    # Result beat: pull back so the LED is seen arriving on the back board,
    # still at the forehead.
    rec.focus_full(ms=700)
    rec.settle_camera()
    rec.mark("full view: LED now on the back board, at the forehead")
    rec.hold(300)

    # 2. Beat two: on the BACK canvas, 1:1 on the board, drag the LED down
    # under the visor. Same-side items win the hit test, so a back LED is
    # grabbed on the back view; Alt because the canonical spot is off SNAP's
    # grid. The camera eases onto the board while the hand travels to the LED.
    led = cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y})")
    focus_board(rec, "back", pad=70, ms=800)
    rec.mark("drag LED down (back canvas, camera on the board)")
    rec.drag_mm((led["x"], led["y"]), LED_TO, side="back", alt=True, ms=900)
    cap.wait_state(f"Math.abs(state.leds[0].x - {LED_TO[0]}) < 0.3"
                   f" && Math.abs(state.leds[0].y - {LED_TO[1]}) < 0.3")
    rec.mark("LED under the visor")
    rec.hold(400)

    # 3. The D1 label moves too: pick a nearby spot the app will honour
    # (refdesOkAt runs the real layout with the position in place) and drag
    # the ink there on the face it prints on, the camera staying on that board.
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
        if lab["face"] != "back":
            focus_board(rec, lab["face"], pad=70, ms=700)
        rec.mark(f"drag D1 label ({lab['face']} canvas)")
        rec.drag_mm((lab["x"], lab["y"]), label_moved, side=lab["face"], ms=650)
        cap.wait_state("!!state.leds[0].dlabel_at")
        after = d1_label(cap)
        rec.mark(f"D1 label at ({after['x']:.2f}, {after['y']:.2f}), hand={after['hand']}")
    else:
        rec.mark("no legal label spot found; label step skipped")

    # 4. What the front shows of a back LED: 1:1 on the FRONT board, the via
    # dot and the dashed ghost behind the visor (the hand rests off the board).
    cap.wait_state("customActive() && boardCarved()")
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    focus_board(rec, "front", pad=70, ms=700)
    rec.settle_camera()
    rec.mark("front board 1:1: via dot and dashed ghost")
    rec.hold(500)

    # End: the whole window. Landing behind the visor posts the app's own
    # reconnect-trace status message (6 s info); it belongs in the take, but
    # the last frame must be clean, so wait it out in real time (no frames
    # are shot) before the closing pan and hold.
    cap.wait_state("document.querySelectorAll('#toasts .toast').length === 0", timeout=16_000)
    rec.focus_full(ms=600)
    rec.settle_camera()
    rec.mark("end: full view, LED behind the visor, no toast")
    rec.hold(1250)
    return {"blocking_problems": problems, "label_moved": label_moved,
            "label_after": d1_label(cap),
            "toasts_at_end": cap.js("() => document.querySelectorAll('#toasts .toast').length"),
            "led": cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y,"
                          " side: state.leds[0].side, color: state.leds[0].color})")}


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
    ap.add_argument("--out", default=str(REPO / "minibadge_designer" / "static" / "help" / "leds.webp"))
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
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
    print("result:", json.dumps({k: v for k, v in result.items()}, default=str))
    print("webp:", out, stats)
    print("contact sheet:", sheet)
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems: {result['blocking_problems']}")
    led = result["led"]
    if led["side"] != "back" or abs(led["x"] - LED_TO[0]) > 0.3 or abs(led["y"] - LED_TO[1]) > 0.3:
        raise SystemExit(f"LED did not land behind the visor: {led}")


if __name__ == "__main__":
    main()
