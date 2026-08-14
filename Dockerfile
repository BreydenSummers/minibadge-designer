# minibadge-designer: Flask UI + KiCad's command line tools.
#
# kicad-cli is what turns a generated board into the interactive 3D view
# (`pcb export glb`), so the image needs a real KiCad — not just Python.
# Debian's packages are used rather than the official kicad/kicad image
# because they ship the 3D model libraries in both STEP and VRML, and
# because pulling only the parts we place keeps this ~20x smaller than
# kicad/kicad:9.0-full (which is 6.3 GB, nearly all of it models,
# symbols and demos we never touch).
#
# KiCad 9.x is required: generated boards resolve models through
# ${KICAD9_3DMODEL_DIR}, which only a 9.x install defines.

# ---- stage 1: just the 3D models we can actually place ----------------------
FROM debian:trixie-slim AS models
RUN apt-get update \
 && apt-get install -y --no-install-recommends kicad-packages3d \
 && rm -rf /var/lib/apt/lists/*
# minibadge_designer.pcb can only ever reference these ten parts (LED + resistor in
# each package, plus the connector header). The stock tree is ~5 GB; this is
# about half a megabyte. Keep in sync with pcb.PKG / model_path().
RUN set -eux; \
    src=/usr/share/kicad/3dmodels; \
    for f in \
      LED_THT.3dshapes/LED_D1.8mm_W3.3mm_H2.4mm \
      LED_THT.3dshapes/LED_D3.0mm \
      LED_THT.3dshapes/LED_Rectangular_W5.0mm_H2.0mm \
      LED_SMD.3dshapes/LED_0603_1608Metric \
      LED_SMD.3dshapes/LED_0805_2012Metric \
      LED_SMD.3dshapes/LED_1206_3216Metric \
      Resistor_SMD.3dshapes/R_0603_1608Metric \
      Resistor_SMD.3dshapes/R_0805_2012Metric \
      Resistor_SMD.3dshapes/R_1206_3216Metric \
      Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x02_P2.54mm_Vertical \
    ; do \
      mkdir -p "/models/$(dirname "$f")"; \
      cp "$src/$f.step" "/models/$f.step"; \
    done

# ---- stage 2: runtime -------------------------------------------------------
FROM debian:trixie-slim

# `kicad` brings kicad-cli plus its libraries; installing it through apt (as
# opposed to copying the binary out) is what keeps the shared-library graph
# correct. The stock symbol/footprint/demo trees go: generated boards embed
# every footprint they use, and nothing here reads a schematic.
RUN apt-get update \
 && apt-get install -y --no-install-recommends kicad python3-pip \
 && rm -rf /var/lib/apt/lists/* \
      /usr/share/kicad/demos \
      /usr/share/kicad/symbols \
      /usr/share/kicad/internat \
      /usr/share/kicad/3dmodels \
      /usr/share/doc \
 # Mesa's software GL stack (~150 MB, mostly LLVM) arrives with KiCad's GUI
 # libraries. The GLB export tessellates through OpenCascade on the CPU and
 # never opens a GL context; the smoke test at the end of this file proves
 # the export still works without them.
 && rm -f /usr/lib/*/libLLVM.so.* /usr/lib/*/libgallium-*.so

COPY --from=models /models /usr/share/kicad/3dmodels

# Debian marks the system interpreter externally managed; this image exists
# to run one app, so install into it rather than carrying a venv.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# kicad-cli wants a writable HOME for its config; a login user provides one.
RUN useradd --create-home --shell /usr/sbin/nologin badge
WORKDIR /app

# Third-party runtime assets (the bundled typefaces and the <model-viewer>
# component) are other people's files under other people's licences, so they
# are not kept in this repository. Fetch them by pinned version, checked
# against a SHA-256, before the app is copied in. Needs network at build time.
COPY scripts/fetch_assets.py /app/scripts/fetch_assets.py
RUN python3 scripts/fetch_assets.py

COPY --chown=badge:badge . /app
RUN chown -R badge:badge /app

# Smoke-test the 3D pipeline at build time. Every way this has broken before
# was silent — a missing model, a format kicad-cli's VRML reader rejects, a
# KiCad major version that no longer defines KICAD9_3DMODEL_DIR — and each
# one yields a *successful* export of a board with no components on it. Fail
# the build instead of shipping an image that quietly renders a bare PCB.
RUN python3 - <<'PY'
import json, struct, subprocess, sys, tempfile, pathlib
from minibadge_designer import pcb

spec = pcb.BadgeSpec(name="smoke", leds=[
    pcb.Led(6.5, 8.0, "red", size="3mm"),      # through-hole
    pcb.Led(13.5, 12.0, "green", size="0805"),  # SMD
])
with tempfile.TemporaryDirectory() as td:
    board = pathlib.Path(td, "smoke.kicad_pcb")
    board.write_text(pcb.generate_pcb(spec))
    glb = pathlib.Path(td, "smoke.glb")
    subprocess.run(
        ["kicad-cli", "pcb", "export", "glb", "--subst-models", "--include-tracks",
         "--include-pads", "--include-zones", "--force", "-o", str(glb), str(board)],
        check=True, capture_output=True, timeout=300)
    raw = glb.read_bytes()
    ln, _ = struct.unpack_from("<I4s", raw, 12)
    g = json.loads(raw[20:20 + ln])
    bodies = {n["name"] for n in g["nodes"]
              if n.get("name") and not n["name"].startswith("=>")}
    missing = {"D1", "D2", "R1", "R2", "J1"} - bodies
    if missing:
        sys.exit(f"3D models did not resolve — missing {sorted(missing)}. "
                 f"Check pcb.MODEL_EXT and the models copied above.")
    # Colour check: this board exports 10 materials when the part colours
    # survive and ~7 when they are stripped (which is what kicad-cli builds
    # with a stricter OpenCascade produce). 9 sits between the two without
    # tripping on a stray material.
    if len(g.get("materials", [])) < 9:
        sys.exit(f"models loaded but lost their colours "
                 f"({len(g['materials'])} materials); expected the full palette.")
    print(f"3D smoke test OK: {sorted(bodies)}, {len(g['materials'])} materials")
PY

USER badge

ENV KICAD_CLI=/usr/bin/kicad-cli \
    PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python3", "main.py"]
