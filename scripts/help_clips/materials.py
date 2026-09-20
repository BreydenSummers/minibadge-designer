"""Record the "?" tip clip for TIPS.materials ("What the helmet is made of").

    .venv/bin/python scripts/help_clips/materials.py [--workdir DIR] [--out minibadge_designer/static/help/materials.webp]

A tour by hovering, on the shared Recorder (scripts/help_recorder.py). Starts
from stage3-wand (helmet; by-colour art with the gold stripes as exposed
copper, the white and the grey goggle frame Ignore; the visor lens bare board
through the wand override, the goggle frame removed by another), Art panel
open. The grey row is silkscreen (the mouth detail); the frame is gone by the wand:
its palette reads Ignore / Ignore / Exposed copper / Ignore, so only the gold
row and the wand chip are hovered.

The camera alternates card -> board -> card -> board (see "The camera" in the
skill): it opens 1:1 on the palette rows and the wand chip so the row labels
read at the popover's size; the hand rests on the gold row, then the camera
pulls back to the whole window so the magenta highlight on the copper stripes
is seen on the board; back 1:1 for the wand chip, then full again so the bare
visor lights up on screen. Then 1:1 on the Board characteristics card, Finish
flipped from Gold ENIG to Silver HASL, and full so the stripes and the
connector pads turn silver on the canvas; then Finish back to gold (1:1) and
a full-window end hold, so the design is left exactly as loaded.

Nothing is dragged, uploaded or typed: hovering is the gesture, and the
highlight on the canvas is the outcome. The hover selectors are the ones the
app binds (renderArtList: `.swrow` rows set artHL.type 'palette', `.ovchips
.chip2` sets 'override'); the take waits on artHL before each hold so a frame
never claims a highlight the app has not drawn.

Timing: a tip clip runs 7-10 s and renders at 360 px in the popover, so
660 px frames. `--fps 8 --quality 70` are the camera-clip defaults (~500 KB);
frame count, not choreography, changes with `--fps`.
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
    STEP_MS,
    TIP_H,
    TIP_W,
    GifCapture,
    Recorder,
    assemble_webp,
    contact_sheet,
)

FIXTURE = "stage3-wand"
GOLD_J = 2                 # palette index of the gold stripes (copper) in the fixture
ROWS = "#artlist .swrow"
GOLD_ROW = f"{ROWS} >> nth={GOLD_J}"
OVERRIDE_CHIP = "#artlist .ovchips .chip2:nth-child(2)"   # the visor (bare) chip; the first is the frame
SILVER = "hasl"            # the Finish option that is not gold
RESULT_HOLD_MS = 650       # every highlight / finish result stays on the board this long


def _in_view(rec: Recorder, selector: str) -> bool:
    b = rec.box(selector)
    vh = rec.page.viewport_size["height"]
    return b["y"] >= 0 and b["y"] + b["height"] <= vh


def _show_result(rec: Recorder, hold_ms: int = RESULT_HOLD_MS) -> None:
    """Pull back to the whole window and keep the board on screen: a result
    that happens off-camera did not happen for the viewer."""
    rec.focus_full(ms=500)
    rec.settle_camera()
    rec.hold(hold_ms)


def _press_here(rec: Recorder, settle_ms: int = 180, after_ms: int = 100) -> None:
    """Click where the hand already rests (a settle, the press, a beat)."""
    rec.hold(settle_ms)
    rec.press()
    if after_ms:
        rec.hold(after_ms)


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.show_panel("art")
    cap.wait_state("state.art.length === 1 && state.art[0].overrides.length === 2"
                   f" && state.art[0].palette[{GOLD_J}].material === 'copper'")
    finish0 = cap.js("() => state.finish")
    if finish0 == SILVER:
        raise AssertionError(f"fixture finish is already {SILVER}; the clip needs to start on gold")
    mats = cap.js("() => state.art[0].palette.map(p => p.material)")
    if mats.count("silk") < 1:
        raise AssertionError(f"the grey row (goggle frame, mouth detail) must be silkscreen; palette is {mats}")
    # Palette rows and the override chip must both be on screen before the
    # first frame so nothing jumps; the front canvas too, for the highlights.
    cap.js("(s) => document.querySelector(s).scrollIntoView({block: 'center'})", OVERRIDE_CHIP)
    cap.scroll_into_view("front")
    for sel in (f"{ROWS} >> nth=0", OVERRIDE_CHIP):
        if not _in_view(rec, sel):
            raise AssertionError(f"{sel} is off screen at the start")
    # Loading the fixture posts the bridges status toast (6 s, bottom right,
    # over the back board's pads). It is the shape tip's subject, not this
    # one's: let it expire before the first frame rather than record it.
    cap.wait_state("!document.querySelector('#toasts .toast.info')", timeout=10_000)
    # Rest the hand beside the layer card, off every control, and open the
    # clip already framed 1:1 on the palette rows and the wand chip.
    b = rec.box(OVERRIDE_CHIP)
    rec.mx, rec.my = b["x"] + b["width"] + 60, b["y"] + 70
    page.mouse.move(rec.mx, rec.my)
    rec.start_focused(f"{ROWS} >> nth=0", pad=36, include=[OVERRIDE_CHIP])
    rec.mark("stage3-wand: Art panel, 1:1 on palette rows + wand chip")
    rec.hold(200)

    # 1. Hover the gold row (1:1, the label reads), then the board: the copper
    # stripes light up magenta.
    rec.move_to(*rec.center(GOLD_ROW, fx=0.12), ms=600)
    cap.wait_state(f"artHL !== null && artHL.type === 'palette' && artHL.j === {GOLD_J}")
    rec.mark("hover gold row (Exposed copper)")
    rec.hold(320)
    _show_result(rec)
    rec.mark("full: copper stripes highlighted on the board")

    # 2. Hover the wand chip (1:1 on the way in), then the board: the bare
    # visor lights up.
    rec.focus_on(OVERRIDE_CHIP, pad=36, include=[f"{ROWS} >> nth=0"])
    rec.move_to(*rec.center(OVERRIDE_CHIP, fx=0.3), ms=600)
    cap.wait_state("artHL !== null && artHL.type === 'override' && artHL.j === 1")
    rec.mark("hover wand chip (bare board)")
    rec.hold(320)
    _show_result(rec)
    rec.mark("full: bare visor highlighted on the board")

    # 3. Board characteristics card, Finish -> silver: stripes and pads turn silver.
    rec.move_to(*rec.center("#tab-shape"), ms=600)
    _press_here(rec)
    cap.wait_state("activePanel === 'shape'")
    if not _in_view(rec, "#finish"):
        cap.js("() => document.querySelector('#finish').scrollIntoView({block: 'center'})")
        rec.frame(STEP_MS)
    rec.focus_on("#finish", pad=60)
    rec.mark("Board characteristics, 1:1 on Finish")
    rec.move_to(*rec.center("#finish"), ms=600)
    _press_here(rec)                       # a native <select> popup never renders headless
    page.select_option("#finish", SILVER)
    cap.wait_state(f"state.finish === '{SILVER}'")
    rec.mark(f"Finish -> Silver HASL ({SILVER})")
    rec.hold(320)
    _show_result(rec)
    rec.mark("full: stripes and pads silver")

    # 4. And back to gold, leaving the design as it was loaded. The hand is
    # still on the select, so it presses again where it rests.
    rec.focus_on("#finish", pad=60, ms=300)
    rec.settle_camera()
    _press_here(rec, settle_ms=100, after_ms=0)
    page.select_option("#finish", finish0)
    cap.wait_state(f"state.finish === '{finish0}'")
    rec.mark(f"Finish -> {finish0} again")
    rec.hold(300)                          # the select reads Gold ENIG again, 1:1
    rec.focus_full(ms=500)
    rec.settle_camera()
    rec.hold(750)
    rec.mark("end")

    return {"blocking_problems": cap.js("() => blockingProblems()"),
            "finish_start": finish0,
            "finish_end": cap.js("() => state.finish"),
            "finish_select": cap.js("() => $('finish').value"),
            "palette_materials": mats,
            "gold_material": cap.js(f"() => state.art[0].palette[{GOLD_J}].material"),
            "override": cap.js("() => state.art[0].overrides[1]"),
            "mask": cap.js("() => state.mask")}


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
    ap.add_argument("--out", default=str(HELP_DIR / "materials.webp"))
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="tip-materials-"))
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
    print("state at the end:", result)
    print("webp:", out, stats)
    print("contact sheet:", sheet)
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems: {result['blocking_problems']}")
    if result["finish_end"] != result["finish_start"] or result["finish_select"] != result["finish_start"]:
        raise SystemExit(f"finish did not return to {result['finish_start']}: {result}")
    if result["gold_material"] != "copper" or not result["override"] or result["override"]["material"] != "bare":
        raise SystemExit(f"fixture materials are not the story's: {result}")


if __name__ == "__main__":
    main()
