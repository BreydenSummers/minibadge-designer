"""Record the "?" tip clip for `basics` (top bar): getting around the finished helmet.

    .venv/bin/python scripts/help_clips/basics.py [--workdir DIR] [--out minibadge_designer/static/help/rotate.webp]

Starts from scripts/help_fixtures/stage5-text.json (the finished helmet: bare
visor, red LED on the back, "half" on the back at 2 mm) in Both view and shows
the canvas gestures every later tip assumes, on the real app, driven the way a
person drives it. The camera frames the BACK board at 1:1 for the canvas
beats, so the corner squares, the round knob and the snap guide read at the
popover's size; it pulls back to the whole window only to cross to the front.

  1. click "half" on the BACK canvas: it selects, corner squares and knob shown;
  2. a SNAP drag: up and off the board's vertical centre line, then back across
     it, so the pink centre-line guide the tip text names is on screen while
     the text is held on it (the guide lives and dies with the drag);
  3. an Alt-drag back to the story's spot (10.16, 14.9), off SNAP's grid;
  4. double-click "half": +90 deg, held; then three more double-clicks walk
     it round through 180 and 270 back to 0;
  5. Shift+Up nudges 1 mm, Shift+Down brings it home;
  6. cross to the FRONT canvas, rest on the LED's dashed ghost and click it: a
     back part is grabbable from the front, and its handles appear there.

The round knob is on screen from the first click on (SNAP turns it in 15 deg
steps), but it is not turned in this clip: bringing the text back from 90 with
the knob does not work in the app today. handleAt's rotate pickup
(index.html, `let rot0 = (a ? a.rot : L ? L.rot : el ? el.rot : 0)`) never
reads a text's own rot, so grabbing the knob of a text at 90 deg restarts the
angle from 0 and the text jumps (measured: a -90 deg sweep from rot 90 landed
on 270, via 345/330/.../285). That is a dogfood finding, reported with the
clip; the double-clicks do the return instead, and the clip stays inside 11 s.

Ends held with everything where it started and blockingProblems() empty.

Measured while planning this clip (probe over the fixture):
- outlineBounds() is [0.16, 0.02, 20.16, 20.3], so the board's vertical centre
  line is x = 10.16: "half" at (10.16, 14.9) already sits on it. A SNAP drag
  that stays within 7 px (0.35 mm) of it keeps the pink guide; the hop to
  x = 8.9 lets go of it, the sweep back re-catches it.
- textWarnings() is empty for "half" at rot 0/15/30/90/180/270 and for every
  centre y >= 13.0 near x = 10.16, so nothing here raises a toast.
- In Both view both canvases fit the 1320x1150 viewport (front y 128-574,
  back y 664-1110), so nothing scrolls; the shared drag_mm() would
  scrollIntoView() the back canvas and jump the frame, hence the local drag.
- The text's grab box at rot 90/180/270 still contains the anchor (10.16,
  14.9), so a hand resting there double-clicks the text at every angle.
- Playwright's mouse.down() sends clickCount 1; Chromium only fires dblclick
  for a press with clickCount 2, so the double-click is press, then
  down/up with click_count=2.
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
SNAP_OFF = (8.9, 13.5)        # up and 1.26 mm left: the centre guide lets go
SNAP_ON = (10.16, 13.5)       # back across the centre line: the guide re-catches
LED_AT = (10.3625, 10.0)      # back, behind the visor; ghosted on the front
BOARD_CENTRE = (10.16, 10.16) # outlineBounds() midpoint: where the camera looks


def canvas_rect(cap, side: str) -> dict:
    return cap.js("(s) => { const r = (s === 'back' ? cvB : cvF).getBoundingClientRect();"
                  " return {x: r.x, y: r.y, w: r.width, h: r.height}; }", side)


def frame_board(rec: Recorder, side: str, ms: int) -> None:
    """Camera on one canvas's board at 1:1 (the crop grows to the output size)."""
    cx, cy = rec.cap.board_to_client(*BOARD_CENTRE, side)
    r = canvas_rect(rec.cap, side)
    rec.focus_centre(cx, cy, r["w"], r["h"], ms=ms)


def drag_path(rec: Recorder, points, side: str, alt: bool, seg_ms: int,
              hold_pressed_ms: int = 0):
    """Press at points[0] and drag the cursor through the rest, eased per
    segment, without rec.drag_mm's scrollIntoView (both canvases are already
    in view; a scroll would jump the frame). Returns the snap guides seen
    while the pointer rested pressed at the end."""
    cap, page = rec.cap, rec.page
    x0, y0 = cap.board_to_client(*points[0], side)
    if math.hypot(x0 - rec.mx, y0 - rec.my) > 2:  # move_to costs 450 ms even for 0 px
        rec.move_to(x0, y0)
    rec.hold(180)
    if alt:
        page.keyboard.down("Alt")
    page.mouse.down()
    rec.down_ms = rec.clock_ms
    rec.frame(90)
    steps = max(6, round(seg_ms / STEP_MS))
    for to in points[1:]:
        sx, sy = rec.mx, rec.my
        for i in range(1, steps + 1):
            u = (1 - math.cos(math.pi * i / steps)) / 2
            x1, y1 = cap.board_to_client(*to, side)   # live canvas rect each step
            rec.mx, rec.my = sx + (x1 - sx) * u, sy + (y1 - sy) * u
            page.mouse.move(rec.mx, rec.my)
            rec.frame(STEP_MS)
    guides = cap.js("() => JSON.parse(JSON.stringify(snapGuides))")
    if hold_pressed_ms:
        rec.hold(hold_pressed_ms)
    page.mouse.up()
    if alt:
        page.keyboard.up("Alt")
    return guides


def double_click(rec: Recorder) -> None:
    """Two presses where the hand is; the second carries clickCount 2, which
    is what makes Chromium fire dblclick on the canvas."""
    page = rec.page
    page.mouse.down()
    rec.down_ms = rec.clock_ms
    rec.frame(70)
    page.mouse.up()
    rec.frame(70)
    page.mouse.down(click_count=2)
    rec.down_ms = rec.clock_ms
    rec.frame(70)
    page.mouse.up(click_count=2)
    rec.frame(80)


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
    cap.wait_state("snapOn === true")
    cap.js("() => draw()")
    # The fixture's "bridges were added" info toast (6 s) sits over the back
    # canvas; let it expire before the first frame, as leds.py does.
    cap.wait_state("![...document.querySelectorAll('#toasts .toast')]"
                   ".some(t => /Bridges were added/.test(t.textContent))", timeout=9_000)
    scroll = cap.js("() => ({y: window.scrollY, doc: document.documentElement.scrollHeight,"
                    " win: window.innerHeight})")
    start = text_state(cap)
    rec.mark("stage 5: the finished helmet, Both view")
    rec.hold(200)

    # 1. Camera onto the BACK board at 1:1 while the hand goes to "half";
    # click it: it selects, corner squares and the round knob appear.
    frame_board(rec, "back", ms=550)
    rec.move_to(*cap.board_to_client(*TEXT_AT, "back"), ms=550)
    rec.hold(200)
    rec.press()
    cap.wait_state("selected && selected.kind === 'text'")
    rec.mark("click 'half' on the back: selected, handles shown")
    rec.hold(300)

    # 2. A SNAP drag (no Alt): up and off the centre line, then back across it.
    # The text is held on the line for a beat so the pink guide is on screen.
    rec.mark("SNAP drag: off the centre line and back onto it (pink guide)")
    guides = drag_path(rec, [TEXT_AT, SNAP_OFF, SNAP_ON], "back", alt=False,
                       seg_ms=420, hold_pressed_ms=380)
    snapped = text_state(cap)
    rec.hold(220)

    # 3. Alt-drag it home: the story's spot is off SNAP's grid. The cursor is
    # still at SNAP_ON; the anchor sits wherever the snap put it, so the
    # cursor's destination is offset by the same amount.
    rec.mark("Alt-drag back to (10.16, 14.9)")
    back_to = (SNAP_ON[0] + TEXT_AT[0] - snapped["x"], SNAP_ON[1] + TEXT_AT[1] - snapped["y"])
    drag_path(rec, [SNAP_ON, back_to], "back", alt=True, seg_ms=420)
    cap.wait_state(f"Math.abs(state.texts[0].x - {TEXT_AT[0]}) < 0.1"
                   f" && Math.abs(state.texts[0].y - {TEXT_AT[1]}) < 0.1")
    rec.hold(300)

    # 4. Double-click: +90 deg. The hand is already on the text (the Alt-drag
    # released it there); hold so the quarter turn reads, then three more
    # double-clicks walk it round to 0 again.
    rec.hold(150)
    rec.mark("double-click: +90 deg")
    double_click(rec)
    cap.wait_state("(state.texts[0].rot || 0) === 90")
    rot_dbl = text_state(cap)["rot"]
    rec.hold(420)
    rots_seen: list[int] = [rot_dbl]
    rec.mark("three more double-clicks: 180, 270, back to 0")
    for want in (180, 270, 0):
        double_click(rec)
        cap.wait_state(f"(state.texts[0].rot || 0) === {want}")
        rots_seen.append(text_state(cap)["rot"])
        rec.hold(230)
    rec.hold(150)

    # 5. Arrow-key nudge: Shift+Up is 1 mm (a bare arrow is 0.2 mm, two frame
    # pixels at this scale), then Shift+Down brings it home.
    y_before = text_state(cap)["y"]
    rec.mark("Shift+Up nudges 1 mm")
    page.keyboard.press("Shift+ArrowUp")
    cap.wait_state(f"Math.abs(state.texts[0].y - {y_before - 1.0}) < 0.05")
    rec.hold(400)
    rec.mark("Shift+Down brings it back")
    page.keyboard.press("Shift+ArrowDown")
    cap.wait_state(f"Math.abs(state.texts[0].y - {y_before}) < 0.05")
    rec.hold(400)

    # 6. Cross to the FRONT canvas: the camera pulls back to the whole window
    # for the crossing, then closes on the front board. The back LED is a
    # dashed ghost there; rest on it, then click it: it selects, handles
    # around the ghost.
    rec.mark("to the front: rest on the LED's ghost")
    rec.focus_full(ms=650)
    rec.move_to(*cap.board_to_client(*LED_AT, "front"), ms=750)
    rec.hold(250)
    frame_board(rec, "front", ms=450)
    rec.settle_camera()
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
    rec.hold(1200)
    return {"blocking_problems": problems, "toasts_at_end": toasts, "scroll": scroll,
            "start": start, "snap_guides_held": guides, "snapped_to": snapped,
            "rot_after_dblclick": rot_dbl, "rots_by_dblclick": rots_seen,
            "ghost_cursor": ghost_cursor,
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
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
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
    pink = any(g.get("axis") == "x" and g.get("center") for g in result["snap_guides_held"])
    ok = (not result["blocking_problems"]
          and abs(t["x"] - TEXT_AT[0]) < 0.1 and abs(t["y"] - TEXT_AT[1]) < 0.1
          and t["rot"] == 0 and result["rot_after_dblclick"] == 90 and pink)
    if not ok:  # keep the evidence, never ship it over the current asset
        out = workdir / out.name
    frames = thin(rec.frames, args.fps)
    stats = assemble_webp(frames, out, quality=args.quality)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    n, total = webp_duration_ms(out)
    (workdir / "result.json").write_text(json.dumps(
        {"result": result, "stats": stats, "anmf": [n, total], "marks": rec.marks,
         "frames": [(p.name, d) for p, d in rec.frames]}, default=str, indent=1))
    print("result:", json.dumps(result, default=str))
    print("webp:", out, stats, f"ANMF frames {n}, {total} ms")
    print("contact sheet:", sheet)
    if not ok:
        raise SystemExit(f"take rejected: problems={result['blocking_problems']} text={t}"
                         f" rot_after_dblclick={result['rot_after_dblclick']} pink_guide={pink}")


if __name__ == "__main__":
    main()
