"""Record the "?" tip clip for `placement`: "Move parts freely" lets the
resistor and the via be dragged one by one.

    .venv/bin/python scripts/help_clips/placement.py [--workdir DIR] [--out minibadge_designer/static/help/placement.webp]
                                                    [--no-nudge] [--fps 6] [--quality 82]

Starts from scripts/help_fixtures/stage4-led.json (helmet, visor bare board,
one red LED on the BACK behind the visor at (10.36, 10.0)) and shows one
thing: the LEDs panel comes up (grabbing the LED opens it; see the nudge
below), the LED card's Advanced fold is opened, **Move parts freely** is
ticked, then on the BACK canvas R1 is dragged 1.5 mm sideways and held, then
the via 1 mm down and held. Every destination is probed first with the checks
the card's own warning uses (advConflict, unitInsideBoard, padConflict,
blockingProblems) after the real snap (snapXY) and clamp (clampAdvParts), and
the whole drag path is probed for pour bridges (a bridge posts a 6 s toast),
so the landing is legal, the card never warns and no toast sits over the
ending. Ends held on the back view with blockingProblems() empty. One
continuous take on the shared Recorder (scripts/help_recorder.py), 660 px
wide, an animated webp of independent full frames; motion frames are folded
to 6 fps on disk (measured 2026-09-20: 12 fps = 312 KB, 6 fps = 207 KB for
the same 9.8 s take; the 300 KB soft target rules).

**The nudge (default on; `--no-nudge` records the storyboard literally).**
Measured 2026-09-20 on this fixture: the standard-layout unit polygon spans
x 3.34-13.64 at y 8.6-11.4 and sits on solid board, but the moment free
placement is ticked the unit is judged part by part (unitPartQuads), and the
via's 1.7 mm square lands at x 3.24-4.94 -- 0.1 mm past the helmet's solid
edge. unitInsideBoard() turns false with nothing moved, and the card's
reposition() jumps the LED to freeSpot() = (12.25, 3.5), the top of the dome,
out from behind the visor. That is an app finding (reported with the clip,
not worked around in app code). So that the clip teaches the checkbox and not
the jump, the take first drags the LED 0.64 mm right to (11.0, 10.0) with Alt
held (still behind the visor; the free-placement envelope is then 0.5 mm
inside the edge), which is a legitimate gesture the app honours; grabbing the
LED is also what opens its panel, so the take needs no tab click. The literal
take (`--no-nudge`: tab click, then the tick) exists so the jump itself can be
looked at.
"""

from __future__ import annotations

import argparse
import json
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

FIXTURE = "stage4-led"
LED_NUDGE_TO = (11.0, 10.0)   # 0.64 mm right of the story spot; see the docstring
# Sideways first (the unit lies along x), then a little off-axis, in mm.
RES_MOVES = [(1.5, 0.0), (2.0, 0.0), (-1.5, 0.0), (-2.0, 0.0), (1.75, 0.0),
             (1.5, -0.5), (1.5, 0.5), (2.0, -0.5), (2.0, 0.5), (-1.5, -0.5), (-1.5, 0.5)]
VIA_MOVES = [(1.5, 0.0), (1.0, 0.0), (-1.0, 0.0), (-1.5, 0.0), (1.0, -0.5), (1.0, 0.5),
             (0.0, 1.0), (0.0, -1.0), (1.5, -0.5), (1.5, 0.5), (-1.0, -0.5), (-1.0, 0.5)]

# The in-page legality probe for a free-placed part: apply the destination
# exactly the way the drag handler does (snap on the board point, unit-frame
# offset rounded to 0.05, clamp), read back where it landed and what the
# card would say, then restore. Returns {ok, landed, ...} without redrawing.
PROBE_JS = """([kind, tx, ty]) => {
  const L = state.leds[0];
  if (!L || !L.adv) return {ok: false, why: 'no adv'};
  // Everything below runs with the status toast muted and the bridge memo's
  // memory snapshotted: blockingProblems() and the bridge check both call
  // allBridges(), which posts a 6 s "no path to the power plane" toast the
  // first time a unit needs a bridge and remembers having told. A probe must
  // leave nothing on screen and nothing in that memory (a probe that forgot
  // this put the toast over the ending of the first takes, 2026-09-20).
  const save = JSON.stringify(L.adv);
  const told = new Set(_bridgeTold), cache = _bridgeCache, say = setStatus;
  setStatus = () => {};
  try {
    const [px, py] = snapXY(tx, ty, {kind, index: 0}, true);
    const guides = snapGuides.length;
    const [ox, oy] = rotOff(px - L.x, py - L.y, -(L.rot || 0));
    const cl = v => Math.min(20, Math.max(-20, Math.round(v * 20) / 20));
    if (kind === 'ledres') { L.adv.rx = cl(ox); L.adv.ry = cl(oy); }
    else { L.adv.vx = cl(ox); L.adv.vy = cl(oy); }
    const before = JSON.stringify(L.adv);
    clampAdvParts(L);
    const clamped = JSON.stringify(L.adv) !== before;
    const g = geomOf(L);
    const landed = kind === 'ledres' ? unitPoint(L, g.res[0], g.res[1])
                                     : unitPoint(L, g.viaF[0], g.viaF[1]);
    const conflict = advConflict(L), inside = unitInsideBoard(L), pad = padConflict(L);
    const bp = blockingProblems().length;
    const b = allBridges()[0] || {};
    const bridges = Object.keys(b).filter(k => b[k]);
    return {ok: !conflict && inside && !pad && !bp && !clamped, landed, snapped: [px, py],
            conflict, inside, pad, bp, clamped, guides, bridges};
  } finally {
    L.adv = JSON.parse(save); snapGuides = [];
    setStatus = say; _bridgeTold = told; _bridgeCache = cache;
  }
}"""

BRIDGES_JS = """() => { const b = allBridges()[0] || {}; return Object.keys(b).filter(k => b[k]); }"""

PART_JS = """(kind) => { const L = state.leds[0]; const g = geomOf(L);
  return kind === 'ledres' ? unitPoint(L, g.res[0], g.res[1]) : unitPoint(L, g.viaF[0], g.viaF[1]); }"""

STATE_JS = """() => { const L = state.leds[0]; const g = geomOf(L);
  return {x: L.x, y: L.y, side: L.side, adv: L.adv ? {...L.adv} : null,
          res: unitPoint(L, g.res[0], g.res[1]), via: unitPoint(L, g.viaF[0], g.viaF[1]),
          conflict: L.adv ? advConflict(L) : null, inside: unitInsideBoard(L),
          pad: padConflict(L), problems: blockingProblems(),
          warn: !![...document.querySelectorAll('#warns .warn, .warnbar, #warnings *')]
            .some(e => /different nets too close/.test(e.textContent))}; }"""


def _in_view(rec: Recorder, selector: str) -> bool:
    b = rec.box(selector)
    vp = rec.page.viewport_size
    return b["y"] >= 0 and b["y"] + b["height"] <= vp["height"] and b["x"] >= 0


def pick_move(cap, kind: str, moves, path_samples: int = 8) -> tuple | None:
    """First candidate offset (mm) whose landing the card would accept and
    whose whole straight path needs no pour bridge the unit does not already
    have. The drag snaps on every pointer move, so an intermediate snapped spot
    that isolates the via from the rail fires the 6 s "no path to the power
    plane" toast even when the landing is clean (seen 2026-09-20 on the via's
    +1 mm move: the ending was bridge-free and the toast was still up)."""
    x0, y0 = cap.js(PART_JS, kind)
    have = set(cap.js(BRIDGES_JS))
    for dx, dy in moves:
        r = cap.js(PROBE_JS, [kind, x0 + dx, y0 + dy])
        if not (r["ok"] and set(r["bridges"]) <= have):
            continue
        clean = True
        for k in range(1, path_samples):
            u = k / path_samples
            mid = cap.js(PROBE_JS, [kind, x0 + dx * u, y0 + dy * u])
            if not set(mid["bridges"]) <= have or mid["conflict"] or not mid["inside"]:
                clean = False
                break
        if clean:
            return (x0 + dx, y0 + dy), tuple(r["landed"]), (dx, dy)
    return None


def record(rec: Recorder, nudge: bool = True) -> dict:
    cap = rec.cap
    cap.load_fixture(FIXTURE)
    cap.wait_state("customActive() && boardCarved()")
    # Loading a fixture posts the "bridges were added" status toast (6 s
    # info). Let it expire before the first frame; nothing is shown meanwhile.
    cap.wait_state("![...document.querySelectorAll('#toasts .toast')]"
                   ".some(t => /Bridges were added/.test(t.textContent))", timeout=9_000)
    led0 = cap.js(STATE_JS)
    if led0["side"] != "back" or led0["adv"]:
        raise AssertionError(f"fixture is not the story's (back LED, standard layout): {led0}")
    # The back canvas is where the parts get dragged; have it on screen from
    # the first frame so nothing scrolls mid-take, with the panel tabs and the
    # LED card still in view.
    cap.scroll_into_view("back")
    for sel in ("#tab-leds",):
        if not _in_view(rec, sel):
            raise AssertionError(f"{sel} is off screen at the start")
    rec.mark("stage4-led: LED on the back behind the visor, standard layout")
    rec.hold(150)

    # 1. The LEDs panel. With the nudge, grabbing the LED is what opens it
    # (pointerdown runs showPanel for the part's kind); the 0.64 mm Alt-drag
    # to (11.0, 10.0) keeps the free-placement envelope on the board (see the
    # docstring). Without it, the panel tab is clicked.
    if nudge:
        rec.mark("grab the LED on the back canvas, nudge to (11.0, 10.0) with Alt; its panel opens")
        rec.drag_mm((led0["x"], led0["y"]), LED_NUDGE_TO, side="back", alt=True, ms=400)
        cap.wait_state(f"Math.abs(state.leds[0].x - {LED_NUDGE_TO[0]}) < 0.05"
                       f" && Math.abs(state.leds[0].y - {LED_NUDGE_TO[1]}) < 0.05")
        cap.wait_state("activePanel === 'leds'")
        rec.hold(100)
    else:
        rec.move_to(*rec.center("#tab-leds"), ms=550)
        rec.hold(160)
        rec.press()
        rec.hold(160)
        cap.wait_state("activePanel === 'leds'")
    if not _in_view(rec, "#ledlist details.advbox summary"):
        raise AssertionError("the LED card's Advanced fold is off screen")
    rec.mark("LEDs panel")

    # 2. Open the card's Advanced fold (its summary; not the "?" button in it).
    if cap.js("() => document.querySelector('#ledlist details.advbox').open"):
        raise AssertionError("Advanced is already open on this fixture; the clip needs to open it")
    rec.move_to(*rec.center("#ledlist details.advbox summary", fx=0.12), ms=700)
    rec.hold(160)
    rec.press()
    cap.wait_state("document.querySelector('#ledlist details.advbox').open === true")
    rec.mark("Advanced fold open")
    rec.hold(150)

    # 4. Tick "Move parts freely".
    pre_tick = cap.js(STATE_JS)
    rec.move_to(*rec.center("#ledlist input.adv"), ms=450)
    rec.hold(160)
    rec.press()
    cap.wait_state("!!state.leds[0].adv && document.querySelector('#ledlist input.adv').checked")
    post_tick = cap.js(STATE_JS)
    jumped = abs(post_tick["x"] - pre_tick["x"]) > 0.05 or abs(post_tick["y"] - pre_tick["y"]) > 0.05
    rec.mark(f"Move parts freely ticked; LED {'JUMPED to' if jumped else 'stayed at'}"
             f" ({post_tick['x']:.2f}, {post_tick['y']:.2f})")
    rec.hold(300)

    # 5. Drag R1 sideways on the BACK canvas to a spot the card accepts.
    res_move = pick_move(cap, "ledres", RES_MOVES)
    if res_move:
        target, landed, (dx, dy) = res_move
        frm = tuple(cap.js(PART_JS, "ledres"))
        rec.mark(f"drag R1 by ({dx:+.2f}, {dy:+.2f}) mm -> lands ({landed[0]:.2f}, {landed[1]:.2f})")
        rec.drag_mm(frm, target, side="back", ms=500)
        cap.wait_state(f"Math.abs(unitPoint(state.leds[0], geomOf(state.leds[0]).res[0], geomOf(state.leds[0]).res[1])[0] - {landed[0]}) < 0.06"
                       f" && Math.abs(unitPoint(state.leds[0], geomOf(state.leds[0]).res[0], geomOf(state.leds[0]).res[1])[1] - {landed[1]}) < 0.06")
        rec.hold(250)
    else:
        rec.mark("no legal spot for R1 within the probes; R1 not moved")

    # 6. Drag the via a little.
    via_move = pick_move(cap, "ledvia", VIA_MOVES)
    if via_move:
        target, landed, (dx, dy) = via_move
        frm = tuple(cap.js(PART_JS, "ledvia"))
        rec.mark(f"drag via by ({dx:+.2f}, {dy:+.2f}) mm -> lands ({landed[0]:.2f}, {landed[1]:.2f})")
        rec.drag_mm(frm, target, side="back", ms=450)
        cap.wait_state(f"Math.abs(unitPoint(state.leds[0], geomOf(state.leds[0]).viaF[0], geomOf(state.leds[0]).viaF[1])[0] - {landed[0]}) < 0.06"
                       f" && Math.abs(unitPoint(state.leds[0], geomOf(state.leds[0]).viaF[0], geomOf(state.leds[0]).viaF[1])[1] - {landed[1]}) < 0.06")
    else:
        rec.mark("no legal spot for the via within the probes; via not moved")

    cap.wait_state("customActive() && boardCarved()")
    cap.js("() => draw()")
    end = cap.js(STATE_JS)
    end["bridges"] = cap.js(BRIDGES_JS)
    end["toasts"] = cap.js("() => [...document.querySelectorAll('#toasts .toast')].map(t => t.textContent.slice(0, 80))")
    rec.mark("end: back view, parts moved, card clean")
    rec.hold(1500)
    return {"blocking_problems": end["problems"], "conflict": end["conflict"],
            "inside": end["inside"], "pad": end["pad"], "warn_shown": end["warn"],
            "led_jumped_on_tick": jumped, "pre_tick": pre_tick, "post_tick": post_tick,
            "res_move": res_move, "via_move": via_move, "end": end, "nudge": nudge}


def thin(frames: list[tuple[Path, int]], fps: int) -> list[tuple[Path, int]]:
    """Fold motion frames (STEP_MS holds) into their neighbour to hit `fps`.
    Holds longer than a motion step are kept; the choreographed time is unchanged."""
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
    ap.add_argument("--out", default=str(HELP_DIR / "placement.webp"))
    ap.add_argument("--no-nudge", action="store_true",
                    help="record the storyboard literally (the tick relocates the LED; see the docstring)")
    ap.add_argument("--fps", type=int, default=6,
                    help="motion frame rate on disk (12 = as shot; 6 is what fits the 300 KB target)")
    ap.add_argument("--quality", type=int, default=82)
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="tip-placement-"))
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("f*.png"):
        old.unlink()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = None
    with GifCapture(workdir=workdir) as cap:
        rec = Recorder(cap, workdir, out_w=TIP_W, out_h=TIP_H)
        try:
            result = record(rec, nudge=not args.no_nudge)
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
    print("state at the end:", json.dumps(result, default=str))
    print("webp:", out, stats)
    print("contact sheet:", sheet)
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems: {result['blocking_problems']}")
    if result["conflict"] or not result["inside"] or result["pad"]:
        raise SystemExit(f"the card would warn at the end: {result['end']}")
    if result["end"].get("toasts"):
        raise SystemExit(f"a toast is up over the ending: {result['end']['toasts']}")
    if not result["res_move"] and not result["via_move"]:
        raise SystemExit("neither R1 nor the via found a legal spot; nothing was demonstrated")
    if not result["nudge"] and not result["led_jumped_on_tick"]:
        print("note: the tick did NOT relocate the LED this time; the nudge may no longer be needed")


if __name__ == "__main__":
    main()
