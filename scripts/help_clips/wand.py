"""Record the "?" tip clip for the magic wand (TIPS key `wand`).

    .venv/bin/python scripts/help_clips/wand.py [--workdir DIR] [--out minibadge_designer/static/help/wand.webp] [--fps 8] [--quality 70]

Step 3 of the helmet story, on the shared Recorder (scripts/help_recorder.py).
Starts from stage2-art (helmet, by-colour art, no overrides) with the Art
panel scrolled to the Magic wand row and the camera at 1:1 on that row. The
hand rests on the wand material select so the viewer reads what the wand
will paint (Bare board, the app's default; an earlier take staged Glow window
to show a change, and the reviewer found that sent newcomers hunting for a
switch already made), presses Pick region, then the camera pulls back to the whole window and the hand
travels onto the FRONT canvas: over the crown first, where the whole body
lights up as the region a click would grab, then down onto the visor lens,
where only the lens lights up; it clicks and the visor turns bare (tan) while
the grey rim keeps its silkscreen and the body stays black mask. The camera
closes on the new override chip under the layer while the hand hovers it (the
picked region highlights on the canvas), then pulls back and the hand leaves
the chip so the clip ends on the UN-hovered tan visor.

The visor is addressed in art image space (VISOR_UV, the u/v the stage3-wand
fixture records) and converted through the live art placement and
cap.board_to_client, so a change in art width or centre cannot move the click
off the lens. BODY_UV is the crown, the black body above the grey rim.

Camera and encoding follow scripts/help_clips/shape.py: 1:1 crops for the
control beats, `focus_full()+settle_camera()` for every result beat, `--fps 8`
on disk (motion frames folded, choreographed time unchanged), quality 70.
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

FIXTURE = "stage2-art"
VISOR_UV = (0.4988, 0.4891)   # the visor lens, in art image space (stage3-wand.json)
BODY_UV = (0.5, 0.25)         # the crown: black body, above the grey rim
WAND_MATERIAL = "bare"


def art_client(cap, uv, side="front"):
    """An art-space (u, v) as a client point on `side`, via the live placement."""
    a = cap.js("() => ({cx: state.art[0].cx, cy: state.art[0].cy, w: state.art[0].wmm,"
               " h: state.art[0].wmm * state.art[0].ih / state.art[0].iw})")
    x = a["cx"] + (uv[0] - 0.5) * a["w"]
    y = a["cy"] + (uv[1] - 0.5) * a["h"]
    return (x, y), cap.board_to_client(x, y, side)


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.show_panel("art")
    cap.wait_state("state.art.length === 1 && state.art[0].overrides.length === 0")
    # A person has the wand row in view before reaching for it; the panel
    # scroll happens before the first frame so nothing jumps on screen.
    cap.js("() => document.querySelector('#artlist .wandb').scrollIntoView({block: 'center'})")
    cap.scroll_into_view("front")
    # The select ships reading Bare board and the clip starts from the real
    # default: the reviewer's second pass found that a staged Glow window made
    # newcomers hunt for a switch the app has already made for them.
    cap.wait_state(f"document.querySelector('#artlist select.wandm').value === '{WAND_MATERIAL}'")
    # Rest the hand near the panel, off every control.
    b = rec.box("#artlist .wandb")
    rec.mx, rec.my = b["x"] + b["width"] + 40, b["y"] - 60
    page.mouse.move(rec.mx, rec.my)
    # Open on the Magic wand row at 1:1, already framed (the camera is parked
    # there before the first frame; a single frame at full view would jump).
    rec.start_focused("#artlist select.wandm", pad=70, include=["#artlist .wandb"])
    rec.mark("stage2-art: helmet with by-colour art, wand row at 1:1")
    rec.hold(400)

    # 1. The material select: the hand rests on it so the viewer reads what the
    # wand will paint (Bare board, the default), without changing it.
    rec.move_to(*rec.center("#artlist select.wandm"))
    rec.hold(500)
    rec.mark("wand material read: Bare board (default)")

    # 2. Pick region.
    rec.click("#artlist .wandb", after_ms=200)
    cap.wait_state("!!picking")
    rec.mark("Pick region pressed (picking)")

    # 3. Onto the FRONT canvas at full view: the camera pulls back while the
    # hand travels to the crown. With the wand armed, the region under the
    # cursor lights up: the whole body over the crown, then only the lens.
    (_, body_pt) = art_client(cap, BODY_UV)
    rec.focus_full(ms=800)
    rec.move_to(*body_pt, ms=900)
    cap.wait_state("artHL !== null && artHL.type === 'wandpick'")
    rec.mark("hover crown: whole body lights up")
    rec.hold(400)
    ((vx, vy), lens_pt) = art_client(cap, VISOR_UV)
    rec.click_at(*lens_pt, settle_ms=300, after_ms=150)
    rec.wait_shown("state.art[0].overrides.length === 1"
                   f" && state.art[0].overrides[0].material === '{WAND_MATERIAL}'", 15_000)
    rec.mark("visor clicked -> bare board override")
    rec.hold(600)

    # 4. The override chip under the layer: the camera closes on the chips
    # while the hand travels; hovering the chip highlights its region.
    rec.focus_on("#artlist .ovchips", pad=90, ms=700)
    rec.move_to(*rec.center("#artlist .ovchips .chip2"))
    cap.wait_state("artHL !== null && artHL.type === 'override'")
    rec.mark("hover override chip (region highlighted)")
    rec.hold(600)

    # 5. End: pull back to the whole window, the hand leaves the chip so the
    # highlight clears, and the clip holds on the un-hovered tan visor.
    rec.focus_full(ms=700)
    c = rec.box("#artlist .ovchips")
    rec.move_to(c["x"] + c["width"] + 60, c["y"] - 90, ms=600)
    cap.wait_state("artHL === null")
    rec.settle_camera(150)
    rec.mark("end hold: tan visor, nothing hovered")
    rec.hold(1500)

    n_over = cap.js("() => state.art[0].overrides.length")
    body_mat = cap.js("() => state.art[0].palette[0].material")   # the black class
    problems = cap.js("() => blockingProblems()")
    return {"overrides": n_over, "black_class_material": body_mat,
            "override": cap.js("() => state.art[0].overrides[0]"),
            "select_value": cap.js("() => document.querySelector('#artlist select.wandm').value"),
            "hovered_at_end": cap.js("() => artHL"),
            "blocking_problems": problems, "visor_mm": (vx, vy)}


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
    ap.add_argument("--out", default=str(HELP_DIR / "wand.webp"))
    ap.add_argument("--fps", type=int, default=8, help="motion frame rate on disk (12 = as shot)")
    ap.add_argument("--quality", type=int, default=70)
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
    print("result:", result)
    print("webp:", out, stats)
    print("contact sheet:", sheet)
    # The end checks: one bare override on the visor, the body still on
    # Ignore (mask), nothing hovered, and a legal board.
    if result["blocking_problems"]:
        raise SystemExit(f"blocking problems: {result['blocking_problems']}")
    if result["overrides"] != 1 or result["override"]["material"] != WAND_MATERIAL:
        raise SystemExit(f"expected one {WAND_MATERIAL} override, got {result['override']}")
    if result["black_class_material"] != "ignore":
        raise SystemExit(f"the black class should still be Ignore, got {result['black_class_material']}")
    if result["hovered_at_end"] is not None:
        raise SystemExit(f"the clip must end un-hovered, artHL = {result['hovered_at_end']}")


if __name__ == "__main__":
    main()
