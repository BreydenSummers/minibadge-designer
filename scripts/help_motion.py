"""Remotion back-end for the illustrated help assets.

help_capture.Capture records CLEAN screenshots plus ring/caption metadata
(`Capture.step`); this module hands that storyboard to the Remotion project in
scripts/help_remotion (HelpClip / HelpStill compositions), renders a PNG
sequence, collapses the pixel-identical hold frames back into held steps, and
assembles the same 660x575 animated webp (or still png) the "?" tips have
always served. The polish - crossfades, the draw-on magenta ring, the caption
chip with step dots - lives entirely in the Remotion compositions; this file
only moves bytes.

The compositions finish ALL motion in the first HEAD frames of a step and are
pixel-static afterwards; dedupe below depends on that. If a hold refuses to
dedupe, something in HelpClip.tsx is still animating during the hold.
"""

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
REMOTION = REPO / "scripts" / "help_remotion"
NPX = REMOTION / "node_modules" / ".bin" / "remotion"

FRAME_W, FRAME_H = 660, 575
FPS = 15
FRAME_MS = 1000 / FPS
QUALITY = 72

RENDER_TIMEOUT = 15 * 60  # first render downloads Chrome Headless Shell


def _stage(steps, stem):
    """Copy step screenshots into the Remotion public dir; return props steps."""
    dst = REMOTION / "public" / "help_src" / stem
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    out = []
    for i, s in enumerate(steps):
        name = f"step_{i:02d}.png"
        shutil.copy(s["img"], dst / name)
        out.append({
            "img": f"help_src/{stem}/{name}",
            "caption": s.get("caption", ""),
            "ring": s.get("ring"),
            "hold_ms": s["hold_ms"],
        })
    return out


def _run(args, timeout=RENDER_TIMEOUT):
    proc = subprocess.run(
        args, cwd=REMOTION, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"remotion failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


def render_storyboard(steps, out_path, src_w=1320, src_h=1150):
    """steps: [{img: Path, caption: str, ring: dict|None, hold_ms: int}].
    Renders HelpClip and writes an animated webp at out_path. Returns out_path
    and the list of (frame_count-per-held-step) diagnostics."""
    out_path = Path(out_path)
    stem = out_path.stem
    props = {"steps": _stage(steps, stem), "src_w": src_w, "src_h": src_h}
    with tempfile.TemporaryDirectory() as td:
        pj = Path(td) / "props.json"
        pj.write_text(json.dumps(props))
        seq = Path(td) / "seq"
        _run([str(NPX), "render", "src/index.ts", "HelpClip", str(seq),
              "--sequence", "--image-format=png", f"--props={pj}",
              "--log=error"])
        frames = sorted(seq.glob("*.png"),
                        key=lambda p: int("".join(c for c in p.stem if c.isdigit())))
        if not frames:
            raise RuntimeError(f"remotion produced no frames in {seq}")
        imgs, durations, hashes = [], [], []
        for p in frames:
            data = p.read_bytes()
            h = hashlib.md5(data).hexdigest()
            if hashes and hashes[-1] == h:
                durations[-1] += FRAME_MS
                continue
            hashes.append(h)
            durations.append(FRAME_MS)
            imgs.append(Image.open(p).convert("RGB").resize(
                (FRAME_W, FRAME_H), Image.LANCZOS))
        durations = [max(20, round(d)) for d in durations]
        # Frames are encoded independently, NEVER as lossy deltas:
        # minimize_size/allow_mixed let libwebp diff frames, and on this
        # low-contrast dark UI the deltas leave the previous step's board
        # and panel ghosting through every later frame (shipped 2026-08-31,
        # user-visible). _verify_encode below guards the property.
        imgs[0].save(out_path, save_all=True, append_images=imgs[1:], loop=0,
                     duration=durations, quality=QUALITY, method=6)
        _verify_encode(out_path, imgs)
    return out_path, {"rendered": len(frames), "kept": len(imgs),
                      "bytes": out_path.stat().st_size}


# Backstop against encoder surprises. The real guarantee is structural (no
# delta flags above); this catches gross ghosting/corruption. Measured on this
# UI at QUALITY=72: normal lossy noise peaks at a per-block mean of ~11, and
# the 2026-08-31 delta-ghosting defect read ~12 - too close to threshold away,
# which is exactly why the flags are gone rather than merely policed.
GHOST_BLOCK_MEAN = 16.0
BLOCK = 16


def _verify_encode(out_path, srcs):
    from PIL import ImageChops
    dec = Image.open(out_path)
    if getattr(dec, "n_frames", 1) != len(srcs):
        raise RuntimeError(
            f"{out_path.name}: encoder wrote {getattr(dec, 'n_frames', 1)} "
            f"frames, expected {len(srcs)}")
    worst = (0.0, -1)
    peaks = []
    for i, src in enumerate(srcs):
        dec.seek(i)
        diff = ImageChops.difference(dec.convert("RGB"), src)
        w, h = diff.size
        small = diff.convert("L").resize((w // BLOCK, h // BLOCK), Image.BOX)
        peak = max(small.getdata())
        peaks.append((peak, i))
        if peak > worst[0]:
            worst = (peak, i)
        if peak > GHOST_BLOCK_MEAN:
            raise RuntimeError(
                f"{out_path.name} frame {i}: a {BLOCK}px block deviates from "
                f"the rendered frame by mean {peak:.1f} (> {GHOST_BLOCK_MEAN})"
                " - encoder ghosting or corruption; do not ship this file")
    return worst


def render_still(img, out_path, caption="", src_w=None, src_h=None):
    """Render one styled still (HelpStill) from a screenshot/render png.
    The composition adopts the source image's aspect; the output is 660 wide
    (matching the animations) with proportional height."""
    out_path = Path(out_path)
    with Image.open(img) as probe:
        iw, ih = probe.size
    src_w = src_w or iw
    src_h = src_h or ih
    dst = REMOTION / "public" / "help_src" / out_path.stem
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copy(img, dst / "still.png")
    props = {"img": f"help_src/{out_path.stem}/still.png", "caption": caption,
             "src_w": src_w, "src_h": src_h}
    with tempfile.TemporaryDirectory() as td:
        pj = Path(td) / "props.json"
        pj.write_text(json.dumps(props))
        raw = Path(td) / "still.png"
        _run([str(NPX), "still", "src/index.ts", "HelpStill", str(raw),
              f"--props={pj}", "--log=error"])
        out_h = round(FRAME_W * src_h / src_w)
        Image.open(raw).convert("RGB").resize(
            (FRAME_W, out_h), Image.LANCZOS).save(out_path)
    return out_path
