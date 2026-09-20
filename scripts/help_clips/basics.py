"""Record the "?" tip clip for `basics` (top bar): getting around the finished helmet.

    .venv/bin/python scripts/help_clips/basics.py [--workdir DIR] [--out minibadge_designer/static/help/rotate.webp]

Starts from scripts/help_fixtures/stage5-text.json (the finished helmet: bare
visor, red LED on the back, "half" on the back at 2 mm) in Both view and shows
the canvas gestures every later tip assumes, on the real app, driven the way a
person drives it: click "half" on the BACK canvas to select it (corner squares
and the round knob appear), Alt-drag it 1.5 mm up and back to its spot at
(10.16, 14.9), grab the knob and turn it to 30 deg and back in one gesture
(SNAP is on, so the turn steps 15 deg), nudge it up 1 mm with Shift+Up and
back with Shift+Down, then cross to the FRONT canvas, rest on the LED's dashed
ghost and click it: a back part is grabbable from the front, and its handles
appear around the ghost. Ends held with everything where it started.

Measured while planning this clip (probe over the fixture):
- "half" at (10.16, 14.9): grab box y 13.69-15.9, knob at (10.16, 12.90),
  pivot at the anchor (10.16, 14.9). textWarnings() is empty for every centre
  y >= 13.0 and for rot 15/30/345, so the 1.5 mm hop and the 30 deg turn
  never raise a toast; y <= 12.5 overlaps the D1/R1 unit.
- In Both view both canvases fit the 1320x1150 viewport (front y 128-574,
  back y 664-1110), so nothing here scrolls; the shared drag_mm() would
  scrollIntoView() the back canvas and jump the frame, hence the local drag.
- The text knob is drawn at the top of the text's axis-aligned box, so it
  does not swing with the text mid-turn; the angle is read from the pointer,
  so turning and returning inside one press lands on rot 0 exactly.
- Loading the fixture posts a 6 s "Bridges were added" info toast at the
  bottom-right, over the back canvas; it is left to expire before the first
  frame, as in leds.py.
One continuous take on the shared Recorder (scripts/help_recorder.py), 660 px
wide, encoded as an animated webp of independent full frames.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
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
TEXT_AT = (10.16, 14.9)       # the story's spot; legal for "half" at 1.3-2.2 mm
TEXT_UP = (10.16, 13.4)       # 1.5 mm up: still clear of the D1/R1 unit
LED_AT = (10.3625, 10.0)      # back, behind the visor; ghosted on the front
TURN_DEG = 30                 # two SNAP steps; visible on "half" at 2 mm


def drag_board(rec: Recorder, frm, to, side: str, alt: bool, ms: int) -> None:
    """rec.drag_mm without its scrollIntoView (both canvases are already in
    view; a scroll would jump the frame). Destination recomputed each step."""
    cap, page = rec.cap, rec.page
    x0, y0 = cap.board_to_client(*frm, side)
    if math.hypot(x0 - rec.mx, y0 - rec.my) > 2:  # move_to costs 450 ms even for 0 px
        rec.move_to(x0, y0)
    rec.hold(180)
    if alt:
        page.keyboard.down("Alt")
    page.mouse.down()
    rec.down_ms = rec.clock_ms
    rec.frame(90)
    steps = max(6, round(ms / STEP_MS))
    for i in range(1, steps + 1):
        u = (1 - math.cos(math.pi * i / steps)) / 2
        x1, y1 = cap.board_to_client(*to, side)
        rec.mx, rec.my = x0 + (x1 - x0) * u, y0 + (y1 - y0) * u
        page.mouse.move(rec.mx, rec.my)
        rec.frame(STEP_MS)
    page.mouse.up()
    if alt:
        page.keyboard.up("Alt")


def arc(rec: Recorder, pivot, start_deg: float, sweep_deg: float, radius: float,
        side: str, ms: int) -> float:
    """Sweep the held pointer around `pivot` (board mm) from start_deg by
    sweep_deg, eased; returns the angle it ended on. Angles are in board
    coordinates (the mirror of the back view is board_to_client's business)."""
    steps = max(6, round(ms / STEP_MS))
    a = start_deg
    for i in range(1, steps + 1):
        u = (1 - math.cos(math.pi * i / steps)) / 2
        a = start_deg + sweep_deg * u
        mx = pivot[0] + radius * math.cos(math.radians(a))
        my = pivot[1] + radius * math.sin(math.radians(a))
        rec.mx, rec.my = rec.cap.board_to_client(mx, my, side)
        rec.page.mouse.move(rec.mx, rec.my)
        rec.frame(STEP_MS)
    return a


def text_state(cap) -> dict:
    return cap.js("() => ({x: state.texts[0].x, y: state.texts[0].y, rot: state.texts[0].rot || 0})")


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.set_view("both")
    cap.wait_state("customActive() && boardCarved()")
    cap.wait_state("!!FONT_INK['blackops']")
    page.evaluate("() => document.fonts.load('16px bm-blackops')")
    cap.wait_state("[...document.fonts].some(f => f.family === 'bm-blackops' && f.status === 'loaded')")
    cap.js("() => draw()")
    # The fixture's "bridges were added" info toast (6 s) sits over the back
    # canvas; let it expire before the first frame, as leds.py does.
    cap.wait_state("![...document.querySelectorAll('#toasts .toast')]"
                   ".some(t => /Bridges were added/.test(t.textContent))", timeout=9_000)
    scroll = cap.js("() => ({y: window.scrollY, doc: document.documentElement.scrollHeight,"
                    " win: window.innerHeight})")
    rec.mark("stage 5: the finished helmet, Both view")
    rec.hold(250)

    # 1. Click "half" on the BACK canvas: it selects, handles appear.
    rec.move_to(*cap.board_to_client(*TEXT_AT, "back"), ms=550)
    rec.hold(200)
    rec.press()
    cap.wait_state("selected && selected.kind === 'text'")
    rec.mark("click 'half' on the back: selected, handles shown")
    rec.hold(300)

    # 2. Alt-drag it 1.5 mm up, then back to its spot (both off SNAP's grid).
    rec.mark("Alt-drag up 1.5 mm")
    drag_board(rec, TEXT_AT, TEXT_UP, "back", alt=True, ms=400)
    cap.wait_state(f"Math.abs(state.texts[0].y - {TEXT_UP[1]}) < 0.15")
    rec.hold(250)
    rec.mark("Alt-drag back down")
    drag_board(rec, TEXT_UP, TEXT_AT, "back", alt=True, ms=400)
    cap.wait_state(f"Math.abs(state.texts[0].x - {TEXT_AT[0]}) < 0.15"
                   f" && Math.abs(state.texts[0].y - {TEXT_AT[1]}) < 0.15")
    rec.hold(300)

    # 3. The round knob: one press, turn to 30 deg and back. SNAP is on, so
    # the angle steps 15 deg under the hand, exactly as the tip text says.
    si = cap.js("() => JSON.parse(JSON.stringify(selectionInfo()))")
    pivot, knob = si["pivot"], si["knob"]
    radius = math.hypot(knob[0] - pivot[0], knob[1] - pivot[1])
    a0 = math.degrees(math.atan2(knob[1] - pivot[1], knob[0] - pivot[0]))
    rec.move_to(*cap.board_to_client(*knob, "back"), ms=450)
    rec.hold(200)
    page.mouse.down()
    rec.down_ms = rec.clock_ms
    rec.frame(90)
    rec.mark(f"grab the knob, turn {TURN_DEG} deg")
    a1 = arc(rec, pivot, a0, TURN_DEG, radius, "back", ms=450)
    rot_mid = text_state(cap)["rot"]
    rec.hold(250)
    rec.mark("turn back to 0")
    arc(rec, pivot, a1, -TURN_DEG, radius, "back", ms=450)
    page.mouse.up()
    cap.wait_state("(state.texts[0].rot || 0) === 0")
    rec.hold(300)

    # 4. Arrow-key nudge: Shift+Up is 1 mm (a bare arrow is 0.2 mm, two frame
    # pixels at this scale), then Shift+Down brings it home.
    rec.mark("Shift+Up nudges 1 mm")
    page.keyboard.press("Shift+ArrowUp")
    cap.wait_state(f"Math.abs(state.texts[0].y - {TEXT_AT[1] - 1.0}) < 0.05")
    rec.hold(400)
    rec.mark("Shift+Down brings it back")
    page.keyboard.press("Shift+ArrowDown")
    cap.wait_state(f"Math.abs(state.texts[0].y - {TEXT_AT[1]}) < 0.05")
    rec.hold(400)

    # 5. Cross to the FRONT canvas: the back LED is a dashed ghost there.
    # Rest on it, then click it: it selects, handles around the ghost.
    rec.mark("to the front: rest on the LED's ghost")
    rec.move_to(*cap.board_to_client(*LED_AT, "front"), ms=750)
    rec.hold(700)
    ghost_cursor = cap.js("() => cvF.style.cursor")
    rec.mark("click the ghost: it selects from here too")
    rec.press()
    cap.wait_state("selected && selected.kind === 'led'")
    rec.hold(200)

    cap.wait_state("customActive() && boardCarved()")
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    toasts = cap.js("() => [...document.querySelectorAll('#toasts .toast')]"
                    ".map(e => e.className + ': ' + e.textContent.slice(0, 80))")
    rec.mark("end: everything where it started")
    rec.hold(1500)
    return {"blocking_problems": problems, "toasts_at_end": toasts, "scroll": scroll,
            "rot_mid_turn": rot_mid, "ghost_cursor": ghost_cursor,
            "text": text_state(cap),
            "led": cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y,"
                          " side: state.leds[0].side, rot: state.leds[0].rot || 0})"),
            "selected": cap.js("() => selected && selected.kind")}


def thin(frames: list[tuple[Path, int]], fps: int) -> list[tuple[Path, int]]:
    """Fold motion frames (STEP_MS holds) into their neighbour to hit `fps`.
    Holds longer than a motion step are kept; choreographed time is unchanged.
    (Same fallback materials.py carries; size is cut by fps first, per the skill.)"""
    if fps >= round(1000 / STEP_MS):
        return frames
    keep_every = max(1, round((1000 / STEP_MS) / fps))
    out: list[tuple[Path, int]] = []
    run = 0
    for p, d in frames:
        if d <= STEP_MS + 1:
            if run % keep_every and out:
                out[-1] = (out[-1][0], out[-1][1] + d)
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
    ap.add_argument("--out", default=str(HELP_DIR / "rotate.webp"))
    ap.add_argument("--fps", type=int, default=12, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=82)
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="tip-basics-"))
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
    t = result["text"]
    ok = (not result["blocking_problems"]
          and abs(t["x"] - TEXT_AT[0]) < 0.3 and abs(t["y"] - TEXT_AT[1]) < 0.3
          and t["rot"] == 0)
    if not ok:  # keep the evidence, never ship it over the current asset
        out = workdir / out.name
    frames = thin(rec.frames, args.fps)
    stats = assemble_webp(frames, out, quality=args.quality)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    n, total = webp_duration_ms(out)
    print("result:", json.dumps(result, default=str))
    print("webp:", out, stats, f"ANMF frames {n}, {total} ms")
    print("contact sheet:", sheet)
    if not ok:
        raise SystemExit(f"end state is wrong: problems={result['blocking_problems']} text={t}")


if __name__ == "__main__":
    main()
