"""Record the "?" tip clip for `clk`: the LED blinks with the host badge's clock.

    .venv/bin/python scripts/help_clips/clk.py [--workdir DIR] [--out minibadge_designer/static/help/clk.webp]

Starts from scripts/help_fixtures/stage4-led.json (helmet, bare visor, the red
LED on the back; pins 9/10/15/16 kept, so pin 9 CLK is available) with the LEDs
panel already open, and shows only the CLK hookup: tick "Blink with the badge
clock (CLK)" on the LED card, so the CLK hookup card appears under it and the
three-pad solder jumper appears on the front canvas (its home beside pin 10
is off the helmet, so the app parks it on the chin and says so in a toast);
switch the hookup to "Direct trace to pin 9" and back, so the jumper leaves and
returns; then drag the jumper a few millimetres up the chin with Alt held (the
spot is off SNAP's centre lines) to a place jumperOkAt() accepts, its traces
following. Ends held with blockingProblems() empty. One continuous take on the
shared Recorder (scripts/help_recorder.py), 660 px wide, encoded as an animated
webp of independent full frames.
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

LENGTH_MS = (7_000, 10_000)   # a tip clip: one control, one gesture, one outcome

FIXTURE = "stage4-led"
# Offsets (mm) tried in order for the drag; the first jumperOkAt() accepts wins.
# On the helmet the jumper lands at ~(9.77, 17.06) and the legal room is up
# the chin, not sideways (measured 2026-09-20).
JUMPER_MOVES = [(0.0, -2.5), (0.0, -2.0), (-2.0, -2.5), (2.0, -2.5),
                (-1.5, -2.0), (1.5, -2.0), (0.0, -1.5), (0.0, -3.0)]
TOAST_GONE = "!document.querySelector('#toasts .toast')"


def jumper(cap) -> dict | None:
    """Where the app draws the CLK jumper now, and on which face."""
    return cap.js("() => { const ci = clkActive() ? clkInfo() : null;"
                  " return ci && ci.jumper ? {x: ci.jumper[0], y: ci.jumper[1],"
                  " rot: ci.jumper[2], side: ci.side, ok: !jumperConflict()} : null; }")


def record(rec: Recorder) -> dict:
    cap = rec.cap
    cap.load_fixture(FIXTURE)
    cap.wait_state("customActive() && boardCarved()")
    # Loading the fixture posts the "bridges were added" status toast (6 s
    # info). Let it expire before the first frame, as a person opening a saved
    # design a moment later would see it; nothing is shown while we wait.
    cap.wait_state(TOAST_GONE, timeout=9_000)
    # The previous step (the LED) ended on this panel; start there.
    cap.show_panel("leds")
    if cap.js("() => !pinOn('9')"):
        raise AssertionError("pin 9 is unticked in the fixture; CLK is disabled")
    rec.mark("stage4: LED on the back, LEDs panel open")
    rec.hold(300)

    # 1. Tick "Blink with the badge clock (CLK)" on the LED card. The jumper's
    # home beside pin 10 is off the helmet, so the app parks it on the chin
    # and posts a 6 s toast (bottom-right, clear of the board) saying so.
    rec.move_to(*rec.center("#ledlist .item input.clk"), ms=650)
    rec.hold(220)
    rec.press()
    cap.wait_state("state.leds[0].clk === true && !!document.querySelector('#clkopts .item')")
    j0 = jumper(cap)
    rec.mark(f"CLK ticked; jumper at ({j0['x']:.2f}, {j0['y']:.2f}) {j0['side']}")
    rec.hold(750)

    # 2. The hookup card: Direct trace to pin 9 (the jumper leaves, a trace
    # runs to pin 9), then back to the solder jumper.
    rec.move_to(*rec.center("#clkopts label[for=clktrace]", fx=0.3), ms=550)
    rec.hold(200)
    rec.press()
    cap.wait_state("state.clk.jumper === false")
    rec.mark("Direct trace to pin 9")
    rec.hold(600)
    rec.move_to(*rec.center("#clkopts label[for=clkjump]", fx=0.3), ms=450)
    rec.hold(200)
    rec.press()
    cap.wait_state("state.clk.jumper === true")
    j1 = jumper(cap)
    rec.mark(f"Solder jumper again; at ({j1['x']:.2f}, {j1['y']:.2f})")
    rec.hold(500)

    # 3. Drag the jumper a few millimetres on its own face. The canvas slides
    # a drag along illegal room rather than entering it, so the drop is legal
    # by construction; the destination is still checked first so the gesture
    # goes where it is meant to. Alt: the spot is off SNAP's centre lines.
    dest = None
    for dx, dy in JUMPER_MOVES:
        tx, ty = j1["x"] + dx, j1["y"] + dy
        if cap.js("([x, y]) => jumperOkAt(x, y)", [tx, ty]):
            dest = (tx, ty)
            break
    if dest is None:
        raise AssertionError("no legal jumper destination within a few mm")
    rec.mark("drag jumper")
    rec.drag_mm((j1["x"], j1["y"]), dest, side=j1["side"], alt=True, ms=900)
    cap.wait_state(f"Math.abs(clkInfo().jumper[0] - {dest[0]}) < 0.3"
                   f" && Math.abs(clkInfo().jumper[1] - {dest[1]}) < 0.3")
    j2 = jumper(cap)
    rec.mark(f"jumper at ({j2['x']:.2f}, {j2['y']:.2f}), legal={j2['ok']}")

    # The relocation toast (6 s from the tick) must not sit in the closing hold.
    rec.wait_shown(TOAST_GONE, timeout_ms=9_000)
    cap.wait_state("customActive() && boardCarved()")
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    rec.mark("end: jumper on the chin, traces following")
    rec.hold(1500)
    return {"blocking_problems": problems, "jumper_after_tick": j0,
            "jumper_dest": dest, "jumper_after": j2,
            "clk": cap.js("() => state.clk"),
            "led": cap.js("() => ({x: state.leds[0].x, y: state.leds[0].y,"
                          " side: state.leds[0].side, clk: !!state.leds[0].clk})")}


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
    ap.add_argument("--out", default=str(REPO / "minibadge_designer" / "static" / "help" / "clk.webp"))
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="clk-clip-"))
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
    stats["frames_in_file"], stats["duration_ms"] = webp_duration_ms(out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    (workdir / "result.json").write_text(json.dumps(
        {"result": result, "stats": stats, "marks": rec.marks,
         "frames": [(p.name, d) for p, d in rec.frames]}, default=str, indent=1))
    print("result:", json.dumps({k: v for k, v in result.items()}, default=str))
    print("webp:", out, stats)
    print("contact sheet:", sheet)
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems at the end: {result['blocking_problems']}")
    if not result["jumper_after"] or not result["jumper_after"]["ok"]:
        raise SystemExit(f"the jumper did not end on a legal spot: {result['jumper_after']}")
    if not LENGTH_MS[0] <= stats["duration_ms"] <= LENGTH_MS[1]:
        raise SystemExit(f"clip runs {stats['duration_ms']} ms; a tip clip is {LENGTH_MS[0]}-{LENGTH_MS[1]} ms")


if __name__ == "__main__":
    main()
