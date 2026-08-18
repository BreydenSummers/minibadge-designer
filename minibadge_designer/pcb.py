"""KiCad PCB generation for minibadges.

Emits a complete .kicad_pcb (s-expression, KiCad 7+ format) containing:

- The official MiniBadge_Simple connector footprint (pad geometry follows
  https://github.com/lukejenkins/minibadge, Apache-2.0, (c) Luke Jenkins and
  contributors; the coordinates are reproduced here, no files are copied).
- 0805 LED + series-resistor pairs at user-chosen positions.
- A 3V3 copper pour on F.Cu and a GND pour on B.Cu, so LED circuits connect
  regardless of where they are placed (each LED cathode drops to the back
  pour through a via).
- The user's logo as filled polygons on the front silkscreen.

Board coordinates ("board mm") match the official footprint's local frame:
the outline runs from (0.16, 0.16) to (20.16, 20.16). Everything is offset
by ORIGIN onto the KiCad page.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

ORIGIN = 100.0  # page offset of the footprint frame, mm

# Board outline in board mm (from MiniBadge_Simple Edge.Cuts).
OUTLINE = (0.16, 0.16, 20.16, 20.16)

# Connector pads: (pad number, x, y, net label or None, row).
# Vendored from MiniBadge_Simple.kicad_mod: 1.75 mm circular pads, 0.95 mm drill.
# Each row alone provides 3V3 + GND, so a badge may keep just one of them.
CONNECTOR_PADS = [
    ("1", 1.27, 1.27, None, "top"),      # +VBATT (unused: must not tie to 3V3)
    ("2", 3.81, 1.27, "GND", "top"),
    ("7", 16.51, 1.27, "3V3", "top"),
    ("8", 19.05, 1.27, "GND", "top"),
    ("9", 1.27, 19.05, None, "bottom"),  # CLK (see connector_pads: it joins
                                         # the netlist once a unit runs on it)
    ("10", 3.81, 19.05, None, "bottom"), # NC (reserved, never connect)
    ("15", 16.51, 19.05, "3V3", "bottom"),
    ("16", 19.05, 19.05, "GND", "bottom"),
]

# Pin captions printed on BOTH silkscreens, one per pad pair, tucked inside
# the pad keepout strip (y <= 2.5 / >= 17.82) where LED units can never sit,
# so they can't collide with unit silk and never clip the board edge.
# The pads come in four corner pairs. A design may keep or drop any single
# pin (plenty of badges only populate the pair they actually use), so the
# plate, keepout, caption and 3D header all follow the pair, and the caption
# names only the pins that survived.
PAD_PAIRS = {
    "tl": {"pins": ("1", "2"), "row": "top", "at": (2.54, 2.62), "header": (2.54, 1.27),
           "plate": (0.16, 0.16, 5.0, 3.4), "keepout": (0.04, 0.04, 5.04, 3.0)},
    "tr": {"pins": ("7", "8"), "row": "top", "at": (17.78, 2.62), "header": (17.78, 1.27),
           "plate": (15.32, 0.16, 20.16, 3.4), "keepout": (15.28, 0.04, 20.28, 3.0)},
    "bl": {"pins": ("9", "10"), "row": "bottom", "at": (2.54, 17.7), "header": (2.54, 19.05),
           "plate": (0.16, 16.92, 5.0, 20.16), "keepout": (0.04, 17.32, 5.04, 20.28)},
    "br": {"pins": ("15", "16"), "row": "bottom", "at": (17.78, 17.7), "header": (17.78, 19.05),
           "plate": (15.32, 16.92, 20.16, 20.16), "keepout": (15.28, 17.32, 20.28, 20.28)},
}
# Printed name of each pin, in board order within its pair.
PIN_LABELS = {"1": "VBAT", "2": "GND", "7": "3V3", "8": "GND",
              "9": "CLK", "10": "NC", "15": "3V3", "16": "GND"}
ALL_PINS = ("1", "2", "7", "8", "9", "10", "15", "16")


def pair_of(pin: str) -> str:
    """Which corner pair a pin belongs to."""
    for key, pair in PAD_PAIRS.items():
        if pin in pair["pins"]:
            return key
    raise KeyError(pin)


def active_pairs(pins) -> list[str]:
    """Corner pairs with at least one pin kept, in board order.

    Raises on a non-empty list holding no valid pin: that means a caller
    handed over the old row names, which would otherwise silently read as
    "no pads kept" and quietly drop every keepout.
    """
    keep = [k for k, v in PAD_PAIRS.items() if any(q in pins for q in v["pins"])]
    if pins and not keep:
        raise ValueError(f"no known connector pins in {tuple(pins)!r}")
    return keep


def pair_caption(key: str, pins) -> str:
    """Caption for a pair, naming only the pins that are actually there."""
    return " ".join(PIN_LABELS[q] for q in PAD_PAIRS[key]["pins"] if q in pins)


def pair_caption_at(key: str, pins) -> tuple[float, float]:
    """Where that caption sits, centred on the pins that survived.

    With a pin dropped the pair's midpoint is no longer over any copper, so
    the label would float a millimetre off the pad it names.
    """
    kept = [x for num, x, _y, _net, _row in CONNECTOR_PADS
            if num in PAD_PAIRS[key]["pins"] and num in pins]
    x, y = PAD_PAIRS[key]["at"]
    return (sum(kept) / len(kept) if kept else x, y)


def power_missing(pins) -> list[str]:
    """Nets the LED circuits need but no kept pin supplies."""
    have = {net for num, _x, _y, net, _row in CONNECTOR_PADS
            if num in pins and net}
    return [net for net in ("3V3", "GND") if net not in have]


# CLK drive. Pin 9 carries the main badge's blink clock; chosen LED units may
# run off it so they pulse with the badge instead of burning steady. Two
# hookup styles, both classic minibadge patterns:
#
#   jumper: a 3-pad solder jumper. The centre pad feeds the CLK units' own
#           rail; the outer pads carry 3V3 and CLK, and the builder bridges
#           exactly ONE side: steady or blinking. The two sources can never
#           be tied together (bridging CLK straight to 3V3 would back-drive
#           the badge's shared clock line for the whole chain), and an
#           unbridged jumper simply leaves those LEDs dark.
#   trace:  no jumper; the units' supply is wired straight to pin 9, so
#           they always blink.
#
# A front CLK unit keeps its GND via and fetches supply through a routed
# trace instead of the pour. A back CLK unit keeps its cathode in the GND
# pour, and its supply trace runs on B.Cu to a plated hole (pin 9, or the
# rail via beside the jumper), replacing its 3V3 via entirely.
JUMPER_AT = (9.2, 18.45)  # default jumper centre, board mm: past pin 10's
                          # pad and caption so the CLK silk label clears
                          # them, and high enough that the inflated art
                          # margins clear the board edge
JUMPER_PITCH = 1.3        # centre-to-centre jumper pad spacing
JUMPER_PAD = (1.0, 1.5)   # each jumper pad, mm, at rotation 0
JUMPER_VIA = (0.0, -1.5)  # rail via offset from the centre pad (unit frame)
# A back-side jumper's 3V3 pad has no 3V3 copper on its own face (the back
# pour is GND), so it reaches the front pour through its own via, offset
# past the centre pad on the 3V3 side, in line with the rail via.
JUMPER_V3VIA = (JUMPER_PITCH, -1.5)
CLK_RAIL = "CLK_LED"      # the jumper's centre pad and the runs it feeds


def connector_pads(clk: bool = False) -> list:
    """CONNECTOR_PADS, with pin 9 carrying CLK once the design uses it."""
    if not clk:
        return CONNECTOR_PADS
    return [(num, x, y, "CLK" if num == "9" else net, row)
            for num, x, y, net, row in CONNECTOR_PADS]


def clk_info(spec: "BadgeSpec") -> dict | None:
    """Everything the routers need to know about this board's CLK hookup.

    None when no unit runs on CLK, and also when pin 9 was dropped: with no
    plated hole carrying the clock the flag cannot be honoured, so the units
    fall back to plain pour feeds exactly like every other invalid parameter
    (the webapp refuses such a design with a real message first).

    Keys: net (the supply net CLK units carry), jumper ((x, y, rot) or None
    for the trace style), side ("front"/"back": the face carrying the
    jumper's pads), pads ([(net, cx, cy)] the jumper pads in board mm),
    via ((x, y) rail via or None; it exists exactly while a unit on the
    OTHER face needs a plated hole to reach the centre pad -- nothing gates
    it), v3via ((x, y) or None: a back-side jumper's 3V3 pad dropping
    straight into the front pour, when jumper_via asks for that), v3link
    (True when the 3V3 pad is instead fed by a routed trace to a 3V3 pin on
    its own face; see clk_v3_link), nodes/v3nodes (hand-placed bends for
    the two routed links), front/back (where each face's supply runs
    terminate), pin9 ((x, y)).
    """
    if not any(led.clk for led in spec.leds) or "9" not in spec.pins:
        return None
    p9 = next((x, y) for num, x, y, _net, _row in CONNECTOR_PADS
              if num == "9")
    if not spec.clk_jumper:
        return {"net": "CLK", "jumper": None, "side": "front", "pads": [],
                "via": None, "v3via": None, "v3link": False,
                "nodes": (), "v3nodes": (), "v3pin": None,
                "front": p9, "back": p9, "pin9": p9}
    jx, jy = spec.jumper if spec.jumper else JUMPER_AT
    rot = float(spec.jumper_rot) % 360
    side = "back" if spec.jumper_side == "back" else "front"

    def at(dx: float, dy: float) -> tuple[float, float]:
        rx, ry = _r(dx, dy, rot)
        return (jx + rx, jy + ry)

    pads = [("CLK", *at(-JUMPER_PITCH, 0.0)), (CLK_RAIL, jx, jy),
            ("3V3", *at(JUMPER_PITCH, 0.0))]
    far_face = "back" if side == "front" else "front"
    far = at(*JUMPER_VIA)
    via = (far if any(led.clk and led.side == far_face for led in spec.leds)
           else None)
    near = (jx, jy)
    return {"net": CLK_RAIL, "jumper": (jx, jy, rot), "side": side,
            "pads": pads, "via": via,
            "v3via": (at(*JUMPER_V3VIA)
                      if side == "back" and spec.jumper_via else None),
            "v3link": side == "back" and not spec.jumper_via,
            "nodes": tuple(spec.jumper_nodes or ()),
            "v3nodes": tuple(spec.jumper_v3nodes or ()),
            "v3pin": str(spec.jumper_v3pin) if spec.jumper_v3pin else None,
            "front": near if side == "front" else far,
            "back": near if side == "back" else far,
            "pin9": p9}


def _jumper_pad_quad(clk, cx: float, cy: float, extra: float = 0.0) -> list:
    """One jumper pad as a rotated quad, grown `extra` mm per side."""
    rot = clk["jumper"][2]
    w2, h2 = JUMPER_PAD[0] / 2 + extra, JUMPER_PAD[1] / 2 + extra
    out = []
    for qx, qy in ((-w2, -h2), (w2, -h2), (w2, h2), (-w2, h2)):
        rx, ry = _r(qx, qy, rot)
        out.append((cx + rx, cy + ry))
    return out


def jumper_caption_boxes(clk) -> list[tuple[float, float, float, float]]:
    """Bounding boxes of the jumper's CLK/3V3 silk labels (art keeps clear).

    Empty at any rotation but the two horizontal ones: a rotated label would
    need a rotated box, and the canvas only offers 90-degree steps, where
    the vertical labels print rotated with the jumper (so the boxes swap
    axes around the label centres).
    """
    if not clk or not clk["jumper"]:
        return []
    jx, jy, rot = clk["jumper"]
    out = []
    for lbl, (_net, _cx, _cy), sgn in (("CLK", clk["pads"][0], -1),
                                       ("3V3", clk["pads"][2], 1)):
        lx, ly = _r(sgn * (JUMPER_PITCH + JUMPER_PAD[0] / 2 + 1.0), 0.0, rot)
        cx, cy = jx + lx, jy + ly
        hw = len(lbl) * 0.6 / 2 + 0.3
        if rot % 180 == 90:
            out.append((cx - 0.55, cy - hw, cx + 0.55, cy + hw))
        else:
            out.append((cx - hw, cy - 0.55, cx + hw, cy + 0.55))
    return out


def jumper_copper_pieces(clk) -> list[tuple[str, list]]:
    """The jumper's copper plus the margin art must clear, as labeled quads.

    The same contract as unit_copper_pieces: pads grown 0.5 mm per side,
    each via as its 16-gon collar, each stub as a 1.1 mm band. The link
    trace to pin 9 is NOT here: it is routed against the finished board, so
    its keepout comes from clk_link's own points.
    """
    if not clk or not clk["jumper"]:
        return []
    pieces = [(lbl, _jumper_pad_quad(clk, cx, cy, 0.5))
              for lbl, (_net, cx, cy) in
              zip(("pad_clk", "pad_rail", "pad_3v3"), clk["pads"])]
    jx, jy, _rot = clk["jumper"]
    if clk["via"]:
        pieces.append(("via", _via_collar(*clk["via"])))
        pieces.append(("trace_stub", _quad_seg((jx, jy), clk["via"], 1.1)))
    if clk["v3via"]:
        v3 = clk["pads"][2]
        pieces.append(("via_3v3", _via_collar(*clk["v3via"])))
        pieces.append(("trace_3v3",
                       _quad_seg((v3[1], v3[2]), clk["v3via"], 1.1)))
    return pieces


def _jumper_copper_quads(clk, net: str | None = None,
                         face: str | None = None) -> list:
    """The jumper's REAL copper on other nets than `net`, for route hazards.

    Real pad sizes like _unit_copper_quads, not the inflated art keepouts.
    Via barrels penetrate both layers, so they count for every layer's runs;
    the SMD pads and stubs live on the jumper's face only, so with `face`
    given they count only when the run shares it.
    """
    if not clk or not clk["jumper"]:
        return []
    out = []
    on_face = face is None or face == clk["side"]
    if on_face:
        out += [_jumper_pad_quad(clk, cx, cy)
                for pnet, cx, cy in clk["pads"] if pnet != net]
    jx, jy, _rot = clk["jumper"]
    if clk["via"] and clk["net"] != net:
        out.append(_round_hazard(*clk["via"], VIA_SIZE / 2 + NOVIA_CLEAR))
        if on_face:
            out.append(_quad_seg((jx, jy), clk["via"], TRACK_W))
    if clk["v3via"] and net != "3V3":
        out.append(_round_hazard(*clk["v3via"], VIA_SIZE / 2 + NOVIA_CLEAR))
        if on_face:
            v3 = clk["pads"][2]
            out.append(_quad_seg((v3[1], v3[2]), clk["v3via"], TRACK_W))
    return out

# Minimal board tabs that carry each connector pad *pair*. Custom outlines
# union only these (never a full-width strip), so the image's own cuts win
# everywhere except directly under the pads: the silhouette shapes the
# whole edge, and pads always sit on solid material.
PAD_PLATES = {
    row: tuple(v["plate"] for v in PAD_PAIRS.values() if v["row"] == row)
    for row in ("top", "bottom")
}

# Custom outlines may extend this far beyond the standard square: three
# extra badge-widths in every direction (~120 x 124 mm), far past the
# oversized boards on minibadge.wiki. (Big boards overhang neighbouring
# slots and cost more to fab; that's the designer's call.) The connector
# strips always stay at standard positions.
OUTLINE_EXTENT = (-49.84, -51.84, 70.16, 72.16)

# LED unit geometry, relative to the LED center (x, y), at rotation 0.
# Two layouts share the same circuit (3V3 pour -> R -> LED -> GND pour):
#
#   stacked: resistor above the LED, joined by a short vertical trace on
#            the anode side. Compact block, ~5.6 x 5.4 mm.
#   inline:  R and LED end-to-end in one row: [via_back] R -> LED [via_front].
#            Long and thin, ~10 x 2.8 mm; lays along a board edge. The LED
#            is flipped so its anode faces the resistor.
#
# The power hookup depends on the mounting side: front units feed the
# resistor straight from the F.Cu 3V3 pour and drop the cathode through a
# via into the B.Cu GND pour; back units sit their cathode directly in the
# GND pour and reach 3V3 through a via instead. The whole unit rotates in
# 90-degree steps (Led.rot, clockwise from the front); every offset goes
# through _r().
TRACK_W = 0.3
# 0.7 mm pad on a 0.3 mm drill. The drill is what fabs charge for, and 0.3 mm
# is standard everywhere badge people order: JLCPCB's small-hole upcharge
# starts at 0.2 mm, PCBWay's below 0.2 mm, OSH Park's two-layer floor is
# 0.254 mm. Keeping the pad at 0.7 leaves a 0.2 mm annular ring (same ring as
# KiCad's 0.8/0.4 default, and clear of PCBWay's 0.15 mm minimum rather than
# sitting exactly on it) while the smaller hole eats less copper out of the
# pours and tents under soldermask more reliably.
VIA_SIZE, VIA_DRILL = 0.7, 0.3

# Region the (rotated) unit bbox must stay inside on the standard square:
# 0.54 mm in from the board edge. Custom outlines widen this to their own
# bounding box (see unit_safe); actual outline containment is checked
# separately. Clearance to the connector pads is NOT part of this region;
# that is per pad pair (PAD_KEEPOUTS), so units may sit between the pads.
UNIT_SAFE = (0.7, 0.7, 19.62, 19.62)

# Keepout boxes around each connector pad *pair*: the pads' copper (1.75 mm
# circles) expanded by 0.35 mm pour/DRC clearance. A unit bbox may not
# overlap a kept row's boxes, but the strip between the two pairs (and the
# strip of a dropped row) is fair game.
# The extra 0.5 mm beyond the pads covers the printed pin captions, so a
# unit's silk can never collide with them.
PAD_KEEPOUTS = {
    row: tuple(v["keepout"] for v in PAD_PAIRS.values() if v["row"] == row)
    for row in ("top", "bottom")
}

# Package parameters (the LED and its resistor share the size, except
# through-hole LEDs whose resistor stays an SMD: "res_pkg"). "dx" is the
# pad-center offset, pw/ph the pad size, res_dy the stacked resistor lift,
# gap the inline LED->resistor spacing, body the part outline for silk/fab.
# Through-hole LEDs, the sizes badgelife folks actually put on minibadges:
# tiny 1.8 mm and standard 3 mm domes plus the 5x2 mm rectangular "light
# bar". All follow KiCad's LED_THT footprints: 2.54 mm lead pitch, 1.8 mm
# circular pads, 0.9 mm drill ("drill" marks the package as through-hole).
# body is the base outline; "lens" the dome diameter (0 = no dome, a bar);
# "th_model" the standard-library 3D model stem.
# SMD pads follow KiCad's *_HandSolder proportions: these boards get built
# with an iron at a conference, not a reflow oven, so each pad carries extra
# copper past the end of the chip for the tip and a visible fillet. The growth
# is entirely OUTWARD: spans are KiCad's 2.80 / 3.20 / 4.40 mm while the
# inner gap the body sits in is untouched, which keeps component fit and the
# reverse-mount hole exactly as they were.
PKG = {
    # gap is the pad-edge-to-pad-edge run between the LED and its resistor in
    # the inline layout. It has to leave the two courtyards clear of each
    # other (see test_inline_courtyards_do_not_overlap): the hand-solder
    # courtyard is dx + 1.05 either side, so an inline pair needs
    # gap >= cyx_led + cyx_res - dx - rdx. 0603 sat 0.2 mm under that and
    # tripped KiCad's courtyards_overlap error.
    "0603": {"dx": 0.875, "pw": 1.05, "ph": 0.95, "res_dy": 2.2, "gap": 2.15, "body": (1.6, 0.8)},
    "0805": {"dx": 1.025, "pw": 1.15, "ph": 1.4, "res_dy": 2.6, "gap": 2.2, "body": (2.0, 1.25)},
    "1206": {"dx": 1.5375, "pw": 1.325, "ph": 1.8, "res_dy": 3.2, "gap": 2.6, "body": (3.2, 1.6)},
    "1.8mm": {"dx": 1.27, "pw": 1.8, "ph": 1.8, "res_dy": 2.8, "gap": 2.2, "body": (3.3, 2.4),
              "lens": 1.8, "drill": 0.9, "res_pkg": "0805", "th_desc": "radial",
              "th_model": "LED_D1.8mm_W3.3mm_H2.4mm"},
    "3mm": {"dx": 1.27, "pw": 1.8, "ph": 1.8, "res_dy": 3.0, "gap": 2.3, "body": (3.0, 3.0),
            "lens": 3.0, "drill": 0.9, "res_pkg": "0805", "th_desc": "radial",
            "th_model": "LED_D3.0mm"},
    "5x2mm": {"dx": 1.27, "pw": 1.8, "ph": 1.8, "res_dy": 2.6, "gap": 2.7, "body": (5.0, 2.0),
              "lens": 0.0, "drill": 0.9, "res_pkg": "0805", "th_desc": "rectangular",
              "th_model": "LED_Rectangular_W5.0mm_H2.0mm"},
}
PKG_METRIC = {"0603": "1608", "0805": "2012", "1206": "3216"}


def res_pkg(pkg: str) -> str:
    """The package the unit's series resistor uses (SMD even for TH LEDs)."""
    return PKG.get(pkg, PKG["0805"]).get("res_pkg", pkg)
# Standard-library 3D models, so KiCad's 3D viewer (and our server-side
# render preview) shows populated boards. The env var resolves inside
# KiCad; older versions that don't define it just skip the model.
MODEL_DIR = "${KICAD9_3DMODEL_DIR}"
# STEP, not the VRML (.wrl) twin KiCad also ships. Both render the same on
# macOS, but every Linux kicad-cli reads VRML through OpenCascade, which
# rejects these very files ("IrrelevantNumber": the stock models carry an
# out-of-spec ambientIntensity > 1) and then silently omits the part. STEP
# loads everywhere, colors included.
MODEL_EXT = ".step"
LED_SIZES = tuple(PKG)


def model_path(library: str, stem: str) -> str:
    """Path to a stock 3D model, as the board file references it."""
    return f"{MODEL_DIR}/{library}.3dshapes/{stem}{MODEL_EXT}"


def caption_boxes(pins) -> list[tuple[float, float, float, float]]:
    """Bounding boxes of the printed pin captions (art must stay clear)."""
    out = []
    for key in active_pairs(pins):
        label = pair_caption(key, pins)
        if not label:
            continue
        x, y = pair_caption_at(key, pins)
        hw = len(label) * 0.6 / 2 + 0.3
        out.append((x - hw, y - 0.55, x + hw, y + 0.55))
    return out


def _layout(name: str, size: str = "0805", reverse: bool = False) -> dict:
    """Unit geometry for a layout at a package size (offsets in unit mm).

    `reverse` is the badgelife through-board trick: a 1206 LED soldered
    upside-down over a routed hole so the light shines out the other face.
    It composes with either layout (the hole sits under the LED) but
    forces the 1206 package: smaller pads sit too close to the hole for
    the 0.2 mm copper-to-edge DRC rule.
    """
    if name == "reverse":  # legacy spelling: stacked + reverse
        name, reverse = "stacked", True
    if reverse:
        size = "1206"
    if size not in PKG:
        size = "0805"
    p = PKG[size]
    rp = PKG[res_pkg(size)]  # the resistor's package (SMD even for TH LEDs)
    q = lambda v: round(v, 6)  # keep chained offsets float-drift free
    dx, res_dy = p["dx"], p["res_dy"]
    rdx = rp["dx"]
    # Vertical envelope: pads plus the LED body (a TH lens is far wider than
    # its pads); rhh is the resistor end's own, smaller extent.
    hh = q(max(p["ph"] / 2 + 0.7, p["body"][1] / 2 + 0.4))
    rhh = q(rp["ph"] / 2 + 0.7)
    hole = max(0.8, round(2 * (dx - p["pw"] / 2) - 0.5, 2)) if reverse else 0.0
    if name == "inline":
        g = p["gap"]
        res_out = q(-(dx + g))
        res = q(res_out - rdx)
        res_in = q(res - rdx)
        return {
            "res": (res, 0.0),
            "led_k": (dx, 0.0), "led_a": (-dx, 0.0),
            "res_in": (res_in, 0.0), "res_out": (res_out, 0.0),
            "via_front": (q(dx + 1.55), 0.0), "via_back": (q(res_in - 1.0), 0.0),
            "led_flip": True, "hole": hole, "pkg": size,
            "bbox": (q(res_in - 1.75), -hh, q(max(dx + 2.25, p["body"][0] / 2 + 0.4)), hh),
        }
    return {
        "res": (0.0, -res_dy),
        "led_k": (-dx, 0.0), "led_a": (dx, 0.0),
        "res_in": (-rdx, -res_dy), "res_out": (rdx, -res_dy),
        "via_front": (q(-(dx + 1.55)), 0.0), "via_back": (q(-(rdx + 1.55)), -res_dy),
        "led_flip": False, "hole": hole, "pkg": size,
        "bbox": (q(-(dx + 2.35)), q(-(res_dy + rhh)),
                 q(max(dx + 1.35, p["body"][0] / 2 + 0.4)), hh),
    }


def _r(dx: float, dy: float, ang: float) -> tuple[float, float]:
    """Rotate a center-relative offset clockwise (y-down) by degrees.

    Multiples of 90 stay exact (no float drift on the classic orientations).
    """
    ang = float(ang) % 360
    if ang % 90 == 0:
        k = int(ang // 90) % 4
        if k == 0:
            return dx, dy
        if k == 1:
            return -dy, dx
        if k == 2:
            return -dx, -dy
        return dy, -dx
    import math

    t = math.radians(ang)
    return (dx * math.cos(t) - dy * math.sin(t),
            dx * math.sin(t) + dy * math.cos(t))

LED_COLORS = {
    # color -> (series resistor, typical forward voltage note)
    "red": "220",
    "orange": "220",
    "yellow": "220",
    "green": "120",
    "blue": "120",
    "white": "120",
}


@dataclass
class Led:
    x: float
    y: float
    color: str = "red"
    side: str = "front"      # "front" or "back"
    rot: float = 0           # degrees clockwise, viewed from the front
    layout: str = "stacked"  # "stacked" or "inline"
    size: str = "0805"       # SMD package: "0603", "0805", or "1206"
    reverse: bool = False    # through-board mount over a routed hole (forces 1206)
    # Reach the far pour through a connector pad instead of the unit's own
    # via. Those pads are plated through, so a trace on the unit's own layer
    # that lands on one crosses to the other side just as well: the common
    # hand-routed minibadge style, and it keeps vias off the face entirely.
    novia: bool = False
    # Board-mm bends the via-less run must pass through, in order. Set by
    # dragging handles on the canvas; empty means route automatically.
    nodes: tuple = ()
    # Board-mm bends for the unit's two internal traces, same contract as
    # nodes: anodes bends the resistor-output-to-LED-anode link, vnodes bends
    # the pad-to-power-via stub (which only exists while novia is off). Both
    # matter once free placement scatters the parts and the straight line
    # between them starts crossing things the user can see.
    anodes: tuple = ()
    vnodes: tuple = ()
    # Where the via-less run ends. None = the nearest connector pad carrying
    # the net (the classic choice). ("pad", "16") = that specific connector
    # pad; ("unit", 3) = the same-net pad of another unit on this face, so
    # several runs can share one path to the rail. An invalid choice (wrong
    # net, dropped pin, other face, or a chain that loops back on itself)
    # falls back to None rather than refusing; see novia_term().
    term: tuple | None = None
    # Run this unit's supply off the badge's blink clock (pin 9) instead of
    # the 3V3 pour, so it pulses with the badge. The board-level hookup
    # (BadgeSpec.clk_jumper) decides whether the run lands on the solder
    # jumper's rail or straight on pin 9. Ignored while pin 9 is dropped:
    # clk_info() then returns None and the unit feeds from 3V3 as usual.
    clk: bool = False
    # Board-mm bends for the CLK supply run, same contract as nodes.
    cnodes: tuple = ()
    # Put just the LED on the opposite face, with a via inside each of its
    # pads carrying the connections through, the via-in-pad style other
    # minibadge designers use. Not combinable with reverse mount.
    farled: bool = False
    # Advanced placement: free resistor/via offsets in the unit frame (mm),
    # seeded from a standard layout. rrot spins the resistor on its own
    # center, lrot spins the LED on its own center; the whole unit still
    # rotates with Led.rot. None = standard.
    adv: dict | None = None  # {"rx","ry","rrot","lrot","vx","vy"}


def _bbox_offsets(rot: float, layout: str = "stacked",
                  size: str = "0805",
                  reverse: bool = False) -> tuple[float, float, float, float]:
    # Axis-aligned envelope of the rotated unit bbox (all four corners).
    bb = _layout(layout, size, reverse)["bbox"]
    pts = [_r(px, py, rot) for px, py in
           ((bb[0], bb[1]), (bb[2], bb[1]), (bb[0], bb[3]), (bb[2], bb[3]))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def led_geometry(led: Led) -> dict:
    """The unit's geometry dict: its layout, or the advanced override.

    Advanced units keep the LED pads and reverse hole at the anchor but move
    the resistor (with its own rotation) and the via wherever the user put
    them; the bbox becomes the envelope of the actual copper + margins.
    """
    g = _layout(led.layout, led.size, led.reverse)
    a = led.adv
    if not a:
        return g
    g = dict(g)
    p = PKG[g["pkg"]]
    rp = PKG[res_pkg(g["pkg"])]
    dx, rdx = p["dx"], rp["dx"]
    rx, ry = float(a.get("rx", 0)), float(a.get("ry", 0))
    rrot = float(a.get("rrot", 0)) % 360
    lrot = float(a.get("lrot", 0)) % 360
    vx, vy = float(a.get("vx", 0)), float(a.get("vy", 0))
    rin, rout = _r(-rdx, 0, rrot), _r(rdx, 0, rrot)
    g["res"] = (rx, ry)
    g["res_in"] = (rx + rin[0], ry + rin[1])
    g["res_out"] = (rx + rout[0], ry + rout[1])
    g["led_k"] = _r(-dx, 0, lrot)
    g["led_a"] = _r(dx, 0, lrot)
    g["via_front"] = g["via_back"] = (vx, vy)
    g["res_rot"] = rrot
    g["led_rot"] = lrot
    g["led_flip"] = False
    # Envelope (unit frame) of every copper piece + art margin. A TH LED's
    # entries also cover the body: the lens is far wider than its pads.
    th = "drill" in p
    pw2 = (max(p["pw"], p["body"][0]) if th else p["pw"]) / 2 + 0.5
    ph2 = (max(p["ph"], p["body"][1]) if th else p["ph"]) / 2 + 0.5
    rw2, rh2 = rp["pw"] / 2 + 0.5, rp["ph"] / 2 + 0.5
    pts = []
    for cx, cy, ang, sw, sh in ((*g["led_k"], lrot, pw2, ph2),
                                (*g["led_a"], lrot, pw2, ph2),
                                (*g["res_in"], rrot, rw2, rh2),
                                (*g["res_out"], rrot, rw2, rh2)):
        for sx in (-sw, sw):
            for sy in (-sh, sh):
                ox, oy = _r(sx, sy, ang)
                pts.append((cx + ox, cy + oy))
    pts += [(vx - 0.85, vy - 0.85), (vx + 0.85, vy + 0.85)]
    if g["hole"]:
        hh2 = g["hole"] / 2 + 0.5
        pts += [(-hh2, -hh2), (hh2, hh2)]
    xs = [q[0] for q in pts]
    ys = [q[1] for q in pts]
    g["bbox"] = (min(xs), min(ys), max(xs), max(ys))
    return g


def _bbox_offsets_g(g: dict, rot: float) -> tuple[float, float, float, float]:
    bb = g["bbox"]
    corners = [_r(px, py, rot) for px, py in
               ((bb[0], bb[1]), (bb[2], bb[1]), (bb[0], bb[3]), (bb[2], bb[3]))]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def clamp_led_obj(led: Led, safe=None) -> tuple[float, float]:
    """clamp_led for a Led object; honors size/reverse AND advanced offsets."""
    if safe is None:
        safe = UNIT_SAFE
    ox0, oy0, ox1, oy1 = _bbox_offsets_g(led_geometry(led), led.rot)
    x = min(max(led.x, safe[0] - ox0), safe[2] - ox1)
    y = min(max(led.y, safe[1] - oy0), safe[3] - oy1)
    return x, y


def led_unit_bbox(
    led: Led, safe: tuple[float, float, float, float] | None = None
) -> tuple[float, float, float, float]:
    """Axis-aligned envelope of the LED unit (view fitting, edge clamping)."""
    x, y = clamp_led_obj(led, safe)
    ox0, oy0, ox1, oy1 = _bbox_offsets_g(led_geometry(led), led.rot)
    return (x + ox0, y + oy0, x + ox1, y + oy1)


def unit_poly(led: Led, safe: tuple[float, float, float, float] | None = None):
    """The unit's TIGHT footprint: its layout rect rotated with it (shapely).

    Collision and keepouts use this so tilted units only claim the room
    they actually need; the axis-aligned envelope stays for board-edge
    clamping, where it already touches the unit's extreme corners.
    """
    from shapely.geometry import Polygon

    x, y = clamp_led_obj(led, safe)
    bb = led_geometry(led)["bbox"]
    pts = [_r(px, py, led.rot) for px, py in
           ((bb[0], bb[1]), (bb[2], bb[1]), (bb[2], bb[3]), (bb[0], bb[3]))]
    return Polygon([(x + px, y + py) for px, py in pts])


ROWS_ALL = ("top", "bottom")   # legacy alias; pin lists are the truth now


NOVIA_CLEAR = 0.2      # trace edge to other-net copper (the netclass minimum)
NOVIA_ESCAPE = 0.5     # how far past the unit bbox a dogleg steps out


def _point_in_rings(rings, x: float, y: float) -> bool:
    """Even-odd point-in-polygon over a ring list (exterior first, holes after)."""
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            if (y1 > y) != (y2 > y):
                t = (y - y1) / (y2 - y1)
                if x < x1 + t * (x2 - x1):
                    inside = not inside
    return inside


def _seg_dist(a, b, c, d) -> float:
    """Shortest distance between segments a-b and c-d."""
    if _segments_cross(a, b, c, d):
        return 0.0

    def pt_seg(p, s, e):
        dx, dy = e[0] - s[0], e[1] - s[1]
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - s[0]) * dx + (p[1] - s[1]) * dy) / L))
        qx, qy = s[0] + t * dx, s[1] + t * dy
        return ((p[0] - qx) ** 2 + (p[1] - qy) ** 2) ** 0.5

    return min(pt_seg(a, c, d), pt_seg(b, c, d), pt_seg(c, a, b), pt_seg(d, a, b))


def _segments_cross(p1, p2, p3, p4) -> bool:
    """True if segment p1-p2 properly crosses segment p3-p4."""
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = side(p3, p4, p1), side(p3, p4, p2)
    d3, d4 = side(p1, p2, p3), side(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _quad_seg(a, b, w: float) -> list:
    """Rectangle of width w around segment a-b (both already board mm)."""
    (ax, ay), (bx, by) = a, b
    nx, ny = -(by - ay), bx - ax
    ln = (nx * nx + ny * ny) ** 0.5 or 1.0
    nx, ny = nx / ln * w / 2, ny / ln * w / 2
    return [(ax + nx, ay + ny), (bx + nx, by + ny),
            (bx - nx, by - ny), (ax - nx, ay - ny)]


def _unit_copper_quads(led: Led, safe, skip_start: bool,
                       start_pad: str | None = None):
    """A unit's real copper as quads: four pads plus the anode trace.

    Real pad sizes, not the inflated art keepouts: this is what a via-less
    power trace has to stay 0.2 mm clear of. With skip_start the pad the
    trace leaves from is dropped (it shares the trace's net): the side's
    power pad by default, or `start_pad` ("res_in"/"led_k") when the caller
    is routing a different run than the classic one, like a front unit's
    CLK supply, which leaves from res_in rather than the cathode.
    """
    g = led_geometry(led)
    p, rp = PKG[g["pkg"]], PKG[res_pkg(g["pkg"])]
    cx, cy = clamp_led_obj(led, safe)
    ang = led.rot
    front = led.side != "back"

    def tb(dx, dy):
        rx, ry = _r(dx, dy, ang)
        return cx + rx, cy + ry

    apts = unit_trace_pts(led, "a", safe)
    out = [_quad_seg(a, b, TRACK_W) for a, b in zip(apts, apts[1:])]
    for off, w, h, extra, key, is_start in (
        (g["led_k"], p["pw"], p["ph"], g.get("led_rot", 0.0), "led_k", front),
        (g["led_a"], p["pw"], p["ph"], g.get("led_rot", 0.0), "led_a", False),
        (g["res_in"], rp["pw"], rp["ph"], g.get("res_rot", 0.0), "res_in", not front),
        (g["res_out"], rp["pw"], rp["ph"], g.get("res_rot", 0.0), "res_out", False),
    ):
        if skip_start and (key == start_pad if start_pad else is_start):
            continue  # the pad the trace leaves from shares its net
        pts = []
        for qx, qy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
            ox, oy = _r(qx, qy, extra) if extra else (qx, qy)
            pts.append(tb(off[0] + ox, off[1] + oy))
        out.append(pts)
    if g["hole"]:
        cxh, cyh = tb(0.0, 0.0)
        out.append(_round_hazard(cxh, cyh, g["hole"] / 2 + 0.2))
    return out


def _unit_via_quads(led: Led, safe, clk, stub: bool = True) -> list:
    """A unit's power via collar (+ its stub legs) as route hazards.

    Empty for units that have no via. The classic runs never need these:
    every via on a layer carries the same net as that layer's via-less runs
    (front vias drop GND, back vias fetch 3V3), so a crossing was legal by
    construction. A CLK supply run is a different net on the same layer,
    and the barrels (which penetrate BOTH layers) become real hazards.
    `stub` includes the pad-to-via trace legs, which exist only on the
    unit's own mounting face.
    """
    front = led.side != "back"
    if led.novia or (clk is not None and led.clk and not front):
        return []  # its power runs as a trace instead; no via, no stub
    g = led_geometry(led)
    cx, cy = clamp_led_obj(led, safe)
    vo = g["via_front"] if front else g["via_back"]
    rx, ry = _r(vo[0], vo[1], led.rot)
    out = [_round_hazard(cx + rx, cy + ry, VIA_SIZE / 2 + NOVIA_CLEAR)]
    if stub:
        vpts = unit_trace_pts(led, "v", safe)
        out += [_quad_seg(a, b, TRACK_W) for a, b in zip(vpts, vpts[1:])]
    return out


def _expanded_corners(led: Led, safe, margin: float) -> list:
    """Corners of a unit's bbox grown by margin: the router's waypoints."""
    g = led_geometry(led)
    bb = g["bbox"]
    cx, cy = clamp_led_obj(led, safe)
    out = []
    for qx, qy in ((bb[0] - margin, bb[1] - margin), (bb[2] + margin, bb[1] - margin),
                   (bb[2] + margin, bb[3] + margin), (bb[0] - margin, bb[3] + margin)):
        rx, ry = _r(qx, qy, led.rot)
        out.append((cx + rx, cy + ry))
    return out


NOVIA_EDGE = 0.2 + TRACK_W / 2   # trace centre to board edge


# tan(pi/8), built from a square root so Python and the browser agree to the
# last bit: IEEE-754 pins sqrt exactly, while cos/sin may differ by an ulp,
# and a one-ulp disagreement is enough to send the two routers down different
# paths on a borderline clearance test.
_OCT_T = 2.0 ** 0.5 - 1.0


def _round_hazard(cx: float, cy: float, r: float) -> list:
    """Octagon just containing a circle of radius r (edges tangent to it).

    Connector pads and via barrels are round; bounding them with a square
    claims ~0.45 mm of clearance that isn't there, which needlessly rejects
    45-degree corners near a pad and pushes routes wide.
    """
    t = r * _OCT_T
    return [(cx + r, cy + t), (cx + t, cy + r), (cx - t, cy + r), (cx - r, cy + t),
            (cx - r, cy - t), (cx - t, cy - r), (cx + t, cy - r), (cx + r, cy - t)]


# Window/art keepout margin round a via barrel: the netclass copper-to-copper
# minimum. Deliberately tighter than POUR_CLEARANCE: the plane a same-net
# via keeps inside this collar only has to join its barrel to the bridge that
# feeds it, and the FILL still clears other-net vias by the pour rule no
# matter how close a window is allowed to erase.
VIA_COLLAR_CLEAR = 0.2


# A unit 16-gon from square roots alone; like OCT_T above, sqrt is pinned
# exactly by IEEE-754 while cos/sin may differ by an ulp between here and the
# JS mirror, and these pieces feed the bridge scan where an ulp can flip a
# route. cos/sin of 22.5° and the 1/cos(11.25°) circumradius factor all have
# nested-sqrt closed forms.
_C16 = (2 + 2 ** 0.5) ** 0.5 / 2
_S16 = (2 - 2 ** 0.5) ** 0.5 / 2
_H45 = 2 ** 0.5 / 2
_R16 = 2 / (2 + (2 + 2 ** 0.5) ** 0.5) ** 0.5
_HEXADECAGON = ((1.0, 0.0), (_C16, _S16), (_H45, _H45), (_S16, _C16),
                (0.0, 1.0), (-_S16, _C16), (-_H45, _H45), (-_C16, _S16),
                (-1.0, 0.0), (-_C16, -_S16), (-_H45, -_H45), (-_S16, -_C16),
                (0.0, -1.0), (_S16, -_C16), (_H45, -_H45), (_C16, -_S16))


def _via_collar(cx: float, cy: float) -> list:
    """16-gon just containing the plane a via keeps on a face.

    The art/window keepout round a via barrel. Sixteen sides instead of
    `_round_hazard`'s eight so the surviving patch of plane renders as the
    round pad it effectively is. Through a window the old octagon read as
    a mysterious oversized pad (0.76 mm to its corners); this collar stops
    at 0.55 mm flat-to-flat. Mirrored by viaCollar in index.html.
    """
    r = (VIA_SIZE / 2 + VIA_COLLAR_CLEAR) * _R16
    return [(cx + r * ux, cy + r * uy) for ux, uy in _HEXADECAGON]


def _knees45(a, b):
    """Corner options that turn leg a-b into an axis run plus a 45 diagonal.

    Empty when the leg is already horizontal, vertical or exactly diagonal.
    Board houses and KiCad's own router both avoid square corners, so a
    trace reads as hand-routed only if every turn is two 45s.
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) < 1e-9 or abs(dy) < 1e-9 or abs(abs(dx) - abs(dy)) < 1e-9:
        return []
    m = min(abs(dx), abs(dy))
    sx = 1.0 if dx > 0 else -1.0
    sy = 1.0 if dy > 0 else -1.0
    # Leave along the long axis first, then break to 45: squarer exit from a
    # pad. Diagonal-first is the fallback when that corner is blocked.
    axis_first = ((a[0] + sx * (abs(dx) - m), a[1]) if abs(dx) > abs(dy)
                  else (a[0], a[1] + sy * (abs(dy) - m)))
    return [axis_first, (a[0] + sx * m, a[1] + sy * m)]


def mitre45(pts, ok):
    """Rebuild a polyline so every corner is two 45s. ok(a, b) vets each leg.

    Legs that cannot be mitred cleanly stay as they were, so this can only
    improve a route, never break one.
    """
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        for knee in _knees45(a, b):
            if ok(a, knee) and ok(knee, b):
                out += [knee, b]
                break
        else:
            out.append(b)
    # drop any zero-length hops the corner maths produced
    keep = [out[0]]
    for q in out[1:]:
        if abs(q[0] - keep[-1][0]) > 1e-9 or abs(q[1] - keep[-1][1]) > 1e-9:
            keep.append(q)
    return keep


def soften45(pts, ok):
    """Ease every corner turning more than 45 degrees. ok(a, b) vets legs.

    mitre45 shapes each leg but says nothing about the joint between legs,
    so a route forced to approach its pad from the far side can fold back
    on itself there. A fold-back sharper than a hairpin is pure stub copper
    and is cut outright; any other sharp corner is chamfered along its
    bisector, which halves the turn, so repeated sweeps bring even a
    near-reversal under 45. A chamfer ok() rejects is retried shorter and
    then kept as it was: like mitre45 this can only improve a route.

    Mirrored by soften45 in index.html; sqrt-only maths on purpose, exactly
    like the polygon tables above, so the two stay bit-identical.
    """
    pts = list(pts)
    for _sweep in range(6):
        changed = False
        i = 1
        while i + 1 < len(pts):
            a, c, b = pts[i - 1], pts[i], pts[i + 1]
            v1x, v1y = c[0] - a[0], c[1] - a[1]
            v2x, v2y = b[0] - c[0], b[1] - c[1]
            l1 = (v1x * v1x + v1y * v1y) ** 0.5
            l2 = (v2x * v2x + v2y * v2y) ** 0.5
            dot = v1x * v2x + v1y * v2y
            # Zero-length legs and hairpins both collapse to dropping the
            # corner: the trace already covers the straight remainder.
            if l1 < 1e-9 or l2 < 1e-9 or dot <= -(1 - 1e-6) * l1 * l2:
                del pts[i]
                changed = True
                continue
            if dot >= (_H45 - 1e-6) * l1 * l2:
                i += 1  # already 45 or gentler
                continue
            for f in (1.0, 0.5, 0.25):
                t = f * min(0.6, l1 / 2, l2 / 2)
                p = (c[0] - v1x / l1 * t, c[1] - v1y / l1 * t)
                q = (c[0] + v2x / l2 * t, c[1] + v2y / l2 * t)
                # a-p and q-b are subsegments of vetted legs, so only the
                # bridge needs checking.
                if ok(p, q):
                    pts[i:i + 1] = [p, q]
                    changed = True
                    i += 1
                    break
            i += 1
        if not changed:
            break
    return pts


def unit_trace_pts(led: Led, which: str, safe=None) -> list:
    """Board-mm polyline of a unit's internal trace, through its bends.

    which = "a" is the resistor-output-to-LED-anode link; "v" is the
    pad-to-power-via stub (front: cathode to via, back: resistor input to
    via). Without bends this is the straight two-point segment every
    consumer used to assume. Hand-placed bends are kept exactly where they
    were put and only the corners between them are softened into 45s, the
    same contract as the via-less run's nodes. Mirrored by unitTracePts in
    index.html.
    """
    g = led_geometry(led)
    cx, cy = clamp_led_obj(led, safe)
    front = led.side != "back"

    def tb(off):
        rx, ry = _r(off[0], off[1], led.rot)
        return (cx + rx, cy + ry)

    if which == "a":
        a, b = tb(g["res_out"]), tb(g["led_a"])
        bends = led.anodes
    else:
        a = tb(g["led_k"] if front else g["res_in"])
        b = tb(g["via_front"] if front else g["via_back"])
        bends = led.vnodes
    if not bends:
        return [a, b]
    pts = [a] + [(float(px), float(py)) for px, py in bends] + [b]
    return mitre45(pts, lambda _u, _v: True)


def novia_term(led: Led, leds=(), pins=ALL_PINS, safe=None, clk=None):
    """Resolve led.term to ((x, y), target_led | None), or None for auto.

    None means "route to the nearest pad", both when no terminal was chosen
    and when the chosen one is invalid: a pad on the wrong net or a dropped
    pin, a unit on the other face (its SMD pads have no copper on this
    layer), a back unit whose supply pad now carries CLK (chaining 3V3 onto
    it would short the two rails), or a unit chain that loops back on itself
    and so never reaches a plated hole. Falling back matches how every other
    bad parameter is handled here, and the canvas never offers those choices
    in the first place.
    """
    if not led.novia or not led.term:
        return None
    if clk is not None and led.side == "back" and led.clk:
        return None  # its run goes to the CLK hookup; terms don't apply
    net = "GND" if led.side != "back" else "3V3"
    kind, ref = led.term[0], led.term[1]
    if kind == "pad":
        for num, px, py, pnet, _row in CONNECTOR_PADS:
            if num == ref and pnet == net and num in pins:
                return (px, py), None
        return None
    if kind != "unit":
        return None
    leds = list(leds)
    try:
        k = int(ref)
        target = leds[k]
    except (TypeError, ValueError, IndexError):
        return None
    if k < 0 or target is led or target.side != led.side:
        return None
    if clk is not None and net == "3V3" and target.clk:
        return None  # that unit's supply pad carries CLK now, not 3V3
    # A chain has to bottom out at a plated hole. Every link that is itself
    # invalid falls back to a connector pad (this same function), so the only
    # way a chain never lands is a true cycle: follow the links and refuse
    # those. Everything else (a link that turns out unroutable, a stranded
    # pour) stays resolve_novia's judgement, exactly as without a terminal.
    seen = {id(led), id(target)}
    cur = target
    while cur.novia and cur.term and cur.term[0] == "unit":
        if clk is not None and cur.side == "back" and cur.clk:
            break  # its supply run lands on a plated hole; chain bottoms out
        try:
            nxt = leds[int(cur.term[1])]
        except (TypeError, ValueError, IndexError):
            break  # invalid link: that unit will route to a pad instead
        if int(cur.term[1]) < 0 or nxt is cur or nxt.side != cur.side:
            break
        if clk is not None and cur.side == "back" and nxt.clk:
            break  # invalid link (CLK supply pad): that unit routes to a pad
        if id(nxt) in seen:
            return None  # a loop feeds nothing
        seen.add(id(nxt))
        cur = nxt
    g = led_geometry(target)
    tx, ty = clamp_led_obj(target, safe)
    off = g["led_k"] if net == "GND" else g["res_in"]
    ox, oy = _r(off[0], off[1], target.rot)
    return (tx + ox, ty + oy), target


def _route_run(start, targets, net, bends, hazards, waypoints, outline,
               term_flag: bool):
    """The shared power-trace router behind novia, CLK and link runs.

    Finds the shortest clear polyline from `start` to the first reachable
    entry of `targets`, staying NOVIA_CLEAR off every hazard quad and
    NOVIA_EDGE off the outline. Hand-placed `bends` win outright: the point
    of dragging them is to choose the path yourself. Every leg is still
    checked, and a bad one is flagged so the UI can say so rather than ship
    a shorted trace.

    Returns {"pts": [board mm, ...], "net": str, "pad": (x, y)}, plus
    "manual"/"tight"/"term" flags exactly as novia_route always did.
    """
    rings = outline if outline else [
        [(OUTLINE[0], OUTLINE[1]), (OUTLINE[2], OUTLINE[1]),
         (OUTLINE[2], OUTLINE[3]), (OUTLINE[0], OUTLINE[3])]
    ]
    edges = [(ring[i], ring[(i + 1) % len(ring)])
             for ring in rings for i in range(len(ring))]

    def on_board(pt) -> bool:
        return (_point_in_rings(rings, *pt)
                and all(_seg_dist(pt, pt, a, b) >= NOVIA_EDGE for a, b in edges))

    def clear(a, b) -> bool:
        run = _quad_seg(a, b, TRACK_W)
        if any(_quads_overlap(run, h, NOVIA_CLEAR) for h in hazards):
            return False
        # Copper this close to the outline trips copper_edge_clearance, and a
        # leg that leaves the board entirely is worse still.
        return all(_seg_dist(a, b, e0, e1) >= NOVIA_EDGE for e0, e1 in edges)

    if bends:
        pad = targets[0]
        pts = [start] + [(float(x), float(y)) for x, y in bends] + [pad]
        bad = not all(clear(a, b) for a, b in zip(pts, pts[1:]))
        # The bends stay exactly where they were put; only the corners between
        # them are softened into 45s.
        out = {"pts": mitre45(pts, clear), "net": net, "pad": pad, "manual": True}
        if term_flag:
            out["term"] = True
        if bad:
            out["tight"] = True
        return out

    def route_to(pad):
        """Shortest clear polyline from start to pad, or None."""
        if clear(start, pad):
            return [start, pad]
        # Dijkstra over the visibility graph. Small (a handful of units), so
        # this is instant and finds the best path whenever one exists.
        import heapq

        nodes = [start, pad] + waypoints
        n = len(nodes)
        dist = [float("inf")] * n
        prev = [-1] * n
        seen = [False] * n
        dist[0] = 0.0
        heap = [(0.0, 0)]
        while heap:
            d, u = heapq.heappop(heap)
            if seen[u]:
                continue
            seen[u] = True
            if u == 1:
                break
            for v in range(n):
                if seen[v] or v == u or not clear(nodes[u], nodes[v]):
                    continue
                nd = d + ((nodes[v][0] - nodes[u][0]) ** 2
                          + (nodes[v][1] - nodes[u][1]) ** 2) ** 0.5
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))
        if not seen[1]:
            return None
        chain, at = [], 1
        while at != -1:
            chain.append(nodes[at])
            at = prev[at]
        return chain[::-1]

    # Corners alone leave the graph too sparse to find a way around a unit
    # that sits between the pad and the trace's start, so seed a coarse grid
    # as well. Anything off-board or inside an obstacle is dropped.
    step = 1.5
    gx = OUTLINE[0] + step / 2
    while gx < OUTLINE[2]:
        gy = OUTLINE[1] + step / 2
        while gy < OUTLINE[3]:
            waypoints.append((gx, gy))
            gy += step
        gx += step
    waypoints = [w for w in waypoints
                 if on_board(w)
                 and not any(_quads_overlap(_quad_seg(w, w, TRACK_W), h, NOVIA_CLEAR)
                             for h in hazards)]

    for pad in targets:
        pts = route_to(pad)
        if pts is None:
            continue
        out = {"pts": soften45(mitre45(pts, clear), clear),
               "net": net, "pad": pad}
        if term_flag:
            out["term"] = True
        return out
    # Nothing legal reaches any pad: flag it so the UI can warn rather than
    # ship a board whose LED never lights.
    out = {"pts": [start, targets[0]], "net": net, "pad": targets[0], "tight": True}
    if term_flag:
        out["term"] = True
    return out


def _far_side_hazards(led: Led, others, safe) -> list:
    """Copper the far side's units still land on this unit's layer.

    TH pads and routed holes penetrate always. Power-via barrels penetrate
    too, but the classic runs may ignore them (a barrel always carries the
    running layer's own run net); a CLK run may not, and adds them itself
    through _unit_via_quads.
    """
    out = []
    for o in others:
        if o is led or o.side == led.side:
            continue
        og = led_geometry(o)
        if "drill" in PKG[og["pkg"]]:
            for x, y, r in th_pad_circles(o, safe):
                out.append(_round_hazard(x, y, r + NOVIA_CLEAR))
        if og["hole"]:
            ocx, ocy = clamp_led_obj(o, safe)
            out.append(_round_hazard(ocx, ocy, og["hole"] / 2 + NOVIA_CLEAR))
    return out


def _jumper_waypoints(clk) -> list:
    """Corners of the jumper's grown bounding box: router turning points."""
    if not clk or not clk["jumper"]:
        return []
    xs, ys = [], []
    for _net, cx, cy in clk["pads"]:
        for px, py in _jumper_pad_quad(clk, cx, cy):
            xs.append(px)
            ys.append(py)
    for v in (clk["via"], clk["v3via"]):
        if v:
            xs += [v[0] - VIA_SIZE / 2, v[0] + VIA_SIZE / 2]
            ys += [v[1] - VIA_SIZE / 2, v[1] + VIA_SIZE / 2]
    m = NOVIA_CLEAR + NOVIA_ESCAPE
    x0, y0, x1, y1 = min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# Route results, memoised. The routers are pure functions of their arguments,
# but they call EACH OTHER for hazard context (a CLK run dodges every classic
# run; the link dodges everything), and the webapp asks for the same routes
# once per keepout face, once per zone fill, once per bridge scan and once at
# emission. Without this cache a two-unit CLK+novia board multiplied out to
# ~12 s per download; with it the same board generates in well under a second.
# Keys are reprs of every input (dataclass reprs carry every field), so any
# change that could move a route misses the cache. Bounded: cleared rather
# than evicted; route dicts are treated as immutable by every caller.
_ROUTE_CACHE: dict = {}


def _route_key(tag, led, pins, safe, others, outline, term, clk):
    return (tag, repr(led), tuple(pins), repr(safe),
            tuple(repr(o) for o in others), repr(outline), repr(term),
            repr(clk))


def novia_route(led: Led, pins=ALL_PINS, safe=None, others=(),
                outline=None, term=None, clk=None):
    """Where a via-less unit runs its power trace, or None.

    A unit sits in one pour and needs the other net. Normally it drops
    through its own via; with novia set it runs a trace across its own layer
    to the nearest connector pad carrying that net instead. Those pads are
    plated through, so landing on one reaches the far pour exactly as a via
    would.

    Front units chase GND from the cathode, back units chase 3V3 from the
    resistor's input, the same net the via used to fetch. Ties break on
    CONNECTOR_PADS order so the web preview picks the same pad.

    `term` (from novia_term) overrides the destination: the run goes to that
    exact point: a chosen connector pad, or another unit's same-net pad so
    several runs can share one path. A chosen destination is never traded
    for a reachable one; an unreachable choice comes back flagged "tight"
    so the UI can say so, because silently landing somewhere else would make
    the preview lie about the board.

    With `clk` (from clk_info) a BACK unit whose Led.clk is set routes its
    supply here too, whether or not novia is: the trace chases the CLK
    hookup (pin 9 or the jumper's rail via, both plated holes) instead of a
    3V3 pad, replacing the unit's 3V3 via exactly like a novia run would.
    Its bends come from cnodes, and terms don't apply. A FRONT CLK unit's
    supply is clk_route's job; this function still handles its GND side.

    Returns {"pts": [board mm, ...], "net": str, "pad": (x, y)}. The run is a
    straight shot where that clears the unit's own copper, and doglegs across
    the unit's short axis where it does not: with the resistor on the far
    side of the LED from the pad, a straight run skims its own anode pad by
    0.19 mm against a 0.2 mm rule.
    """
    front = led.side != "back"
    supply = clk if (clk is not None and led.clk and not front) else None
    if supply is None and not led.novia:
        return None
    key = _route_key("novia", led, pins, safe, others, outline, term, clk)
    if key in _ROUTE_CACHE:
        return _ROUTE_CACHE[key]
    g = led_geometry(led)
    cx, cy = clamp_led_obj(led, safe)
    ang = led.rot

    def to_board(dx, dy):
        rx, ry = _r(dx, dy, ang)
        return cx + rx, cy + ry

    if supply is not None:
        net = supply["net"]
        start = to_board(*g["res_in"])
        bends = led.cnodes
        term = None
        targets = [supply["back"]]
    else:
        net = "GND" if front else "3V3"
        bends = led.nodes
        s_off = g["led_k"] if front else g["res_in"]
        start = to_board(*s_off)
        # A front-side through-hole LED needs nothing at all: its cathode
        # lead is plated through to the back face, where the GND pour already
        # is. This is the cleanest via-less unit there is: no extra copper,
        # no channel cut across the pour. (A back-side unit still has to
        # fetch its supply for the SMD resistor, so it gets a trace.)
        if front and "drill" in PKG[g["pkg"]]:
            return {"pts": [start], "net": net, "pad": None, "direct": True}
        if term is not None:
            targets = [term[0]]
        else:
            targets = sorted(
                ((px, py) for num, px, py, pnet, _row in CONNECTOR_PADS
                 if pnet == net and num in pins),
                key=lambda t: (t[0] - start[0]) ** 2 + (t[1] - start[1]) ** 2)
            if not targets:
                return None  # no kept pin carries this net; the caller warns

    # Everything the run has to stay clear of: this unit's own copper bar the
    # pad it leaves from, every other unit sharing this layer, and any
    # connector pad on a different net (VBATT and NC included; landing
    # on those would be worse than a short). A chained-to unit's same-net pad
    # is the destination, so it is dropped the same way the start pad is.
    term_led = term[1] if term else None
    hazards = _unit_copper_quads(led, safe, skip_start=True,
                                 start_pad="res_in" if supply else None)
    siblings = [o for o in others if o is not led and o.side == led.side]
    for o in siblings:
        hazards += _unit_copper_quads(o, safe, skip_start=o is term_led)
    # A unit on the far side still lands copper on this layer wherever its
    # pads are plated through, and a routed hole is a hole on every layer.
    hazards += _far_side_hazards(led, others, safe)
    for num, px, py, pnet, _row in connector_pads(clk is not None):
        if num not in pins or pnet == net:
            continue
        hazards.append(_round_hazard(px, py, 0.875 + NOVIA_CLEAR))
    if clk is not None:
        # The jumper's other-net pads (and, through them, its vias) are
        # copper like any other; a run on any net but theirs keeps off.
        hazards += _jumper_copper_quads(clk, net,
                                        "front" if front else "back")
    if supply is not None:
        # A CLK supply run is a DIFFERENT net from everything else on its
        # layer, so the same-net liberties the classic runs enjoy are gone:
        # sibling via barrels and stubs (all 3V3 here), the far side's
        # barrels (GND), and sibling 3V3 runs are all real hazards now.
        for o in siblings:
            hazards += _unit_via_quads(o, safe, clk)
        for o in others:
            if o is not led and o.side != led.side:
                hazards += _unit_via_quads(o, safe, clk, stub=False)
        for o in siblings:
            if not (o.novia and not o.clk):
                continue  # a sibling CLK run shares this net; crossing is legal
            r2 = novia_route(o, pins, safe, others, outline=outline,
                             term=novia_term(o, others, pins, safe, clk), clk=clk)
            if r2 and len(r2["pts"]) > 1:
                hazards += [_quad_seg(a, b, TRACK_W)
                            for a, b in zip(r2["pts"], r2["pts"][1:])]

    # Turning points worth considering: the corners of every obstacle's grown
    # bounding box.
    waypoints = list(_expanded_corners(led, safe, NOVIA_ESCAPE))
    for o in siblings:
        waypoints += _expanded_corners(o, safe, NOVIA_ESCAPE)
    for num, px, py, pnet, _row in connector_pads(clk is not None):
        if num not in pins or pnet == net:
            continue
        r = 0.875 + NOVIA_CLEAR + NOVIA_ESCAPE
        waypoints += [(px - r, py - r), (px + r, py - r),
                      (px + r, py + r), (px - r, py + r)]
    if clk is not None:
        waypoints += _jumper_waypoints(clk)

    if len(_ROUTE_CACHE) > 2048:
        _ROUTE_CACHE.clear()
    out = _route_run(start, targets, net, bends, hazards, waypoints,
                     outline, term is not None)
    _ROUTE_CACHE[key] = out
    return out


def clk_route(led: Led, pins=ALL_PINS, safe=None, others=(),
              outline=None, clk=None):
    """A front CLK unit's supply run to the jumper rail or pin 9, or None.

    The front pour is 3V3, so a front unit that blinks cannot feed its
    resistor from the plane: the supply arrives as a routed F.Cu trace from
    res_in to the jumper's centre pad (jumper style) or pin 9 (trace style).
    The unit's GND side is untouched: its via (or novia run) stays.

    Routed AFTER every classic run: this net is a stranger on its layer, so
    it dodges the GND runs, every sibling's via barrel and stub, and the
    jumper's other pads; those were all routed without knowing about it,
    and one side dodging is all the clearance rule needs.
    """
    if clk is None or not led.clk or led.side == "back":
        return None
    key = _route_key("clk", led, pins, safe, others, outline, None, clk)
    if key in _ROUTE_CACHE:
        return _ROUTE_CACHE[key]
    g = led_geometry(led)
    cx, cy = clamp_led_obj(led, safe)
    rx, ry = _r(*g["res_in"], led.rot)
    start = (cx + rx, cy + ry)
    net = clk["net"]
    targets = [clk["front"]]

    hazards = _unit_copper_quads(led, safe, skip_start=True, start_pad="res_in")
    hazards += _unit_via_quads(led, safe, clk)
    siblings = [o for o in others if o is not led and o.side == led.side]
    for o in siblings:
        hazards += _unit_copper_quads(o, safe, skip_start=False)
        hazards += _unit_via_quads(o, safe, clk)
    hazards += _far_side_hazards(led, others, safe)
    for o in others:
        if o is not led and o.side != led.side:
            hazards += _unit_via_quads(o, safe, clk, stub=False)
    for num, px, py, pnet, _row in connector_pads(True):
        if num not in pins or pnet == net:
            continue
        hazards.append(_round_hazard(px, py, 0.875 + NOVIA_CLEAR))
    hazards += _jumper_copper_quads(clk, net, "front")
    own = novia_route(led, pins, safe, others, outline=outline,
                      term=novia_term(led, others, pins, safe, clk), clk=clk)
    runs = [own] + [
        novia_route(o, pins, safe, others, outline=outline,
                    term=novia_term(o, others, pins, safe, clk), clk=clk)
        for o in siblings if o.novia and not (o.clk and o.side == "back")]
    for r2 in runs:
        if r2 and len(r2["pts"]) > 1:
            hazards += [_quad_seg(a, b, TRACK_W)
                        for a, b in zip(r2["pts"], r2["pts"][1:])]

    waypoints = list(_expanded_corners(led, safe, NOVIA_ESCAPE))
    for o in siblings:
        waypoints += _expanded_corners(o, safe, NOVIA_ESCAPE)
    for num, px, py, pnet, _row in connector_pads(True):
        if num not in pins or pnet == net:
            continue
        r = 0.875 + NOVIA_CLEAR + NOVIA_ESCAPE
        waypoints += [(px - r, py - r), (px + r, py - r),
                      (px + r, py + r), (px - r, py + r)]
    waypoints += _jumper_waypoints(clk)

    if len(_ROUTE_CACHE) > 2048:
        _ROUTE_CACHE.clear()
    out = _route_run(start, targets, net, led.cnodes, hazards, waypoints,
                     outline, False)
    _ROUTE_CACHE[key] = out
    return out


def clk_link(leds=(), pins=ALL_PINS, safe=None, outline=None, clk=None):
    """The trace from the jumper's CLK pad to pin 9, on the jumper's own
    face, or None.

    Only the jumper style has one (the trace style lands the runs on pin 9
    directly). Routed LAST, after every unit run, so it dodges them all:
    the units' pads, vias, stubs and runs on this face, whatever the other
    face's units land through the board, the connector pads on other nets,
    and the jumper's own rail and 3V3 pads.
    """
    if clk is None or not clk["jumper"]:
        return None
    leds = list(leds)
    key = _route_key("link", None, pins, safe, leds, outline, None, clk)
    if key in _ROUTE_CACHE:
        return _ROUTE_CACHE[key]
    net = "CLK"
    jside = clk["side"]
    start = (clk["pads"][0][1], clk["pads"][0][2])
    targets = [clk["pin9"]]

    hazards = []
    for led in leds:
        if led.side == jside:
            hazards += _unit_copper_quads(led, safe, skip_start=False)
            hazards += _unit_via_quads(led, safe, clk)
        else:
            g = led_geometry(led)
            if "drill" in PKG[g["pkg"]]:
                for x, y, r in th_pad_circles(led, safe):
                    hazards.append(_round_hazard(x, y, r + NOVIA_CLEAR))
            if g["hole"]:
                ocx, ocy = clamp_led_obj(led, safe)
                hazards.append(_round_hazard(ocx, ocy, g["hole"] / 2 + NOVIA_CLEAR))
            hazards += _unit_via_quads(led, safe, clk, stub=False)
    for num, px, py, pnet, _row in connector_pads(True):
        if num not in pins or pnet == net:
            continue
        hazards.append(_round_hazard(px, py, 0.875 + NOVIA_CLEAR))
    hazards += _jumper_copper_quads(clk, net, jside)
    for led in leds:
        if led.side != jside:
            continue  # its runs are copper on the other face, not this one
        runs = [novia_route(led, pins, safe, leds, outline=outline,
                            term=novia_term(led, leds, pins, safe, clk), clk=clk),
                clk_route(led, pins, safe, leds, outline=outline, clk=clk)]
        for r2 in runs:
            if r2 and r2["net"] != net and len(r2["pts"]) > 1:
                hazards += [_quad_seg(a, b, TRACK_W)
                            for a, b in zip(r2["pts"], r2["pts"][1:])]

    waypoints = []
    for led in leds:
        waypoints += _expanded_corners(led, safe, NOVIA_ESCAPE)
    for num, px, py, pnet, _row in connector_pads(True):
        if num not in pins or pnet == net:
            continue
        r = 0.875 + NOVIA_CLEAR + NOVIA_ESCAPE
        waypoints += [(px - r, py - r), (px + r, py - r),
                      (px + r, py + r), (px - r, py + r)]
    waypoints += _jumper_waypoints(clk)

    if len(_ROUTE_CACHE) > 2048:
        _ROUTE_CACHE.clear()
    out = _route_run(start, targets, net, clk["nodes"], hazards, waypoints,
                     outline, False)
    _ROUTE_CACHE[key] = out
    return out


def clk_v3_link(leds=(), pins=ALL_PINS, safe=None, outline=None, clk=None):
    """The trace feeding a back-side jumper's 3V3 pad from a 3V3 connector
    pin on its own face, or None.

    Exists only while the jumper sits on the back with its dedicated via
    turned off (BadgeSpec.jumper_via False): the pin's plated hole carries
    the front pour's net, so a plain same-face trace is all the steady side
    needs. Routed after everything else, the CLK link included: this net is
    3V3, so it dodges every run and pad on any other net and may freely
    cross the pour's own copper.
    """
    if clk is None or not clk["jumper"] or not clk.get("v3link"):
        return None
    leds = list(leds)
    key = _route_key("v3link", None, pins, safe, leds, outline, None, clk)
    if key in _ROUTE_CACHE:
        return _ROUTE_CACHE[key]
    net = "3V3"
    jside = clk["side"]
    start = (clk["pads"][2][1], clk["pads"][2][2])
    # A hand-picked destination pin wins outright and is never traded for a
    # reachable one (an unreachable choice comes back flagged tight), the
    # same contract as a via-less run's chosen terminal. An invalid choice
    # (wrong net, dropped pin) falls back to nearest-first.
    term = next(((px, py) for num, px, py, pnet, _row in connector_pads(True)
                 if num == clk.get("v3pin") and pnet == net and num in pins),
                None)
    if term is not None:
        targets = [term]
    else:
        targets = sorted(
            ((px, py) for num, px, py, pnet, _row in connector_pads(True)
             if pnet == net and num in pins),
            key=lambda t: (t[0] - start[0]) ** 2 + (t[1] - start[1]) ** 2)
        if not targets:
            return None  # no 3V3 pin kept at all; power_missing() says so

    hazards = []
    for led in leds:
        if led.side == jside:
            hazards += _unit_copper_quads(led, safe, skip_start=False)
            hazards += _unit_via_quads(led, safe, clk)
        else:
            g = led_geometry(led)
            if "drill" in PKG[g["pkg"]]:
                for x, y, r in th_pad_circles(led, safe):
                    hazards.append(_round_hazard(x, y, r + NOVIA_CLEAR))
            if g["hole"]:
                ocx, ocy = clamp_led_obj(led, safe)
                hazards.append(_round_hazard(ocx, ocy, g["hole"] / 2 + NOVIA_CLEAR))
            hazards += _unit_via_quads(led, safe, clk, stub=False)
    for num, px, py, pnet, _row in connector_pads(True):
        if num not in pins or pnet == net:
            continue
        hazards.append(_round_hazard(px, py, 0.875 + NOVIA_CLEAR))
    hazards += _jumper_copper_quads(clk, net, jside)
    for led in leds:
        if led.side != jside:
            continue  # its runs are copper on the other face, not this one
        runs = [novia_route(led, pins, safe, leds, outline=outline,
                            term=novia_term(led, leds, pins, safe, clk), clk=clk),
                clk_route(led, pins, safe, leds, outline=outline, clk=clk)]
        for r2 in runs:
            if r2 and r2["net"] != net and len(r2["pts"]) > 1:
                hazards += [_quad_seg(a, b, TRACK_W)
                            for a, b in zip(r2["pts"], r2["pts"][1:])]
    lk = clk_link(leds, pins, safe, outline, clk)
    if lk is not None and len(lk["pts"]) > 1:
        hazards += [_quad_seg(a, b, TRACK_W)
                    for a, b in zip(lk["pts"], lk["pts"][1:])]

    waypoints = []
    for led in leds:
        waypoints += _expanded_corners(led, safe, NOVIA_ESCAPE)
    for num, px, py, pnet, _row in connector_pads(True):
        if num not in pins or pnet == net:
            continue
        r = 0.875 + NOVIA_CLEAR + NOVIA_ESCAPE
        waypoints += [(px - r, py - r), (px + r, py - r),
                      (px + r, py + r), (px - r, py + r)]
    waypoints += _jumper_waypoints(clk)

    if len(_ROUTE_CACHE) > 2048:
        _ROUTE_CACHE.clear()
    out = _route_run(start, targets, net, clk["v3nodes"], hazards, waypoints,
                     outline, term is not None)
    _ROUTE_CACHE[key] = out
    return out


def unit_copper_pieces(led: Led, safe=None, pins=ALL_PINS,
                       others=(), outline=None,
                       face: str | None = None,
                       clk=None) -> list[tuple[str, list]]:
    """Convex quads covering the unit's copper plus the margin art must clear.

    Pads inflated 0.5 mm per side (solder-mask-bridge rule + hand-soldering
    margin), the via as a square 0.45 mm clear of its barrel, traces as
    1.1 mm-wide rects (track 0.3 + 0.4 each side), and the reverse hole
    (+0.5). Labeled (board mm, unit-rotated); the web UI paints identical
    pieces, so art hugs units the same way in the preview and on the board.
    Much tighter than the old bounding-box rectangle.

    ``face`` ("front"/"back") asks for the pieces as they exist on that board
    face. It only changes a far-side ("LED on the other side") unit, whose
    copper is genuinely split across the board: the LED face carries just the
    LED pads, the power via and any routed hole, while on the resistor face
    the departed LED pads shrink to the two via barrels sunk in them; art
    and windows reclaim the rest of the old pad room instead of leaving a
    pad-shaped slab of copper on a face the part is not even on.
    """
    g = led_geometry(led)
    p = PKG[g["pkg"]]
    rp = PKG[res_pkg(g["pkg"])]
    x, y = clamp_led_obj(led, safe)
    ang = led.rot
    rrot = g.get("res_rot", 0.0)
    lrot = g.get("led_rot", 0.0)
    front = led.side != "back"

    def pt(dx: float, dy: float) -> tuple[float, float]:
        rx, ry = _r(dx, dy, ang)
        return x + rx, y + ry

    def quad_rect(cx: float, cy: float, w: float, h: float, extra: float = 0.0) -> list:
        corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        out = []
        for qx, qy in corners:
            ox, oy = _r(qx, qy, extra) if extra else (qx, qy)
            out.append(pt(cx + ox, cy + oy))
        return out

    pw, ph = p["pw"] + 1.0, p["ph"] + 1.0
    rw, rh = rp["pw"] + 1.0, rp["ph"] + 1.0
    vo = g["via_front"] if front else g["via_back"]
    pieces = [
        ("pad_led_k", quad_rect(*g["led_k"], pw, ph, lrot)),
        ("pad_led_a", quad_rect(*g["led_a"], pw, ph, lrot)),
        ("pad_res_in", quad_rect(*g["res_in"], rw, rh, rrot)),
        ("pad_res_out", quad_rect(*g["res_out"], rw, rh, rrot)),
    ]
    # One label for every leg: the consumers test membership, never count.
    apts = unit_trace_pts(led, "a", safe)
    pieces += [("trace_a", _quad_seg(a, b, 1.1)) for a, b in zip(apts, apts[1:])]
    route = novia_route(led, pins, safe, others, outline=outline,
                        term=novia_term(led, others, pins, safe, clk), clk=clk)
    if route:
        # No via to clear, but a long run to the connector pad that artwork
        # must keep off just the same: copper art touching it would short
        # the trace to the pour it crosses.
        pts = route["pts"]
        for n, (a, b) in enumerate(zip(pts, pts[1:])):
            pieces.append((f"trace_pad{n}" if n else "trace_pad",
                           _quad_seg(a, b, 1.1)))
    else:
        # A round collar round the barrel; see _via_collar for why it is a
        # 16-gon at the 0.2 mm netclass minimum rather than the old octagon
        # at the pour rule (whose corners reached 0.76 mm and left a patch of
        # plane that read as a mysterious oversized pad through a window).
        # Mirrored by unitCopperPieces in index.html: the preview draws
        # the same carve, and tests/test_browser.py holds the two in parity.
        pieces.append(("via", _via_collar(*pt(*vo))))
        vpts = unit_trace_pts(led, "v", safe)
        pieces += [("trace_stub", _quad_seg(a, b, 1.1))
                   for a, b in zip(vpts, vpts[1:])]
    # A front CLK unit's supply run: one more band of copper on its face
    # that artwork has to keep off. (A back CLK unit's supply IS `route`.)
    crun = clk_route(led, pins, safe, others, outline=outline, clk=clk)
    if crun and len(crun["pts"]) > 1:
        cpts = crun["pts"]
        pieces += [(f"trace_clk{n}" if n else "trace_clk",
                    _quad_seg(a, b, 1.1))
                   for n, (a, b) in enumerate(zip(cpts, cpts[1:]))]
    if g["hole"]:
        pieces.append(("hole", quad_rect(0.0, 0.0, g["hole"] + 1.0, g["hole"] + 1.0)))
    if "drill" in p:
        # A through-hole LED's silkscreen outline follows its lens, which
        # reaches well past the pads, so the pad quads above do NOT cover it
        # and artwork would print straight over the part's own silk (the fab
        # then clips whichever lost). Claim the body plus a silk margin.
        bw, bh = p["body"]
        lens = p.get("lens", 0.0)
        pieces.append(("silk_body", quad_rect(0.0, 0.0,
                                              max(bw, lens) + 0.8,
                                              max(bh, lens) + 0.8, lrot)))
    far = bool(led.farled) and not g["hole"] and "drill" not in p
    if face is not None and far:
        far_face = "back" if led.side != "back" else "front"
        if face == far_face:
            # Only the LED half crossed over: its pads (vias sunk in them),
            # the power via's barrel and a routed hole exist here; the
            # resistor, its pads and every trace stayed behind.
            keep = {"pad_led_k", "pad_led_a", "via", "hole"}
            pieces = [(lb, q) for lb, q in pieces if lb in keep]
        else:
            # The LED left this face: where its pads were there are only the
            # two via barrels carrying the nets through.
            pieces = [(lb, q) for lb, q in pieces
                      if lb not in ("pad_led_k", "pad_led_a")]
            for lb, off in (("padvia_k", g["led_k"]), ("padvia_a", g["led_a"])):
                pieces.append((lb, _via_collar(*pt(*off))))
    return pieces


def unit_copper_poly(led: Led, safe=None, pins=ALL_PINS, others=(),
                     outline=None, face: str | None = None, clk=None):
    """unit_copper_pieces as one shapely geometry (for art keepouts)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    return unary_union([Polygon(q) for _, q in
                        unit_copper_pieces(led, safe, pins, others, outline,
                                           face, clk)])


def th_pad_circles(led: Led, safe=None) -> list[tuple[float, float, float]]:
    """Board-mm (x, y, r) of a through-hole LED's pad annuli.

    Empty for SMD packages. TH pads exist on BOTH faces, so decor on the
    unit's far side must keep clear of these the way it clears reverse
    holes.
    """
    g = led_geometry(led)
    p = PKG[g["pkg"]]
    if "drill" not in p:
        return []
    x, y = clamp_led_obj(led, safe)
    out = []
    for off in (g["led_k"], g["led_a"]):
        rx, ry = _r(off[0], off[1], led.rot)
        out.append((x + rx, y + ry, p["pw"] / 2))
    return out


def _quads_overlap(a: list, b: list, gap: float = 0.0) -> bool:
    """SAT overlap for convex quads: the exact mirror of the UI's polysClear."""
    for poly in (a, b):
        n = len(poly)
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            nx, ny = y1 - y0, x0 - x1
            ln = (nx * nx + ny * ny) ** 0.5 or 1.0
            nx, ny = nx / ln, ny / ln
            da = [px * nx + py * ny for px, py in a]
            db = [px * nx + py * ny for px, py in b]
            if min(db) >= max(da) + gap or min(da) >= max(db) + gap:
                return False
    return True


def _ray_exit_edge(sx: float, sy: float, dx: float, dy: float, rings):
    """First outline crossing along (dx,dy) from (sx,sy): (distance, edge).

    Rings are closed point lists (exterior first, then holes); a hole
    boundary counts as an exit too, so bridges never span board cut-outs.
    The edge comes back with the distance because a pullback measured along
    the ray is not a clearance: only the angle the ray makes with the edge it
    meets turns one into the other.
    """
    best = None
    for ring in rings:
        n = len(ring)
        for i in range(n):
            px, py = ring[i]
            qx, qy = ring[(i + 1) % n]
            rx, ry = qx - px, qy - py
            den = rx * dy - ry * dx
            if abs(den) < 1e-12:
                continue
            ax, ay = px - sx, py - sy
            t = (rx * ay - ry * ax) / den
            s = (dx * ay - dy * ax) / den
            if t > 1e-9 and -1e-9 <= s <= 1 + 1e-9:
                if best is None or t < best[0]:
                    best = (t, ((px, py), (qx, qy)))
    return best


# Bridge traces: 0.3 mm copper from each unit to the pour's perimeter ring.
# They replace the old reserved 2 mm window corridor: glow/bare windows may
# now hug a unit, and the thin bridge stays visible where it crosses one.
BRIDGE_INSET = 0.8   # endpoint pullback from the outline (lands in the ring)
BRIDGE_MIN = 0.5     # shortest useful bridge
_BRIDGE_EXCLUDE = {"pad_res_in": ("pad_res_in",), "pad_led_k": ("pad_led_k",),
                   "via": ("via", "trace_stub")}


def _bridge_route(start, own_pieces, skip_labels, obstacles, rings):
    """First clear straight segment from `start` to the perimeter ring.

    Candidates are 16 compass directions ordered nearest-exit-first; a
    candidate survives if its 1.0 mm-wide swath (track + pour clearance)
    misses every obstacle quad. Deterministic: the web UI runs the same
    scan and draws the same segment. None when everything is blocked.
    """
    import math

    sx, sy = start
    own = [q for lbl, q in own_pieces if lbl not in skip_labels]
    cands = []
    for k in range(16):
        a = math.radians(k * 22.5)
        dx, dy = math.cos(a), math.sin(a)
        hit = _ray_exit_edge(sx, sy, dx, dy, rings)
        if hit is None:
            continue
        t, ((e0x, e0y), (e1x, e1y)) = hit
        # BRIDGE_INSET is a CLEARANCE (how far the endpoint has to sit back
        # from the outline), so it has to be taken perpendicular to the edge
        # the ray meets, not along the ray. The scan walks 16 directions
        # 22.5 deg apart, so a ray meeting a horizontal edge at 22.5 deg used
        # to pull back only 0.8*sin(22.5) = 0.306 mm of real clearance; less
        # TRACK_W/2 that is 0.156 mm of copper-to-edge against a 0.2 mm rule,
        # which is a fab reject on an utterly ordinary placement.
        ex_, ey_ = e1x - e0x, e1y - e0y
        elen = math.hypot(ex_, ey_)
        if elen < 1e-12:
            continue
        sin_a = abs(dx * ey_ - dy * ex_) / elen   # sin of ray-to-edge angle
        if sin_a < 1e-9:
            continue  # running along the edge; no pullback is enough
        ln = t - BRIDGE_INSET / sin_a
        if ln < BRIDGE_MIN:
            continue
        cands.append((t, k, dx, dy, ln))
    cands.sort(key=lambda c: (c[0], c[1]))
    for t, _k, dx, dy, ln in cands:
        ex, ey = sx + dx * ln, sy + dy * ln
        nx, ny = -dy * 0.5, dx * 0.5
        swath = [(sx + nx, sy + ny), (ex + nx, ey + ny),
                 (ex - nx, ey - ny), (sx - nx, sy - ny)]
        if any(_quads_overlap(swath, q) for q in own):
            continue
        if any(_quads_overlap(swath, q) for q in obstacles):
            continue
        return ((sx, sy), (ex, ey))
    return None


def resolve_novia(spec: BadgeSpec, safe=None) -> tuple[list, list[int]]:
    """Check every via-less unit can actually reach its power. -> (leds, bad).

    A via-less run cuts a channel across the pour on its own layer, and in a
    few placements that channel fences the pour's own connector pad onto an
    island: the board would then fail DRC with the unit's resistor
    unconnected. Only the filled copper can settle that, so each unit's route
    is judged against a real fill.

    The route always goes to the NEAREST pad carrying the net, the same
    choice the browser preview makes, so what is drawn is what gets built.
    Hunting for a further pad that happens to work salvages under 1% of
    placements and would make the preview lie, so unreachable units are
    reported instead; the caller refuses the download and says so.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union

    leds = list(spec.leds)
    clk = clk_info(spec)
    if not any(led.novia for led in leds) and clk is None:
        return leds, []
    if safe is None:
        safe = unit_safe(spec)
    problems: list[int] = []
    fills: dict = {}

    def islands(pour, layer):
        if (pour, layer) not in fills:
            fills[(pour, layer)] = _fill_geometry(pour, layer, spec)
        return fills[(pour, layer)]

    bridges = unit_bridges(spec, safe)

    for i, led in enumerate(leds):
        runs = []
        if led.novia or (clk is not None and led.clk and led.side == "back"):
            runs.append(novia_route(
                led, spec.pins, safe, leds, outline=spec.outline,
                term=novia_term(led, leds, spec.pins, safe, clk), clk=clk))
        runs.append(clk_route(led, spec.pins, safe, leds,
                              outline=spec.outline, clk=clk))
        runs = [r for r in runs if r is not None and not r.get("direct")]
        if not runs:
            continue  # nothing routed, so nothing can cut the pour
        if any(r.get("tight") for r in runs):
            problems.append(i)
            continue
        # The channel a run cuts can fence the pour's own connector pads
        # apart. That splits the whole RAIL, not just this unit (KiCad answers
        # with unconnected_items on the net), so the unit's contact and every
        # kept pad of its pour have to end up on one island. A CLK unit's
        # supply pad is not a pour contact at all (its run is real emitted
        # copper), so only the side that still feeds from the pour counts;
        # and with the jumper on this face's pour, its 3V3 pad has to stay
        # attached too, or bridging the jumper to steady would do nothing.
        front = led.side != "back"
        layer, pour = ("F.Cu", "3V3") if front else ("B.Cu", "GND")
        g = led_geometry(led)
        cx, cy = clamp_led_obj(led, safe)
        must = []
        if not front or not (clk is not None and led.clk):
            ox, oy = _r(*(g["res_in"] if front else g["led_k"]), led.rot)
            must.append((cx + ox, cy + oy))
        must += [(px, py) for num, px, py, pnet, _row in CONNECTOR_PADS
                 if pnet == pour and num in spec.pins]
        if front and clk is not None and clk["jumper"]:
            if clk["side"] == "front":
                must.append((clk["pads"][2][1], clk["pads"][2][2]))
            elif clk["v3via"]:
                must.append(clk["v3via"])
            # via off: the steady side is fed by an emitted trace to a 3V3
            # pin whose hole is already in the must list; the trace itself
            # is judged by its own tight flag, not by the fill.
        # The unit's perimeter bridge is real same-net copper on this layer
        # and can be the only thing joining its island to the plane; a
        # far-side LED's contact is just its via-in-pad collar, tied to the
        # ring by the bridge, and judging the fill without it refused boards
        # whose download was DRC-clean (while the 2D preview happily routed).
        seg = bridges.get(i, {}).get(layer)
        copper = unary_union(
            list(islands(pour, layer))
            + ([LineString(seg).buffer(TRACK_W / 2)] if seg else []))
        if not any(all(part.distance(Point(*m)) < 0.7 for m in must)
                   for part in getattr(copper, "geoms", [copper])):
            problems.append(i)

    # Then the question that check cannot ask: a channel is cut across a whole
    # pour, and the unit it fences off is very often SOMEBODY ELSE. Asking only
    # whether each via-less unit's own contact still reaches a pad missed the
    # case entirely: two via-less runs can jointly enclose a third unit's
    # ordinary via, and that third unit was never examined at all because it is
    # not via-less. So walk every unit's rail contacts too.
    for i, led in enumerate(leds):
        if i in problems:
            continue
        g = led_geometry(led)
        cx, cy = clamp_led_obj(led, safe)

        def at(off, cx=cx, cy=cy, led=led):
            rx, ry = _r(off[0], off[1], led.rot)
            return (cx + rx, cy + ry)

        front = led.side != "back"
        # The two rail terminals, exactly as unit_bridges anchors them: a
        # front unit feeds 3V3 into the resistor from the F.Cu pour and drops
        # GND through its via; a back unit is the mirror image.
        contacts = {
            "F.Cu": (at(g["res_in"]) if front else at(g["via_back"]), "3V3"),
            "B.Cu": (at(g["via_front"]) if front else at(g["led_k"]), "GND"),
        }
        if led.novia:
            # No via: the far rail arrives through the connector pad the run
            # lands on (or, for a front through-hole LED, through its own
            # plated lead), so there is no far-layer terminal to strand.
            contacts.pop("B.Cu" if front else "F.Cu", None)
        if clk is not None and led.clk:
            # The supply is a routed CLK trace now: a front unit's res_in no
            # longer touches the 3V3 pour, and a back unit's 3V3 via is gone.
            # Either way there is nothing of this unit left on F.Cu's rail.
            contacts.pop("F.Cu", None)
        for layer, (pt, pour) in contacts.items():
            pads = [(px, py) for num, px, py, pnet, _row in CONNECTOR_PADS
                    if pnet == pour and num in spec.pins]
            if not pads:
                continue  # no rail on this badge at all; power_missing() says so
            # The unit's perimeter bridge is real same-net copper on this
            # layer and can be the only thing joining its island to the plane.
            seg = bridges.get(i, {}).get(layer)
            copper = unary_union(
                list(islands(pour, layer))
                + ([LineString(seg).buffer(TRACK_W / 2)] if seg else []))
            if copper.is_empty or not any(
                    part.distance(Point(*pt)) < 0.7
                    and any(part.distance(Point(*q)) < 1.0 for q in pads)
                    for part in getattr(copper, "geoms", [copper])):
                problems.append(i)
                break
    return leds, sorted(problems)


def unit_bridges(spec: BadgeSpec, safe=None) -> dict:
    """Per unit: {i: {"F.Cu": seg | None, "B.Cu": seg | None}} in board mm.

    F bridges carry 3V3, B bridges carry GND; each starts at the pad or
    via that feeds the unit on that layer, so a window that fully encircles
    the unit can no longer strand its copper island. A None means no clear
    straight path existed; the caller falls back to the reserved corridor.
    """
    rings = spec.outline if spec.outline else [
        [(OUTLINE[0], OUTLINE[1]), (OUTLINE[2], OUTLINE[1]),
         (OUTLINE[2], OUTLINE[3]), (OUTLINE[0], OUTLINE[3])]
    ]
    pads = []
    for _num, px, py, _net, _row in CONNECTOR_PADS:  # holes exist in all rows
        hw = 1.225  # pad copper 0.875 + 0.35 clearance
        pads.append([(px - hw, py - hw), (px + hw, py - hw),
                     (px + hw, py + hw), (px - hw, py + hw)])
    clk = clk_info(spec)
    if clk is not None and clk["jumper"]:
        # The jumper's copper (and its routed link to pin 9) blocks bridges
        # exactly like a connector pad would.
        pads += [q for _lbl, q in jumper_copper_pieces(clk)]
        for run in (clk_link(spec.leds, spec.pins, safe, spec.outline, clk),
                    clk_v3_link(spec.leds, spec.pins, safe, spec.outline,
                                clk)):
            if run is not None and len(run["pts"]) > 1:
                pads += [_quad_seg(a, b, 1.0)
                         for a, b in zip(run["pts"], run["pts"][1:])]
    all_pieces = [unit_copper_pieces(led, safe, spec.pins, spec.leds,
                                     spec.outline, clk=clk)
                  for led in spec.leds]
    out: dict = {}
    for i, led in enumerate(spec.leds):
        g = led_geometry(led)
        x, y = clamp_led_obj(led, safe)

        def pt(off):
            rx, ry = _r(off[0], off[1], led.rot)
            return x + rx, y + ry

        front = led.side != "back"
        starts = {
            "F.Cu": ("pad_res_in", g["res_in"]) if front else ("via", g["via_back"]),
            "B.Cu": ("via", g["via_front"]) if front else ("pad_led_k", g["led_k"]),
        }
        if led.novia:
            # No via, so nothing of this unit lives on the far layer to be
            # stranded; its trace reaches a connector pad instead. Dropping
            # the key (rather than setting None) keeps the caller from
            # reserving a window corridor it no longer needs.
            del starts["B.Cu" if front else "F.Cu"]
        if clk is not None and led.clk:
            # The supply is a CLK trace now, not a 3V3 pour feed: a front
            # unit's res_in carries CLK (bridging it to the 3V3 ring would be
            # a dead short), and a back unit's 3V3 via is gone entirely.
            starts.pop("F.Cu", None)
        obstacles = pads + [q for j, ps in enumerate(all_pieces) if j != i
                            for _lbl, q in ps]
        th = "drill" in PKG[g["pkg"]]
        far = bool(led.farled) and not g["hole"] and not th
        out[i] = {}
        for layer, (lbl, off) in starts.items():
            own = all_pieces[i]
            if lbl == "via":
                # This bridge runs on the unit's FAR layer, where its SMD pads
                # and traces are not copper at all: they live on the mounting
                # face only. Treating them as obstacles walled the via in: an
                # inline unit's via sits 1.0 mm from the resistor pad center,
                # inside that pad's inflated quad, so every one of the 16 rays
                # "collided" and the unit fell back to the reserved 2 mm window
                # corridor, a fat band of pour where a 0.3 mm trace belongs.
                # Only pieces that penetrate the board can truly block this
                # layer: the reverse-mount hole, a TH LED's pad annuli, and a
                # far-side LED's via-in-pads.
                keep = {"hole"} | ({"pad_led_k", "pad_led_a"} if th or far
                                   else set())
                own = [(lb, q) for lb, q in own if lb in keep]
            out[i][layer] = _bridge_route(
                pt(off), own, _BRIDGE_EXCLUDE[lbl], obstacles, rings)
    return out


def resolve_overlap(
    a: Led, b: Led, gap: float = 0.2,
    safe: tuple[float, float, float, float] | None = None,
    pins=ALL_PINS,
) -> Led:
    """Return b, shifted if needed so its unit does not overlap a's.

    Units conflict even on opposite sides: each one's via penetrates both
    copper layers. The web UI prevents overlap during drag; this is the
    server-side backstop for hand-crafted requests.

    Callers run this AFTER resolve_pad_overlap, so a slide that parks b back
    on a connector pad quietly undoes the pad backstop and ships a short
    (measured on two reverse 1206s at y=3.59, where the pad resolver moved the
    second unit from x=16.49 to 13.63 and this routine put it straight back).
    Landing on a pad is therefore a tiebreak, not a hard rule: the search runs
    once refusing the kept pads and again without them, so separating the two
    units still wins if nothing else can.
    """
    from dataclasses import replace

    from shapely.geometry import Polygon
    from shapely.geometry import box as sbox

    if safe is None:
        safe = UNIT_SAFE
    pa = unit_poly(a, safe)
    eps = 1e-6  # sliding to exactly `gap` separation must count as clear
    if unit_poly(b, safe).distance(pa) >= gap - eps:
        return b
    # Hoisted out of the candidate loop: the rotated footprint corners and the
    # kept pad keepouts do not depend on where the probe goes, and the search
    # below can try several hundred probes.
    _bg = led_geometry(b)
    _bb = _bg["bbox"]
    _corners = [_r(px, py, b.rot) for px, py in
                ((_bb[0], _bb[1]), (_bb[2], _bb[1]),
                 (_bb[2], _bb[3]), (_bb[0], _bb[3]))]
    _ox0, _oy0, _ox1, _oy1 = _bbox_offsets_g(_bg, b.rot)
    _keepouts = [sbox(*PAD_PAIRS[k]["keepout"]) for k in active_pairs(pins)]

    def fits(x, y, avoid_pads=True):
        # clamp_led_obj would silently pull the probe back onto the board, so
        # a candidate outside the legal centre range is not a real choice.
        if not (safe[0] - _ox0 <= x <= safe[2] - _ox1
                and safe[1] - _oy0 <= y <= safe[3] - _oy1):
            return None
        poly = Polygon([(x + px, y + py) for px, py in _corners])
        if poly.distance(pa) < gap - eps:
            return None
        if avoid_pads and any(poly.intersects(k) for k in _keepouts):
            return None
        return replace(b, x=x, y=y)

    ba, bb = led_unit_bbox(a, safe), led_unit_bbox(b, safe)

    def axis_slides(avoid):
        for x, y in (
            (b.x + (ba[2] + gap - bb[0]), b.y),  # slide right
            (b.x - (bb[2] - ba[0] + gap), b.y),  # slide left
            (b.x, b.y + (ba[3] + gap - bb[1])),  # slide down
            (b.x, b.y - (bb[3] - ba[1] + gap)),  # slide up
        ):
            probe = fits(x, y, avoid)
            if probe is not None:
                return probe
        return None

    # Pad avoidance is preferred at EQUAL displacement, never at the cost of
    # a much bigger move: each stage is tried pad-free and then relaxed before
    # the next, wider stage starts. Exhausting the whole search pad-first
    # instead flung units across the board and measurably made three- and
    # four-unit boards worse (271 -> 282 of 600) while fixing nothing extra.
    for stage in (axis_slides,
                  lambda avoid: _resolve_overlap_min_push(b, gap, safe, pa,
                                                          fits, avoid)):
        for avoid in (True, False):
            found = stage(avoid)
            if found is not None:
                return found

    # The ring walk interleaves the two passes instead of running twice: it is
    # by far the most expensive stage, and "pad-free at this radius, else any
    # at this radius" is the same preference for half the probes.
    def place(x, y):
        for avoid in (True, False):
            probe = fits(x, y, avoid)
            if probe is not None:
                return probe
        return None

    return _resolve_overlap_rings(b, safe, place) or b


def _resolve_overlap_min_push(b, gap, safe, pa, fits, avoid=True):
    """Smallest translation that separates b's tight footprint from `pa`.

    resolve_overlap's four axis slides clear a's whole axis-aligned ENVELOPE,
    which is far more room than two tilted units actually need, and on the big
    packages all four land outside `safe`, so the unit stayed exactly where it
    was and shipped a pad-to-pad short (measured: 35 of 600 random two-unit
    boards, every one of them a give-up rather than a bad slide).

    Both footprints are convex quads, so a separating axis exists whenever they
    can be parted at all, and each quad edge normal supplies one: push b just
    far enough along it to open `gap`. Shortest push first, so the unit ends up
    as close to where the user put it as the board allows.
    """
    pushes = []
    for poly in (pa, unit_poly(b, safe)):
        pts = list(poly.exterior.coords[:-1])
        n = len(pts)
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            nx, ny = y1 - y0, x0 - x1
            ln = (nx * nx + ny * ny) ** 0.5
            if ln < 1e-12:
                continue
            nx, ny = nx / ln, ny / ln
            da = [px * nx + py * ny for px, py in pa.exterior.coords[:-1]]
            db = [px * nx + py * ny for px, py in
                  unit_poly(b, safe).exterior.coords[:-1]]
            pushes.append((max(da) + gap - min(db), nx, ny))
            pushes.append((max(db) + gap - min(da), -nx, -ny))
    for dist, nx, ny in sorted(pushes):
        if dist <= 0:
            continue
        probe = fits(b.x + nx * dist, b.y + ny * dist, avoid)
        if probe is not None:
            return probe
    return None


def _resolve_overlap_rings(b, safe, place):
    """Last resort when even the minimal push runs off `safe`.

    Walk outward in rings until something fits. Nearest ring first keeps the
    unit near where it was asked for, and this only runs on a board that would
    otherwise ship two units shorted together.
    """
    import math

    lim = max(safe[2] - safe[0], safe[3] - safe[1]) if safe else 20.0
    r = 0.6
    while r <= lim:
        for k in range(16):
            t = math.radians(k * 22.5)
            probe = place(b.x + r * math.cos(t), b.y + r * math.sin(t))
            if probe is not None:
                return probe
        r += 0.6
    return None  # nothing fits under this pass's rules; the caller relaxes them


def pad_conflict(
    led: Led, pins=ALL_PINS,
    safe: tuple[float, float, float, float] | None = None,
) -> bool:
    """True if the unit's rotated footprint overlaps a kept pad pair.

    Dropping every pin of a corner frees that corner for artwork or a unit.
    """
    from shapely.geometry import box as sbox

    poly = unit_poly(led, safe)
    return any(poly.intersects(sbox(*PAD_PAIRS[k]["keepout"]))
               for k in active_pairs(pins))


def resolve_pad_overlap(
    led: Led, pins=ALL_PINS,
    safe: tuple[float, float, float, float] | None = None,
) -> Led:
    """Slide a unit off the connector pad keepouts (server-side backstop).

    The web UI never drops a unit on a pad pair; this covers hand-crafted
    requests the same way resolve_overlap does for unit-unit overlaps.
    """
    from dataclasses import replace

    from shapely.geometry import box as sbox

    start = led
    for _ in range(3):
        b = led_unit_bbox(led, safe)
        poly = unit_poly(led, safe)
        hit = next(
            (PAD_PAIRS[k]["keepout"] for k in active_pairs(pins)
             if poly.intersects(sbox(*PAD_PAIRS[k]["keepout"]))),
            None,
        )
        if hit is None:
            return led
        # Vet each slide with clamp_led_obj, never the legacy positional
        # clamp_led: that one defaults to size="0805" and reverse=False and
        # knows nothing about Led.adv, so it rejects the legal slides of every
        # other package and leaves the unit parked on the pads, shorting 3V3
        # to GND on a board the user still gets a 200 for.
        #
        # A slide that runs off `safe` is CLAMPED back onto the board rather
        # than discarded. The pair being cleared is in one axis; the clamp only
        # moves the other one, so the slide still does its job, and discarding
        # it stranded units whose envelope only fits the board one way round.
        cands = []
        for x, y in (
            (led.x + (hit[2] + 0.05 - b[0]), led.y),  # slide right
            (led.x - (b[2] - hit[0] + 0.05), led.y),  # slide left
            (led.x, led.y + (hit[3] + 0.05 - b[1])),  # slide down
            (led.x, led.y - (b[3] - hit[1] + 0.05)),  # slide up
        ):
            probe = replace(led, x=x, y=y)
            cx, cy = clamp_led_obj(probe, safe)
            cands.append(replace(probe, x=cx, y=cy))
        # Prefer a slide that actually lands the unit clear of EVERY kept pair,
        # not merely the first one that stays on the board. A wide unit (an
        # inline 1206) clearing the pair it started on can drop straight onto
        # the other pair of the same row, and the old first-legal-wins pick
        # then ping-ponged between them until the retry budget ran out.
        clear = next((p for p in cands if not pad_conflict(p, pins, safe)), None)
        if clear is not None:
            return clear
        moved = next((p for p in cands if (p.x, p.y) != (led.x, led.y)), None)
        if moved is None:
            break  # every slide was clamped straight back; retrying is a no-op
        led = moved

    # An axis slide clears the pair the unit sits on; it cannot help a unit
    # whose envelope has to thread BETWEEN pairs, which an advanced placement
    # (a via dragged metres from its LED) routinely produces. Walk outward from
    # where the unit was asked for until something fits, rather than shipping
    # copper sitting on the connector's 3V3 and GND pads.
    import math

    from shapely.geometry import Polygon

    if safe is None:
        safe = UNIT_SAFE
    # Everything that does not depend on the probe position is hoisted: the
    # rotated envelope, the legal centre range clamp_led_obj enforces, the
    # rotated footprint corners and the kept keepout boxes. The scan is a few
    # hundred probes on a board that would otherwise ship a short, and this is
    # the difference between it costing microseconds and milliseconds.
    g = led_geometry(start)
    ox0, oy0, ox1, oy1 = _bbox_offsets_g(g, start.rot)
    lo_x, hi_x = safe[0] - ox0, safe[2] - ox1
    lo_y, hi_y = safe[1] - oy0, safe[3] - oy1
    bb = g["bbox"]
    corners = [_r(px, py, start.rot) for px, py in
               ((bb[0], bb[1]), (bb[2], bb[1]), (bb[2], bb[3]), (bb[0], bb[3]))]
    keepouts = [sbox(*PAD_PAIRS[k]["keepout"]) for k in active_pairs(pins)]
    lim = max(safe[2] - safe[0], safe[3] - safe[1])
    r = 0.5
    while r <= lim:
        for k in range(16):
            t = math.radians(k * 22.5)
            x, y = start.x + r * math.cos(t), start.y + r * math.sin(t)
            if not (lo_x <= x <= hi_x and lo_y <= y <= hi_y):
                continue  # clamp_led_obj would move it back; not a real choice
            poly = Polygon([(x + px, y + py) for px, py in corners])
            if not any(poly.intersects(box) for box in keepouts):
                return replace(start, x=x, y=y)
        r += 0.5
    return led  # no room; KiCad DRC will flag it


@dataclass
class Text:
    x: float
    y: float
    text: str = ""
    size: float = 1.5        # glyph height, mm
    side: str = "front"      # "front" or "back"
    rot: float = 0           # degrees clockwise, viewed from the front
    # "kicad" = KiCad's stroke font as a gr_text (silkscreen only). Any other
    # value is a bundled TTF key (textpoly.FONTS): the string becomes exact
    # polygons and may use any art material.
    font: str = "kicad"
    material: str = "silk"   # one of ART_MATERIALS; forced to silk for "kicad"


# Artwork materials, in the PCB-art tradition of minibadge.wiki:
#   silk   - white silkscreen ink on the front (plain artwork)
#   copper - front soldermask opened over the 3V3 pour: shiny copper/ENIG art
#   glow   - copper removed from BOTH layers, mask kept: a tinted translucent
#            window that back-side LED light diffuses through
#   bare   - copper removed AND mask opened on both sides: raw FR4 laminate,
#            the brightest light window
#   cut    - the board itself removed: the region becomes a real cutout
#            through copper, mask and laminate. Cut regions never become an
#            ArtLayer; the webapp subtracts them from the outline instead,
#            so everything downstream sees the true board shape.
ART_MATERIALS = ("silk", "copper", "glow", "bare", "cut")


@dataclass
class ArtLayer:
    material: str = "silk"
    # Rectangles in board mm: (x, y, w, h), top-left anchored. The raster
    # (pixel-grid) pipeline emits these.
    rects: list[tuple[float, float, float, float]] = field(default_factory=list)
    # Exact polygons in board mm: each polygon is a list of (x, y) rings,
    # exterior first, the rest holes. The SVG vector pipeline emits these.
    polys: list[list[list[tuple[float, float]]]] = field(default_factory=list)
    # Which face silk/copper renders on ("front"/"back"). Glow and bare cut
    # through the whole board, so their side only matters for bookkeeping.
    side: str = "front"
    # For windows (glow/bare): which face(s) this layer cuts copper from.
    # "through" cuts both; "front"/"back" cut one face: the other keeps its
    # copper (and, for bare, its mask: bare opens only the mask of the faces
    # it cuts). The webapp splits every glow and through-bare drawing into
    # one layer per face, each carved around that face's own copper, so the
    # far side of a unit keeps its via and nothing else instead of a slab of
    # pour shadowing the whole part. Light still crosses wherever the two
    # face layers overlap, which is everywhere except those unit shadows,
    # where a part blocks the light anyway.
    window: str = "through"


@dataclass
class BadgeSpec:
    name: str = "minibadge"
    leds: list[Led] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    art: list[ArtLayer] = field(default_factory=list)
    mask_color: str = "green"
    # Surface finish: "enig" (gold) or "hasl" (silver). Board-wide fab
    # choice: it colors every exposed pad, via, and copper-art opening.
    finish: str = "enig"
    # Connector pins kept on this badge, by pad number. Any combination is
    # allowed; power_missing() reports when the LEDs are left without a rail.
    pins: tuple[str, ...] = ALL_PINS
    # Custom board outline as rings of (x, y) board-mm points: first ring
    # is the exterior, the rest are holes. None = the standard 20x20 square.
    outline: list[list[tuple[float, float]]] | None = None
    # Via tenting: cover vias with soldermask (every fab's default). Off
    # writes `(tenting none)` on each via so the annulus plates bare, and
    # window mask openings stop keeping a cap of mask over vias they cross.
    tenting: bool = True
    # How CLK units (Led.clk) meet the blink clock, once any exist. True =
    # the 3-pad solder jumper (bridge one side: steady 3V3 or blinking CLK);
    # False = their supply is wired straight to pin 9. See clk_info().
    clk_jumper: bool = True
    # Jumper centre in board mm (None = JUMPER_AT) and its rotation in
    # degrees clockwise. Only meaningful while clk_jumper is on.
    jumper: tuple | None = None
    jumper_rot: float = 0
    # Which face carries the jumper's pads and silk. On the back, its 3V3
    # pad has no 3V3 copper of its own (the back pour is GND), so it is fed
    # per jumper_via below. Blinking LEDs on the opposite face from the
    # jumper ALWAYS reach its rail through the rail via; no option gates
    # that.
    jumper_side: str = "front"
    # How a BACK-side jumper's 3V3 pad is fed: True = its own via straight
    # down into the front pour; False = a routed trace on its own face to a
    # kept 3V3 connector pin (whose plated hole carries the pour's net).
    # Ignored on a front-side jumper, whose 3V3 pad sits in the pour.
    jumper_via: bool = True
    # Hand-placed bends for the jumper's routed links, same contract as
    # Led.nodes: jumper_nodes bends the CLK-pad-to-pin-9 link, and
    # jumper_v3nodes bends the 3V3-pad-to-3V3-pin link (which only exists
    # while jumper_side is "back" and jumper_via is off).
    jumper_nodes: tuple = ()
    jumper_v3nodes: tuple = ()
    # Which kept 3V3 connector pin the traced 3V3 hookup lands on, by pad
    # number ("7"/"15"). None = the nearest, like a via-less run; an invalid
    # choice (wrong net, dropped pin) falls back to None rather than
    # refusing, the same contract as Led.term.
    jumper_v3pin: str | None = None

    @property
    def rows(self) -> tuple[str, ...]:
        """Rows still carrying at least one pin (strip-level geometry)."""
        live = {PAD_PAIRS[k]["row"] for k in active_pairs(self.pins)}
        return tuple(r for r in ("top", "bottom") if r in live)


def outline_polygon(spec: BadgeSpec):
    """The board shape as a shapely polygon (standard square if not custom)."""
    from shapely.geometry import Polygon, box

    if spec.outline:
        return Polygon(spec.outline[0], spec.outline[1:])
    return box(*OUTLINE)


def unit_safe(spec: BadgeSpec) -> tuple[float, float, float, float]:
    """The rectangle LED unit bboxes may occupy: the board bounds, inset.

    On a custom outline this follows the outline's bounding box, so units
    can go wherever the board goes; whether a unit actually sits on solid
    board is checked against the outline polygon separately (webapp).
    """
    if spec.outline:
        # 0.56: strictly more than the 0.55 solid-board margin the webapp
        # enforces, so a unit clamped to this rect is never relocated.
        b = outline_polygon(spec).bounds
        return (b[0] + 0.56, b[1] + 0.56, b[2] - 0.56, b[3] - 0.56)
    return UNIT_SAFE


def clamp_led(
    x: float, y: float, rot: int = 0, layout: str = "stacked",
    safe: tuple[float, float, float, float] | None = None,
    size: str = "0805",
    reverse: bool = False,
) -> tuple[float, float]:
    """Clamp an LED center so its whole (rotated) unit stays in `safe`."""
    if safe is None:
        safe = UNIT_SAFE
    ox0, oy0, ox1, oy1 = _bbox_offsets(rot, layout, size, reverse)
    x = min(max(x, safe[0] - ox0), safe[2] - ox1)
    y = min(max(y, safe[1] - oy0), safe[3] - oy1)
    return x, y


def _n(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _ts(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"minibadge-designer:{key}"))


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


LAYERS = """  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )"""


def _nets(spec: BadgeSpec) -> tuple[list[str], dict[str, int]]:
    names = ["", "3V3", "GND"]
    clk = clk_info(spec)
    if clk is not None:
        names.append("CLK")
        if clk["jumper"]:
            names.append(CLK_RAIL)
    names += [f"/LED{i + 1}_A" for i in range(len(spec.leds))]
    index = {name: i for i, name in enumerate(names)}
    lines = [f'  (net {i} "{name}")' for i, name in enumerate(names)]
    return lines, index


def _connector_footprint(nets: dict[str, int], pins=ALL_PINS,
                         clk: bool = False) -> str:
    out = [
        f'  (footprint "MiniBadge:MiniBadge_Simple" (layer "F.Cu") (tstamp {_ts("fp-conn")})',
        f"    (at {_n(ORIGIN)} {_n(ORIGIN)})",
        "    (attr through_hole)",
        '    (fp_text reference "J1" (at 10.16 -1.2 unlocked) (layer "F.Fab")',
        "      (effects (font (size 1 1) (thickness 0.15)))",
        f"      (tstamp {_ts('fp-conn-ref')})",
        "    )",
        '    (fp_text value "MiniBadge_Simple" (at 10.16 21.59 unlocked) (layer "F.Fab")',
        "      (effects (font (size 1 1) (thickness 0.15)))",
        f"      (tstamp {_ts('fp-conn-val')})",
        "    )",
    ]
    # Pin captions print on BOTH silkscreens (the editor preview shows them
    # on both faces: the fab board should match; the Dwgs.User layer the
    # official footprint used never prints at all). Centered text mirrors
    # in place, so the back copy only needs the mirror flag.
    for i, key in enumerate(active_pairs(pins)):
        label = pair_caption(key, pins)
        if not label:
            continue
        x, y = pair_caption_at(key, pins)
        # Each caption names the PAIR, left word for the left pad. Seen from
        # the back the pair is mirrored, so the words have to swap too;
        # otherwise the back silk labels 3V3 as GND and vice versa, which is
        # exactly the kind of thing someone hand-soldering trusts. A pair with
        # only one pin kept names just that pin, so nothing to swap.
        flipped = " ".join(reversed(label.split()))
        for layer, mirror, label in (("F.SilkS", "", label),
                                     ("B.SilkS", " (justify mirror)", flipped)):
            out += [
                f'    (fp_text user "{label}" (at {_n(x)} {_n(y)} unlocked) (layer "{layer}")',
                f"      (effects (font (size 0.6 0.6) (thickness 0.11)){mirror})",
                f"      (tstamp {_ts(f'fp-conn-label-{i}-{layer}')})",
                "    )",
            ]
    for num, x, y, net, _row in connector_pads(clk):
        if num not in pins:
            continue
        net_s = f' (net {nets[net]} "{net}")' if net else ""
        out.append(
            f'    (pad "{num}" thru_hole circle (at {_n(x)} {_n(y)}) (size 1.75 1.75) '
            f'(drill 0.95) (layers "*.Cu" "*.Mask"){net_s} (tstamp {_ts(f"pad-{num}")}))'
        )
    # 3D: a 1x02 male header per kept pad pair, mounted on the BACK with the
    # pins pointing away from the front face: how a minibadge actually
    # plugs into the badge's socket strip. Model x-rotation 180 flips it
    # under the board; the z-rotation lays the two pins along the pair.
    # A pair with one pin dropped gets a single-pin header over the pad that
    # is left, not nothing: the part really is there on the finished badge.
    headers = {
        2: model_path("Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical"),
        1: model_path("Connector_PinHeader_2.54mm", "PinHeader_1x01_P2.54mm_Vertical"),
    }
    for i, key in enumerate(PAD_PAIRS):
        kept = [q for q in PAD_PAIRS[key]["pins"] if q in pins]
        if not kept:
            continue
        # Centre the body on the pins it actually covers: a 1x02 spans the
        # pair's midpoint, a 1x01 sits on its own pad.
        xs = [x for num, x, _y, _net, _row in CONNECTOR_PADS if num in kept]
        px = sum(xs) / len(xs)
        py = PAD_PAIRS[key]["header"][1]
        # offset z -1.6 (board thickness) + x-rot 180: body flush on the
        # BACK face, pins pointing away from the front: how a minibadge
        # plugs into the badge's socket strip.
        out.append(
            f'    (model "{headers[len(kept)]}"\n'
            f"      (offset (xyz {_n(px - (1.27 if len(kept) == 2 else 0.0))} "
            f"{_n(-py)} -1.6)) (scale (xyz 1 1 1)) "
            "(rotate (xyz 180 0 90))\n"
            "    )"
        )
    out.append("  )")
    return "\n".join(out)


def _jumper_footprint(nets: dict[str, int], clk) -> str:
    """The 3-pad CLK solder jumper, emitted at its board position.

    Pad 1 carries CLK (the end nearest pin 9 by default), pad 2 the rail
    the CLK units' supply runs land on, pad 3 sits in the 3V3 pour. The
    builder bridges 2-3 for steady LEDs or 1-2 to blink with the badge;
    the silk names each end so nobody has to guess with an iron in their
    hand. The footprint anchor is emitted unrotated and every child carries
    the rotation itself, mirroring how the canvas draws it.
    """
    jx, jy, rot = clk["jumper"]
    back = clk["side"] == "back"
    cu, mask, silk, fab = (("B.Cu", "B.Mask", "B.SilkS", "B.Fab") if back
                           else ("F.Cu", "F.Mask", "F.SilkS", "F.Fab"))
    mirror = " (justify mirror)" if back else ""
    krot = (-rot) % 360  # KiCad angles count counterclockwise; ours clockwise
    at_rot = f" {_n(krot)}" if krot else ""
    out = [
        f'  (footprint "Jumper:SolderJumper_3_CLK" (layer "{cu}") (tstamp {_ts("fp-jumper")})',
        f"    (at {_n(ORIGIN + jx)} {_n(ORIGIN + jy)})",
        "    (attr smd exclude_from_pos_files exclude_from_bom)",
        f'    (fp_text reference "JP1" (at 0 -2.4{at_rot} unlocked) (layer "{fab}")',
        f"      (effects (font (size 1 1) (thickness 0.15)){mirror})",
        f"      (tstamp {_ts('fp-jumper-ref')})",
        "    )",
        f'    (fp_text value "CLK_SEL" (at 0 2.4{at_rot} unlocked) (layer "{fab}")',
        f"      (effects (font (size 1 1) (thickness 0.15)){mirror})",
        f"      (tstamp {_ts('fp-jumper-val')})",
        "    )",
    ]
    # CLK / 3V3 labels just past each end pad, 0.6 mm silk like the pin
    # captions, so the bridge choice is legible on the finished board.
    for lbl, sgn in (("CLK", -1), ("3V3", 1)):
        lx, ly = _r(sgn * (JUMPER_PITCH + JUMPER_PAD[0] / 2 + 1.0), 0.0, rot)
        out += [
            f'    (fp_text user "{lbl}" (at {_n(lx)} {_n(ly)}{at_rot} unlocked) (layer "{silk}")',
            f"      (effects (font (size 0.6 0.6) (thickness 0.11)){mirror})",
            f"      (tstamp {_ts(f'fp-jumper-label-{lbl}')})",
            "    )",
        ]
    # A thin bracket above and below the pad row.
    hx, hy = 1.9, JUMPER_PAD[1] / 2 + 0.3
    for tag, y0 in (("t", -hy), ("b", hy)):
        (ax, ay), (bx, by) = _r(-hx, y0, rot), _r(hx, y0, rot)
        out.append(
            f"    (fp_line (start {_n(ax)} {_n(ay)}) (end {_n(bx)} {_n(by)}) "
            f'(stroke (width 0.12) (type solid)) (layer "{silk}") '
            f"(tstamp {_ts(f'fp-jumper-line-{tag}')}))"
        )
    for num, net, dx in (("1", "CLK", -JUMPER_PITCH), ("2", CLK_RAIL, 0.0),
                         ("3", "3V3", JUMPER_PITCH)):
        px, py = _r(dx, 0.0, rot)
        out.append(
            f'    (pad "{num}" smd rect (at {_n(px)} {_n(py)}{at_rot}) '
            f"(size {_n(JUMPER_PAD[0])} {_n(JUMPER_PAD[1])}) "
            f'(layers "{cu}" "{mask}") (net {nets[net]} "{net}") '
            f"(tstamp {_ts(f'fp-jumper-pad-{num}')}))"
        )
    out.append("  )")
    return "\n".join(out)


def _seg_outside_discs(a, b, discs, min_len: float = 0.15) -> list:
    """The pieces of segment a->b that lie outside every disc (cx, cy, r).

    Silkscreen must not print over an exposed via's mask aperture (the fab
    clips it and DRC flags it), so when tenting is off, a unit's silk lines
    are broken around the via the way TH dome silk is already broken around
    its pads. Pieces shorter than min_len are dropped: a fleck of ink that
    small prints as nothing.
    """
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-12:
        return []
    spans = [(0.0, 1.0)]
    for cx, cy, r in discs:
        fx, fy = ax - cx, ay - cy
        bb = 2 * (fx * dx + fy * dy)
        cc = fx * fx + fy * fy - r * r
        det = bb * bb - 4 * l2 * cc
        if det <= 0:
            continue
        s = det ** 0.5
        t0 = max((-bb - s) / (2 * l2), 0.0)
        t1 = min((-bb + s) / (2 * l2), 1.0)
        if t0 >= t1:
            continue
        spans = [seg for lo, hi in spans
                 for seg in ((lo, min(hi, t0)), (max(lo, t1), hi))
                 if seg[0] < seg[1]]
    ln = l2 ** 0.5
    return [((ax + dx * lo, ay + dy * lo), (ax + dx * hi, ay + dy * hi))
            for lo, hi in spans if (hi - lo) * ln >= min_len]


def _smd(
    key: str,
    ref: str,
    value: str,
    x: float,
    y: float,
    net1: tuple[int, str],
    net2: tuple[int, str],
    cathode_mark: bool,
    side: str = "front",
    ang: float = 0,
    flip: bool = False,
    pkg: str = "0805",
    model: str | None = None,
    silk_avoid: tuple = (),
) -> str:
    """A minimal two-pad SMD footprint (0603/0805/1206) at page coords.

    Rotation (degrees clockwise, any angle) is baked into the local geometry
    rather than expressed as a footprint angle, so front/back placement and
    rotation compose without relying on KiCad transform conventions. Pads
    carry the equivalent KiCad orientation (which counts counterclockwise).
    `flip` mirrors the local x axis first (pad 1 moves to the + side), used
    by the inline layout to face the LED anode toward the resistor.
    """
    p = "F" if side != "back" else "B"
    mirror = "" if side != "back" else " (justify mirror)"
    f = -1.0 if flip else 1.0
    pk = PKG.get(pkg, PKG["0805"])
    dx, pw, ph = pk["dx"], pk["pw"], pk["ph"]
    bw, bh = pk["body"]
    drill = pk.get("drill")  # set = through-hole part (pads on both faces)
    yh = ph / 2 + 0.15   # silk end-line half-height
    ty = max(ph / 2 + 1.0, bh / 2 + 0.6)   # ref/value text offset
    # Cathode bar: clear of a TH lens AND of a round pad's mask opening.
    mark = max(dx + 0.8, bw / 2 + 0.3, dx + pw / 2 + 0.2)

    def line(x0: float, y0: float, x1: float, y1: float, width: float, tag: str) -> str:
        a, b = _r(f * x0, y0, ang), _r(f * x1, y1, ang)
        # silk_avoid discs (an exposed via's mask aperture) break the line;
        # ink over open mask gets clipped by the fab and flagged by DRC.
        pieces = (_seg_outside_discs(a, b, silk_avoid) if silk_avoid
                  else [(a, b)])
        return "\n".join(
            f"    (fp_line (start {_n(pa[0])} {_n(pa[1])}) (end {_n(pb[0])} {_n(pb[1])}) "
            f'(stroke (width {_n(width)}) (type solid)) (layer "{p}.SilkS") '
            f"(tstamp {_ts(f'fp-{key}-{tag}' if n == 0 else f'fp-{key}-{tag}-{n}')}))"
            for n, (pa, pb) in enumerate(pieces))

    def rect(x0: float, y0: float, x1: float, y1: float, width: float, layer: str, tag: str) -> str:
        # A rotated rectangle: fp_rect is axis-aligned only, so emit a poly.
        pts = " ".join(
            f"(xy {_n(px)} {_n(py)})"
            for px, py in (_r(f * qx, qy, ang) for qx, qy in
                           ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))
        )
        return (
            f"    (fp_poly (pts {pts}) "
            f'(stroke (width {_n(width)}) (type solid)) (fill none) (layer "{p}.{layer}") '
            f"(tstamp {_ts(f'fp-{key}-{tag}')}))"
        )

    def pad(num: str, dx: float, net: tuple[int, str]) -> str:
        px, py = _r(f * dx, 0, ang)
        if drill:
            # Round TH pad: rotation-invariant, barrel on both copper faces.
            return (
                f'    (pad "{num}" thru_hole circle (at {_n(px)} {_n(py)}) '
                f"(size {_n(pw)} {_n(ph)}) (drill {_n(drill)}) "
                f'(layers "*.Cu" "*.Mask") (net {net[0]} "{net[1]}") '
                f"(tstamp {_ts(f'fp-{key}-p{num}')}))"
            )
        # KiCad pad orientation counts counterclockwise on screen; our
        # rotation is clockwise, so the angle flips sign.
        a = f" {_n((-ang) % 360)}" if ang % 360 else ""
        return (
            f'    (pad "{num}" smd rect (at {_n(px)} {_n(py)}{a}) (size {_n(pw)} {_n(ph)}) '
            f'(layers "{p}.Cu" "{p}.Paste" "{p}.Mask") (net {net[0]} "{net[1]}") '
            f"(tstamp {_ts(f'fp-{key}-p{num}')}))"
        )

    lines = [
        f'  (footprint "minibadge-designer:{value}_{pkg}" (layer "{p}.Cu") (tstamp {_ts(f"fp-{key}")})',
        f"    (at {_n(x)} {_n(y)})",
        f"    (attr {'through_hole' if drill else 'smd'})",
        f'    (fp_text reference "{ref}" (at 0 {_n(-ty)} unlocked) (layer "{p}.Fab")',
        f"      (effects (font (size 0.7 0.7) (thickness 0.1)){mirror})",
        f"      (tstamp {_ts(f'fp-{key}-ref')})",
        "    )",
        f'    (fp_text value "{value}" (at 0 {_n(ty)} unlocked) (layer "{p}.Fab")',
        f"      (effects (font (size 0.7 0.7) (thickness 0.1)){mirror})",
        f"      (tstamp {_ts(f'fp-{key}-val')})",
        "    )",
    ]
    if cathode_mark:
        # Bar on the cathode (pad 1) side of the silkscreen.
        lines += [
            line(-mark, -yh, -mark, yh, 0.15, "k1"),
            line(-mark, -yh, -(mark - 0.4), -yh, 0.15, "k2"),
            line(-mark, yh, -(mark - 0.4), yh, 0.15, "k3"),
        ]
    else:
        lines += [
            # Measured from the pad EDGE, not from dx: hand-solder pads are
            # wide enough that a fixed dx offset lands on the pad, and silk
            # over a mask opening gets clipped by the fab.
            line(-(dx + pw / 2 + 0.15), -yh, -(dx + pw / 2 + 0.15), yh, 0.12, "s1"),
            line(dx + pw / 2 + 0.15, -yh, dx + pw / 2 + 0.15, yh, 0.12, "s2"),
        ]
    lens = pk.get("lens", 0.0) if drill else 0.0
    if drill and lens == bw == bh:
        # Bare round lens (a classic dome): its outline IS the body. Silk is
        # two arcs broken around the pads so the ink never clips their mask
        # openings.
        import math

        lines.append(
            f"    (fp_circle (center 0 0) (end {_n(bw / 2)} 0) "
            f'(stroke (width 0.1) (type solid)) (fill none) (layer "{p}.Fab") '
            f"(tstamp {_ts(f'fp-{key}-fab')}))"
        )
        sr = bw / 2 + 0.1
        for tag, a0, a1 in (("arc1", 40.0, 140.0), ("arc2", 220.0, 320.0)):
            pts = []
            for deg in (a0, (a0 + a1) / 2, a1):
                t = math.radians(deg)
                px_, py_ = _r(f * sr * math.cos(t), sr * math.sin(t), ang)
                pts.append(f"{_n(px_)} {_n(py_)}")
            lines.append(
                f"    (fp_arc (start {pts[0]}) (mid {pts[1]}) (end {pts[2]}) "
                f'(stroke (width 0.12) (type solid)) (layer "{p}.SilkS") '
                f"(tstamp {_ts(f'fp-{key}-{tag}')}))"
            )
    elif drill:
        # Rectangular body (a bar, or a small dome on a rectangular base):
        # outline on Fab plus two horizontal silk lines along the body edges;
        # they clear the pads' mask openings vertically. A dome narrower
        # than its base gets its own Fab circle.
        sy = bh / 2 + 0.15
        lines += [
            rect(-bw / 2, -bh / 2, bw / 2, bh / 2, 0.1, "Fab", "fab"),
            line(-bw / 2, -sy, bw / 2, -sy, 0.12, "b1"),
            line(-bw / 2, sy, bw / 2, sy, 0.12, "b2"),
        ]
        if lens:
            lines.append(
                f"    (fp_circle (center 0 0) (end {_n(lens / 2)} 0) "
                f'(stroke (width 0.1) (type solid)) (fill none) (layer "{p}.Fab") '
                f"(tstamp {_ts(f'fp-{key}-lens')}))"
            )
    else:
        lines.append(rect(-bw / 2, -bh / 2, bw / 2, bh / 2, 0.1, "Fab", "fab"))
    cyx = max(dx + 1.05, bw / 2 + 0.25)
    cyy = max(ph / 2 + 0.4, bh / 2 + 0.25)
    lines += [
        rect(-cyx, -cyy, cyx, cyy, 0.05, "CrtYd", "cy"),
        pad("1", -dx, net1),
        pad("2", dx, net2),
    ]
    if model:
        # Our rotation is baked into the pad geometry (no footprint angle), so
        # the model carries the equivalent z-rotation itself. On the front a
        # model's z-rotation turns the same way our _r turns board geometry;
        # a back-side footprint is flipped through the board plane, which
        # reverses that sense, so the angle negates. Getting this wrong leaves
        # the body lying across its own pads at twice the angle: invisible on
        # a round part, obvious on a chip (both signs checked against renders).
        mz = (ang + (180 if flip else 0)) % 360
        if p == "B":
            mz = (-mz) % 360
        # SMD models sit centered on our footprint origin and need no offset.
        # The THT LED models are anchored at pin 1, so the offset walks them
        # to pad 1, in the model's own (already flipped) frame, hence mz, and
        # with 3D y counting upward against our y-down board.
        if drill:
            # Front: pad 1 is at _r(f * -dx, ang) and `f` supplies the inline
            # flip. Back: the flip is already inside mz (it carries the +180),
            # so applying `f` as well would flip twice and land the body a
            # whole pad pitch away, which is what used to happen.
            ox, oy = _r(-dx, 0, mz) if p == "B" else _r(f * -dx, 0, ang)
            off = f"{_n(ox)} {_n(-oy)} 0"
        else:
            off = "0 0 0"
        lines.append(
            f'    (model "{model}"\n'
            f"      (offset (xyz {off})) (scale (xyz 1 1 1)) (rotate (xyz 0 0 {_n(mz)}))\n"
            "    )"
        )
    lines.append("  )")
    return "\n".join(lines)


def _led_unit(
    i: int, led: Led, nets: dict[str, int],
    safe: tuple[float, float, float, float] | None = None,
    pins=ALL_PINS,
    others: tuple = (),
    outline=None,
    tenting: bool = True,
    clk=None,
) -> str:
    """LED + resistor footprints, connecting traces, and the power via."""
    ang = led.rot
    g = led_geometry(led)
    x, y = clamp_led_obj(led, safe)
    anode = f"/LED{i + 1}_A"
    front = led.side != "back"
    cu = "F.Cu" if front else "B.Cu"
    gnd, v33 = (nets["GND"], "GND"), (nets["3V3"], "3V3")
    if clk is not None and led.clk:
        # The resistor feeds from the CLK hookup, not the 3V3 pour.
        v33 = (nets[clk["net"]], clk["net"])
    an = (nets[anode], anode)
    # KiCad 9 tents vias by default; only "leave them bare" needs saying.
    tent = "" if tenting else " (tenting none)"

    def at(dx: float, dy: float) -> tuple[float, float]:
        rx, ry = _r(dx, dy, ang)
        return ORIGIN + x + rx, ORIGIN + y + ry

    def seg(a: tuple[float, float], b: tuple[float, float], layer: str, net: int, tag: str) -> str:
        return (
            f"  (segment (start {_n(a[0])} {_n(a[1])}) (end {_n(b[0])} {_n(b[1])}) "
            f'(width {_n(TRACK_W)}) (layer "{layer}") (net {net}) (tstamp {_ts(tag)}))'
        )

    pk = PKG[g["pkg"]]
    rpkg = res_pkg(g["pkg"])
    if "th_model" in pk:
        led_model = model_path("LED_THT", pk["th_model"])
    else:
        led_model = model_path(
            "LED_SMD", f"LED_{g['pkg']}_{PKG_METRIC[g['pkg']]}Metric")
    # "LED on the other side": the LED alone crosses to the far face and a via
    # inside each of its pads carries the connections through, the via-in-pad
    # style other minibadge designers use. The resistor and its trace stay put.
    far = (bool(led.farled) and not g["hole"]
           and "drill" not in PKG[g["pkg"]])
    led_side = ("back" if led.side != "back" else "front") if far else led.side
    route = novia_route(led, pins, safe, others, outline=outline,
                        term=novia_term(led, others, pins, safe, clk), clk=clk)
    avoid_led: tuple = ()
    avoid_res: tuple = ()
    if not tenting and route is None:
        # An exposed via opens a mask aperture right beside the unit's silk
        # (an inline resistor's bracket passes 0.19 mm from the barrel), and
        # ink over open mask is clipped by the fab and flagged by DRC. Break
        # the silk around the aperture instead: same treatment TH dome silk
        # already gets around its pads. Discs are in each footprint's emitted
        # frame: board-oriented mm, relative to its anchor.
        vo = g["via_front"] if front else g["via_back"]
        r_ap = VIA_SIZE / 2 + 0.15
        avoid_led = ((*_r(vo[0], vo[1], ang), r_ap),)
        avoid_res = ((*_r(vo[0] - g["res"][0], vo[1] - g["res"][1], ang), r_ap),)
    parts = [
        # LED: pad 1 = cathode, pad 2 = anode (facing the resistor's pad 2).
        _smd(f"led{i}", f"D{i + 1}", f"LED_{led.color.upper()}",
             *at(0, 0), gnd, an, True, led_side,
             (ang + g.get("led_rot", 0.0)) % 360, g["led_flip"], g["pkg"],
             led_model, silk_avoid=avoid_led),
        _smd(f"res{i}", f"R{i + 1}", f"{LED_COLORS.get(led.color, '220')}R",
             *at(*g["res"]), v33, an, False, led.side,
             (ang + g.get("res_rot", 0.0)) % 360, False, rpkg,
             model_path("Resistor_SMD", f"R_{rpkg}_{PKG_METRIC[rpkg]}Metric"),
             silk_avoid=avoid_res),
    ]
    # Resistor pad 2 to LED anode, through any hand-placed bends.
    apts = [(ORIGIN + px, ORIGIN + py)
            for px, py in unit_trace_pts(led, "a", safe)]
    parts += [seg(a, b, cu, nets[anode], f"seg-a{i}-{n}" if n else f"seg-a{i}")
              for n, (a, b) in enumerate(zip(apts, apts[1:]))]
    if far:
        for tag, off, net in (("k", g["led_k"], nets["GND"]),
                              ("a", g["led_a"], nets[anode])):
            vx, vy = at(*off)
            parts.append(
                f"  (via (at {_n(vx)} {_n(vy)}) (size {_n(VIA_SIZE)}) "
                f'(drill {_n(VIA_DRILL)}) (layers "F.Cu" "B.Cu"){tent} (net {net}) '
                f"(tstamp {_ts(f'padvia-{i}-{tag}')}))"
            )
    if g["hole"]:
        # Reverse-mount: routed hole under the LED so it shines through the
        # board. A second closed Edge.Cuts contour = internal cutout.
        hx, hy = at(0, 0)
        r = g["hole"] / 2
        parts.append(
            f"  (gr_circle (center {_n(hx)} {_n(hy)}) (end {_n(hx + r)} {_n(hy)}) "
            f'(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") '
            f"(tstamp {_ts(f'hole-{i}')}))"
        )
    if route:
        # Via-less: a run across the unit's own layer to a connector pad,
        # whose plated barrel carries the net to the far pour. Electrically
        # the same circuit as the via version, with nothing drilled.
        pts = [(ORIGIN + px, ORIGIN + py) for px, py in route["pts"]]
        for n, (a, b) in enumerate(zip(pts, pts[1:])):
            parts.append(seg(a, b, cu, nets[route["net"]], f"seg-n{i}-{n}"))
    else:
        # Front unit: R pad 1 sits in the F.Cu 3V3 pour; cathode drops to the
        # B.Cu GND pour through a via. Back unit: cathode sits in the B.Cu
        # GND pour; R pad 1 reaches the F.Cu 3V3 pour through a via at the
        # resistor's input end. The stub runs through any hand-placed bends.
        stub_net = nets["GND"] if front else nets["3V3"]
        stub_tag = "k" if front else "v"
        vpts = [(ORIGIN + px, ORIGIN + py)
                for px, py in unit_trace_pts(led, "v", safe)]
        via = vpts[-1]
        parts += [seg(a, b, cu, stub_net,
                      f"seg-{stub_tag}{i}-{n}" if n else f"seg-{stub_tag}{i}")
                  for n, (a, b) in enumerate(zip(vpts, vpts[1:]))]
        parts.append(
            f"  (via (at {_n(via[0])} {_n(via[1])}) (size {_n(VIA_SIZE)}) "
            f'(drill {_n(VIA_DRILL)}) (layers "F.Cu" "B.Cu"){tent} '
            f"(net {stub_net}) (tstamp {_ts(f'via-{i}')}))")
    # A front CLK unit's supply run: F.Cu trace from res_in to the jumper's
    # rail pad or pin 9. (A back CLK unit's supply came through `route`.)
    crun = clk_route(led, pins, safe, others, outline=outline, clk=clk)
    if crun and len(crun["pts"]) > 1:
        cpts = [(ORIGIN + px, ORIGIN + py) for px, py in crun["pts"]]
        for n, (a, b) in enumerate(zip(cpts, cpts[1:])):
            parts.append(seg(a, b, cu, nets[crun["net"]], f"seg-c{i}-{n}"))
    return "\n".join(parts)


POUR_CLEARANCE = 0.35    # copper pour to other-net copper
POUR_EDGE_INSET = 0.4    # copper pour to board edge
POUR_MIN_WIDTH = 0.25    # slivers thinner than this are opened away


def _fill_geometry(zone_net: str, layer: str, spec: BadgeSpec):
    """Compute the filled copper for a zone as shapely polygons (board mm).

    KiCad normally computes zone fills when you press B; we precompute a
    solid-connect fill so the board is electrically complete (and DRC-clean)
    straight out of the zip. Refilling in KiCad simply replaces these.
    """
    from shapely.geometry import LineString, Point, Polygon, box
    from shapely.ops import unary_union

    board = outline_polygon(spec)
    region = board.buffer(-POUR_EDGE_INSET)
    clk = clk_info(spec)

    obstacles = []
    anchors = []  # same-net copper; fill islands must touch one to survive
    for num, px, py, net, _row in connector_pads(clk is not None):
        # through-hole: both layers
        if num not in spec.pins:
            continue
        pad = Point(px, py).buffer(0.875, quad_segs=16)
        if net != zone_net:
            obstacles.append(pad.buffer(POUR_CLEARANCE))
        else:
            anchors.append(pad)

    track_r = TRACK_W / 2 + POUR_CLEARANCE
    safe = unit_safe(spec)
    for i, led in enumerate(spec.leds):
        ang = led.rot
        g = led_geometry(led)
        lx, ly = clamp_led_obj(led, safe)
        rres = g.get("res_rot", 0.0)
        rled = g.get("led_rot", 0.0)
        p = PKG[g["pkg"]]
        rp = PKG[res_pkg(g["pkg"])]
        th = "drill" in p  # TH LED pads penetrate both copper layers
        pw2, ph2 = p["pw"] / 2, p["ph"] / 2
        rw2, rh2 = rp["pw"] / 2, rp["ph"] / 2
        anode = f"/LED{i + 1}_A"
        front = led.side != "back"
        unit_layer = "F.Cu" if front else "B.Cu"
        via_net = "GND" if front else "3V3"

        def at(dx: float, dy: float, lx=lx, ly=ly, ang=ang) -> tuple[float, float]:
            rx, ry = _r(dx, dy, ang)
            return lx + rx, ly + ry

        def pad_box(dx: float, dy: float, extra: float = 0.0, ang=ang, pw2=pw2, ph2=ph2):
            from shapely.affinity import rotate as srotate

            px, py = at(dx, dy)
            b = box(px - pw2, py - ph2, px + pw2, py + ph2)
            # shapely's rotation matrix matches _r's on these coordinates.
            a = (ang + extra) % 360
            return srotate(b, a, origin=(px, py)) if a else b

        vx, vy = at(*(g["via_front"] if front else g["via_back"]))

        # Reverse-mount hole: copper on BOTH layers stays clear of the cutout.
        if g["hole"]:
            obstacles.append(
                Point(*at(0, 0)).buffer(g["hole"] / 2 + POUR_EDGE_INSET, quad_segs=16)
            )

        # The via barrel exists on both copper layers. A via-less unit has
        # none: its power run is a trace on its own layer, handled below.
        # (A back CLK unit's supply run arrives through the same door, so
        # its 3V3 via disappears here exactly like a novia one.)
        route = novia_route(led, spec.pins, safe, spec.leds, outline=spec.outline,
                            term=novia_term(led, spec.leds, spec.pins, safe, clk),
                            clk=clk)
        if route is None:
            if via_net != zone_net:
                obstacles.append(
                    Point(vx, vy).buffer(VIA_SIZE / 2 + POUR_CLEARANCE, quad_segs=16))
            else:
                anchors.append(Point(vx, vy).buffer(VIA_SIZE / 2, quad_segs=16))

        # SMD pads and traces live on the unit's side only; a TH LED's pad
        # barrels reach the other copper layer too (anchoring same-net pour,
        # blocking the other net's).
        far = (bool(led.farled) and not g["hole"]
               and "drill" not in p)
        if far:
            # The LED's own pads live on the far face, and the via inside each
            # of them puts that net on BOTH layers.
            for off, net in ((g["led_k"], "GND"), (g["led_a"], anode)):
                pt = Point(*at(*off))
                if net != zone_net:
                    obstacles.append(pt.buffer(VIA_SIZE / 2 + POUR_CLEARANCE, quad_segs=16))
                else:
                    anchors.append(pt.buffer(VIA_SIZE / 2, quad_segs=16))
        supply = clk["net"] if (clk is not None and led.clk) else "3V3"
        unit_pads = [
            (*g["led_k"], "GND", rled, pw2, ph2, th or far),
            (*g["led_a"], anode, rled, pw2, ph2, th or far),
            (*g["res_in"], supply, rres, rw2, rh2, False),
            (*g["res_out"], anode, rres, rw2, rh2, False),
        ]
        for dx, dy, net, extra, w2, h2, both in unit_pads:
            if layer != unit_layer and not both:
                continue
            pad = pad_box(dx, dy, extra, pw2=w2, ph2=h2)
            if net != zone_net:
                obstacles.append(pad.buffer(POUR_CLEARANCE))
            else:
                anchors.append(pad)
        if layer != unit_layer:
            continue  # the unit's traces live on its own side only
        if zone_net != anode:
            obstacles.append(
                LineString(unit_trace_pts(led, "a", safe)).buffer(track_r))
        # Power stub from the pad to the via, or, for a via-less unit, the
        # long run to the connector pad. Either way the other net's pour on
        # this layer opens a channel around it.
        if route is not None:
            if zone_net != route["net"] and len(route["pts"]) > 1:
                obstacles.append(LineString(route["pts"]).buffer(track_r))
        elif zone_net != via_net:
            obstacles.append(
                LineString(unit_trace_pts(led, "v", safe)).buffer(track_r))
        # A front CLK unit's supply run cuts its own channel across this
        # layer's pour, exactly like a via-less run would.
        crun = clk_route(led, spec.pins, safe, spec.leds,
                         outline=spec.outline, clk=clk)
        if (crun is not None and zone_net != crun["net"]
                and len(crun["pts"]) > 1):
            obstacles.append(LineString(crun["pts"]).buffer(track_r))

    # The CLK jumper's copper: three SMD pads on its own face (a same-net
    # pad merges into that face's pour; the others open clearance holes),
    # via barrels on BOTH layers, feed stubs, and the routed link to pin 9,
    # which cuts a channel like a novia run. A back-side jumper's 3V3 via
    # anchors the FRONT pour: it is what feeds the steady side at all.
    if clk is not None and clk["jumper"]:
        jlayer = "F.Cu" if clk["side"] == "front" else "B.Cu"
        if layer == jlayer:
            for pnet, cx, cy in clk["pads"]:
                pad = Polygon(_jumper_pad_quad(clk, cx, cy))
                if pnet != zone_net:
                    obstacles.append(pad.buffer(POUR_CLEARANCE))
                else:
                    anchors.append(pad)
            jx, jy, _jrot = clk["jumper"]
            if clk["via"]:
                obstacles.append(
                    LineString([(jx, jy), clk["via"]]).buffer(track_r))
            if clk["v3via"]:
                v3 = clk["pads"][2]
                obstacles.append(
                    LineString([(v3[1], v3[2]), clk["v3via"]]).buffer(track_r))
            link = clk_link(spec.leds, spec.pins, safe, spec.outline, clk)
            if link is not None and len(link["pts"]) > 1:
                obstacles.append(LineString(link["pts"]).buffer(track_r))
            v3l = clk_v3_link(spec.leds, spec.pins, safe, spec.outline, clk)
            if (v3l is not None and len(v3l["pts"]) > 1
                    and zone_net != "3V3"):
                obstacles.append(LineString(v3l["pts"]).buffer(track_r))
        if clk["via"]:
            obstacles.append(
                Point(*clk["via"]).buffer(VIA_SIZE / 2 + POUR_CLEARANCE,
                                          quad_segs=16))
        if clk["v3via"]:
            pt = Point(*clk["v3via"])
            if zone_net == "3V3":
                anchors.append(pt.buffer(VIA_SIZE / 2, quad_segs=16))
            else:
                obstacles.append(
                    pt.buffer(VIA_SIZE / 2 + POUR_CLEARANCE, quad_segs=16))

    # Window art strips copper so the laminate shows through. A window layer
    # cuts the face(s) its `window` field names: the webapp splits a glow (or
    # through-bare) drawing into one layer per face, each carved around only
    # that face's copper, so the far face of a unit keeps its via and nothing
    # else, not a slab of pour shadowing the whole part. A hand-built spec's
    # glow layer defaults to "through" and cuts both faces, as before.
    # Expanded 0.1 mm past the mask opening so the copper edge hides under
    # the mask despite fab registration tolerance.
    # Windows never reach the outer 1.5 mm of the outline: the pours keep a
    # continuous perimeter ring, so a full-width window can't split a plane
    # into disconnected halves.
    interior = board.buffer(-1.5)
    face = "front" if layer.startswith("F") else "back"
    for art in spec.art:
        if art.material not in ("glow", "bare"):
            continue
        if art.window not in ("through", face):
            continue  # window does not cut this face; its copper stays
        obstacles += [
            box(rx - 0.1, ry - 0.1, rx + rw + 0.1, ry + rh + 0.1).intersection(interior)
            for rx, ry, rw, rh in art.rects
        ]
        obstacles += [
            g.buffer(0.1).intersection(interior) for g in _art_shapely(art.polys)
        ]

    filled = region.difference(unary_union(obstacles)) if obstacles else region
    # Morphological opening: drop slivers narrower than the zone min_thickness.
    filled = filled.buffer(-POUR_MIN_WIDTH / 2).buffer(POUR_MIN_WIDTH / 2)

    # Copper-material art enclosed by a glow/bare window becomes an isolated
    # island (the window severs it from the plane). Those are intentional
    # decoration (a skull's gold eyes inside a bare face), so they survive
    # the floating-copper filter below (the zone's island_removal_mode keeps
    # them through a KiCad refill as well).
    keep = []
    for art in spec.art:
        if art.material != "copper" or (layer.startswith("F")) != (art.side != "back"):
            continue
        keep += [box(rx, ry, rx + rw, ry + rh) for rx, ry, rw, rh in art.rects]
        keep += _art_shapely(art.polys)
    keep_union = unary_union(keep) if keep else None

    # Drop floating copper BEFORE opening the holes, so the decision is made
    # on the real plane. Doing it afterwards judged the fracture's own
    # fragments: a hole vented out to the edge could fence a live piece of
    # rail off from the pad that feeds it, and the fragment still touched an
    # anchor pad of its own, so it passed the filter and shipped disconnected.
    polys = list(filled.geoms) if filled.geom_type == "MultiPolygon" else [filled]
    anchor_union = unary_union(anchors)
    def alive(p):
        return (not p.is_empty and p.area > 0.05
                and (p.intersects(anchor_union)
                     or keep_union is not None and p.intersects(keep_union)))

    polys = [p.simplify(0.005) for p in polys if alive(p)]
    # Then make each island representable: KiCad stores fills as simple
    # outlines with no interior rings, so every hole has to be vented out to
    # the boundary. `_open_holes` keeps the island in one piece while it does
    # that; the filter runs again only to catch the fragments its last-resort
    # branch can leave behind.
    out = []
    for p in polys:
        out += _open_holes(p)
    return [p for p in out if alive(p)]


SLIT_W = 0.12     # width of the hole-venting slit (see _open_holes)
SLIVER_W = 0.10   # hairs this thin are shaved off the vented fill


def _open_holes(poly) -> list:
    """Vent a fill polygon's holes to its boundary without severing the plane.

    ``(filled_polygon (pts ...))`` has no syntax for an interior ring and
    KiCad treats every polygon in a zone as its own island (even where two of
    them share an edge or overlap, both measured here), so each hole has to be
    opened out to the boundary by actually removing a slit of copper.

    A slit from a hole to the boundary is topologically harmless on its own.
    Running every slit in the same direction, out to the same board edge, is
    not: two of them either side of a unit run straight through the perimeter
    ring that is supposed to keep the plane continuous and fence the unit's
    rail onto an island wired to nothing. The board passes every gate
    ``/generate`` applies, the user downloads it, and the LED never lights.

    So each hole is vented one at a time, shortest slit first, and a direction
    is only accepted if the piece it leaves behind is still in one piece.
    """
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.geometry import box

    def parts(geom):
        return [g for g in getattr(geom, "geoms", [geom])
                if g.geom_type == "Polygon" and not g.is_empty]

    pending, done = [poly], []
    # One hole is vented per pass, and every pass strictly reduces the hole
    # count of the piece it touches, so this terminates; the cap is only there
    # so a degenerate ring cannot spin.
    for _ in range(400):
        if not pending:
            break
        p = pending.pop()
        if not p.interiors:
            done.append(p)
            continue
        # The slit must start at a point genuinely inside the void: a hole's
        # bounding-box centre lands on copper for C/U-shaped holes, which
        # would leave the hole intact and let the emitter flood other-net pads.
        pt = ShapelyPolygon(p.interiors[0]).representative_point()
        x0, y0, x1, y1 = p.bounds
        h = SLIT_W / 2
        cands = sorted((
            (y1 + 1.0 - pt.y, box(pt.x - h, pt.y, pt.x + h, y1 + 1.0)),
            (pt.y - (y0 - 1.0), box(pt.x - h, y0 - 1.0, pt.x + h, pt.y)),
            (x1 + 1.0 - pt.x, box(pt.x, pt.y - h, x1 + 1.0, pt.y + h)),
            (pt.x - (x0 - 1.0), box(x0 - 1.0, pt.y - h, pt.x, pt.y + h)),
        ), key=lambda c: c[0])
        cut = None
        for _len, slit in cands:
            trial = p.difference(slit)
            # One piece out means the vent reduced the hole count and nothing
            # else: no fenced-off island, so no stranded rail.
            if trial.geom_type == "Polygon" and not trial.is_empty:
                cut = trial
                break
        if cut is None:
            # Every direction fences something. Take the shortest slit and let
            # the floating-copper filter in the caller judge the fragments,
            # strictly better than shipping an unrepresentable hole.
            cut = p.difference(cands[0][1])
        pending += parts(cut)

    # A slit that grazes another void leaves a hair of copper beside it, and
    # KiCad reports those as [copper_sliver]; a fab flags them, and a sliver
    # that lifts can bridge to whatever it lands on. Shave them off with a
    # morphological opening well under POUR_MIN_WIDTH, so it cannot touch any
    # neck the zone's own min_thickness already guarantees, and only where the
    # piece stays whole and hole-free afterwards.
    out = []
    for p in done + pending:
        if p.area <= 1e-9 or p.interiors:
            continue
        shaved = p.buffer(-SLIVER_W / 2).buffer(SLIVER_W / 2)
        out.append(shaved if (shaved.geom_type == "Polygon" and not shaved.is_empty
                              and not shaved.interiors) else p)
    return out


def _window_geometry(spec: BadgeSpec, face: str | None = None):
    """The glow/bare light windows as shapely polygons (board mm), or None.

    Same shapes _fill_geometry cuts out of the pours: expanded 0.1 mm past
    the mask opening and held inside the perimeter ring. Copper artwork that
    sits *inside* a window (a skull's gold eyes in a bare face) is carved
    back out, so it keeps its copper.

    With a face given, only windows that actually cut copper there count;
    every window layer (glow included) carries the face(s) it cuts in its
    `window` field, and a layer for one face leaves the other's copper alone.
    """
    from shapely.ops import unary_union

    from shapely.geometry import box

    board = outline_polygon(spec)
    interior = board.buffer(-1.5)
    wins = []
    for art in spec.art:
        if art.material not in ("glow", "bare"):
            continue
        if face and art.window not in ("through", face):
            continue
        wins += [box(rx - 0.1, ry - 0.1, rx + rw + 0.1, ry + rh + 0.1).intersection(interior)
                 for rx, ry, rw, rh in art.rects]
        wins += [g.buffer(0.1).intersection(interior) for g in _art_shapely(art.polys)]
    if not wins:
        return None
    keep = []
    for art in spec.art:
        if art.material != "copper":
            continue
        keep += [box(rx, ry, rx + rw, ry + rh) for rx, ry, rw, rh in art.rects]
        keep += _art_shapely(art.polys)
    geom = unary_union(wins)
    if keep:
        geom = geom.difference(unary_union(keep))
    # Sit a hair inside the copper cutout. Sharing an edge with the shipped
    # fill reads to DRC as the pour intruding on the keepout; 20 µm of slack
    # costs nothing on a refill and keeps the board clean as shipped.
    geom = geom.buffer(-0.02)
    return geom if not geom.is_empty else None


def _keepout_zones(spec: BadgeSpec) -> list[str]:
    """Rule areas that stop KiCad pouring copper back into the light windows.

    The shipped fills already exclude the windows, but a zone fill is not a
    fixed artifact: the moment anyone refills (pressing B, as the README
    asks), KiCad recomputes from its own rules (which know nothing about
    why that copper is missing) and floods the windows solid, quietly
    turning every glow/bare window back into ordinary board. A keepout
    encodes the intent so the refill agrees with us.
    """
    out = []
    for face, layer in (("front", "F.Cu"), ("back", "B.Cu")):
        out += _keepout_zones_for(spec, face, layer)
    return out


def _keepout_zones_for(spec: BadgeSpec, face: str, layer: str) -> list[str]:
    """The keepout rule areas for one face (see _keepout_zones)."""
    geom = _window_geometry(spec, face)
    if geom is None:
        return []
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    out = []
    for i, poly in enumerate(polys):
        if poly.is_empty or poly.area < 0.01:
            continue
        # A hole here just means "copper allowed", and any sliver the
        # fracture leaves behind is thinner than the zone's min_thickness,
        # so it never becomes copper.
        ring = poly.exterior if not poly.interiors else _fracture(poly).exterior
        pts = " ".join(f"(xy {_n(ORIGIN + x)} {_n(ORIGIN + y)})"
                       for x, y in ring.coords[:-1])
        out.append(
            f'  (zone (net 0) (net_name "") (layer "{layer}") '
            f'(tstamp {_ts(f"keepout-{face}-{i}")}) (hatch edge 0.508)\n'
            "    (connect_pads (clearance 0))\n"
            "    (min_thickness 0.25)\n"
            "    (keepout (tracks allowed) (vias allowed) (pads allowed) "
            "(copperpour not_allowed) (footprints allowed))\n"
            "    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
            f"    (polygon (pts {pts}))\n"
            "  )"
        )
    return out


def _fracture(poly):
    """Drop a polygon's holes by slitting each one out to the boundary."""
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.geometry import box
    from shapely.ops import unary_union

    bottom = poly.bounds[3] + 1.0
    for _ in range(8):
        if not poly.interiors:
            break
        slits = [box(ShapelyPolygon(r).representative_point().x - 0.06,
                     ShapelyPolygon(r).representative_point().y,
                     ShapelyPolygon(r).representative_point().x + 0.06, bottom)
                 for r in poly.interiors]
        cut = poly.difference(unary_union(slits))
        poly = max(cut.geoms, key=lambda g: g.area) if cut.geom_type == "MultiPolygon" else cut
    return poly


def _zone(key: str, net: int, net_name: str, layer: str, spec: BadgeSpec) -> str:
    x0, y0, x1, y1 = (v + ORIGIN for v in outline_polygon(spec).bounds)
    out = [
        (f'  (zone (net {net}) (net_name "{net_name}") (layer "{layer}") (tstamp {_ts(key)}) '
        "(hatch edge 0.508)"),
        "    (connect_pads yes (clearance 0.3))",
        "    (min_thickness 0.25) (filled_areas_thickness no)",
        "    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 1) (island_area_min 0))",
        (f"    (polygon (pts (xy {_n(x0)} {_n(y0)}) (xy {_n(x1)} {_n(y0)}) "
        f"(xy {_n(x1)} {_n(y1)}) (xy {_n(x0)} {_n(y1)})))"),
    ]
    for poly in _fill_geometry(net_name, layer, spec):
        if poly.interiors:
            # Emitting only the exterior would pour copper over whatever the
            # hole was protecting; refuse rather than generate a short.
            raise ValueError(f"zone fill for {net_name}/{layer} still has holes")
        pts = " ".join(
            f"(xy {_n(ORIGIN + px)} {_n(ORIGIN + py)})" for px, py in poly.exterior.coords[:-1]
        )
        out.append(f'    (filled_polygon (layer "{layer}") (pts {pts}))')
    out.append("  )")
    return "\n".join(out)


def _art_polys(
    rects: list[tuple[float, float, float, float]], layer: str, key: str
) -> list[str]:
    out = []
    for i, (x, y, w, h) in enumerate(rects):
        x0, y0 = ORIGIN + x, ORIGIN + y
        x1, y1 = x0 + w, y0 + h
        out.append(
            f"  (gr_poly (pts (xy {_n(x0)} {_n(y0)}) (xy {_n(x1)} {_n(y0)}) "
            f"(xy {_n(x1)} {_n(y1)}) (xy {_n(x0)} {_n(y1)})) "
            f'(stroke (width 0) (type solid)) (fill solid) (layer "{layer}") '
            f"(tstamp {_ts(f'{key}-{i}')}))"
        )
    return out


def _art_shapely(polys: list[list[list[tuple[float, float]]]]) -> list:
    """ArtLayer.polys as shapely Polygons (invalid input healed, empties dropped)."""
    from shapely.geometry import Polygon

    out = []
    for rings in polys:
        if not rings or len(rings[0]) < 3:
            continue
        poly = Polygon(rings[0], [r for r in rings[1:] if len(r) >= 3]).buffer(0)
        if not poly.is_empty:
            out.append(poly)
    return out


def _slit_holes(geom, half_w: float):
    """Open every interior ring to the outside with a hairline slit.

    gr_poly (like zone fills) can't represent holes; a slit far below any
    printable feature size (2*half_w wide) turns each hole into an edge
    notch that fabs (and eyes) can't tell from a true hole.
    """
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.geometry import box
    from shapely.ops import unary_union

    for _ in range(8):
        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        slits = []
        for poly in polys:
            for ring in poly.interiors:
                pt = ShapelyPolygon(ring).representative_point()
                slits.append(box(pt.x - half_w, pt.y, pt.x + half_w, poly.bounds[3] + 1.0))
        if not slits:
            break
        geom = geom.difference(unary_union(slits))
    return [p for p in (list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom])
            if not p.is_empty]


def _art_vector_items(
    polys: list[list[list[tuple[float, float]]]], layer: str, key: str
) -> list[str]:
    """Exact art polygons as filled gr_poly items (holes become slits)."""
    from shapely.ops import unary_union

    geoms = _art_shapely(polys)
    if not geoms:
        return []
    out = []
    for i, poly in enumerate(_slit_holes(unary_union(geoms), 0.01)):
        pts = " ".join(
            f"(xy {_n(ORIGIN + px)} {_n(ORIGIN + py)})" for px, py in poly.exterior.coords[:-1]
        )
        out.append(
            f"  (gr_poly (pts {pts}) "
            f'(stroke (width 0) (type solid)) (fill solid) (layer "{layer}") '
            f"(tstamp {_ts(f'{key}-v{i}')}))"
        )
    return out


def _window_mask_covers(spec: BadgeSpec, bridges: dict, safe) -> dict:
    """Per mask layer, the soldermask kept over copper that crosses windows.

    A bare window's mask opening used to expose whatever copper crossed it:
    the hairline perimeter bridges plated bare (a trace with no mask is a
    corrosion and short hazard, and no fab would leave it that way) and a
    via lost the tenting the rest of the board gives it. Every bridge now
    keeps a dam of mask over its track, and (while the board's tenting
    option is on) every via keeps its cap. Returns {layer: geometry | None};
    the copper cut is untouched; only the mask opening shrinks.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union

    covers: dict = {"F.Mask": [], "B.Mask": []}
    clk = clk_info(spec)
    for i, led in enumerate(spec.leds):
        for layer, mask in (("F.Cu", "F.Mask"), ("B.Cu", "B.Mask")):
            seg = bridges.get(i, {}).get(layer)
            if seg:
                covers[mask].append(
                    LineString(seg).buffer(TRACK_W / 2 + 0.1, quad_segs=8))
        # A back CLK unit's 3V3 via is gone (its supply runs as a trace), so
        # there is no barrel to cap.
        has_via = not led.novia and not (clk is not None and led.clk
                                         and led.side == "back")
        if spec.tenting and has_via:
            g = led_geometry(led)
            x, y = clamp_led_obj(led, safe)
            vo = g["via_front"] if led.side != "back" else g["via_back"]
            rx, ry = _r(vo[0], vo[1], led.rot)
            # The barrel crosses the whole board: cap it on both faces.
            disc = Point(x + rx, y + ry).buffer(VIA_SIZE / 2 + 0.1, quad_segs=16)
            covers["F.Mask"].append(disc)
            covers["B.Mask"].append(disc)
    if spec.tenting and clk is not None:
        for v in (clk["via"], clk["v3via"]):
            if not v:
                continue
            disc = Point(*v).buffer(VIA_SIZE / 2 + 0.1, quad_segs=16)
            covers["F.Mask"].append(disc)
            covers["B.Mask"].append(disc)
    return {k: (unary_union(v) if v else None) for k, v in covers.items()}


def _art_mask_items(art: ArtLayer, layer: str, key: str, cover) -> list[str]:
    """A bare window's mask opening, minus the mask kept over crossing copper.

    Falls back to the plain emitters when the cover misses this layer's
    shapes entirely, so untouched windows keep their rect-per-rect output.
    """
    from shapely.geometry import box
    from shapely.ops import unary_union

    geoms = [box(x, y, x + w, y + h) for x, y, w, h in art.rects]
    geoms += _art_shapely(art.polys)
    if not geoms:
        return []
    geom = unary_union(geoms)
    if not geom.intersects(cover):
        return (_art_polys(art.rects, layer, key)
                + _art_vector_items(art.polys, layer, key))
    geom = geom.difference(cover)
    if geom.is_empty:
        return []
    out = []
    for i, poly in enumerate(_slit_holes(geom, 0.01)):
        pts = " ".join(
            f"(xy {_n(ORIGIN + px)} {_n(ORIGIN + py)})" for px, py in poly.exterior.coords[:-1]
        )
        out.append(
            f"  (gr_poly (pts {pts}) "
            f'(stroke (width 0) (type solid)) (fill solid) (layer "{layer}") '
            f"(tstamp {_ts(f'{key}-m{i}')}))"
        )
    return out


# Which drawn layers an art material paints, per board face. Mask layers are
# negatives: a polygon on F.Mask/B.Mask is an *opening* in the soldermask.
# "glow" draws nothing: it only cuts the copper pours (see _fill_geometry).
def _art_target_layers(
    material: str, side: str = "front", window: str = "through"
) -> tuple[str, ...]:
    back = side == "back"
    if material == "silk":
        return ("B.SilkS",) if back else ("F.SilkS",)
    if material == "copper":
        return ("B.Mask",) if back else ("F.Mask",)
    if material == "bare":
        if window == "front":
            return ("F.Mask",)
        if window == "back":
            return ("B.Mask",)
        return ("F.Mask", "B.Mask")
    return ()


def _text_items(spec: BadgeSpec) -> list[str]:
    out = []
    for i, t in enumerate(spec.texts):
        if t.font != "kicad":
            continue  # TTF texts arrive as ArtLayer polygons instead
        content = _esc(t.text.strip())
        if not content:
            continue
        front = t.side != "back"
        layer = "F.SilkS" if front else "B.SilkS"
        mirror = "" if front else " (justify mirror)"
        thickness = round(t.size * 0.15, 3)
        # KiCad text angles count counterclockwise; ours run clockwise.
        rot = (-float(t.rot)) % 360
        at = (f"{_n(ORIGIN + t.x)} {_n(ORIGIN + t.y)}"
              + (f" {_n(rot)}" if rot else ""))
        out.append(
            f'  (gr_text "{content}" (at {at}) (layer "{layer}") '
            f"(tstamp {_ts(f'text-{i}')})\n"
            f"    (effects (font (size {_n(t.size)} {_n(t.size)}) (thickness {_n(thickness)})){mirror})\n"
            "  )"
        )
    return out


def generate_pcb(spec: BadgeSpec) -> str:
    net_lines, nets = _nets(spec)
    x0, y0, x1, y1 = (v + ORIGIN for v in OUTLINE)

    # The physical stackup carries the chosen mask color and surface
    # finish, so KiCad's 3D viewer (and the app's render preview) shows the
    # board the way the fab would build it: green/purple/... mask, white
    # silk, gold (ENIG) or silver (HASL) exposed metal.
    mask = spec.mask_color.capitalize()
    finish = "ENIG" if spec.finish != "hasl" else "HAL lead-free"
    stackup = (
        "  (setup\n"
        "    (stackup\n"
        '      (layer "F.SilkS" (type "Top Silk Screen") (color "White"))\n'
        '      (layer "F.Paste" (type "Top Solder Paste"))\n'
        f'      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01) (color "{mask}"))\n'
        '      (layer "F.Cu" (type "copper") (thickness 0.035))\n'
        '      (layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))\n'
        '      (layer "B.Cu" (type "copper") (thickness 0.035))\n'
        f'      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01) (color "{mask}"))\n'
        '      (layer "B.Paste" (type "Bottom Solder Paste"))\n'
        '      (layer "B.SilkS" (type "Bottom Silk Screen") (color "White"))\n'
        f'      (copper_finish "{finish}")\n'
        "      (dielectric_constraints no)\n"
        "    )\n"
        "    (pad_to_mask_clearance 0)\n"
        "  )"
    )
    clk = clk_info(spec)
    body: list[str] = [
        '(kicad_pcb (version 20221018) (generator "minibadge-designer")',
        "  (general (thickness 1.6))",
        '  (paper "A4")',
        LAYERS,
        stackup,
        *net_lines,
        _connector_footprint(nets, spec.pins, clk is not None),
    ]
    safe = unit_safe(spec)
    tent = "" if spec.tenting else " (tenting none)"
    if clk is not None and clk["jumper"]:
        body.append(_jumper_footprint(nets, clk))
        jx, jy, _jrot = clk["jumper"]
        jcu = "F.Cu" if clk["side"] == "front" else "B.Cu"
        if clk["via"]:
            # The rail via: the plated hole the OTHER face's supply runs
            # land on, fed from the centre pad by a short stub.
            vx, vy = clk["via"]
            body.append(
                f"  (segment (start {_n(ORIGIN + jx)} {_n(ORIGIN + jy)}) "
                f"(end {_n(ORIGIN + vx)} {_n(ORIGIN + vy)}) (width {_n(TRACK_W)}) "
                f'(layer "{jcu}") (net {nets[CLK_RAIL]}) (tstamp {_ts("jumper-stub")}))'
            )
            body.append(
                f"  (via (at {_n(ORIGIN + vx)} {_n(ORIGIN + vy)}) (size {_n(VIA_SIZE)}) "
                f'(drill {_n(VIA_DRILL)}) (layers "F.Cu" "B.Cu"){tent} '
                f"(net {nets[CLK_RAIL]}) (tstamp {_ts('jumper-via')}))"
            )
        if clk["v3via"]:
            # A back-side jumper's steady option: its 3V3 pad rises to the
            # front pour through this via.
            v3 = clk["pads"][2]
            vx, vy = clk["v3via"]
            body.append(
                f"  (segment (start {_n(ORIGIN + v3[1])} {_n(ORIGIN + v3[2])}) "
                f"(end {_n(ORIGIN + vx)} {_n(ORIGIN + vy)}) (width {_n(TRACK_W)}) "
                f'(layer "{jcu}") (net {nets["3V3"]}) (tstamp {_ts("jumper-3v3-stub")}))'
            )
            body.append(
                f"  (via (at {_n(ORIGIN + vx)} {_n(ORIGIN + vy)}) (size {_n(VIA_SIZE)}) "
                f'(drill {_n(VIA_DRILL)}) (layers "F.Cu" "B.Cu"){tent} '
                f"(net {nets['3V3']}) (tstamp {_ts('jumper-3v3-via')}))"
            )
        link = clk_link(spec.leds, spec.pins, safe, spec.outline, clk)
        if link is not None:
            lpts = [(ORIGIN + px, ORIGIN + py) for px, py in link["pts"]]
            for n, (a, b) in enumerate(zip(lpts, lpts[1:])):
                body.append(
                    f"  (segment (start {_n(a[0])} {_n(a[1])}) "
                    f"(end {_n(b[0])} {_n(b[1])}) (width {_n(TRACK_W)}) "
                    f'(layer "{jcu}") (net {nets["CLK"]}) '
                    f"(tstamp {_ts(f'jumper-link-{n}')}))"
                )
        v3l = clk_v3_link(spec.leds, spec.pins, safe, spec.outline, clk)
        if v3l is not None and len(v3l["pts"]) > 1:
            vpts = [(ORIGIN + px, ORIGIN + py) for px, py in v3l["pts"]]
            for n, (a, b) in enumerate(zip(vpts, vpts[1:])):
                body.append(
                    f"  (segment (start {_n(a[0])} {_n(a[1])}) "
                    f"(end {_n(b[0])} {_n(b[1])}) (width {_n(TRACK_W)}) "
                    f'(layer "{jcu}") (net {nets["3V3"]}) '
                    f"(tstamp {_ts(f'jumper-v3link-{n}')}))"
                )
    bridges = unit_bridges(spec, safe)
    for i, led in enumerate(spec.leds):
        body.append(_led_unit(i, led, nets, safe, spec.pins, spec.leds,
                              spec.outline, spec.tenting, clk))
        for layer, net in (("F.Cu", "3V3"), ("B.Cu", "GND")):
            seg = bridges.get(i, {}).get(layer)
            if seg is None:
                continue  # webapp reserves the old corridor instead
            (ax, ay), (bx, by) = seg
            body.append(
                f"  (segment (start {_n(ORIGIN + ax)} {_n(ORIGIN + ay)}) "
                f"(end {_n(ORIGIN + bx)} {_n(ORIGIN + by)}) (width {_n(TRACK_W)}) "
                f'(layer "{layer}") (net {nets[net]}) (tstamp {_ts(f"bridge-{i}-{layer}")}))'
            )

    if spec.outline:
        for ri, ring in enumerate(spec.outline):
            pts = " ".join(f"(xy {_n(ORIGIN + px)} {_n(ORIGIN + py)})" for px, py in ring)
            body.append(
                f"  (gr_poly (pts {pts}) (stroke (width 0.12) (type solid)) "
                f'(fill none) (layer "Edge.Cuts") (tstamp {_ts(f"edge-{ri}")}))'
            )
    else:
        body.append(
            f"  (gr_rect (start {_n(x0)} {_n(y0)}) (end {_n(x1)} {_n(y1)}) "
            f'(stroke (width 0.12) (type solid)) (fill none) (layer "Edge.Cuts") (tstamp {_ts("edge")}))'
        )
    covers = _window_mask_covers(spec, bridges, safe)
    for ai, art in enumerate(spec.art):
        for layer in _art_target_layers(art.material, art.side, art.window):
            cover = covers.get(layer) if art.material == "bare" else None
            if cover is not None:
                body.extend(_art_mask_items(art, layer, f"art{ai}-{layer}", cover))
            else:
                body.extend(_art_polys(art.rects, layer, f"art{ai}-{layer}"))
                body.extend(_art_vector_items(art.polys, layer, f"art{ai}-{layer}"))
    body.extend(_text_items(spec))
    body.extend(_keepout_zones(spec))
    body += [
        _zone("zone-3v3", nets["3V3"], "3V3", "F.Cu", spec),
        _zone("zone-gnd", nets["GND"], "GND", "B.Cu", spec),
        ")",
    ]
    return "\n".join(body) + "\n"


def generate_project(name: str) -> str:
    # min_copper_edge_clearance 0.2: the official minibadge connector pads sit
    # 0.235 mm from the outline, so KiCad's 0.25 default false-flags them.
    # min_text_height 0.6: the printed pin captions are 0.6 mm silk, small
    # but well within what fabs print; user text stays >= 0.8 in the UI.
    # lib_footprint_issues ignored: all footprints are embedded in the board.
    return (
        "{\n"
        '  "board": { "design_settings": {\n'
        '    "rules": {\n'
        '      "min_clearance": 0.15, "min_copper_edge_clearance": 0.2, "min_track_width": 0.2,\n'
        '      "min_text_height": 0.6, "min_text_thickness": 0.1\n'
        "    },\n"
        '    "rule_severities": {\n'
        '      "lib_footprint_issues": "ignore", "lib_footprint_mismatch": "ignore",\n'
        '      "isolated_copper": "ignore"\n'
        "    }\n"
        "  } },\n"
        f'  "meta": {{ "filename": "{_esc(name)}.kicad_pro", "version": 1 }}\n'
        "}\n"
    )


def generate_bom(spec: BadgeSpec) -> str:
    clk = clk_info(spec)
    lines = ["Reference,Value,Footprint,Side,Qty,Notes"]
    if clk is not None and clk["jumper"]:
        lines.append(
            "JP1,CLK/3V3 select,solder jumper 3 pads (no part; bridge with "
            f"solder),{clk['side']},1,bridge the 3V3 side for steady LEDs or "
            "the CLK side to blink with the badge; NEVER bridge both"
        )
    for i, led in enumerate(spec.leds):
        side = "back" if led.side == "back" else "front"
        g = led_geometry(led)
        pkg = g["pkg"]
        rpkg = res_pkg(pkg)
        if "drill" in PKG[pkg]:
            fp = f"LED {pkg} {PKG[pkg]['th_desc']} TH (2.54 mm pitch)"
            note = "through-hole: short lead (cathode) toward silkscreen bar"
        else:
            fp = f"LED {pkg} ({PKG_METRIC.get(pkg, '')} metric)"
            note = "cathode toward silkscreen bar"
        if g["hole"]:
            note = ("reverse-mount: solder upside-down over the hole so it "
                    "shines through the board; cathode toward silkscreen bar")
        if led.farled:
            far = "front" if side == "back" else "back"
            note += f"; mounts on the {far} face (via in each pad)"
        if led.novia:
            if led.term and led.term[0] == "unit":
                note += f"; no via, chained onto LED D{int(led.term[1]) + 1}'s pad"
            else:
                note += "; no via, wired to a connector pad"
        if led.clk and clk is not None:
            note += ("; blinks with the badge CLK (via the JP1 jumper)"
                     if clk["jumper"]
                     else "; blinks with the badge CLK (wired to pin 9)")
        led_side = ("front" if side == "back" else "back") if led.farled else side
        lines.append(f"D{i + 1},LED {led.color},{fp},{led_side},1,{note}")
        r = LED_COLORS.get(led.color, "220")
        lines.append(
            f"R{i + 1},{r} ohm,R {rpkg} ({PKG_METRIC.get(rpkg, '')} metric),{side},1,"
            f"series resistor for D{i + 1}"
        )
    return "\n".join(lines) + "\n"


def generate_readme(spec: BadgeSpec, slug: str | None = None) -> str:
    led_desc = ", ".join(f"D{i + 1} ({led.color})" for i, led in enumerate(spec.leds)) or "none"
    slug = slug or spec.name
    holes = 0
    if spec.outline:
        b = outline_polygon(spec).bounds
        # Rings after the first are interior contours: real routed holes. The
        # fab quotes and tools for those, so the count belongs up front rather
        # than only in the geometry.
        holes = max(0, len(spec.outline) - 1)
        board_desc = (
            f"custom outline, {b[2] - b[0]:.1f} x {b[3] - b[1]:.1f} mm bounding box "
            "(minibadge v2 connector)"
        )
        if holes:
            board_desc += (
                f", with {holes} routed cutout{'s' if holes != 1 else ''} "
                "through the board"
            )
    else:
        board_desc = "20 x 20 mm, minibadge v2 standard"
    finish_desc = (
        "lead-free HASL (shiny SILVER pads and copper art)"
        if spec.finish == "hasl"
        else "ENIG (shiny GOLD pads and copper art)"
    )
    clk = clk_info(spec)
    if clk is None:
        clk_desc = ("Power comes from the badge's 3V3 pins. VBATT, CLK, and "
                    "NC pins are left\nunconnected, per the standard (never "
                    "connect NC; never tie VBATT to 3V3).")
    else:
        blinkers = ", ".join(f"D{i + 1}" for i, led in enumerate(spec.leds)
                             if led.clk)
        if clk["jumper"]:
            clk_desc = (
                f"LEDs {blinkers} run off the badge's CLK (blink) line "
                "through the JP1 solder\njumper. Bridge JP1's centre pad to "
                "its 3V3 side for steady light, or to its\nCLK side to blink "
                "with the badge. Bridge exactly ONE side; bridging both\n"
                "ties the badge's shared clock line to 3V3. Until a side is "
                "bridged those\nLEDs stay dark. Power for everything else "
                "comes from the 3V3 pins. VBATT\nand NC stay unconnected, "
                "per the standard.")
        else:
            clk_desc = (
                f"LEDs {blinkers} are wired straight to the badge's CLK "
                "(blink) line on pin 9,\nso they pulse with the badge clock. "
                "Power for everything else comes from\nthe 3V3 pins. VBATT "
                "and NC stay unconnected, per the standard.")
    return f"""{spec.name}: minibadge
=================================

Generated by minibadge designer. Board: {board_desc}.
LEDs: {led_desc}. Suggested soldermask color: {spec.mask_color}.
Surface finish to order: {finish_desc}.

Open and finish in KiCad (7 or newer)
-------------------------------------
1. Open {slug}.kicad_pro in KiCad and open the PCB editor.
2. Press B to fill the copper zones (front = 3V3, back = GND).
   The LED circuits connect through these pours. Do not skip this.
3. Run DRC (Inspect > Design Rules Checker) and confirm no errors. Silkscreen
   *warnings* are possible where you deliberately put text or art over a pad:
   those are cosmetic, and the fab clips the overlap when it prints.
4. File > Fabrication Outputs > Gerbers (plot all layers + drill files),
   zip them, and upload to your fab (JLCPCB, PCBWay, OSH Park, ...),
   or use the designer's "Gerbers for fab" button, which plots this exact
   package for you. OSH Park also accepts the .kicad_pcb itself.
   Order 1.6 mm thickness, 2 layers, surface finish as noted above (the
   finish sets whether exposed metal comes out gold or silver).

Assembly and how the wiring works
---------------------------------
Each LED is wired 3V3 -> resistor -> LED -> GND. The power connections are
made through the copper pours rather than visible traces: the front copper
layer is one solid 3V3 plane and the back layer is a GND plane.

- Front-side LED: the resistor's input pad connects directly into the front
  3V3 pour; the LED cathode runs through a short trace to a via that drops
  into the back GND pour.
- Back-side LED: the LED cathode connects directly into the back GND pour;
  the resistor's input pad runs through a short trace to a via that rises
  to the front 3V3 pour.

A unit with "LED on the other side" mounts its LED on the opposite face from
its resistor, with a via inside each of the LED's pads carrying the
connections through the board, so the LED faces out while the resistor hides
behind it.

Units with "No power via" ticked run power as a trace to a
connector pad instead, whose plated hole carries the net to the other side.
A front-side through-hole LED needs no extra copper at all, since its own
leads are already plated through to the back pour.

So in the PCB editor a resistor pad or via can look "unattached": it is
connected by the pour (check with the highlight-net tool). LED cathode goes
toward the silkscreen bar next to the footprint; on a through-hole LED that
is the SHORT lead (the flat side of the lens). See BOM.csv for parts and
which side each part mounts on. SMD pads use KiCad's hand-solder proportions
(extra copper past each end of the chip for the iron tip and a visible
fillet), and a through-hole LED's resistor stays SMD. Note that a back-side LED faces the
badge when plugged in, so it is only visible from behind unless you use a
reverse-mount LED over a via/hole.

{clk_desc}

Artwork materials
-----------------
Silkscreen art is white ink. "Exposed copper" art is a soldermask opening
over the copper plane (gold/ENIG finish shows). "Glow window" art strips
the copper from both layers but keeps the mask: light from a nearby
back-side LED diffuses through the laminate and glows in the mask color.
"Bare board" also opens the mask on both sides (raw laminate, brightest).
"Cut through board" is not ink at all: those regions are removed from the
board outline, so they arrive as closed contours on Edge.Cuts and the fab
routs them clean through the laminate. They need no special handling, but
they are a routing operation rather than artwork -- check the Edge.Cuts
layer matches what you expect before ordering. Connector pads always keep
a tab of board, so a cut can never sever the badge from its mount.
The shipped zone fills route thin fracture slits from copper cutouts to
the board edge (a file-format requirement); refilling zones in KiCad (B)
replaces them with proper holes, so always refill before plotting. Glow
and bare windows carry keepout rule areas, so refilling leaves them
clear instead of pouring copper back into the light path.

Credits
-------
Minibadge standard and connector footprint: (c) Luke Jenkins and
contributors, https://github.com/lukejenkins/minibadge (Apache-2.0).
"""
