"""Record the "?" tip clip for TIPS.materials ("What the helmet is made of").

    .venv/bin/python scripts/help_clips/materials.py [--workdir DIR] [--out minibadge_designer/static/help/materials.webp]

A tour by hovering, on the shared Recorder (scripts/help_recorder.py). Starts
from stage3-wand (helmet; by-colour art with the gold stripes as exposed
copper, the white and the grey goggle frame Ignore; the visor lens bare board
through the wand override), Art panel open with the palette rows and the
override chip in view. The hand rests on the gold row so the copper stripes
light up on the canvas, then on the wand chip so the bare visor lights up.
It then goes to the Board panel and flips Finish from Gold ENIG to Silver
HASL: the stripes and the connector pads turn silver on the canvas. It sets
the finish back to gold and holds, so the design is left exactly as loaded.

Nothing is dragged, uploaded or typed: hovering is the gesture, and the
highlight on the canvas is the outcome. The hover selectors are the ones the
app binds (renderArtList: `.swrow` rows set artHL.type 'palette', `.ovchips
.chip2` sets 'override'); the take waits on artHL before each hold so a frame
never claims a highlight the app has not drawn.

Timing: a tip clip runs 4-10 s and renders at 360 px in the popover, so
660 px frames. `--fps 6` halves the motion reel on disk if the webp needs to
come in under the ~300 KB soft target; frame count, not choreography, changes.
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
GOLD_ROW = f"#artlist .swrow >> nth={GOLD_J}"
OVERRIDE_CHIP = "#artlist .ovchips .chip2"
SILVER = "hasl"            # the Finish option that is not gold


def _in_view(rec: Recorder, selector: str) -> bool:
    b = rec.box(selector)
    vh = rec.page.viewport_size["height"]
    return b["y"] >= 0 and b["y"] + b["height"] <= vh


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.show_panel("art")
    cap.wait_state("state.art.length === 1 && state.art[0].overrides.length === 1"
                   f" && state.art[0].palette[{GOLD_J}].material === 'copper'")
    finish0 = cap.js("() => state.finish")
    if finish0 == SILVER:
        raise AssertionError(f"fixture finish is already {SILVER}; the clip needs to start on gold")
    # Palette rows and the override chip must both be on screen before the
    # first frame so nothing jumps; the front canvas too, for the highlights.
    cap.js("(s) => document.querySelector(s).scrollIntoView({block: 'center'})", OVERRIDE_CHIP)
    cap.scroll_into_view("front")
    for sel in (GOLD_ROW, OVERRIDE_CHIP):
        if not _in_view(rec, sel):
            raise AssertionError(f"{sel} is off screen at the start")
    # Rest the hand beside the layer card, off every control.
    b = rec.box(OVERRIDE_CHIP)
    rec.mx, rec.my = b["x"] + b["width"] + 60, b["y"] + 70
    page.mouse.move(rec.mx, rec.my)
    rec.mark("stage3-wand: Art panel, palette rows + wand chip in view")
    rec.hold(250)

    # 1. Hover the gold row: the copper stripes light up on the canvas.
    rec.move_to(*rec.center(GOLD_ROW, fx=0.12))
    cap.wait_state(f"artHL !== null && artHL.type === 'palette' && artHL.j === {GOLD_J}")
    rec.mark("hover gold row -> copper stripes highlighted")
    rec.hold(900)

    # 2. Hover the wand chip: the bare visor lights up.
    rec.move_to(*rec.center(OVERRIDE_CHIP, fx=0.3))
    cap.wait_state("artHL !== null && artHL.type === 'override' && artHL.j === 0")
    rec.mark("hover wand chip -> bare visor highlighted")
    rec.hold(900)

    # 3. Board panel, Finish -> silver: stripes and pads turn silver.
    rec.click("#tab-shape", after_ms=200)
    cap.wait_state("activePanel === 'shape'")
    if not _in_view(rec, "#finish"):
        cap.js("() => document.querySelector('#finish').scrollIntoView({block: 'center'})")
        rec.frame(STEP_MS)
    rec.mark("Board panel")
    rec.choose("#finish", SILVER, after_ms=1000)
    cap.wait_state(f"state.finish === '{SILVER}'")
    rec.mark(f"Finish -> {SILVER}: metal turns silver")

    # 4. And back to gold, leaving the design as it was loaded.
    rec.choose("#finish", finish0, after_ms=200)
    cap.wait_state(f"state.finish === '{finish0}'")
    rec.mark(f"Finish -> {finish0} again")
    rec.hold(1500)
    rec.mark("end")

    return {"blocking_problems": cap.js("() => blockingProblems()"),
            "finish_start": finish0,
            "finish_end": cap.js("() => state.finish"),
            "finish_select": cap.js("() => $('finish').value"),
            "gold_material": cap.js(f"() => state.art[0].palette[{GOLD_J}].material"),
            "override": cap.js("() => state.art[0].overrides[0]"),
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
    ap.add_argument("--fps", type=int, default=12, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=82)
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
