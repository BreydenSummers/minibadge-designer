"""KiCad PCB generation for SAINTCON minibadges.

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
    ("9", 1.27, 19.05, None, "bottom"),  # CLK (unused)
    ("10", 3.81, 19.05, None, "bottom"), # NC (reserved, never connect)
    ("15", 16.51, 19.05, "3V3", "bottom"),
    ("16", 19.05, 19.05, "GND", "bottom"),
]

# Pin captions printed on BOTH silkscreens, one per pad pair, tucked inside
# the pad keepout strip (y <= 2.5 / >= 17.82) where LED units can never sit —
# so they can't collide with unit silk and never clip the board edge.
PAD_LABELS = [
    ("VBAT GND", 2.54, 2.62, "top"),
    ("3V3 GND", 17.78, 2.62, "top"),
    ("CLK NC", 2.54, 17.7, "bottom"),
    ("3V3 GND", 17.78, 17.7, "bottom"),
]

# Minimal board tabs that carry each connector pad *pair*. Custom outlines
# union only these (never a full-width strip), so the image's own cuts win
# everywhere except directly under the pads — the silhouette shapes the
# whole edge, and pads always sit on solid material.
PAD_PLATES = {
    "top": ((0.16, 0.16, 5.0, 3.4), (15.32, 0.16, 20.16, 3.4)),
    "bottom": ((0.16, 16.92, 5.0, 20.16), (15.32, 16.92, 20.16, 20.16)),
}

# Custom outlines may extend this far beyond the standard square — three
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
#            Long and thin, ~10 x 2.8 mm — lays along a board edge. The LED
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
# 0.254 mm. Keeping the pad at 0.7 leaves a 0.2 mm annular ring — same ring as
# KiCad's 0.8/0.4 default, and clear of PCBWay's 0.15 mm minimum rather than
# sitting exactly on it — while the smaller hole eats less copper out of the
# pours and tents under soldermask more reliably.
VIA_SIZE, VIA_DRILL = 0.7, 0.3

# Region the (rotated) unit bbox must stay inside on the standard square:
# 0.54 mm in from the board edge. Custom outlines widen this to their own
# bounding box (see unit_safe); actual outline containment is checked
# separately. Clearance to the connector pads is NOT part of this region —
# that is per pad pair (PAD_KEEPOUTS), so units may sit between the pads.
UNIT_SAFE = (0.7, 0.7, 19.62, 19.62)

# Keepout boxes around each connector pad *pair*: the pads' copper (1.75 mm
# circles) expanded by 0.35 mm pour/DRC clearance. A unit bbox may not
# overlap a kept row's boxes, but the strip between the two pairs — and the
# strip of a dropped row — is fair game.
# The extra 0.5 mm beyond the pads covers the printed pin captions, so a
# unit's silk can never collide with them.
PAD_KEEPOUTS = {
    "top": ((0.04, 0.04, 5.04, 3.0), (15.28, 0.04, 20.28, 3.0)),
    "bottom": ((0.04, 17.32, 5.04, 20.28), (15.28, 17.32, 20.28, 20.28)),
}

# Package parameters (the LED and its resistor share the size, except
# through-hole LEDs whose resistor stays an SMD — "res_pkg"). "dx" is the
# pad-center offset, pw/ph the pad size, res_dy the stacked resistor lift,
# gap the inline LED->resistor spacing, body the part outline for silk/fab.
# Through-hole LEDs — the sizes badgelife folks actually put on minibadges:
# tiny 1.8 mm and standard 3 mm domes plus the 5x2 mm rectangular "light
# bar". All follow KiCad's LED_THT footprints: 2.54 mm lead pitch, 1.8 mm
# circular pads, 0.9 mm drill ("drill" marks the package as through-hole).
# body is the base outline; "lens" the dome diameter (0 = no dome, a bar);
# "th_model" the standard-library 3D model stem.
# SMD pads follow KiCad's *_HandSolder proportions: these boards get built
# with an iron at a conference, not a reflow oven, so each pad carries extra
# copper past the end of the chip for the tip and a visible fillet. The growth
# is entirely OUTWARD — spans are KiCad's 2.80 / 3.20 / 4.40 mm while the
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


def caption_boxes(rows: tuple[str, ...]) -> list[tuple[float, float, float, float]]:
    """Bounding boxes of the printed pin captions (art must stay clear)."""
    out = []
    for label, x, y, row in PAD_LABELS:
        if row not in rows:
            continue
        hw = len(label) * 0.6 / 2 + 0.3
        out.append((x - hw, y - 0.55, x + hw, y + 0.55))
    return out


def _layout(name: str, size: str = "0805", reverse: bool = False) -> dict:
    """Unit geometry for a layout at a package size (offsets in unit mm).

    `reverse` is the badgelife through-board trick: a 1206 LED soldered
    upside-down over a routed hole so the light shines out the other face.
    It composes with either layout — the hole sits under the LED — but
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
    # that lands on one crosses to the other side just as well — the common
    # hand-routed minibadge style, and it keeps vias off the face entirely.
    novia: bool = False
    # Board-mm bends the via-less run must pass through, in order. Set by
    # dragging handles on the canvas; empty means route automatically.
    nodes: tuple = ()
    # Put just the LED on the opposite face, with a via inside each of its
    # pads carrying the connections through — the via-in-pad style other
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
    """clamp_led for a Led object — honors size/reverse AND advanced offsets."""
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


ROWS_ALL = ("top", "bottom")


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


def _unit_copper_quads(led: Led, safe, skip_start: bool):
    """A unit's real copper as quads: four pads plus the anode trace.

    Real pad sizes, not the inflated art keepouts — this is what a via-less
    power trace has to stay 0.2 mm clear of.
    """
    g = led_geometry(led)
    p, rp = PKG[g["pkg"]], PKG[res_pkg(g["pkg"])]
    cx, cy = clamp_led_obj(led, safe)
    ang = led.rot
    front = led.side != "back"

    def tb(dx, dy):
        rx, ry = _r(dx, dy, ang)
        return cx + rx, cy + ry

    out = [_quad_seg(tb(*g["res_out"]), tb(*g["led_a"]), TRACK_W)]
    for off, w, h, extra, is_start in (
        (g["led_k"], p["pw"], p["ph"], g.get("led_rot", 0.0), front),
        (g["led_a"], p["pw"], p["ph"], g.get("led_rot", 0.0), False),
        (g["res_in"], rp["pw"], rp["ph"], g.get("res_rot", 0.0), not front),
        (g["res_out"], rp["pw"], rp["ph"], g.get("res_rot", 0.0), False),
    ):
        if skip_start and is_start:
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


def _expanded_corners(led: Led, safe, margin: float) -> list:
    """Corners of a unit's bbox grown by margin — the router's waypoints."""
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
# last bit: IEEE-754 pins sqrt exactly, while cos/sin may differ by an ulp —
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
    # Leave along the long axis first, then break to 45 — squarer exit from a
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


def novia_route(led: Led, rows: tuple[str, ...] = ROWS_ALL, safe=None, others=(),
                outline=None):
    """Where a via-less unit runs its power trace, or None.

    A unit sits in one pour and needs the other net. Normally it drops
    through its own via; with novia set it runs a trace across its own layer
    to the nearest connector pad carrying that net instead. Those pads are
    plated through, so landing on one reaches the far pour exactly as a via
    would.

    Front units chase GND from the cathode, back units chase 3V3 from the
    resistor's input — the same net the via used to fetch. Ties break on
    CONNECTOR_PADS order so the web preview picks the same pad.

    Returns {"pts": [board mm, ...], "net": str, "pad": (x, y)}. The run is a
    straight shot where that clears the unit's own copper, and doglegs across
    the unit's short axis where it does not: with the resistor on the far
    side of the LED from the pad, a straight run skims its own anode pad by
    0.19 mm against a 0.2 mm rule.
    """
    if not led.novia:
        return None
    g = led_geometry(led)
    front = led.side != "back"
    net = "GND" if front else "3V3"
    cx, cy = clamp_led_obj(led, safe)
    ang = led.rot

    def to_board(dx, dy):
        rx, ry = _r(dx, dy, ang)
        return cx + rx, cy + ry

    s_off = g["led_k"] if front else g["res_in"]
    start = to_board(*s_off)
    # A front-side through-hole LED needs nothing at all: its cathode lead is
    # plated through to the back face, where the GND pour already is. This is
    # the cleanest via-less unit there is — no extra copper, no channel cut
    # across the pour. (A back-side unit still has to fetch 3V3 for its SMD
    # resistor, so it gets a trace.)
    if front and "drill" in PKG[g["pkg"]]:
        return {"pts": [start], "net": net, "pad": None, "direct": True}
    targets = sorted(
        ((px, py) for _num, px, py, pnet, row in CONNECTOR_PADS
         if pnet == net and row in rows),
        key=lambda t: (t[0] - start[0]) ** 2 + (t[1] - start[1]) ** 2)
    if not targets:
        return None  # no row carries this net (the UI keeps one row on)

    # Everything the run has to stay clear of: this unit's own copper bar the
    # pad it leaves from, every other unit sharing this layer, and any
    # connector pad on a different net (VBATT and CLK/NC included — landing
    # on those would be worse than a short).
    hazards = _unit_copper_quads(led, safe, skip_start=True)
    siblings = [o for o in others if o is not led and o.side == led.side]
    for o in siblings:
        hazards += _unit_copper_quads(o, safe, skip_start=False)
    # A unit on the far side still lands copper on this layer wherever its
    # pads are plated through, and a routed hole is a hole on every layer.
    for o in others:
        if o is led or o.side == led.side:
            continue
        og = led_geometry(o)
        if "drill" in PKG[og["pkg"]]:
            for x, y, r in th_pad_circles(o, safe):
                hazards.append(_round_hazard(x, y, r + NOVIA_CLEAR))
        if og["hole"]:
            ocx, ocy = clamp_led_obj(o, safe)
            hazards.append(_round_hazard(ocx, ocy, og["hole"] / 2 + NOVIA_CLEAR))
    for _num, px, py, pnet, row in CONNECTOR_PADS:
        if row not in rows or pnet == net:
            continue
        hazards.append(_round_hazard(px, py, 0.875 + NOVIA_CLEAR))

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

    # Turning points worth considering: the corners of every obstacle's grown
    # bounding box.
    waypoints = list(_expanded_corners(led, safe, NOVIA_ESCAPE))
    for o in siblings:
        waypoints += _expanded_corners(o, safe, NOVIA_ESCAPE)
    for _num, px, py, pnet, row in CONNECTOR_PADS:
        if row not in rows or pnet == net:
            continue
        r = 0.875 + NOVIA_CLEAR + NOVIA_ESCAPE
        waypoints += [(px - r, py - r), (px + r, py - r),
                      (px + r, py + r), (px - r, py + r)]

    # Hand-placed bends win outright: the point of dragging them is to choose
    # the path yourself. Every leg is still checked, and a bad one is flagged
    # so the UI can say so rather than ship a shorted trace.
    if led.nodes:
        pad = targets[0]
        pts = [start] + [(float(x), float(y)) for x, y in led.nodes] + [pad]
        bad = not all(clear(a, b) for a, b in zip(pts, pts[1:]))
        # The bends stay exactly where they were put; only the corners between
        # them are softened into 45s.
        out = {"pts": mitre45(pts, clear), "net": net, "pad": pad, "manual": True}
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
        return {"pts": mitre45(pts, clear), "net": net, "pad": pad}
    # Nothing legal reaches any pad — flag it so the UI can warn rather than
    # ship a board whose LED never lights.
    return {"pts": [start, targets[0]], "net": net, "pad": targets[0], "tight": True}


def unit_copper_pieces(led: Led, safe=None,
                       rows: tuple[str, ...] = ROWS_ALL,
                       others=(), outline=None) -> list[tuple[str, list]]:
    """Convex quads covering the unit's copper plus the margin art must clear.

    Pads inflated 0.5 mm per side (solder-mask-bridge rule + hand-soldering
    margin), the via as a square 0.45 mm clear of its barrel, traces as
    1.1 mm-wide rects (track 0.3 + 0.4 each side), and the reverse hole
    (+0.5). Labeled (board mm, unit-rotated) — the web UI paints identical
    pieces, so art hugs units the same way in the preview and on the board.
    Much tighter than the old bounding-box rectangle.
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

    def quad_seg(a, b, w: float) -> list:
        ax, ay = pt(*a)
        bx, by = pt(*b)
        nx, ny = -(by - ay), bx - ax
        ln = (nx * nx + ny * ny) ** 0.5 or 1.0
        nx, ny = nx / ln * w / 2, ny / ln * w / 2
        return [(ax + nx, ay + ny), (bx + nx, by + ny),
                (bx - nx, by - ny), (ax - nx, ay - ny)]

    pw, ph = p["pw"] + 1.0, p["ph"] + 1.0
    rw, rh = rp["pw"] + 1.0, rp["ph"] + 1.0
    vo = g["via_front"] if front else g["via_back"]
    pieces = [
        ("pad_led_k", quad_rect(*g["led_k"], pw, ph, lrot)),
        ("pad_led_a", quad_rect(*g["led_a"], pw, ph, lrot)),
        ("pad_res_in", quad_rect(*g["res_in"], rw, rh, rrot)),
        ("pad_res_out", quad_rect(*g["res_out"], rw, rh, rrot)),
        ("trace_a", quad_seg(g["res_out"], g["led_a"], 1.1)),
    ]
    route = novia_route(led, rows, safe, others, outline=outline)
    if route:
        # No via to clear, but a long run to the connector pad that artwork
        # must keep off just the same — copper art touching it would short
        # the trace to the pour it crosses.
        pts = route["pts"]
        for n, (a, b) in enumerate(zip(pts, pts[1:])):
            pieces.append((f"trace_pad{n}" if n else "trace_pad",
                           _quad_seg(a, b, 1.1)))
    else:
        pieces += [
            ("via", quad_rect(*vo, VIA_SIZE + 0.9, VIA_SIZE + 0.9)),
            ("trace_stub", quad_seg(g["led_k"] if front else g["res_in"], vo, 1.1)),
        ]
    if g["hole"]:
        pieces.append(("hole", quad_rect(0.0, 0.0, g["hole"] + 1.0, g["hole"] + 1.0)))
    if "drill" in p:
        # A through-hole LED's silkscreen outline follows its lens, which
        # reaches well past the pads — so the pad quads above do NOT cover it
        # and artwork would print straight over the part's own silk (the fab
        # then clips whichever lost). Claim the body plus a silk margin.
        bw, bh = p["body"]
        lens = p.get("lens", 0.0)
        pieces.append(("silk_body", quad_rect(0.0, 0.0,
                                              max(bw, lens) + 0.8,
                                              max(bh, lens) + 0.8, lrot)))
    return pieces


def unit_copper_poly(led: Led, safe=None, rows: tuple[str, ...] = ROWS_ALL, others=(),
                     outline=None):
    """unit_copper_pieces as one shapely geometry (for art keepouts)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    return unary_union([Polygon(q) for _, q in
                        unit_copper_pieces(led, safe, rows, others, outline)])


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
    """SAT overlap for convex quads — the exact mirror of the UI's polysClear."""
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


def _ray_exit(sx: float, sy: float, dx: float, dy: float, rings) -> float | None:
    """Distance along (dx,dy) from (sx,sy) to the first outline crossing.

    Rings are closed point lists (exterior first, then holes) — a hole
    boundary counts as an exit too, so bridges never span board cut-outs.
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
                if best is None or t < best:
                    best = t
    return best


# Bridge traces: 0.3 mm copper from each unit to the pour's perimeter ring.
# They replace the old reserved 2 mm window corridor — glow/bare windows may
# now hug a unit, and the thin bridge stays visible where it crosses one.
BRIDGE_INSET = 0.8   # endpoint pullback from the outline (lands in the ring)
BRIDGE_MIN = 0.5     # shortest useful bridge
_BRIDGE_EXCLUDE = {"pad_res_in": ("pad_res_in",), "pad_led_k": ("pad_led_k",),
                   "via": ("via", "trace_stub")}


def _bridge_route(start, own_pieces, skip_labels, obstacles, rings):
    """First clear straight segment from `start` to the perimeter ring.

    Candidates are 16 compass directions ordered nearest-exit-first; a
    candidate survives if its 1.0 mm-wide swath (track + pour clearance)
    misses every obstacle quad. Deterministic — the web UI runs the same
    scan and draws the same segment. None when everything is blocked.
    """
    import math

    sx, sy = start
    own = [q for lbl, q in own_pieces if lbl not in skip_labels]
    cands = []
    for k in range(16):
        a = math.radians(k * 22.5)
        dx, dy = math.cos(a), math.sin(a)
        t = _ray_exit(sx, sy, dx, dy, rings)
        if t is None or t - BRIDGE_INSET < BRIDGE_MIN:
            continue
        cands.append((t, k, dx, dy))
    cands.sort(key=lambda c: (c[0], c[1]))
    for t, _k, dx, dy in cands:
        ln = t - BRIDGE_INSET
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

    The route always goes to the NEAREST pad carrying the net — the same
    choice the browser preview makes, so what is drawn is what gets built.
    Hunting for a further pad that happens to work salvages under 1% of
    placements and would make the preview lie, so unreachable units are
    reported instead; the caller refuses the download and says so.
    """
    from shapely.geometry import Point

    leds = list(spec.leds)
    if not any(led.novia for led in leds):
        return leds, []
    if safe is None:
        safe = unit_safe(spec)
    problems: list[int] = []
    for i, led in enumerate(leds):
        if not led.novia:
            continue
        route = novia_route(led, spec.rows, safe, leds, outline=spec.outline)
        if route is None or route.get("direct"):
            continue  # nothing routed, so nothing can cut the pour
        if route.get("tight"):
            problems.append(i)
            continue
        front = led.side != "back"
        layer, pour = ("F.Cu", "3V3") if front else ("B.Cu", "GND")
        g = led_geometry(led)
        cx, cy = clamp_led_obj(led, safe)
        ox, oy = _r(*(g["res_in"] if front else g["led_k"]), led.rot)
        must = [(cx + ox, cy + oy)] + [
            (px, py) for _n, px, py, pnet, row in CONNECTOR_PADS
            if pnet == pour and row in spec.rows]
        polys = _fill_geometry(pour, layer, spec)
        if not any(all(poly.distance(Point(*m)) < 0.7 for m in must) for poly in polys):
            problems.append(i)
    return leds, problems


def unit_bridges(spec: BadgeSpec, safe=None) -> dict:
    """Per unit: {i: {"F.Cu": seg | None, "B.Cu": seg | None}} in board mm.

    F bridges carry 3V3, B bridges carry GND — each starts at the pad or
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
    all_pieces = [unit_copper_pieces(led, safe, spec.rows, spec.leds, spec.outline)
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
            # stranded — its trace reaches a connector pad instead. Dropping
            # the key (rather than setting None) keeps the caller from
            # reserving a window corridor it no longer needs.
            del starts["B.Cu" if front else "F.Cu"]
        obstacles = pads + [q for j, ps in enumerate(all_pieces) if j != i
                            for _lbl, q in ps]
        out[i] = {}
        for layer, (lbl, off) in starts.items():
            out[i][layer] = _bridge_route(
                pt(off), all_pieces[i], _BRIDGE_EXCLUDE[lbl], obstacles, rings)
    return out


def resolve_overlap(
    a: Led, b: Led, gap: float = 0.2,
    safe: tuple[float, float, float, float] | None = None,
) -> Led:
    """Return b, shifted if needed so its unit does not overlap a's.

    Units conflict even on opposite sides: each one's via penetrates both
    copper layers. The web UI prevents overlap during drag; this is the
    server-side backstop for hand-crafted requests.
    """
    from dataclasses import replace

    pa = unit_poly(a, safe)
    eps = 1e-6  # sliding to exactly `gap` separation must count as clear
    if unit_poly(b, safe).distance(pa) >= gap - eps:
        return b
    ba, bb = led_unit_bbox(a, safe), led_unit_bbox(b, safe)
    for x, y in (
        (b.x + (ba[2] + gap - bb[0]), b.y),  # slide right
        (b.x - (bb[2] - ba[0] + gap), b.y),  # slide left
        (b.x, b.y + (ba[3] + gap - bb[1])),  # slide down
        (b.x, b.y - (bb[3] - ba[1] + gap)),  # slide up
    ):
        probe = replace(b, x=x, y=y)
        if clamp_led_obj(probe, safe) == (x, y):
            if unit_poly(probe, safe).distance(pa) >= gap - eps:
                return probe
    return b  # no room; KiCad DRC will flag it


def pad_conflict(
    led: Led, rows: tuple[str, ...],
    safe: tuple[float, float, float, float] | None = None,
) -> bool:
    """True if the unit's rotated footprint overlaps a kept pad pair."""
    from shapely.geometry import box as sbox

    poly = unit_poly(led, safe)
    return any(
        poly.intersects(sbox(*k))
        for row in rows
        for k in PAD_KEEPOUTS.get(row, ())
    )


def resolve_pad_overlap(
    led: Led, rows: tuple[str, ...],
    safe: tuple[float, float, float, float] | None = None,
) -> Led:
    """Slide a unit off the connector pad keepouts (server-side backstop).

    The web UI never drops a unit on a pad pair; this covers hand-crafted
    requests the same way resolve_overlap does for unit-unit overlaps.
    """
    from dataclasses import replace

    from shapely.geometry import box as sbox

    for _ in range(3):
        b = led_unit_bbox(led, safe)
        poly = unit_poly(led, safe)
        hit = next(
            (k for row in rows for k in PAD_KEEPOUTS.get(row, ())
             if poly.intersects(sbox(*k))),
            None,
        )
        if hit is None:
            return led
        for x, y in (
            (led.x + (hit[2] + 0.05 - b[0]), led.y),  # slide right
            (led.x - (b[2] - hit[0] + 0.05), led.y),  # slide left
            (led.x, led.y + (hit[3] + 0.05 - b[1])),  # slide down
            (led.x, led.y - (b[3] - hit[1] + 0.05)),  # slide up
        ):
            if clamp_led(x, y, led.rot, led.layout, safe) == (x, y):
                led = replace(led, x=x, y=y)
                break
        else:
            return led  # no room; KiCad DRC will flag it
    return led


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
ART_MATERIALS = ("silk", "copper", "glow", "bare")


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
    # For "bare" only: which soldermask(s) open over the window. "through"
    # opens both (classic light pipe); "front"/"back" open one face — the
    # other face keeps its mask and looks like a glow window from there.
    # The copper is cut from BOTH pours in every case (light must pass).
    window: str = "through"


@dataclass
class BadgeSpec:
    name: str = "minibadge"
    leds: list[Led] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    art: list[ArtLayer] = field(default_factory=list)
    mask_color: str = "green"
    # Surface finish: "enig" (gold) or "hasl" (silver). Board-wide fab
    # choice — it colors every exposed pad, via, and copper-art opening.
    finish: str = "enig"
    # Connector rows kept on this badge; each row alone carries 3V3 + GND.
    rows: tuple[str, ...] = ("top", "bottom")
    # Custom board outline as rings of (x, y) board-mm points — first ring
    # is the exterior, the rest are holes. None = the standard 20x20 square.
    outline: list[list[tuple[float, float]]] | None = None


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
    names = ["", "3V3", "GND"] + [f"/LED{i + 1}_A" for i in range(len(spec.leds))]
    index = {name: i for i, name in enumerate(names)}
    lines = [f'  (net {i} "{name}")' for i, name in enumerate(names)]
    return lines, index


def _connector_footprint(nets: dict[str, int], rows: tuple[str, ...]) -> str:
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
    # on both faces — the fab board should match; the Dwgs.User layer the
    # official footprint used never prints at all). Centered text mirrors
    # in place, so the back copy only needs the mirror flag.
    for i, (label, x, y, row) in enumerate(PAD_LABELS):
        if row not in rows:
            continue
        # Each caption names a PAIR of pads, left word for the left pad. Seen
        # from the back the pair is mirrored, so the words have to swap too —
        # otherwise the back silk labels 3V3 as GND and vice versa, which is
        # exactly the kind of thing someone hand-soldering trusts.
        flipped = " ".join(reversed(label.split()))
        for layer, mirror, label in (("F.SilkS", "", label),
                                     ("B.SilkS", " (justify mirror)", flipped)):
            out += [
                f'    (fp_text user "{label}" (at {_n(x)} {_n(y)} unlocked) (layer "{layer}")',
                f"      (effects (font (size 0.6 0.6) (thickness 0.11)){mirror})",
                f"      (tstamp {_ts(f'fp-conn-label-{i}-{layer}')})",
                "    )",
            ]
    for num, x, y, net, row in CONNECTOR_PADS:
        if row not in rows:
            continue
        net_s = f' (net {nets[net]} "{net}")' if net else ""
        out.append(
            f'    (pad "{num}" thru_hole circle (at {_n(x)} {_n(y)}) (size 1.75 1.75) '
            f'(drill 0.95) (layers "*.Cu" "*.Mask"){net_s} (tstamp {_ts(f"pad-{num}")}))'
        )
    # 3D: a 1x02 male header per kept pad pair, mounted on the BACK with the
    # pins pointing away from the front face — how a minibadge actually
    # plugs into the badge's socket strip. Model x-rotation 180 flips it
    # under the board; the z-rotation lays the two pins along the pair.
    header = model_path("Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical")
    pairs = [(2.54, 1.27, "top"), (17.78, 1.27, "top"),
             (2.54, 19.05, "bottom"), (17.78, 19.05, "bottom")]
    for i, (px, py, row) in enumerate(pairs):
        if row not in rows:
            continue
        # offset z -1.6 (board thickness) + x-rot 180: body flush on the
        # BACK face, pins pointing away from the front — how a minibadge
        # plugs into the badge's socket strip.
        out.append(
            f'    (model "{header}"\n'
            f"      (offset (xyz {_n(px - 1.27)} {_n(-py)} -1.6)) (scale (xyz 1 1 1)) "
            "(rotate (xyz 180 0 90))\n"
            "    )"
        )
    out.append("  )")
    return "\n".join(out)


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
        (ax, ay), (bx, by) = _r(f * x0, y0, ang), _r(f * x1, y1, ang)
        return (
            f"    (fp_line (start {_n(ax)} {_n(ay)}) (end {_n(bx)} {_n(by)}) "
            f'(stroke (width {_n(width)}) (type solid)) (layer "{p}.SilkS") '
            f"(tstamp {_ts(f'fp-{key}-{tag}')}))"
        )

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
        # outline on Fab plus two horizontal silk lines along the body edges
        # — they clear the pads' mask openings vertically. A dome narrower
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
        # the body lying across its own pads at twice the angle — invisible on
        # a round part, obvious on a chip (both signs checked against renders).
        mz = (ang + (180 if flip else 0)) % 360
        if p == "B":
            mz = (-mz) % 360
        # SMD models sit centered on our footprint origin and need no offset.
        # The THT LED models are anchored at pin 1, so the offset walks them
        # to pad 1 — in the model's own (already flipped) frame, hence mz, and
        # with 3D y counting upward against our y-down board.
        if drill:
            # Front: pad 1 is at _r(f * -dx, ang) and `f` supplies the inline
            # flip. Back: the flip is already inside mz (it carries the +180),
            # so applying `f` as well would flip twice and land the body a
            # whole pad pitch away — which is what used to happen.
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
    rows: tuple[str, ...] = ROWS_ALL,
    others: tuple = (),
    outline=None,
) -> str:
    """LED + resistor footprints, connecting traces, and the power via."""
    ang = led.rot
    g = led_geometry(led)
    x, y = clamp_led_obj(led, safe)
    anode = f"/LED{i + 1}_A"
    front = led.side != "back"
    cu = "F.Cu" if front else "B.Cu"
    gnd, v33 = (nets["GND"], "GND"), (nets["3V3"], "3V3")
    an = (nets[anode], anode)

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
    # inside each of its pads carries the connections through — the via-in-pad
    # style other minibadge designers use. The resistor and its trace stay put.
    far = (bool(led.farled) and not g["hole"]
           and "drill" not in PKG[g["pkg"]])
    led_side = ("back" if led.side != "back" else "front") if far else led.side
    parts = [
        # LED: pad 1 = cathode, pad 2 = anode (facing the resistor's pad 2).
        _smd(f"led{i}", f"D{i + 1}", f"LED_{led.color.upper()}",
             *at(0, 0), gnd, an, True, led_side,
             (ang + g.get("led_rot", 0.0)) % 360, g["led_flip"], g["pkg"],
             led_model),
        _smd(f"res{i}", f"R{i + 1}", f"{LED_COLORS.get(led.color, '220')}R",
             *at(*g["res"]), v33, an, False, led.side,
             (ang + g.get("res_rot", 0.0)) % 360, False, rpkg,
             model_path("Resistor_SMD", f"R_{rpkg}_{PKG_METRIC[rpkg]}Metric")),
        # Resistor pad 2 to LED anode.
        seg(at(*g["res_out"]), at(*g["led_a"]), cu, nets[anode], f"seg-a{i}"),
    ]
    if far:
        for tag, off, net in (("k", g["led_k"], nets["GND"]),
                              ("a", g["led_a"], nets[anode])):
            vx, vy = at(*off)
            parts.append(
                f"  (via (at {_n(vx)} {_n(vy)}) (size {_n(VIA_SIZE)}) "
                f'(drill {_n(VIA_DRILL)}) (layers "F.Cu" "B.Cu") (net {net}) '
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
    route = novia_route(led, rows, safe, others, outline=outline)
    if route:
        # Via-less: a run across the unit's own layer to a connector pad,
        # whose plated barrel carries the net to the far pour. Electrically
        # the same circuit as the via version, with nothing drilled.
        pts = [(ORIGIN + px, ORIGIN + py) for px, py in route["pts"]]
        for n, (a, b) in enumerate(zip(pts, pts[1:])):
            parts.append(seg(a, b, cu, nets[route["net"]], f"seg-n{i}-{n}"))
    elif front:
        # Front unit: R pad 1 sits in the F.Cu 3V3 pour; cathode drops to the
        # B.Cu GND pour through a via.
        via = at(*g["via_front"])
        parts += [
            seg(at(*g["led_k"]), via, "F.Cu", nets["GND"], f"seg-k{i}"),
            (f"  (via (at {_n(via[0])} {_n(via[1])}) (size {_n(VIA_SIZE)}) (drill {_n(VIA_DRILL)}) "
            f'(layers "F.Cu" "B.Cu") (net {nets["GND"]}) (tstamp {_ts(f"via-{i}")}))'),
        ]
    else:
        # Back unit: cathode sits in the B.Cu GND pour; R pad 1 reaches the
        # F.Cu 3V3 pour through a via at the resistor's input end.
        via = at(*g["via_back"])
        parts += [
            seg(at(*g["res_in"]), via, "B.Cu", nets["3V3"], f"seg-v{i}"),
            (f"  (via (at {_n(via[0])} {_n(via[1])}) (size {_n(VIA_SIZE)}) "
            f'(drill {_n(VIA_DRILL)}) (layers "F.Cu" "B.Cu") (net {nets["3V3"]}) '
            f"(tstamp {_ts(f'via-{i}')}))"),
        ]
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
    from shapely.geometry import LineString, Point, box
    from shapely.ops import unary_union

    board = outline_polygon(spec)
    region = board.buffer(-POUR_EDGE_INSET)

    obstacles = []
    anchors = []  # same-net copper; fill islands must touch one to survive
    for _num, px, py, net, row in CONNECTOR_PADS:  # through-hole: both layers
        if row not in spec.rows:
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
        route = novia_route(led, spec.rows, safe, spec.leds, outline=spec.outline)
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
        unit_pads = [
            (*g["led_k"], "GND", rled, pw2, ph2, th or far),
            (*g["led_a"], anode, rled, pw2, ph2, th or far),
            (*g["res_in"], "3V3", rres, rw2, rh2, False),
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
            obstacles.append(LineString([at(*g["res_out"]), at(*g["led_a"])]).buffer(track_r))
        # Power stub from the pad to the via — or, for a via-less unit, the
        # long run to the connector pad. Either way the other net's pour on
        # this layer opens a channel around it.
        if route is not None:
            if zone_net != route["net"] and len(route["pts"]) > 1:
                obstacles.append(LineString(route["pts"]).buffer(track_r))
        elif front and zone_net != "GND":
            obstacles.append(LineString([at(*g["led_k"]), (vx, vy)]).buffer(track_r))
        elif not front and zone_net != "3V3":
            obstacles.append(LineString([at(*g["res_in"]), (vx, vy)]).buffer(track_r))

    # Glow/bare art windows strip copper from both layers so light can pass
    # through the laminate. Expanded 0.1 mm past the mask opening so the
    # copper edge hides under the mask despite fab registration tolerance.
    # Windows never reach the outer 1.5 mm of the outline: the pours keep a
    # continuous perimeter ring, so a full-width window can't split a plane
    # into disconnected halves.
    interior = board.buffer(-1.5)
    for art in spec.art:
        if art.material in ("glow", "bare"):
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

    # KiCad stores fills as simple outlines (no holes). Convert each hole
    # into an edge notch by cutting a thin slit from inside the hole down
    # past the board edge. The slit must start at a point actually inside
    # the void — a hole's bounding-box center can land on copper for
    # C/U-shaped holes, which would leave the hole intact (and the emitter
    # below would then pour copper straight over other-net pads).
    from shapely.geometry import Polygon as ShapelyPolygon

    slit_bottom = board.bounds[3] + 1.0  # past the outline's lowest edge
    for _ in range(8):
        polys = list(filled.geoms) if filled.geom_type == "MultiPolygon" else [filled]
        slits = []
        for poly in polys:
            for ring in poly.interiors:
                pt = ShapelyPolygon(ring).representative_point()
                slits.append(box(pt.x - 0.06, pt.y, pt.x + 0.06, slit_bottom))
        if not slits:
            break
        filled = filled.difference(unary_union(slits))

    # Copper-material art enclosed by a glow/bare window becomes an isolated
    # island (the window severs it from the plane). Those are intentional
    # decoration — a skull's gold eyes inside a bare face — so they survive
    # the floating-copper filter below (the zone's island_removal_mode keeps
    # them through a KiCad refill as well).
    keep = []
    for art in spec.art:
        if art.material != "copper" or (layer.startswith("F")) != (art.side != "back"):
            continue
        keep += [box(rx, ry, rx + rw, ry + rh) for rx, ry, rw, rh in art.rects]
        keep += _art_shapely(art.polys)
    keep_union = unary_union(keep) if keep else None

    polys = list(filled.geoms) if filled.geom_type == "MultiPolygon" else [filled]
    anchor_union = unary_union(anchors)
    return [
        p.simplify(0.005)
        for p in polys
        if not p.is_empty and p.area > 0.05
        and (p.intersects(anchor_union)
             or keep_union is not None and p.intersects(keep_union))
    ]


def _window_geometry(spec: BadgeSpec):
    """The glow/bare light windows as shapely polygons (board mm), or None.

    Same shapes _fill_geometry cuts out of the pours: expanded 0.1 mm past
    the mask opening and held inside the perimeter ring. Copper artwork that
    sits *inside* a window (a skull's gold eyes in a bare face) is carved
    back out, so it keeps its copper.
    """
    from shapely.ops import unary_union

    from shapely.geometry import box

    board = outline_polygon(spec)
    interior = board.buffer(-1.5)
    wins = []
    for art in spec.art:
        if art.material not in ("glow", "bare"):
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
    asks), KiCad recomputes from its own rules — which know nothing about
    why that copper is missing — and floods the windows solid, quietly
    turning every glow/bare window back into ordinary board. A keepout
    encodes the intent so the refill agrees with us.
    """
    geom = _window_geometry(spec)
    if geom is None:
        return []
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    out = []
    for i, poly in enumerate(polys):
        if poly.is_empty or poly.area < 0.01:
            continue
        # A hole here just means "copper allowed" — and any sliver the
        # fracture leaves behind is thinner than the zone's min_thickness,
        # so it never becomes copper.
        ring = poly.exterior if not poly.interiors else _fracture(poly).exterior
        pts = " ".join(f"(xy {_n(ORIGIN + x)} {_n(ORIGIN + y)})"
                       for x, y in ring.coords[:-1])
        out.append(
            f'  (zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") '
            f'(tstamp {_ts(f"keepout-{i}")}) (hatch edge 0.508)\n'
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
            # hole was protecting — refuse rather than generate a short.
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
    notch that fabs — and eyes — can't tell from a true hole.
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


# Which drawn layers an art material paints, per board face. Mask layers are
# negatives: a polygon on F.Mask/B.Mask is an *opening* in the soldermask.
# "glow" draws nothing — it only cuts the copper pours (see _fill_geometry).
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
    # board the way the fab would build it — green/purple/... mask, white
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
    body: list[str] = [
        '(kicad_pcb (version 20221018) (generator "minibadge-designer")',
        "  (general (thickness 1.6))",
        '  (paper "A4")',
        LAYERS,
        stackup,
        *net_lines,
        _connector_footprint(nets, spec.rows),
    ]
    safe = unit_safe(spec)
    bridges = unit_bridges(spec, safe)
    for i, led in enumerate(spec.leds):
        body.append(_led_unit(i, led, nets, safe, spec.rows, spec.leds, spec.outline))
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
    for ai, art in enumerate(spec.art):
        for layer in _art_target_layers(art.material, art.side, art.window):
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
    # min_text_height 0.6: the printed pin captions are 0.6 mm silk — small
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
    lines = ["Reference,Value,Footprint,Side,Qty,Notes"]
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
            note += f"; mounts on the {far} face — via in each pad"
        if led.novia:
            note += "; no via — wired to a connector pad"
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
    if spec.outline:
        b = outline_polygon(spec).bounds
        board_desc = (
            f"custom outline, {b[2] - b[0]:.1f} x {b[3] - b[1]:.1f} mm bounding box "
            "(minibadge v2 connector)"
        )
    else:
        board_desc = "20 x 20 mm, minibadge v2 standard"
    finish_desc = (
        "lead-free HASL (shiny SILVER pads and copper art)"
        if spec.finish == "hasl"
        else "ENIG (shiny GOLD pads and copper art)"
    )
    return f"""{spec.name} — SAINTCON minibadge
=================================

Generated by minibadge designer. Board: {board_desc}.
LEDs: {led_desc}. Suggested soldermask color: {spec.mask_color}.
Surface finish to order: {finish_desc}.

Open and finish in KiCad (7 or newer)
-------------------------------------
1. Open {slug}.kicad_pro in KiCad and open the PCB editor.
2. Press B to fill the copper zones (front = 3V3, back = GND).
   The LED circuits connect through these pours — do not skip this.
3. Run DRC (Inspect > Design Rules Checker) and confirm no errors. Silkscreen
   *warnings* are possible where you deliberately put text or art over a pad:
   those are cosmetic, and the fab clips the overlap when it prints.
4. File > Fabrication Outputs > Gerbers (plot all layers + drill files),
   zip them, and upload to your fab (JLCPCB, PCBWay, OSH Park, ...).
   1.6 mm thickness, 2 layers, surface finish as noted above (the finish
   sets whether exposed metal comes out gold or silver).

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
connections through the board — so the LED faces out while the resistor hides
behind it.

Units with "no via" ticked skip the via entirely: the trace runs to a
connector pad instead, whose plated hole carries the net to the other side.
A front-side through-hole LED needs no extra copper at all, since its own
leads are already plated through to the back pour.

So in the PCB editor a resistor pad or via can look "unattached" — it is
connected by the pour (check with the highlight-net tool). LED cathode goes
toward the silkscreen bar next to the footprint; on a through-hole LED that
is the SHORT lead (the flat side of the lens). See BOM.csv for parts and
which side each part mounts on. SMD pads use KiCad's hand-solder proportions
— extra copper past each end of the chip for the iron tip and a visible
fillet — and a through-hole LED's resistor stays SMD. Note that a back-side LED faces the
badge when plugged in, so it is only visible from behind unless you use a
reverse-mount LED over a via/hole.

Power comes from the badge's 3V3 pins. VBATT, CLK, and NC pins are left
unconnected, per the standard (never connect NC; never tie VBATT to 3V3).

Artwork materials
-----------------
Silkscreen art is white ink. "Exposed copper" art is a soldermask opening
over the copper plane (gold/ENIG finish shows). "Glow window" art strips
the copper from both layers but keeps the mask: light from a nearby
back-side LED diffuses through the laminate and glows in the mask color.
"Bare board" also opens the mask on both sides (raw laminate, brightest).
The shipped zone fills route thin fracture slits from copper cutouts to
the board edge (a file-format requirement); refilling zones in KiCad (B)
replaces them with proper holes, so always refill before plotting. Glow
and bare windows carry keepout rule areas, so refilling leaves them
clear instead of pouring copper back into the light path.

Credits
-------
Minibadge standard and connector footprint: (c) Luke Jenkins and
contributors, https://github.com/lukejenkins/minibadge (Apache-2.0).
Spec: https://saintcon.org/minibadges/
"""
