"""Record the "art" tip clip: the helmet picture added as by-colour artwork.

    .venv/bin/python scripts/help_clips/art.py [--workdir DIR] [--out minibadge_designer/static/help/art.webp]

TIPS key `art` ("Step 2: paint it with materials"). Starts from the
stage1-shape fixture (helmet board, black mask, bottom pins only) with the Art
panel open. The cursor presses **+ Image**, the helmet PNG lands as artwork in
By color mode and the per-colour rows appear; the cursor hovers the gold row
and then the grey row so their pixels light up on the front canvas, switches
the white row and then the grey goggle-frame row to Ignore (cheek pads and
frame vanish; the goggles are mask only, the stripes stay copper), sets the
width to 18 mm, and rests on the painted helmet. 6-10 s, 660 px wide, animated webp.

Uses the shared Recorder (scripts/help_recorder.py); nothing is re-implemented.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from help_recorder import (
    HELMET,
    HELP_DIR,
    TIP_H,
    TIP_W,
    GifCapture,
    Recorder,
    assemble_webp,
    contact_sheet,
)

ART_W = 18.0
FIXTURE = "stage1-shape"


def _is_white(rgb):
    return min(rgb) >= 240


def _is_gold(rgb):
    r, g, b = rgb
    return r > g > b and r - b >= 60


def _is_grey(rgb):
    # The helmet's rim is a light grey (201, 206, 214), well short of white.
    return max(rgb) - min(rgb) <= 30 and 60 <= sum(rgb) / 3 <= 235


def record(rec: Recorder) -> dict:
    cap, page = rec.cap, rec.page
    cap.load_fixture(FIXTURE)
    cap.show_panel("art")
    cap.wait_state("activePanel === 'art' && state.art.length === 0")
    # The fixture load posts step 1's toast ("bridges were added"); let it
    # expire for real (6 s wall clock, no frames shot) so the clip opens clean.
    cap.wait_state("$('toasts').children.length === 0", timeout=12_000)
    # Park the hand near the panel so the first move is short and readable.
    rec.mx, rec.my = rec.center("#artlist")[0], rec.center("#artlist")[1] + 80
    page.mouse.move(rec.mx, rec.my)
    rec.mark("helmet board, Art panel empty")
    rec.hold(400)

    # 1. + Image, pick the helmet PNG (hidden input; the button press reads as
    # the person having picked the file).
    rec.click("#addart", after_ms=150)
    rec.mark("+ Image pressed")
    page.set_input_files("#artfile", str(HELMET))
    rec.wait_shown("state.art.length === 1 && state.art[0].palette && state.art[0].palette.length > 0", 20_000)
    rec.mark("artwork on the board, colour rows shown")
    rec.hold(500)

    palette = cap.js("() => state.art[0].palette.map(p => ({rgb: p.rgb, material: p.material}))")
    gold = next((j for j, p in enumerate(palette) if _is_gold(p["rgb"])), None)
    grey = next((j for j, p in enumerate(palette) if _is_grey(p["rgb"])), None)
    white = next((j for j, p in enumerate(palette) if _is_white(p["rgb"])), None)
    if white != 1:
        raise AssertionError(f"white row expected at data-j=1, palette is {palette}")
    if gold is None or grey is None:
        raise AssertionError(f"no gold/grey row found in palette {palette}")

    # 2. Hover the gold row, then the grey row: the pixels each controls light
    # up on the front canvas.
    rows = "#artlist .swrow"
    rec.move_to(*rec.center(f"{rows} >> nth={gold}", fx=0.35))
    cap.wait_state(f"!!artHL && artHL.type === 'palette' && artHL.j === {gold}")
    rec.mark("hover: gold row lit")
    rec.hold(700)
    rec.move_to(*rec.center(f"{rows} >> nth={grey}", fx=0.35))
    cap.wait_state(f"!!artHL && artHL.type === 'palette' && artHL.j === {grey}")
    rec.mark("hover: grey row lit")
    rec.hold(700)

    # 3. White is background, not ink.
    rec.choose("#artlist select.pm[data-j='1']", "ignore", after_ms=350)
    cap.wait_state("state.art[0].palette[1].material === 'ignore'")
    rec.mark("white row -> Ignore")
    # ...and the grey goggle frame is mask only too, so the goggles read as
    # a shape in the black rather than a white outline. Its select sits
    # right under the white one: a short hop, the same press-then-pick.
    grey_sel = f"#artlist select.pm[data-j='{grey}']"
    rec.move_to(*rec.center(grey_sel), ms=300)
    rec.hold(150)
    rec.press()
    rec.hold(120)
    page.select_option(grey_sel, "ignore")
    cap.wait_state(f"state.art[0].palette[{grey}].material === 'ignore'")
    rec.hold(350)
    rec.mark("grey row -> Ignore")

    # 4. Width 18 mm, same as the board.
    rec.set_number("#artlist input.wdn", f"{ART_W:g}")
    cap.wait_state(f"Math.abs(state.art[0].wmm - {ART_W}) < 0.01")
    rec.mark("width 18 mm")
    # Park the hand off the card so the last frames show the board, not a
    # focused field, and hold on the painted helmet.
    cx, cy = rec.center("#artlist")
    rec.move_to(cx + 60, cy + 40, ms=350)
    # The "Added helmet.png..." status has a 6 s timer; wait (wall clock, no
    # frames) for it to fade so the closing hold is the board alone.
    cap.wait_state("$('toasts').children.length === 0", timeout=12_000)
    cap.js("() => draw()")
    problems = cap.js("() => blockingProblems()")
    rec.mark("painted helmet")
    rec.hold(1500)
    return {"blocking_problems": problems, "palette": palette,
            "design": cap.js("() => designJSON()")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=str(HELP_DIR / "art.webp"))
    args = ap.parse_args()
    import tempfile
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="art-clip-"))
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
    (workdir / "design.json").write_text(json.dumps(result["design"]))
    stats = assemble_webp(rec.frames, out)
    sheet = contact_sheet(rec.frames, workdir / "contact.png")
    print("palette:", result["palette"])
    print("blocking problems at the end:", result["blocking_problems"])
    print("webp:", out, stats)
    print("contact sheet:", sheet)


if __name__ == "__main__":
    main()
