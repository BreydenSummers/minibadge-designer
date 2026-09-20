# minibadge designer

A web app for designing [minibadges](https://github.com/lukejenkins/minibadge):
the little 20 × 20 mm add-on boards that clip onto a conference badge and light
up from its power. You draw the badge in the browser, and the app hands you a
KiCad project (or a Gerber zip) that a board house can make as-is.

<img src="minibadge_designer/static/help/final.png" width="420" alt="A helmet-shaped minibadge in the app's 3D view">

You don't need to know PCB design. Upload a logo, put LEDs where you want
light, type some text, download. The connector, the resistors, the copper
routing and the design-rule checks are all done for you, and if you do know
KiCad, the download opens there and you can take it as far as you like.

## Getting started

The only thing you need is Docker: Engine 20.10 or newer, or Docker Desktop,
with the Compose plugin. The image takes about 1 GB of disk.

```bash
git clone https://github.com/BreydenSummers/minibadge-designer.git
cd minibadge-designer
docker compose up --build
```

The first build takes a few minutes, because it installs KiCad's command-line
tools inside the image so that the 3D view and the Gerber export work without
KiCad on your machine. Then open <http://localhost:8000>.

To run it in the background instead:

```bash
docker compose up -d --build    # start
docker compose logs -f          # watch it
docker compose down             # stop
```

The app only listens on this machine (`127.0.0.1:8000`), so if you want other
people on your network to use it, remove the `127.0.0.1:` prefix from the
`ports` line in `docker-compose.yml`. That is the only change.

### Without Docker

If you'd rather run the Python directly:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python3 scripts/fetch_assets.py     # downloads the typefaces and 3D viewer, once
minibadge-designer                  # http://127.0.0.1:8000
```

Everything works this way except the 3D view and the Gerber export, which need
KiCad 9 installed, and the app will find `kicad-cli` on your `PATH` or at the
usual macOS and Linux install locations once it is.

## Designing a badge

Every control has a "?" next to it with a short animation of what it does. A
badge usually comes together in this order:

1. Start with the board shape. The default is the standard 20 × 20 mm square.
   Upload a picture and its dark pixels become the outline, up to about 120 mm
   across. Basic shapes stack on top to add material or cut holes.

2. Drop in the artwork. Flat-colour art splits by colour, and each colour becomes
   a material: white silkscreen, exposed gold or silver copper, a see-through
   window that glows from an LED behind it, or bare board. Photos use a
   brightness threshold. When two areas share a colour, the magic wand retargets
   just one connected region.

3. Place the LEDs, on either side. Each one brings its own resistor and is wired
   to the connector's power for you. SMD or through-hole, any colour. Put one
   behind a window and the artwork lights up; any LED can run off the badge's
   clock pin so it blinks.

4. Add text, front or back, 0.8 to 5 mm tall, in KiCad's stroke font or one of a
   dozen display typefaces, in any of the materials above.

5. Download. The **⬇ Download** button gives you a KiCad project: the board, the
   project file, a BOM, and a README with assembly notes. Tick **Gerber fab
   package** for a zip you upload straight to JLCPCB or PCBWay. Either way the
   board already passes KiCad's design rule check.

Everything you place can be dragged, resized and rotated on either view, and
parts on the far side show through as dashed ghosts so you can grab them from
there too. Snap is on by default. Hold Alt to skip it for one move.

## What's in the download

- `<name>.kicad_pcb` and `<name>.kicad_pro`: open the project in KiCad 7 or
  later. Press **B** once to refill the copper zones. The shipped fills already
  pass the design checks; refilling only tidies the slits the file format forces.
- `BOM.csv`: every LED and resistor with its value.
- `README.txt`: fab settings and assembly steps for whoever builds it.

The board sits on the official minibadge v2 connector footprint from
[lukejenkins/minibadge](https://github.com/lukejenkins/minibadge), with VBATT
and NC left unconnected as the standard asks, and CLK wired only when an LED
uses it.

## Running it for other people

For a public or shared install, [docs/OPERATIONS.md](docs/OPERATIONS.md) covers
the rest: the worker and memory settings in `.env`, the logs (every failed
request lands in `logs/errors.log` with the reason), the ceilings on what one
upload may cost, and how the site deploys from the `main` branch.

## Development

```bash
pytest                  # the fast suite
pytest -m browser       # the Playwright UI tests, needs Chromium
```

`dev` is the working branch. Merging into `main` deploys.

| File | What it does |
|---|---|
| `minibadge_designer/webapp.py` | Flask app: the routes, upload checks, and the zip |
| `minibadge_designer/pcb.py` | Writes the `.kicad_pcb`, project file, and BOM |
| `minibadge_designer/logo.py` | Turns images into silkscreen and window polygons |
| `minibadge_designer/svgart.py` | Same, for SVG |
| `minibadge_designer/templates/index.html` | The whole browser editor |

## Credits and licences

The minibadge standard, spec, and connector footprint are by Luke Jenkins and
contributors, [lukejenkins/minibadge](https://github.com/lukejenkins/minibadge)
(Apache-2.0).

The typefaces and the `<model-viewer>` component are not in this repository,
because they belong to other projects under other licences, so
`scripts/fetch_assets.py` downloads them at pinned versions and records each
licence in `minibadge_designer/fonts/THIRD-PARTY-NOTICE.txt`. KiCad's 3D model
libraries come from Debian at image build time.
