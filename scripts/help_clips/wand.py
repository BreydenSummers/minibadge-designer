"""Record the "?" tip clip for the magic wand (TIPS key `wand`).

    .venv/bin/python scripts/help_clips/wand.py [--workdir DIR] [--out minibadge_designer/static/help/wand.webp]

Step 3 of the helmet story, on the shared Recorder (scripts/help_recorder.py).
Starts from stage2-art (helmet, by-colour art, no overrides) with the Art
panel scrolled to the Magic wand row. The hand picks Bare board in the wand
material select, presses Pick region, travels to the visor lens on the FRONT
canvas and clicks it; the visor turns bare (tan) while the grey rim keeps its
silkscreen and the body stays black mask. Then it hovers the new override
chip, which highlights the picked region on the canvas, and holds.

The wand step is copied from scripts/help_build_gif.py: the visor is addressed
in art image space (VISOR_UV, the u/v the stage3-wand fixture records) and
converted through the live art placement and cap.board_to_client, so a change
in art width or centre cannot move the click off the lens.
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
    TIP_H,
    TIP_W,
    GifCapture,
    Recorder,
    assemble_webp,
    contact_sheet,
)

FIXTURE = "stage2-art"
VISOR_UV = (0.4988, 0.4891)   # the visor lens, in art image space (stage3-wand.json)
WAND_MATERIAL = "bare"


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.show_panel("art")
    cap.wait_state("state.art.length === 1 && state.art[0].overrides.length === 0")
    # A person has the wand row in view before reaching for it; the panel
    # scroll happens before the first frame so nothing jumps on screen.
    cap.js("() => document.querySelector('#artlist .wandb').scrollIntoView({block: 'center'})")
    cap.scroll_into_view("front")
    # Rest the hand near the panel, off every control.
    b = rec.box("#artlist .wandb")
    rec.mx, rec.my = b["x"] + b["width"] + 40, b["y"] - 60
    page.mouse.move(rec.mx, rec.my)
    rec.mark("stage2-art: helmet with by-colour art, wand row in view")
    rec.hold(300)

    # 1. Material: Bare board.
    rec.choose("#artlist select.wandm", WAND_MATERIAL, after_ms=200)
    cap.wait_state(f"document.querySelector('#artlist select.wandm').value === '{WAND_MATERIAL}'")
    rec.mark("wand material: Bare board")

    # 2. Pick region.
    rec.click("#artlist .wandb", after_ms=200)
    cap.wait_state("!!picking")
    rec.mark("Pick region pressed (picking)")

    # 3. The visor lens on the FRONT canvas, addressed in art space.
    a = cap.js("() => ({cx: state.art[0].cx, cy: state.art[0].cy, w: state.art[0].wmm,"
               " h: state.art[0].wmm * state.art[0].ih / state.art[0].iw})")
    vx = a["cx"] + (VISOR_UV[0] - 0.5) * a["w"]
    vy = a["cy"] + (VISOR_UV[1] - 0.5) * a["h"]
    cap.scroll_into_view("front")
    rec.click_at(*cap.board_to_client(vx, vy, "front"), after_ms=150)
    rec.wait_shown("state.art[0].overrides.length === 1"
                   f" && state.art[0].overrides[0].material === '{WAND_MATERIAL}'", 15_000)
    rec.mark("visor clicked -> bare board override")
    rec.hold(600)

    # 4. The override chip under the layer: hovering it highlights the region.
    rec.move_to(*rec.center("#artlist .ovchips .chip2"))
    cap.wait_state("artHL !== null && artHL.type === 'override'")
    rec.mark("hover override chip")
    rec.hold(700)
    rec.mark("end hold")
    rec.hold(1500)

    n_over = cap.js("() => state.art[0].overrides.length")
    body_mat = cap.js("() => state.art[0].palette[0].material")   # the black class
    problems = cap.js("() => blockingProblems()")
    return {"overrides": n_over, "black_class_material": body_mat,
            "override": cap.js("() => state.art[0].overrides[0]"),
            "blocking_problems": problems, "visor_mm": (vx, vy)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=str(HELP_DIR / "wand.webp"))
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="wand-clip-"))
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
    (workdir / "result.json").write_text(json.dumps({"result": result, "stats": stats,
                                                     "marks": rec.marks,
                                                     "frames": [(p.name, d) for p, d in rec.frames]},
                                                    default=str))
    print("result:", result)
    print("webp:", out, stats)
    print("contact sheet:", sheet)


if __name__ == "__main__":
    main()
