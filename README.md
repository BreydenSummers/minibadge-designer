# minibadge designer

A web-based designer for [SAINTCON minibadges](https://saintcon.org/minibadges/): upload a
logo, drop as many LEDs as fit on the board (front or back side), add text in a dozen
typefaces and any material, and download a ready-to-fab KiCad project.

```
┌──────────────┐      ┌─────────────────────┐      ┌──────────────────────┐
│ logo + LEDs  │─────▶│ minibadge-designer  │─────▶│ KiCad project (.zip) │
└──────────────┘      └─────────────────────┘      └──────────────────────┘
```

## What you get

The download is a zip with a complete KiCad 7+ project:

- **A minibadge v2 board** on the official connector footprint (pad geometry follows
  [lukejenkins/minibadge](https://github.com/lukejenkins/minibadge), Apache-2.0) — the
  standard 20 × 20 mm square or a **custom outline composed from parts** — image
  silhouettes and basic shapes (circle, rectangle, triangle, hexagon, star), each
  adding board material or cutting a hole — scaling to roughly 120 × 124 mm (three
  badge-widths beyond the square in every direction; the editor zooms to fit), with
  **adjustable edge smoothing** for raster images (0 = keep raw pixels). Keep or
  drop each connector pin individually — a whole corner, a row, or a single pad;
  the app warns if the LEDs lose 3V3 or GND. Shapes that don't reach a kept
  connector pad are bridged automatically
- **Layered PCB art** from PNG/JPG/SVG — **SVG art is exact**: its vector paths land
  on the board (and can be traced into the board outline) with no pixelation at any
  size, while rasters use a 0.18 mm pixel grid. Flat-color images load in *By color*
  mode where every color gets its own material: **silkscreen**
  (white ink), **exposed copper** (mask opened over the copper plane), **glow windows**
  (copper stripped from both layers so back-side LED light shines through the tinted
  laminate), **bare board** (mask opened too — raw FR4), or *Ignore*. Photos use a
  threshold mode instead. A **magic wand** retargets a single connected region — e.g.
  make a skull's eyes see-through while the same-colored background stays solid — and
  layers can be rotated, mirrored, duplicated, and reordered. Silk auto-carves around
  mask openings, text, pads, and parts; windows keep a perimeter copper ring and a
  corridor to every LED unit so the power planes always stay connected
- **Text** wherever you drop it, front or back, scaled 0.8–5 mm — KiCad's stroke font
  on the silkscreen, or a dozen display typefaces (fetched on setup — see
  [Third-party assets](#third-party-assets) — from arcade to blackletter)
  rendered as exact polygons in **any material**: silk, exposed copper,
  glow window, or bare board
- **LED circuits** (LED + series resistor each — SMD 0603/0805/1206, or **through-hole**
  1.8 mm / 3 mm domes and the 5×2 mm rectangular bar, whose resistor stays an 0805) wired
  3V3 → R → LED → GND through precomputed copper pours (3V3 plane on the front, GND
  plane on the back) — passes KiCad DRC out of the box. Each unit can mount on either
  side, rotate in 90° steps,
  and use a **stacked** (compact block) or **in-line** (thin end-to-end strip) layout
  to fit around the artwork. Units go anywhere the board goes — across the whole of a
  custom outline, and into the connector strips between the pad pairs; only the pads
  themselves are kept clear
- **BOM.csv** and a **README.txt** with fab/assembly steps

VBATT, CLK, and NC pins are left unconnected per the standard.

## How It Works

1. The browser canvas previews the board: raster layers run the same threshold math
   the server uses (the silkscreen you see is the silkscreen you get), and SVG layers
   preview at high resolution because the board gets their exact vector geometry.
2. On download, Flask (`minibadge_designer/webapp.py`) converts raster art to
   run-length-merged rectangles (`minibadge_designer/logo.py`) and SVG art to exact
   polygons (`minibadge_designer/svgart.py` — fill rules, paint order, and occlusion
   included), then emits the `.kicad_pcb` s-expressions with shapely-computed
   zone fills (`minibadge_designer/pcb.py`).
3. Open the project in KiCad, press **B** to refill zones, run DRC, and plot Gerbers.
   The shipped fills are already DRC-clean; refilling just replaces the thin
   slits the file format forces (a fill is stored as one hole-free outline)
   with proper holes. Light windows carry keepout areas so the refill leaves
   them clear — the 3D view shows the board in this refilled state.

## Architecture

| Component | Responsibility |
|---|---|
| `minibadge_designer/webapp.py` | Flask app: serves the UI, validates params, zips the project |
| `minibadge_designer/pcb.py` | `.kicad_pcb` / `.kicad_pro` / BOM generation, zone-fill geometry |
| `minibadge_designer/logo.py` | image → thresholded silkscreen rectangles with keepouts |
| `minibadge_designer/templates/index.html` | canvas editor: front + mirrored back views, drag logo/LEDs |

## Running it with Docker (recommended)

Docker is the supported way to run minibadge-designer and the only one that is
feature-complete out of the box. The image carries KiCad's command line tools,
so the interactive **3D view** works, generated boards are **DRC-clean**, and
the 3D model shows the **populated board** in real part colours. The host needs
nothing but Docker — no KiCad, no Python.

### Requirements

- Docker Engine 20.10+ (or Docker Desktop) with the Compose plugin
- ~1 GB of disk for the image, and network access during the build

### Start it

```bash
git clone <this repo> && cd minibadge-designer
docker compose up --build       # first build takes a few minutes
```

Then open <http://localhost:8000>. Use `Ctrl-C` to stop, or run it detached:

```bash
docker compose up -d --build    # background
docker compose logs -f          # follow the log
docker compose down             # stop and remove
```

Rebuild after changing the code with `docker compose up --build`; only the
layers you touched are redone. To serve on another port, change the left-hand
side of the `ports` mapping in `docker-compose.yml` (`"9000:8000"`).

### What the build does

1. Pulls KiCad 9.x from Debian and keeps **only the ten 3D models** this app
   can place — the stock library is ~5 GB, this is half a megabyte. The result
   is ~800 MB rather than the 6.3 GB of the official `kicad/kicad:9.0-full`.
2. Fetches the third-party runtime assets (typefaces, `<model-viewer>`) by
   pinned version and SHA-256 — see [Third-party assets](#third-party-assets).
3. Runs a **smoke test**: it exports a two-LED board and fails the build if
   the component models did not resolve or lost their colours. Every way this
   has broken before was silent, producing a perfectly valid export of an
   empty board, so the build refuses to ship one.

The container runs as a non-root user and reports health to Compose. A `.env`
file is picked up if present but is not required.

## Running it with local Python (development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python3 scripts/fetch_assets.py     # one-time: typefaces + 3D viewer
minibadge-designer                  # serves http://127.0.0.1:8000
pytest
```

Good for fast edit/reload cycles, but not feature-complete unless you also
install **KiCad 9.x**: without it the 3D view returns an error pointing at the
downloaded project, and the DRC tests skip instead of running. The app looks
for `kicad-cli` in `$KICAD_CLI`, on `PATH`, and at the standard macOS and Linux
install paths. Everything else — the editor, artwork, and project download —
works with Python alone.

## Third-party assets

This repository contains no third-party files. The typefaces and the
`<model-viewer>` web component belong to other projects under other licences,
so they are downloaded on setup instead of being committed here:

```bash
python3 scripts/fetch_assets.py            # fetch (Docker does this for you)
python3 scripts/fetch_assets.py --check    # verify what is installed
```

Every asset is pinned to an exact version and verified against a SHA-256, so a
re-fetch cannot silently change a glyph; a mismatch aborts rather than
installing. The script records what it fetched, and under which licence, in
`minibadge_designer/fonts/THIRD-PARTY-NOTICE.txt`:

| Asset | Source | Licence |
|---|---|---|
| 11 display typefaces | [google/fonts](https://github.com/google/fonts) (pinned commit) | SIL Open Font License 1.1 |
| Special Elite | [google/fonts](https://github.com/google/fonts) (pinned commit) | Apache License 2.0 |
| `<model-viewer>` 4.0.0 | [google/model-viewer](https://github.com/google/model-viewer) | BSD 3-Clause |

KiCad's 3D model libraries are likewise never vendored — the Docker image
installs them from Debian at build time (CC-BY-SA-4.0 with the KiCad library
exception).

## CLI Reference

```
minibadge-designer [--host HOST] [--port PORT] [--debug]
```

## Credits

Minibadge standard, spec, and connector footprint: Luke Jenkins and contributors —
[lukejenkins/minibadge](https://github.com/lukejenkins/minibadge) (Apache-2.0).
