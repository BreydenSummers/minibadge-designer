"""Shared assertion library for the minibadge test suite.

**Grep this file before writing a new assertion.** Everything here arrived with
a measured true-positive and false-positive rate; anything you add must too.

Three oracles live here and none of them subsumes the others:

| oracle                      | sees                                                                                                     | cost/board |
|-----------------------------|----------------------------------------------------------------------------------------------------------|-----------|
| `check_board` (in-process)  | net index/name agreement, **the net a track is on vs the pads it lands on**, via layer spans and size,    | 3-52 ms   |
|                             | stackup (mask colour, finish), pour islands, keepouts, light windows, clearance to edge and to other-net  |           |
|                             | copper, unit connectivity, board size                                                                     |           |
| `assert_drc_clean` (kicad)  | courtyard overlap, silk over copper/edge, solder-mask bridges, copper slivers, hole clearance, **pad-to-** | ~620 ms   |
|                             | **pad shorts**, crossing tracks, unconnected items                                                        |           |
| GLB / raster sections       | where a 3D model actually landed; whether the pour is one island in the *rendered* copper                  | ~0.5-2 s  |

A defect invisible to one is routinely caught by another; two measured pairs
make the point better than the rule does:

* A via emitted as `(layers "F.Cu" "F.Cu")` passes full DRC and every coordinate
  assertion — `assert_vias_cross_the_board` is the only thing that sees it.
* Move the anode trace onto the `3V3` net and DRC reports **zero violations**
  (the copper is byte-identical, and KiCad's connectivity comes from pads and
  zones, not tracks) — `assert_tracks_carry_the_net_of_the_pads_they_touch` is
  the only thing that sees it. *Delete* that same trace and the situation
  inverts: the in-process set is silent and DRC reports `unconnected_items`.
* A 0603 unit left sitting on a connector pad (live defect #2) is a pad-to-pad
  short that only DRC sees.

Run **both** the in-process set and DRC; never pick one.

Conventions
-----------
* ``assert_*(...)``      raises ``AssertionError``. The message names the
  invariant, the actual value, and the user-visible consequence. "assert 3 == 1"
  is a failure; "GND pour on B.Cu split into 3 islands (expected 1) — LEDs on
  the severed island will never light" is a diagnosis.
* ``check_*(...)``       returns ``list[str]`` of problems, so a caller can
  collect every problem in one artifact (a GLB has many parts) or reason over a
  *group* of boards (rotation invariance is not a per-board property). Feed the
  result to :func:`assert_clean`.
* ``measure_*`` / nouns  return numbers. Numbers are what you assert on; there
  are no golden files here and there must never be — five identical GLB exports
  produce five different sha256, and five identical PNG renders likewise.

Stating expectations independently
----------------------------------
Symbolic constants are used for **inputs and tolerances**, never for the thing
under test. An invariant that sources its expectation from the constant the
production code reads cannot fail when that constant is wrong: a check that
built its expected pinout from ``pcb.CONNECTOR_PADS`` was blind to a bug that
edited ``pcb.CONNECTOR_PADS``. Every value in the "Independently stated
expectations" block below is transcribed from the standard or from a fab rule,
deliberately duplicating what ``pcb.py`` declares. If you change a fab-critical
constant in ``pcb.py``, one of these is *supposed* to go red — read the message,
confirm the change is intended, and update it here too.

Checks whose expectation is necessarily derived from production code are marked
``D2-DERIVED`` in their docstring, with what they can therefore not see.

Assert on the artifact, not on the generator's intentions
---------------------------------------------------------
The same trap has a geometry-shaped version that is easier to walk into.
``Board.fills()`` calls ``pcb._fill_geometry`` — the function ``pcb._zone`` calls
to produce the file — so a check reading it verifies what the generator *would*
compute and never what it *wrote*. Measured: offsetting every emitted pour by
5 mm at the emission site (``pcb.py:2027``) produced **46 real DRC violations**,
a board that shorts, and the entire fast tier stayed green — 397 passed, 0
failed, with kicad-cli disabled.

So: anything describing a property of **copper** reads
:meth:`Board.emitted_fills`, which parses ``(filled_polygon ...)`` back out of
the file. ``Board.fills()`` is legitimate in exactly one role — the *intent* half
of an explicit intent-vs-artifact comparison — and the two checks that use it
that way (:func:`assert_pours_actually_contain_copper` for placement,
:func:`assert_pour_fills_have_no_holes` for quantity) say so in their docstrings.
If you add a pour check, the question to answer in its docstring is not "is this
correct" but "which of the two does it read, and why".

The same rule applies to ``b`` itself. A check that takes a ``Board`` and never
touches it is a unit test of a ``pcb`` helper wearing a board-invariant
signature; two of these shipped inside ``ALL_CHECKS`` and one of them was a
provable tautology. If ``b`` is unused, the check does not belong in this list.
"""
from __future__ import annotations

import io
import json
import math
import re
import struct
import subprocess
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path as _Path

from minibadge_designer import pcb

# ---------------------------------------------------------------------------
# Independently stated expectations. Transcribed from the standard / fab rules,
# NOT read from pcb.py. See the module docstring.
# ---------------------------------------------------------------------------

#: The minibadge v2 connector pinout, from https://saintcon.org/minibadges/ and
#: lukejenkins/minibadge. ``None`` means "must carry no net at all".
STANDARD_PINOUT = {"1": None, "2": "GND", "7": "3V3", "8": "GND",
                   "9": None, "10": None, "15": "3V3", "16": "GND"}

#: The SAINTCON minibadge outline is 20 mm square. ``pcb.OUTLINE`` says the same
#: thing in page coordinates; this is the physical fact.
STANDARD_BOARD_MM = 20.0
STANDARD_BOARD_TOL = 1e-3

#: Via geometry, in mm (commit d0a5bcf, "Shrink vias to 0.7/0.3"). Stated here so
#: that editing ``pcb.VIA_SIZE`` / ``pcb.VIA_DRILL`` cannot pass unnoticed — a
#: 0.3 mm drill is the cheap-tier limit at every fab this project targets and a
#: bigger one changes the quote. If you deliberately move the via size, update
#: this line in the same commit.
VIA_PAD_MM, VIA_DRILL_MM = 0.7, 0.3
MIN_ANNULAR_RING_MM = 0.15      # (pad - drill) / 2, fab floor
MIN_DRILL_MM = 0.2              # smallest drill in the cheap tier

#: DRC rules that ``pcb.generate_project`` writes into the ``.kicad_pro`` and
#: that ``kicad-cli pcb drc`` then enforces on the downloaded project. The
#: geometry invariants below enforce exactly these numbers in-process.
RULE_CLEARANCE_MM = 0.15        # copper to other-net copper
RULE_EDGE_CLEARANCE_MM = 0.2    # copper to board edge
FAB_CLEARANCE_FLOOR_MM = 0.127  # 5 mil — below this no cheap fab will build it

#: ``pcb._n`` writes coordinates at four decimal places, so a vertex read back
#: out of the file can sit up to 5e-5 mm from the geometry that produced it.
#: Every comparison between emitted copper and computed geometry allows this
#: much and no more — it is 1500x smaller than the 0.15 mm clearance rule, so it
#: cannot hide a real violation. Measured worst-case area disagreement over the
#: 26-board corpus: 0.0012 mm^2.
EMITTED_ROUNDING_MM = 1e-4

#: Total emitted-vs-intended pour area may differ only by rounding. 0.05 mm^2 is
#: 40x the worst observed disagreement and far smaller than any hole an emitter
#: bug could flood (``pcb._zone``'s min_thickness alone is 0.25 mm).
EMITTED_AREA_TOL_MM2 = 0.05

_EPS = 1e-6

#: The one place the emitted copper's file syntax is written down.
_FILLED_POLY_RE = re.compile(
    r'\(filled_polygon \(layer "([^"]+)"\) \(pts (.*?)\)\)\n', re.S)
_XY_RE = re.compile(r"\(xy (-?[\d.]+) (-?[\d.]+)\)")


# ===========================================================================
# 1. Parsing: .kicad_pcb text -> Board
# ===========================================================================


def _parse_sexp(text: str):
    i, n, stack, cur = 0, len(text), [], None
    while i < n:
        c = text[i]
        if c == "(":
            new = []
            if cur is not None:
                cur.append(new)
                stack.append(cur)
            cur = new
            i += 1
        elif c == ")":
            if not stack:
                return cur
            cur = stack.pop()
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            cur.append("".join(buf))
            i = j + 1
        elif c in " \t\r\n":
            i += 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            tok = text[i:j]
            try:
                cur.append(float(tok))
            except ValueError:
                cur.append(tok)
            i = j
    return cur


def _kids(node, name):
    return [c for c in node
            if isinstance(c, list) and c and isinstance(c[0], str) and c[0] == name]


def _kid(node, name):
    k = _kids(node, name)
    return k[0] if k else None


def _val(node, name, idx=1, default=None):
    k = _kid(node, name)
    return k[idx] if k is not None and len(k) > idx else default


@dataclass
class Pad:
    fp: str          # footprint library name, e.g. "minibadge-designer:LED_RED_0805"
    ref: str         # reference designator: "D1", "R1", "J1" — the stable identity
    fp_layer: str    # "F.Cu" / "B.Cu"
    num: str
    kind: str        # "smd" / "thru_hole"
    x: float         # board mm (ORIGIN already subtracted)
    y: float
    net: int
    net_name: str
    layers: tuple
    shape: str = "rect"   # "rect" / "circle", as written in the file
    w: float = 0.0        # (size w h), i.e. the pad's real copper extent
    h: float = 0.0
    angle: float = 0.0    # pad rotation, degrees CCW, as written in the file

    def copper(self):
        """The pad's copper as a shapely polygon, in board mm.

        Parsed from ``(size ...)`` and ``(at ... angle)`` in the file, so it is
        the copper that ships rather than a point. Clearance measured to a pad
        *centre* is measured to a place with no copper in it: an 0805 pad is
        1.0 mm wide, so a 0.25 mm pour shift that leaves only 0.10 mm of real
        copper-to-copper gap still reads as 0.4 mm from the centre and passes a
        0.15 mm rule. Measured — that exact mutation escaped every check until
        this method existed.
        """
        from shapely.affinity import rotate as _srotate
        from shapely.geometry import Point
        from shapely.geometry import box as _box
        if self.shape == "circle" or not self.h:
            return Point(self.x, self.y).buffer(max(self.w, self.h) / 2 or _EPS,
                                                quad_segs=16)
        b = _box(self.x - self.w / 2, self.y - self.h / 2,
                 self.x + self.w / 2, self.y + self.h / 2)
        a = self.angle % 360
        return _srotate(b, a, origin=(self.x, self.y)) if a else b


@dataclass
class Track:
    net: int
    layer: str
    a: tuple
    b: tuple
    width: float


@dataclass
class Via:
    net: int
    x: float
    y: float
    size: float
    drill: float
    layers: tuple


@dataclass
class Zone:
    net: int
    net_name: str
    layer: str
    keepout: bool


class Board:
    """A parsed ``.kicad_pcb``, in *board* mm (``pcb.ORIGIN`` already subtracted).

    Reduces the text to ``pads / tracks / vias / zones / footprints / nets`` and
    attaches the **reference designator** to each pad, which is the only stable
    identity a pad has: footprint library names change with the package table.
    """

    def __init__(self, text: str):
        self.text = text
        self.root = _parse_sexp(text)
        o = pcb.ORIGIN
        self.nets = {int(c[1]): c[2] for c in _kids(self.root, "net")}
        self.net_of = {v: k for k, v in self.nets.items()}
        self.pads: list[Pad] = []
        self.footprints = []
        for fp in _kids(self.root, "footprint"):
            at = _kid(fp, "at")
            fx, fy = float(at[1]) - o, float(at[2]) - o
            ref = next((str(t[2]) for t in _kids(fp, "fp_text")
                        if t[1] == "reference"), "")
            self.footprints.append((fp[1], _val(fp, "layer"), fx, fy, ref, fp))
            for p in _kids(fp, "pad"):
                pat, net, lay = _kid(p, "at"), _kid(p, "net"), _kid(p, "layers")
                size = _kid(p, "size")
                self.pads.append(Pad(
                    fp=fp[1], ref=ref,
                    fp_layer=_val(fp, "layer"), num=str(p[1]), kind=str(p[2]),
                    x=fx + float(pat[1]), y=fy + float(pat[2]),
                    net=int(net[1]) if net else 0, net_name=net[2] if net else "",
                    layers=tuple(lay[1:]) if lay else (),
                    shape=str(p[3]) if len(p) > 3 and isinstance(p[3], str) else "rect",
                    w=float(size[1]) if size else 0.0,
                    h=float(size[2]) if size and len(size) > 2 else 0.0,
                    angle=float(pat[3]) if len(pat) > 3 else 0.0))
        self.tracks = [Track(
            net=int(_val(s, "net", default=0)), layer=str(_val(s, "layer")),
            a=(_kid(s, "start")[1] - o, _kid(s, "start")[2] - o),
            b=(_kid(s, "end")[1] - o, _kid(s, "end")[2] - o),
            width=float(_val(s, "width"))) for s in _kids(self.root, "segment")]
        self.vias = [Via(
            net=int(_val(v, "net", default=0)),
            x=_kid(v, "at")[1] - o, y=_kid(v, "at")[2] - o,
            size=float(_val(v, "size")), drill=float(_val(v, "drill")),
            layers=tuple(_kid(v, "layers")[1:])) for v in _kids(self.root, "via")]
        self.zones = [Zone(
            net=int(_val(z, "net", default=0)),
            net_name=str(_val(z, "net_name", default="")),
            layer=str(_val(z, "layer")), keepout=_kid(z, "keepout") is not None)
            for z in _kids(self.root, "zone")]
        self.setup = _kid(self.root, "setup")
        self._fills: dict = {}
        self._emitted: dict = {}

    def fills(self, spec, net: str, layer: str):
        """``pcb._fill_geometry``, memoised — the copper the generator *intended*.

        **This is not the board.** It is the same function ``pcb._zone`` calls, so
        anything that asserts on it verifies what the generator would compute and
        never what it wrote; a bug anywhere between ``_fill_geometry`` and the
        file is invisible to it. Measured: offsetting every emitted pour by 5 mm
        at the emission site produced 46 real DRC violations on a shorting board
        while every check that read this method stayed green.

        Use :meth:`emitted_fills` to assert on copper. Use this one only as the
        *intent* half of an intent-vs-artifact comparison, and say so in the
        docstring of the check that does it.

        It costs ~33 ms a call and several invariants want the same two answers;
        without the cache the geometry checks dominate (297 ms -> 55 ms for the
        whole check pass).
        """
        key = (net, layer)
        if key not in self._fills:
            self._fills[key] = list(pcb._fill_geometry(net, layer, spec))
        return self._fills[key]

    def emitted_fills(self, layer: str):
        """The ``filled_polygon`` copper **actually written to the file** for
        ``layer``, as shapely polygons in board mm. Memoised.

        The artifact-side counterpart to :meth:`fills`: it parses ``self.text``
        and asks ``pcb`` nothing, so it can disagree with the generator, and that
        disagreement is the whole point. Every pour invariant that describes a
        property of *copper* — where it sits, what it touches, what it leaves
        clear — reads this. Only a check that deliberately compares intent
        against artifact may also call :meth:`fills`.

        Coordinates are written by ``pcb._n`` at four decimal places, so a vertex
        here can differ from the computed geometry by up to
        :data:`EMITTED_ROUNDING_MM`; measured worst-case area disagreement across
        the corpus is 0.0012 mm^2. Clearance comparisons allow for that.
        """
        from shapely.geometry import Polygon
        if layer not in self._emitted:
            polys = []
            for blk in _FILLED_POLY_RE.findall(self.text):
                lay, pts = blk
                if lay != layer:
                    continue
                ring = [(float(mx) - pcb.ORIGIN, float(my) - pcb.ORIGIN)
                        for mx, my in _XY_RE.findall(pts)]
                if len(ring) < 3:
                    continue
                p = Polygon(ring)
                polys.append(p if p.is_valid else p.buffer(0))
            self._emitted[layer] = polys
        return self._emitted[layer]

    def pads_of(self, fp_substring: str) -> list[Pad]:
        return [p for p in self.pads if fp_substring in p.fp]

    def graphics(self, layer: str) -> list:
        out = []
        for kind in ("gr_poly", "gr_rect", "gr_circle", "gr_line", "gr_arc",
                     "gr_text"):
            out += [g for g in _kids(self.root, kind) if _val(g, "layer") == layer]
        return out


# ===========================================================================
# 2. Structural helpers (text / zip level)
# ===========================================================================


def sexpr_balanced(text: str) -> bool:
    """Paren balance honouring quoted strings and backslash escapes.

    An unescaped user string (a mask colour, a board name) can close the
    expression early and produce a file KiCad refuses to open — served with a
    200 and a plausible-looking download.
    """
    depth, inq, esc = 0, False, False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == "\\" and inq:
            esc = True
            continue
        if ch == '"':
            inq = not inq
            continue
        if inq:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not inq


def assert_parses(text: str) -> Board:
    """The board text is a balanced s-expression; returns the parsed Board."""
    assert text.startswith("(kicad_pcb"), (
        f"board text starts {text[:40]!r}, not '(kicad_pcb' — KiCad will "
        "refuse to open the downloaded project")
    assert sexpr_balanced(text), (
        "generated board is not a balanced s-expression — the download opens "
        "as a corrupt file, with no error shown to the user")
    return Board(text)


def assert_project_zip(resp, slug: str) -> str:
    """The whole contract of a successful ``/generate``, and nothing incidental.

    Returns the board text so a caller can hand it straight to
    :func:`assert_parses` / :func:`check_board`.
    """
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    want = {f"{slug}/{slug}.kicad_pcb", f"{slug}/{slug}.kicad_pro",
            f"{slug}/BOM.csv", f"{slug}/README.txt"}
    got = set(zf.namelist())
    assert got == want, (
        f"project zip holds {sorted(got)}, expected {sorted(want)} — a missing "
        "member means the user unzips a project KiCad cannot open")
    board = zf.read(f"{slug}/{slug}.kicad_pcb").decode()
    assert_parses(board)
    return board


def assert_not_crashed(resp) -> None:
    """The only assertion a hostile-input case is allowed to make."""
    assert resp.status_code < 500, (
        f"hostile input produced {resp.status_code} — the user sees a bare "
        f"server error page\n{resp.get_data(as_text=True)[:400]}")


def assert_rejected(resp) -> None:
    assert 400 <= resp.status_code < 500, (
        f"expected a 4xx rejection, got {resp.status_code}")
    body = resp.get_json()
    assert isinstance(body, dict) and isinstance(body.get("error"), str) \
        and body["error"], (
        f"rejection carried no usable error message: {body!r} — the UI has "
        "nothing to show the user")


def assert_clean(problems, what: str = "artifact") -> None:
    """Turn a ``check_*`` problem list into a single AssertionError.

    Accepts one list or several: ``assert_clean(check_a(...) + check_b(...))``.
    """
    problems = list(problems)
    if problems:
        raise AssertionError(f"{what}: " + "; ".join(problems))


def assert_deterministic(spec) -> None:
    """Two generations of the same spec are byte-identical.

    Non-determinism here would make every downstream measurement unreliable and
    would show up to the user as two downloads of "the same" badge differing.
    """
    a, b = pcb.generate_pcb(spec), pcb.generate_pcb(spec)
    assert a == b, "generate_pcb is not deterministic for the same spec"


# ===========================================================================
# 3. Board invariants — netlist
# ===========================================================================


def assert_the_board_carries_what_the_spec_implies(b: Board, spec) -> None:
    """Presence and count, stated from the spec: the parts, the routing, the
    barrels, the nets and the pad layers a design of this shape must contain.

    **Why a whole check exists for "is it there at all".** Every other invariant
    in this file is a ``for`` loop over one of ``Board``'s collections, and a
    loop over ``[]`` passes. Measured, by emptying one collection at a time and
    re-running the battery: ``b.tracks = []``, ``b.vias = []`` and
    ``b.footprints = []`` each left **26 of 26 checks green**. A generator
    regression that emitted no tracks — every LED unconnected — or no vias, or
    no footprints at all, went through the entire in-process battery without a
    murmur. That is the same shape as the missing-copper hole
    (:func:`assert_pours_actually_contain_copper`) and the same shape as
    asserting on ``Board.fills`` instead of the emitted copper: the checks all
    describe *properties of* things, and nothing asserted the things exist.

    So this one holds no ``for`` loop and no property. It counts, against
    numbers derived from the spec alone — two footprints per unit plus the
    connector, four pads per unit, one connector pad per kept pin, an anode net
    and an anode track per unit, a via barrel for every unit that is not
    ``novia``. Break any of those and the failure names what is missing rather
    than leaving 26 loops to iterate over nothing.

    Every number here is measured against 445 clean boards spanning every
    package, rotation, face, layout, pin subset, art material, custom outline
    and the ``farled`` / ``novia`` / ``reverse`` flags: exact for the counts,
    and tight (worst-case slack 0) for the via inequality.
    """
    n = len(spec.leds)
    want_fp = 2 * n + (1 if spec.pins else 0)
    assert len(b.footprints) == want_fp, (
        f"the board carries {len(b.footprints)} footprint(s); {n} unit(s) and "
        f"{len(spec.pins)} connector pin(s) require {want_fp} (an LED and a "
        "resistor each, plus the connector) — parts the user placed are simply "
        "not on the board they download")
    conn = [p for p in b.pads if "MiniBadge" in p.fp]
    unit = [p for p in b.pads if "MiniBadge" not in p.fp]
    assert len(conn) == len(spec.pins), (
        f"{len(conn)} connector pad(s) emitted for {len(spec.pins)} kept pin(s) "
        "— the badge does not seat in the host, or seats with dead pins")
    assert len(unit) == 4 * n, (
        f"{len(unit)} unit pad(s) emitted for {n} unit(s), expected {4 * n} "
        "(two per LED, two per resistor) — a part has nothing to solder to")
    layerless = [f"pad {p.num} of {p.ref}" for p in b.pads if not p.layers]
    assert not layerless, (
        f"{layerless} declare no layers at all — the pad exists in the file and "
        "on no copper, mask or paste layer, so the part is not soldered to "
        "anything and every per-layer check passes over it")
    want_nets = {"3V3", "GND"} | {f"/LED{i + 1}_A" for i in range(n)}
    absent = sorted(want_nets - set(b.nets.values()))
    assert not absent, (
        f"the net table is missing {absent} — KiCad has no node to attach that "
        "rail or unit to and the connection does not exist on the fabricated "
        "board")
    unrouted = sorted(f"/LED{i + 1}_A" for i in range(n)
                      if not [t for t in b.tracks
                              if t.net == b.net_of.get(f"/LED{i + 1}_A")])
    assert not unrouted, (
        f"no copper track is emitted on {unrouted} — the resistor and the LED "
        "are declared on the same net and nothing joins them, so the unit "
        "never lights")
    need_vias = sum(1 for led in spec.leds if not led.novia)
    assert len(b.vias) >= need_vias, (
        f"the board carries {len(b.vias)} via(s) but {need_vias} unit(s) route "
        "their rail through the board — those units reach the far-side plane "
        "through a barrel that was never drilled")


def assert_signal_pins_never_powered(b: Board, spec) -> None:
    """+VBATT (1), CLK (9) and NC (10) must never carry a net.

    Standard-mandated: tying VBATT to 3V3 back-feeds the host badge's battery,
    and NC is reserved. Fabs will happily build it.
    """
    forbidden = {num for num, net in STANDARD_PINOUT.items() if net is None}
    for p in b.pads:
        if p.kind == "thru_hole" and "MiniBadge" in p.fp and p.num in forbidden:
            assert p.net == 0, (
                f"connector pin {p.num} ({pcb.PIN_LABELS[p.num]}) carries net "
                f"{p.net_name!r}; signal/battery pins must stay unconnected — "
                "this back-feeds the host badge and the fab will build it")


def assert_connector_power_pins_wired(b: Board, spec) -> None:
    """Every kept 3V3/GND pin lands on its rail, and only on its rail."""
    want = {num: net for num, net in STANDARD_PINOUT.items() if net}
    present = {p.num for p in b.pads if "MiniBadge" in p.fp}
    assert present == set(spec.pins), (
        f"connector pads {sorted(present)} but spec keeps {sorted(spec.pins)} — "
        "the badge would not seat in the host, or would seat with dead pins")
    for p in b.pads:
        if "MiniBadge" in p.fp and p.num in want:
            assert p.net_name == want[p.num], (
                f"connector pin {p.num} on net {p.net_name!r}, expected "
                f"{want[p.num]!r} — the badge draws power from the wrong pin")


def assert_led_circuits_complete(b: Board, spec) -> None:
    """3V3 -> R -> anode -> LED -> GND, per unit, with no unit cross-wired.

    Derived entirely from the spec: nothing here depends on package sizes,
    coordinates, resistor values, or the file-format spelling. Catches a swapped
    LED polarity, a bypassed resistor, and an off-by-one net index — none of
    which any coordinate assertion notices.
    """
    for i, _led in enumerate(spec.leds):
        anode = f"/LED{i + 1}_A"
        assert anode in b.net_of, (
            f"unit {i}: net {anode!r} missing from the net table — the LED is "
            "wired to nothing")
        led = sorted((p for p in b.pads if p.ref == f"D{i + 1}"), key=lambda p: p.num)
        res = sorted((p for p in b.pads if p.ref == f"R{i + 1}"), key=lambda p: p.num)
        assert len(led) == 2, f"unit {i}: expected 2 LED pads, got {len(led)}"
        assert len(res) == 2, f"unit {i}: expected 2 resistor pads, got {len(res)}"
        assert led[0].net_name == "GND", (
            f"unit {i}: LED pad 1 (cathode) is on {led[0].net_name!r}, expected "
            "GND — reversed polarity fabs fine and never lights")
        assert led[1].net_name == anode, (
            f"unit {i}: LED pad 2 (anode) is on {led[1].net_name!r}, expected "
            f"{anode!r} — the LED is cross-wired to another unit")
        assert res[0].net_name == "3V3", (
            f"unit {i}: resistor pad 1 is on {res[0].net_name!r}, expected 3V3 "
            "— the unit never sees power")
        assert res[1].net_name == anode, (
            f"unit {i}: resistor pad 2 is on {res[1].net_name!r}, expected "
            f"{anode!r} — a resistor bridged to the wrong node leaves the LED "
            "uncurrent-limited and it burns out on first power-up")


def assert_net_index_matches_name(b: Board, spec) -> None:
    """Every ``(net <index> "<name>")`` pair agrees with the net table.

    An off-by-one in the net *index* while the *name* stays right is invisible to
    a name-only check and to every coordinate assertion: the board reads fine and
    the LEDs are cross-wired. KiCad resolves by index.

    D2-DERIVED (self-consistency): compares the file against itself, so it cannot
    see a rename that is applied consistently. ``assert_led_circuits_complete``
    covers that direction by naming the nets independently.
    """
    for p in b.pads:
        if not p.net_name and p.net == 0:
            continue
        assert b.nets.get(p.net) == p.net_name, (
            f"pad {p.num} of {p.ref} says net {p.net} = {p.net_name!r} but the "
            f"net table has {b.nets.get(p.net)!r} — KiCad resolves by index, so "
            "this unit is silently wired to a different node")


#: Emitted track endpoints sit exactly on the pad centre they terminate at
#: (``pcb._led_unit`` builds both from the same ``at(*g[...])`` call), so an
#: exact-coincidence test needs no slack and cannot match a near-miss.
PAD_COINCIDENCE_MM = 1e-6


def assert_tracks_carry_the_net_of_the_pads_they_touch(b: Board, spec) -> None:
    """A track ending on a pad must be on that pad's net.

    **KiCad's connectivity comes from pads and zones, not from tracks.** A track's
    stored net is re-absorbed into whatever cluster its endpoints land in, so a
    track emitted on the wrong net is geometrically byte-identical to the right
    one — same coordinates, same width, same layer, same tstamp — and only the
    net number moves. Measured consequences of that:

    * ``kicad-cli pcb drc --severity-all`` reports **0 violations and 0
      unconnected pads** on the anode-track-on-3V3 mutation. The external oracle
      is blind to this whole class.
    * Every pad-level check stays green too, because the *pads* are still right
      (``D1.1=GND, D1.2=/LED1_A, R1.1=3V3, R1.2=/LED1_A``).

    That leaves this check as the only thing between the user and a board where
    the series resistor is shorted out: the LED sits straight across 3V3 and GND
    with no current limit and burns out on first power-up.

    It also catches the coarser failure of two different-net pads landing on the
    same point, which is a short by construction.
    """
    for t in b.tracks:
        for end in (t.a, t.b):
            for p in b.pads:
                if (abs(p.x - end[0]) > PAD_COINCIDENCE_MM
                        or abs(p.y - end[1]) > PAD_COINCIDENCE_MM):
                    continue
                if t.layer not in p.layers and "*.Cu" not in p.layers:
                    continue
                assert p.net == t.net, (
                    f"the track on {t.layer} from {t.a} to {t.b} is on net "
                    f"{b.nets.get(t.net)!r} but it ends on pad {p.num} of "
                    f"{p.ref}, which is on {p.net_name!r} — that is a short "
                    "between the two nets on the finished board, and it is "
                    "invisible to kicad DRC because the copper is identical "
                    "either way")


def assert_every_net_is_declared(b: Board, spec) -> None:
    """No item references a net index the net table does not define.

    D2-DERIVED (self-consistency); see :func:`assert_net_index_matches_name`.
    """
    declared = set(b.nets)
    for label, items in (("pad", b.pads), ("track", b.tracks), ("via", b.vias)):
        for it in items:
            assert it.net in declared, (
                f"{label} references undeclared net {it.net} — KiCad drops the "
                "connection on load")
    for z in b.zones:
        assert z.net in declared, f"zone references undeclared net {z.net}"
    used = {p.net for p in b.pads} | {t.net for t in b.tracks}
    used |= {v.net for v in b.vias} | {z.net for z in b.zones}
    for idx, name in b.nets.items():
        if name.startswith("/LED"):
            assert idx in used, (
                f"anode net {name!r} declared but nothing is on it — that unit "
                "has no circuit")


def assert_vias_cross_the_board(b: Board, spec) -> None:
    """Every via spans both copper layers, and has an annular ring.

    A 2-layer board has no blind vias; a via that lists one layer twice silently
    disconnects whatever it was supposed to carry, and neither KiCad DRC
    (checked: 141/141 green with this bug injected) nor any coordinate assertion
    notices.
    """
    for v in b.vias:
        assert set(v.layers) == {"F.Cu", "B.Cu"}, (
            f"via at ({v.x:.3f}, {v.y:.3f}) on net {b.nets.get(v.net)!r} spans "
            f"{v.layers} — a 2-layer board cannot build a blind via, so this "
            "connection does not exist on the fabricated board")
        assert v.drill < v.size, (
            f"via at ({v.x:.3f}, {v.y:.3f}): drill {v.drill} >= pad {v.size}, "
            "no annular ring — the drill eats the pad and the barrel is open")


def assert_via_geometry_is_fab_safe(b: Board, spec) -> None:
    """Vias are the size this project buys, with a real annular ring.

    Deliberately states 0.7/0.3 independently of ``pcb.VIA_SIZE`` /
    ``pcb.VIA_DRILL`` (D2). ``assert_vias_cross_the_board``'s relational
    ``drill < size`` still holds if someone bumps the drill to 0.5 mm, which
    moves the board out of every fab's cheap tier without changing anything a
    test could see. If you meant to change the via size, change ``VIA_PAD_MM`` /
    ``VIA_DRILL_MM`` here in the same commit.
    """
    for v in b.vias:
        assert abs(v.size - VIA_PAD_MM) < _EPS and abs(v.drill - VIA_DRILL_MM) < _EPS, (
            f"via at ({v.x:.3f}, {v.y:.3f}) is {v.size}/{v.drill} mm pad/drill, "
            f"this project ships {VIA_PAD_MM}/{VIA_DRILL_MM} — a different via "
            "size changes the fab quote and can push the order out of the cheap "
            "tier")
        assert (v.size - v.drill) / 2 >= MIN_ANNULAR_RING_MM - _EPS, (
            f"via at ({v.x:.3f}, {v.y:.3f}) has a "
            f"{(v.size - v.drill) / 2:.4f} mm annular ring, floor is "
            f"{MIN_ANNULAR_RING_MM} mm — drill tolerance can break the barrel")
        assert v.drill >= MIN_DRILL_MM - _EPS, (
            f"via at ({v.x:.3f}, {v.y:.3f}) drills {v.drill} mm, below the "
            f"{MIN_DRILL_MM} mm cheap-tier floor")


def assert_through_hole_pads_reach_both_faces(b: Board, spec) -> None:
    """A ``thru_hole`` pad's barrel must be plated to both copper layers.

    A front through-hole LED's cathode reaches the back GND pour through its own
    lead — restrict the pad to F.Cu and the circuit silently opens.
    """
    for p in b.pads:
        if p.kind != "thru_hole":
            continue
        cu = {ly for ly in p.layers if ly.endswith(".Cu") or ly == "*.Cu"}
        assert "*.Cu" in cu or cu >= {"F.Cu", "B.Cu"}, (
            f"through-hole pad {p.num} of {p.fp} is only on {p.layers} — the "
            "plated barrel never reaches the far-side pour and the part floats")


# ===========================================================================
# 4. Board invariants — pours and light windows
# ===========================================================================


def assert_exactly_one_pour_per_face(b: Board, spec) -> None:
    """One 3V3 pour on F.Cu, one GND pour on B.Cu, and nothing else."""
    real = [z for z in b.zones if not z.keepout]
    got = sorted((z.net_name, z.layer) for z in real)
    assert got == [("3V3", "F.Cu"), ("GND", "B.Cu")], (
        f"expected one 3V3 pour on F.Cu and one GND pour on B.Cu, got {got} — "
        "a missing or duplicated plane means units on that face have no rail")


def assert_pours_actually_contain_copper(b: Board, spec) -> None:
    """Each declared pour has fill geometry in it, not just a zone header.

    ``assert_exactly_one_pour_per_face`` counts *zone declarations*; a board can
    declare both planes and fill neither. Measured: stripping every
    ``(filled_polygon ...)`` line from the generator left all 25 other checks
    green — the two rails were declared, empty, and nobody noticed. Only real
    DRC caught it, so a developer without kicad-cli installed would have shipped
    a board with no power planes at all.

    This check owns the *placement* half of the intent-vs-artifact comparison:
    where the emitted copper landed against where the generator meant to put it.
    :func:`assert_pour_fills_have_no_holes` owns the *quantity* half (area), and
    every other pour invariant now reads :meth:`Board.emitted_fills` directly, so
    they describe copper rather than the generator's intentions.
    """
    from shapely.ops import unary_union
    for net, layer in (("3V3", "F.Cu"), ("GND", "B.Cu")):
        emitted = b.text.count(f'(filled_polygon (layer "{layer}")')
        assert emitted, (
            f"the {net} pour on {layer} is declared but the board contains no "
            f'(filled_polygon (layer "{layer}") ...) — the rail ships '
            "unconnected and no LED on that face can light")
        # Presence is not enough: an emission bug can write the right polygons
        # to the wrong place. Offsetting this pour by 5 mm at the emission site
        # keeps every filled_polygon line, produces 46 real DRC violations, and
        # was invisible to the whole fast tier until this comparison existed.
        # So compare *where the copper landed* against where the generator meant
        # to put it. The computed geometry is the cross-check here, never the
        # source of truth — that is the whole point.
        intended = b.fills(spec, net, layer)
        got = b.emitted_fills(layer)
        # Not `if not got: continue`. The text count above already proved the
        # file holds filled_polygon blocks for this layer, so an empty parse
        # means the READER is broken, and a broken reader silently switches off
        # every emitted-copper check in this file (D11).
        assert got, (
            f"{emitted} (filled_polygon (layer \"{layer}\") ...) block(s) are in "
            "the file but Board.emitted_fills parsed none of them — the emitted"
            "-copper checks are all reading [] and passing vacuously")
        if not intended:
            continue
        ex0, ey0, ex1, ey1 = unary_union(got).bounds
        ix0, iy0, ix1, iy1 = unary_union(intended).bounds
        drift = max(abs(ex0 - ix0), abs(ey0 - iy0),
                    abs(ex1 - ix1), abs(ey1 - iy1))
        assert drift <= 0.05, (
            f"the {net} pour on {layer} was emitted at "
            f"({ex0:.3f}, {ey0:.3f})-({ex1:.3f}, {ey1:.3f}) but the geometry "
            f"says ({ix0:.3f}, {iy0:.3f})-({ix1:.3f}, {iy1:.3f}), off by "
            f"{drift:.3f} mm — copper lands where the generator did not intend "
            "it, shorting whatever it crosses")


def assert_pours_stay_inside_the_outline(b: Board, spec, slack: float = 0.01) -> None:
    """No pour copper spills past the routed outline.

    Reads the copper **as emitted** (:meth:`Board.emitted_fills`), not
    ``pcb._fill_geometry``. That matters because the failure this describes is an
    emission-path failure by nature: a coordinate transform, an ``ORIGIN`` term
    or a rounding change applied when the polygon is written is exactly what puts
    copper outside the outline, and none of it touches the computed geometry.
    Measured: shifting every emitted pour 5 mm at ``pcb.py:2027`` produced 46 real
    DRC violations and this check, reading computed geometry, stayed green.

    D2-DERIVED for the *outline* (``pcb.outline_polygon``);
    ``assert_board_is_the_standard_size`` states the physical size independently.
    """
    board = pcb.outline_polygon(spec).buffer(slack)
    for _net, layer in (("3V3", "F.Cu"), ("GND", "B.Cu")):
        for poly in b.emitted_fills(layer):
            assert board.contains(poly), (
                f"copper emitted on {layer} spills {poly.difference(board).area:.4f} "
                "mm^2 outside the board outline — the router mills through live "
                "copper")


def assert_pours_keep_fab_clearance(b: Board, spec) -> None:
    """The pour honours the DRC rules the shipped ``.kicad_pro`` declares.

    Both numbers are stated independently here (:data:`RULE_CLEARANCE_MM`,
    :data:`RULE_EDGE_CLEARANCE_MM`) and cross-checked against the real project
    file by :func:`assert_project_rules_match_the_invariants`.

    Every side of every comparison is emitted data: the pour comes from
    :meth:`Board.emitted_fills`, the pads and tracks from the parsed file. This
    is the in-process twin of kicad-cli's ``clearance`` /
    ``copper_edge_clearance`` checks, and it only earns that description if it
    measures the copper that ships. Reading ``pcb._fill_geometry`` instead left
    it green on a board with 46 real DRC violations (pour offset 5 mm at the
    emission site, ``actual 0.0000 mm`` clearance to other-net pads).

    Pads are measured as :meth:`Pad.copper` — the real rectangle or circle the
    file declares — not as centre points. A centre-point measurement has half a
    pad of free slack built into it and let a 0.25 mm pour shift through with
    0.10 mm of actual copper-to-copper gap against a 0.15 mm rule.

    Slack is :data:`EMITTED_ROUNDING_MM`, the file's four-decimal coordinate
    precision, rather than ``_EPS``.
    """
    from shapely.geometry import LineString, Polygon
    edges = [Polygon(r).exterior for r in _outline_rings(spec)]
    for net, layer in (("3V3", "F.Cu"), ("GND", "B.Cu")):
        fills = b.emitted_fills(layer)
        for poly in fills:
            for e in edges:
                assert poly.distance(e) >= RULE_EDGE_CLEARANCE_MM - EMITTED_ROUNDING_MM, (
                    f"{net} pour on {layer} comes within {poly.distance(e):.4f} "
                    f"mm of the board edge (rule {RULE_EDGE_CLEARANCE_MM} mm) — "
                    "copper on the routed edge; fabs reject it")
        for p in b.pads:
            if p.net_name == net or p.net == 0:
                continue
            if layer not in p.layers and "*.Cu" not in p.layers:
                continue
            cu = p.copper()
            for poly in fills:
                d = poly.distance(cu)
                assert d >= RULE_CLEARANCE_MM - EMITTED_ROUNDING_MM, (
                    f"{net} pour on {layer} comes within {d:.4f} mm of the "
                    f"copper of pad {p.num} of {p.ref} (net {p.net_name!r}, "
                    f"{p.w} x {p.h} mm) — rule {RULE_CLEARANCE_MM} mm; that is "
                    "a short on the finished board")
        for t in b.tracks:
            if t.layer != layer or b.nets.get(t.net) == net:
                continue
            run = LineString([t.a, t.b]).buffer(t.width / 2)
            for poly in fills:
                d = poly.distance(run)
                assert d >= RULE_CLEARANCE_MM - EMITTED_ROUNDING_MM, (
                    f"{net} pour on {layer} comes within {d:.4f} mm of a "
                    f"{b.nets.get(t.net)!r} track (rule {RULE_CLEARANCE_MM} mm) "
                    "— a short between the rail and a signal")


def assert_pour_fills_have_no_holes(b: Board, spec) -> None:
    """The shipped fill is a flat polygon list, and it is all of the copper.

    Two halves, on purpose, because the hole problem has a computed side and an
    emitted side and neither sees the other.

    **Computed side — and reading ``pcb._fill_geometry`` here is genuinely
    right.** ``(filled_polygon (pts ...))`` has no syntax for an interior ring:
    a hole cannot be represented in the file at all, so no amount of reading the
    emitted text can find one. The property is a *precondition of the emitter*:
    if the geometry still carries an unfractured hole, ``pcb._zone`` is obliged
    to refuse (it raises ``ValueError``), because emitting the exterior alone
    would flood copper over whatever the hole was protecting. This half asserts
    the precondition where the precondition lives.

    **Emitted side.** That leaves the failure the computed half cannot reach:
    the emitter dropping the hole instead of refusing, or writing a subset or a
    superset of the polygons it was given. Total emitted copper area is compared
    against total intended area — a flooded hole, a missing island or a
    duplicated polygon all move it, and none of them move the bounding box that
    :func:`assert_pours_actually_contain_copper` compares. Tolerance is
    :data:`EMITTED_AREA_TOL_MM2`, 40x the worst rounding disagreement measured
    over the corpus.
    """
    for net, layer in (("3V3", "F.Cu"), ("GND", "B.Cu")):
        intended = b.fills(spec, net, layer)
        for poly in intended:
            assert not list(poly.interiors), (
                f"{net} fill on {layer} kept an unfractured hole — the emitter "
                "must refuse it; emitting the exterior alone floods copper over "
                "whatever the hole was protecting")
        got = b.emitted_fills(layer)
        want_area = sum(p.area for p in intended)
        got_area = sum(p.area for p in got)
        assert abs(got_area - want_area) <= EMITTED_AREA_TOL_MM2, (
            f"the {net} pour on {layer} emitted {got_area:.4f} mm^2 of copper "
            f"in {len(got)} polygon(s) but the geometry says {want_area:.4f} "
            f"mm^2 in {len(intended)} — copper the generator did not intend is "
            "on the board (a dropped hole floods other-net pads) or copper it "
            "did intend is missing (that part of the rail is dead)")


def _interior_probes(geom, n: int = 24, grid: int = 5):
    """Points comfortably inside a (multi)polygon, shrunk to avoid the rim.

    Samples a ``grid`` x ``grid`` lattice across each part in addition to its
    representative point. One representative point per part is not enough: it
    lands at the centre, and a window cut too small — a shrunken inset, a
    clip against the wrong interior ring — leaves the centre clear and copper
    everywhere else, which a single central probe reads as clean. Probes are
    ordered representative-point-first so the common case still costs one
    containment test.
    """
    from shapely.geometry import Point
    out = []
    for g in getattr(geom, "geoms", [geom]):
        shrunk = g.buffer(-0.3)
        if shrunk.is_empty:
            continue
        for s in getattr(shrunk, "geoms", [shrunk]):
            p = s.representative_point()
            out.append((p.x, p.y))
            x0, y0, x1, y1 = s.bounds
            for i in range(grid):
                for j in range(grid):
                    q = Point(x0 + (x1 - x0) * (i + 0.5) / grid,
                              y0 + (y1 - y0) * (j + 0.5) / grid)
                    if s.contains(q):
                        out.append((q.x, q.y))
            if len(out) >= n:
                return out[:n]
    return out[:n]


def assert_light_windows_are_clear_of_copper(b: Board, spec) -> None:
    """Glow always cuts both pours; bare cuts the face(s) whose mask opens.

    Sampled from the window geometry itself rather than from hard-coded points,
    so moving or resizing art cannot make this fire spuriously.

    The copper is read **as emitted** (:meth:`Board.emitted_fills`): "is there
    copper in the light path" is a question about the shipped board, and asking
    ``pcb._fill_geometry`` answered it about the generator's intentions instead
    — a pour written 5 mm off its computed position drops solid copper across
    the window and this check could not see it.

    D2-DERIVED for the *window region* (``pcb._window_geometry``), so it would go
    vacuous if that function returned nothing.
    :func:`assert_light_windows_exist_when_art_asks_for_them` guards that, and
    states its own window region from the spec rather than from that helper.
    """
    from shapely.geometry import Point
    for face, layer in (("front", "F.Cu"), ("back", "B.Cu")):
        geom = pcb._window_geometry(spec, face)
        if geom is None or geom.is_empty:
            continue
        fills = b.emitted_fills(layer)
        for probe in _interior_probes(geom):
            for poly in fills:
                assert not poly.contains(Point(probe)), (
                    f"copper survives on {layer} at {probe} inside a light "
                    "window — the LED shines into solid copper")


def assert_light_windows_carry_keepouts(b: Board, spec) -> None:
    """A cut face carries a keepout rule area over its window.

    The README tells users to press B (refill). Without a keepout rule area the
    refill recomputes the pour from KiCad's own rules — which know nothing about
    the light window — and floods it solid.

    Reads the emitted zones; D2-DERIVED only for the *gate* — whether a window
    exists at all comes from ``pcb._window_geometry``, the same dependency as
    :func:`assert_light_windows_are_clear_of_copper`. That gate is what
    :func:`assert_light_windows_exist_when_art_asks_for_them` now guards from the
    spec side, so this check going quiet is no longer silent.
    """
    for face, layer in (("front", "F.Cu"), ("back", "B.Cu")):
        geom = pcb._window_geometry(spec, face)
        if geom is None or geom.is_empty:
            continue
        assert any(z.keepout and z.layer == layer for z in b.zones), (
            f"a light window cuts {layer} but no keepout rule area protects it "
            "— the first refill in KiCad floods the window solid")


def _spec_window_regions(b: Board, spec) -> dict:
    """``{face: region}`` the *spec's own art* asks to be cut, clipped to the
    *emitted* board. ``pcb`` is asked nothing.

    The window region a user is entitled to is the art they drew: ``glow`` cuts
    both faces, ``bare`` cuts the face(s) its ``window`` mode names. Two
    clippings are applied, and both are read off the board rather than assumed:

    * the emitted ``Edge.Cuts`` outline, inset 1.5 mm — the generator holds
      windows inside a perimeter ring so the pour keeps a path round the edge,
      so art hanging over that ring is not entitled to a cut there;
    * ``copper`` art is subtracted — copper artwork inside a window deliberately
      keeps its copper.

    Everything else in this file that talks about windows derives the region
    from ``pcb._window_geometry``. This one does not, which is what makes it
    able to notice that helper returning nothing.
    """
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union
    wants: dict = {}
    for art in spec.art:
        if art.material == "glow":
            faces = ("front", "back")           # glow always cuts both faces
        elif art.material == "bare":
            win = getattr(art, "window", "through")
            faces = ("front", "back") if win == "through" else (win,)
        else:
            continue
        shapes = [box(rx, ry, rx + rw, ry + rh) for rx, ry, rw, rh in art.rects]
        shapes += list(pcb._art_shapely(art.polys))
        for face in faces:
            wants.setdefault(face, []).extend(shapes)
    if not wants:
        return {}
    rings = [g for g in b.graphics("Edge.Cuts") if g[0] in ("gr_poly", "gr_rect")]
    assert rings, "no Edge.Cuts outline emitted — nothing to clip windows to"
    board = max((_edge_polygon(g) for g in rings), key=lambda p: p.area)
    board = Polygon([(x - pcb.ORIGIN, y - pcb.ORIGIN)
                     for x, y in board.exterior.coords])
    interior = board.buffer(-1.5)
    keep = []
    for art in spec.art:
        if art.material != "copper":
            continue
        keep += [box(rx, ry, rx + rw, ry + rh) for rx, ry, rw, rh in art.rects]
        keep += list(pcb._art_shapely(art.polys))
    out = {}
    for face, shapes in wants.items():
        region = unary_union(shapes).intersection(interior)
        if keep:
            region = region.difference(unary_union(keep))
        out[face] = region
    return out


def assert_light_windows_exist_when_art_asks_for_them(b: Board, spec) -> None:
    """Art that asks for a light window gets one cut in the copper that ships.

    This is the anti-vacuity guard for the two D2-DERIVED window checks above:
    if ``pcb._window_geometry`` ever returned nothing, both of them pass on every
    board and the feature could disappear without a red test.

    A guard that asks ``_window_geometry`` whether ``_window_geometry`` produced
    something cannot do that job, and until now this one did exactly that — its
    ``b`` parameter was unused, so it was a unit test of one ``pcb`` helper
    wearing a board-invariant signature, sitting in a list named ``ALL_CHECKS``.
    It now states the region from the spec (:func:`_spec_window_regions`) and
    asserts on the copper the file actually contains, so it fails if the window
    is not cut *for any reason* — the helper returning ``None``, the fill code
    ignoring it, or the emitter writing the pour somewhere else.
    """
    from shapely.geometry import Point
    for face, region in sorted(_spec_window_regions(b, spec).items()):
        layer = "F.Cu" if face == "front" else "B.Cu"
        assert not region.is_empty, (
            f"the spec has art that opens a light window on the {face} face, but "
            "none of it lands where a window can be cut — the LED shines into "
            "solid copper and the two window invariants pass vacuously")
        probes = _interior_probes(region)
        assert probes, (
            f"the {face}-face window region ({region.area:.3f} mm^2) is too thin "
            "to sample — this check cannot see whether copper was cut, so treat "
            "it as unverified rather than green")
        for probe in probes:
            for poly in b.emitted_fills(layer):
                assert not poly.contains(Point(probe)), (
                    f"art asks for a light window on the {face} face but the "
                    f"emitted {layer} copper still covers {probe} — no window "
                    "was cut and the LED shines into solid copper")


# ===========================================================================
# 5. Board invariants — geometry
# ===========================================================================


def _outline_rings(spec):
    if spec.outline:
        return spec.outline
    x0, y0, x1, y1 = pcb.OUTLINE
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]


def assert_copper_clears_the_board_edge(b: Board, spec,
                                        rule: float = RULE_EDGE_CLEARANCE_MM) -> None:
    """Tracks and vias keep the project's copper-to-edge clearance.

    The fast in-process stand-in for kicad-cli's ``copper_edge_clearance`` check
    — the same violation, found in 0.1 ms instead of 620 ms. ``rule`` is stated
    independently (:data:`RULE_EDGE_CLEARANCE_MM`).
    """
    from shapely.geometry import LineString, Point, Polygon
    edges = [Polygon(r).exterior for r in _outline_rings(spec)]
    for t in b.tracks:
        d = min(LineString([t.a, t.b]).distance(e) for e in edges)
        assert d >= rule + t.width / 2 - _EPS, (
            f"track on {t.layer} net {b.nets.get(t.net)!r} from {t.a} to {t.b} "
            f"is {d - t.width / 2:.4f} mm from the board edge; rule is {rule} mm "
            "— the fab either rejects the order or mills through live copper")
    for v in b.vias:
        d = min(Point(v.x, v.y).distance(e) for e in edges)
        assert d >= rule + v.size / 2 - _EPS, (
            f"via at ({v.x:.3f}, {v.y:.3f}) is {d - v.size / 2:.4f} mm from the "
            f"board edge; rule is {rule} mm — the router breaks the barrel out")


def assert_board_is_the_standard_size(b: Board, spec) -> None:
    """A default-outline badge is the 20 mm square the standard specifies.

    Stated independently of ``pcb.OUTLINE`` (D2): every other outline check
    derives its expectation from that constant, so editing it would be invisible
    — and a badge that is not 20 mm square does not fit a host badge's socket.
    Skipped when the spec supplies its own outline, which is a user input.
    """
    if spec.outline:
        return
    polys = [g for g in b.graphics("Edge.Cuts") if g[0] in ("gr_poly", "gr_rect")]
    assert polys, "no Edge.Cuts outline emitted — the board has no shape to route"
    best, area = None, -1.0
    for g in polys:
        p = _edge_polygon(g)
        if p.area > area:
            best, area = p, p.area
    x0, y0, x1, y1 = best.bounds
    w, h = x1 - x0, y1 - y0
    assert (abs(w - STANDARD_BOARD_MM) < STANDARD_BOARD_TOL
            and abs(h - STANDARD_BOARD_MM) < STANDARD_BOARD_TOL), (
        f"the default badge outline measures {w:.4f} x {h:.4f} mm, the SAINTCON "
        f"minibadge standard is {STANDARD_BOARD_MM} x {STANDARD_BOARD_MM} mm — "
        "a badge off this size does not seat in the host")


def _edge_polygon(g):
    from shapely.geometry import Polygon
    if g[0] == "gr_rect":
        s, e = _kid(g, "start"), _kid(g, "end")
        return Polygon([(s[1], s[2]), (e[1], s[2]), (e[1], e[2]), (s[1], e[2])])
    pts = [(p[1], p[2]) for p in _kids(_kid(g, "pts"), "xy")]
    return Polygon(pts)


def assert_board_outline_is_closed_and_matches_the_spec(b: Board, spec) -> None:
    """Exactly one outer contour on Edge.Cuts, with the area the spec asked for.

    Shape, not spelling: ``gr_rect`` and ``gr_poly`` are both fine.

    D2-DERIVED for the default outline (``pcb.OUTLINE``);
    :func:`assert_board_is_the_standard_size` states that case independently.
    """
    from shapely.geometry import Polygon
    edge_polys = [g for g in b.graphics("Edge.Cuts")
                  if g[0] in ("gr_poly", "gr_rect")]
    assert edge_polys, "no Edge.Cuts outline emitted"
    areas = [_edge_polygon(g).area for g in edge_polys]
    outer = max(areas)
    ring = Polygon(_outline_rings(spec)[0]).area
    assert abs(outer - ring) < 1e-3, (
        f"Edge.Cuts outer contour has area {outer:.4f} mm^2, spec outline has "
        f"{ring:.4f} mm^2 — the routed board is not the shape the user drew")


def assert_units_sit_inside_the_safe_region(b: Board, spec) -> None:
    """Every pad of every unit **on the board** is inside the region clamping is
    supposed to keep it in.

    The rule is unchanged; the data source is. This check used to compare
    ``pcb.led_unit_bbox(led, pcb.unit_safe(spec))`` against
    ``pcb.unit_safe(spec)`` — two ``pcb`` functions against each other, with its
    ``b`` parameter unused. That is a property of the placement arithmetic, and
    it holds by construction: ``led_unit_bbox`` clamps into ``safe`` itself, so
    the assertion could not fail on any input and no board was ever examined
    despite the check shipping inside ``ALL_CHECKS``.

    It now reads the emitted pads of every non-connector footprint. That keeps
    the reason the check exists — it is the cheap way to notice clamping being
    turned off, at which point a unit dragged to the corner puts real copper off
    the board — and makes it able to fail. Worst pad-to-boundary margin measured
    over the corpus is 2.1 mm, so it is nowhere near the edge of firing.

    D2-DERIVED for the safe region itself (``pcb.unit_safe``), which is an input
    here: it cannot see that region being widened.
    :func:`assert_copper_clears_the_board_edge` covers tracks and vias against an
    independently stated edge rule; this covers pads.
    """
    x0, y0, x1, y1 = safe = pcb.unit_safe(spec)
    for p in b.pads:
        if "MiniBadge" in p.fp:
            continue                       # the connector is placed, not clamped
        assert (x0 - _EPS <= p.x <= x1 + _EPS
                and y0 - _EPS <= p.y <= y1 + _EPS), (
            f"pad {p.num} of {p.ref} ({p.fp}) is emitted at "
            f"({p.x:.3f}, {p.y:.3f}), outside the safe region {safe} — the part "
            "hangs off the board edge and the fab routes through its copper")


def assert_every_unit_reaches_its_rails(b: Board, spec) -> None:
    """Each unit's two rail contacts land on a pour island that also reaches a
    connector pad of that rail.

    This is the property the perimeter bridges exist for: a glow or bare window
    that rings a unit can fence its copper onto an island, and the board then
    ships with an LED wired to nothing. Same reasoning as ``resolve_novia`` — only
    the filled copper can settle it.

    TIER NOTE: only meaningful on specs that went through the placement backstop
    (:func:`resolved_spec`). On hand-placed overlapping units it fires for the
    placement, not for a code defect — that is D12's biggest false-positive
    generator.

    D2-DERIVED for contact *positions* (``pcb.led_geometry`` / ``clamp_led_obj``)
    and for the perimeter bridges (``pcb.unit_bridges``); the connectivity itself
    is recomputed from the emitted fill polygons — :meth:`Board.emitted_fills`,
    parsed back out of the file, not ``pcb._fill_geometry``.

    That sentence used to be in this docstring while line 941 called
    ``b.fills(...)``, i.e. the generator's own geometry. The one check that
    promised the reader it had escaped the self-referential blindness was inside
    it, and an auditor reading this file would have cleared it on the strength of
    the claim. It is now true: shift the emitted pours and this check reports the
    unit that lost its rail.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union
    safe = pcb.unit_safe(spec)
    bridges = pcb.unit_bridges(spec, safe)
    for i, led in enumerate(spec.leds):
        g = pcb.led_geometry(led)
        cx, cy = pcb.clamp_led_obj(led, safe)

        def at(off, cx=cx, cy=cy, led=led):
            rx, ry = pcb._r(off[0], off[1], led.rot)
            return (cx + rx, cy + ry)

        front = led.side != "back"
        contacts = {
            "F.Cu": (at(g["res_in"]) if front else at(g["via_back"]), "3V3"),
            "B.Cu": (at(g["via_front"]) if front else at(g["led_k"]), "GND"),
        }
        if led.novia:
            contacts.pop("B.Cu" if front else "F.Cu", None)
        for layer, (pt, net) in contacts.items():
            pads = [(px, py) for num, px, py, pnet, _r2 in pcb.CONNECTOR_PADS
                    if pnet == net and num in spec.pins]
            if not pads:
                continue  # no rail on this badge at all; power_missing() says so
            extra = [LineString(seg).buffer(pcb.TRACK_W / 2)
                     for seg in [bridges.get(i, {}).get(layer)] if seg]
            merged = unary_union(list(b.emitted_fills(layer)) + extra)
            for part in getattr(merged, "geoms", [merged]):
                if part.distance(Point(pt)) < 0.7 and any(
                        part.distance(Point(q)) < 1.0 for q in pads):
                    break
            else:
                raise AssertionError(
                    f"unit {i}: its {net} contact at ({pt[0]:.2f}, {pt[1]:.2f}) "
                    f"is not on a {layer} pour island that reaches a {net} "
                    "connector pad — the LED would ship wired to nothing")


def assert_reverse_mount_leds_are_routed_through(b: Board, spec) -> None:
    """A reverse-mount LED shines through a routed hole.

    Without the hole the feature silently becomes an upside-down LED that lights
    nothing.

    D2-DERIVED: the expected count comes from ``pcb.led_geometry(...)["hole"]``.
    """
    import re
    n = sum(1 for led in spec.leds if pcb.led_geometry(led)["hole"])
    got = len(re.findall(r'\(gr_circle[^\n]*\(layer "Edge\.Cuts"\)', b.text))
    assert got == n, (
        f"{n} reverse-mount unit(s) but {got} routed hole(s) on Edge.Cuts — the "
        "LED faces into the board and nothing shines through")


def assert_far_side_leds_have_a_via_in_each_pad(b: Board, spec) -> None:
    """"LED on the other side" only works because a via sits inside each LED pad.

    Drop them and the part is on the far face connected to nothing.

    D2-DERIVED: the expected via positions come from ``pcb.led_geometry``.

    The tolerance is :data:`EMITTED_ROUNDING_MM`, not ``_EPS``. ``want`` is a
    full-precision float; ``v.x`` was written by ``pcb._n`` at four decimals and
    parsed back, so it may legitimately sit up to 5e-5 mm away. This check used
    ``_EPS`` (1e-6) and therefore **failed on 52 far-side pads across a
    445-board sweep whose vias were all present** — every one of them within
    4.914e-5 mm of where it was wanted, i.e. inside the file's own quantum. Any
    real miss is at least a pad pitch (~1 mm) away, four orders of magnitude
    clear of this slack, so nothing is being hidden.

    Structured corpora never caught it because coordinates like 3.925 / 4.075
    are exactly representable at four decimals; it takes a placement that needs
    a fifth decimal to fire.
    """
    for i, led in enumerate(spec.leds):
        g = pcb.led_geometry(led)
        if not (led.farled and not g["hole"] and "drill" not in pcb.PKG[g["pkg"]]):
            continue
        cx, cy = pcb.clamp_led_obj(led, pcb.unit_safe(spec))
        for off in (g["led_k"], g["led_a"]):
            rx, ry = pcb._r(off[0], off[1], led.rot)
            want = (cx + rx, cy + ry)
            assert any(abs(v.x - want[0]) < EMITTED_ROUNDING_MM
                       and abs(v.y - want[1]) < EMITTED_ROUNDING_MM
                       for v in b.vias), (
                f"unit {i}: far-side LED pad at {want} has no via through the "
                "board — the LED sits on the far face connected to nothing")


def assert_back_side_parts_live_on_back_layers(b: Board, spec) -> None:
    """A unit's footprint, its pads and its silk must all agree on a face."""
    for name, layer, _x, _y, _ref, node in b.footprints:
        if "MiniBadge" in name:
            continue
        face = "F" if layer == "F.Cu" else "B"
        for p in _kids(node, "pad"):
            lay = _kid(p, "layers")[1:]
            if any(ly.startswith("*") for ly in lay):
                continue
            assert all(ly.startswith(face + ".") for ly in lay), (
                f"footprint {name} is on {layer} but a pad is on {lay} — the "
                "part is soldered to a face its pads do not reach")


# ===========================================================================
# 6. Board invariants — fab choices
# ===========================================================================


def assert_fab_choices_reach_the_stackup(b: Board, spec) -> None:
    """Mask colour and surface finish are the two things a user picks that only
    show up in the stackup.

    Nothing else in the file records them, so a regression here is invisible
    until the render or the fab order. (Verified: discarding the user's mask
    colour passes all 141 existing tests, including real DRC.)
    """
    assert b.setup is not None, "no (setup ...) block"
    stack = _kid(b.setup, "stackup")
    assert stack is not None, "no (stackup ...) block — the fab gets no colour "\
        "or finish and ships whatever is on the panel"
    colors = {str(_val(ly, "color")) for ly in _kids(stack, "layer")
              if "Mask" in str(ly[1])}
    assert colors == {spec.mask_color.capitalize()}, (
        f"soldermask layers carry colours {colors}, spec asked for "
        f"{spec.mask_color!r} — the user picks purple and is shipped green")
    finish = str(_val(stack, "copper_finish"))
    assert ("HAL" in finish) == (spec.finish == "hasl"), (
        f"copper_finish {finish!r} does not match spec finish {spec.finish!r} — "
        "the user pays for ENIG and gets HASL, or vice versa")


def assert_project_rules_match_the_invariants(pro_text: str) -> None:
    """The shipped ``.kicad_pro`` declares the rules these invariants enforce.

    Two directions, both real:

    * If the project's clearance is *stricter* than what
      :func:`assert_pours_keep_fab_clearance` enforces, the in-process oracle
      goes quiet on boards that real DRC rejects.
    * If it is looser than the fab floor, the user downloads a project that
      passes DRC locally and comes back from the fab as a scrap panel.
    """
    rules = json.loads(pro_text)["board"]["design_settings"]["rules"]
    got_clear = float(rules["min_clearance"])
    got_edge = float(rules["min_copper_edge_clearance"])
    assert got_clear <= RULE_CLEARANCE_MM + _EPS, (
        f"the project declares min_clearance {got_clear} mm but the in-process "
        f"pour check only enforces {RULE_CLEARANCE_MM} mm — boards that fail "
        "real DRC would pass every fast test")
    assert got_edge <= RULE_EDGE_CLEARANCE_MM + _EPS, (
        f"the project declares min_copper_edge_clearance {got_edge} mm but the "
        f"in-process edge check only enforces {RULE_EDGE_CLEARANCE_MM} mm — "
        "same blind spot")
    for name, got in (("min_clearance", got_clear),
                      ("min_copper_edge_clearance", got_edge)):
        assert got >= FAB_CLEARANCE_FLOOR_MM - _EPS, (
            f"the project declares {name} {got} mm, below the "
            f"{FAB_CLEARANCE_FLOOR_MM} mm fab floor — DRC passes locally and "
            "the panel comes back scrap")


# ===========================================================================
# 7. The one call a board test needs
# ===========================================================================

ALL_CHECKS = [
    # Presence first: every check below it is a loop, and a loop over [] passes.
    assert_the_board_carries_what_the_spec_implies,
    assert_signal_pins_never_powered,
    assert_connector_power_pins_wired,
    assert_led_circuits_complete,
    assert_net_index_matches_name,
    assert_tracks_carry_the_net_of_the_pads_they_touch,
    assert_every_net_is_declared,
    assert_vias_cross_the_board,
    assert_via_geometry_is_fab_safe,
    assert_through_hole_pads_reach_both_faces,
    assert_exactly_one_pour_per_face,
    assert_pours_actually_contain_copper,
    assert_pours_stay_inside_the_outline,
    assert_pours_keep_fab_clearance,
    assert_pour_fills_have_no_holes,
    assert_light_windows_are_clear_of_copper,
    assert_light_windows_carry_keepouts,
    assert_light_windows_exist_when_art_asks_for_them,
    assert_copper_clears_the_board_edge,
    assert_board_is_the_standard_size,
    assert_units_sit_inside_the_safe_region,
    assert_every_unit_reaches_its_rails,
    assert_reverse_mount_leds_are_routed_through,
    assert_far_side_leds_have_a_via_in_each_pad,
    assert_board_outline_is_closed_and_matches_the_spec,
    assert_fab_choices_reach_the_stackup,
    assert_back_side_parts_live_on_back_layers,
]


def check_board(spec, text: str | None = None, checks=None) -> Board:
    """Run every board invariant against ``spec``. The one call a test needs.

    ``text`` defaults to ``pcb.generate_pcb(spec)``; pass the bytes that came out
    of ``/generate`` when you want to assert on what the user actually downloads.
    Returns the parsed :class:`Board` so a test can add its own assertion.

    ~55 ms per board including generation and the memoised fills.
    """
    text = text if text is not None else pcb.generate_pcb(spec)
    b = assert_parses(text)
    for fn in (checks or ALL_CHECKS):
        fn(b, spec)
    return b


# ===========================================================================
# 8. The DRC oracle. Do NOT hand-place LEDs and assert DRC-clean.
# ===========================================================================


def resolved_spec(spec):
    """Apply the webapp's placement backstop, exactly as a real download does.

    ``generate_pcb`` is **not** responsible for DRC cleanliness — it emits what
    the spec says. De-confliction lives in ``webapp.py:774-825``: clamp, clear the
    connector pads, separate units from each other. Feeding hand-written
    ``Led(x, y)`` coordinates straight to ``generate_pcb`` and then asserting
    DRC-clean tests the test author's arithmetic, not the code, and it is the
    single biggest false-positive generator in this area (6 of 8 DRC-dirty boards
    in a 181-board sweep were the sweep's own bad coordinates).

    Either build your corpus through the webapp (``POST /generate``) or route it
    through here. :func:`assert_drc_clean` does it for you.

    **This is the placement backstop, not the whole download path.** ``/generate``
    also runs ``pcb.power_missing`` and ``pcb.resolve_novia`` and **refuses with a
    400** when a via-less unit cannot reach its rail (webapp.py:1212-1236), and on
    a custom outline it relocates units off cut-outs (webapp.py:789-829). A spec
    that comes out of here can therefore be one the app would never ship. If a
    check fires on a randomised spec, rule that out first:

    ``_leds, bad = pcb.resolve_novia(resolved_spec(spec), pcb.unit_safe(spec))``

    — a non-empty ``bad`` means the user gets an error message, not this board.
    (1 of the 8 boards that failed :func:`assert_every_unit_reaches_its_rails` on
    a 445-board sweep was exactly this; the other 7 were real.)
    """
    safe = pcb.unit_safe(spec)
    leds = []
    for led in spec.leds:
        x, y = pcb.clamp_led_obj(led, safe)
        leds.append(replace(led, x=x, y=y))
    leds = [pcb.resolve_pad_overlap(led, spec.pins, safe) for led in leds]
    for i in range(1, len(leds)):
        for prev in leds[:i]:
            leds[i] = pcb.resolve_overlap(prev, leds[i], safe=safe)
    return replace(spec, leds=leds)


def with_connector_tabs(rings, pins=None):
    """Union a custom outline with the connector pad plates, as the webapp does.

    The second half of D12. ``webapp._outline_geometry`` (webapp.py:435-470)
    never ships a user shape raw: it unions a minimal board tab under every kept
    connector pad pair, bridging any tab the shape does not reach solidly. A
    hand-written ``outline=[[...circle...]]`` fed straight to ``generate_pcb``
    produces a board whose connector pads are **off the board** — every rail
    invariant then fires for the test's own geometry, not for a code defect.

    -> a list of rings suitable for ``BadgeSpec(outline=...)``.
    """
    from shapely.geometry import LineString, Polygon
    from shapely.geometry import box as sbox
    from shapely.ops import nearest_points, unary_union
    pins = tuple(pins) if pins is not None else pcb.ALL_PINS
    shape = Polygon(rings[0], rings[1:] if len(rings) > 1 else None)
    plates = [sbox(*pcb.PAD_PAIRS[k]["plate"]) for k in pcb.active_pairs(pins)]
    bridges = []
    for plate in plates:
        inter = shape.intersection(plate)
        if not inter.is_empty and inter.area >= 2.0:
            continue
        p1, p2 = nearest_points(plate.centroid, shape)
        seg = LineString([p1, p2])
        bridges.append(seg.buffer(1.5) if seg.length > 0 else plate.buffer(1.5))
    out = unary_union([shape, *plates, *bridges])
    if out.geom_type == "MultiPolygon":
        out = max(out.geoms, key=lambda g: g.area)
    out = out.simplify(0.02)
    return [list(out.exterior.coords)] + [list(r.coords) for r in out.interiors]


DRC_ARGS = ["pcb", "drc", "--severity-all", "--exit-code-violations",
            "--format", "json"]


def write_project(spec, tmp_path, resolve: bool = True):
    """Write ``spec`` as a KiCad project into ``tmp_path``. -> the .kicad_pcb path.

    ``resolve=True`` (the default, and the only path you should normally use)
    puts the spec through :func:`resolved_spec` first.
    """
    if resolve:
        spec = resolved_spec(spec)
    name = getattr(spec, "name", None) or "board"
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "board"
    board = tmp_path / f"{slug}.kicad_pcb"
    board.write_text(pcb.generate_pcb(spec))
    (tmp_path / f"{slug}.kicad_pro").write_text(pcb.generate_project(slug))
    return board


def drc_violations(board_path, kicad_cli):
    """Run ``kicad-cli pcb drc`` on a written board. -> (violations, full report).

    ~620 ms per board. The ``.kicad_pro`` next to the board supplies the rules;
    without it KiCad falls back to its own defaults and the run means nothing.
    """
    board_path = _Path(board_path)
    assert board_path.with_suffix(".kicad_pro").exists(), (
        f"no .kicad_pro beside {board_path.name} — kicad-cli would fall back to "
        "KiCad's default rules, not the ones this project ships")
    out = board_path.with_name(board_path.stem + "-drc.json")
    subprocess.run([str(kicad_cli), *DRC_ARGS, "-o", str(out), str(board_path)],
                   capture_output=True, timeout=300, check=False)
    assert out.exists(), "kicad-cli pcb drc wrote no report at all"
    report = json.loads(out.read_text())
    viol = list(report.get("violations", [])) + list(report.get("unconnected_items", []))
    return viol, report


def assert_drc_clean(target, kicad_cli, tmp_path=None, ignore=(),
                     resolve: bool = True):
    """The external oracle: KiCad's own DRC finds nothing.

    ``target`` is either a ``BadgeSpec`` — in which case ``tmp_path`` is required
    and the spec goes through :func:`resolved_spec` first — or a path to an
    already-written ``.kicad_pcb`` (``board_dir(...).pcb``).

    Sees what the in-process invariants cannot: courtyard overlap, silk over
    copper, solder-mask bridges, copper slivers, hole clearance, **pad-to-pad
    shorts**, crossing tracks, unconnected items. Blind to what they do see: via
    layer spans, the stackup, and net index/name agreement. Run both — measured,
    each oracle catches defects the other does not.

    ``ignore`` is a set of violation ``type`` strings to tolerate; keep it empty
    unless you are asserting on a deliberately broken fixture.
    """
    if hasattr(target, "leds"):
        assert tmp_path is not None, (
            "assert_drc_clean(spec, ...) needs tmp_path to write the project into")
        name = getattr(target, "name", "?")
        board_path = write_project(target, tmp_path, resolve=resolve)
    else:
        board_path = _Path(target)
        name = board_path.stem
    viol, _report = drc_violations(board_path, kicad_cli)
    bad = [v for v in viol if v.get("type") not in ignore]
    if not bad:
        return []
    lines = []
    for v in bad[:10]:
        where = "; ".join(str(i.get("description", "")) for i in v.get("items", []))
        lines.append(f"{v.get('severity')} {v.get('type')}: "
                     f"{v.get('description')} [{where}]")
    raise AssertionError(
        f"kicad-cli DRC found {len(bad)} violation(s) on {name} — the user "
        "downloads a project that their fab will reject:\n  "
        + "\n  ".join(lines))


# ===========================================================================
# 9. GLB invariants — what the user actually sees in the 3D view
#
# kicad-cli 9.0.4 writes GLB through OpenCASCADE, in METRES, Y-up:
#     glTF x == board page x (mm) / 1000
#     glTF z == board page y (mm) / 1000      (NO sign flip)
#     glTF y == height above the dielectric bottom face
# Every constant below was measured on KiCad 9.0.4, not guessed.
# ===========================================================================

MM = 1000.0                                   # glTF metres -> board mm

CORE_Y = (0.0, 1.51)                          # the "<project>_PCB" mesh
LAYER_Y = {                                   # (min, max) per board-layer mesh
    "PCB": (0.000, 1.510),
    "copper": (-0.035, 1.545),
    "pad": (-0.040, 1.550),
    "soldermask": (-0.050, 1.560),
    "silkscreen": (-0.075, 1.585),
    "via": (-0.035, 1.545),
}
MOUNT_PLANE = {"front": 1.595, "back": -0.085}   # component model origin y
BOARD_MIDPLANE = 0.755
BOARD_LAYERS = frozenset(LAYER_Y)

# Tolerances, all derived from measurement (five repeat exports agreed to
# 0.0000 mm), not from taste.
TOL_EXACT = 1e-4      # same kicad-cli build, same board: numbers are bit-equal
TOL_STACK = 0.005     # stack heights, room for a KiCad point release
TOL_PLACE = 0.30      # |body centre - footprint origin|; clean max 0.1919 mm
                      # (LED_D3.0mm's flange), smallest injected defect 2.5400
TOL_CANON = 0.01      # spread of the de-rotated residual across rot x face;
                      # measured spread 0.0000 mm on every package

_COMP = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
          "MAT2": 4, "MAT3": 9, "MAT4": 16}

GLB_ARGS = ["pcb", "export", "glb", "--subst-models", "--include-tracks",
            "--include-pads", "--include-zones", "--include-silkscreen",
            "--include-soldermask", "--force"]


def parse_glb(data: bytes):
    """-> (gltf json dict, BIN chunk). Raises on a malformed container."""
    if len(data) < 20 or data[:4] != b"glTF":
        raise ValueError("not a GLB")
    _, _, total = struct.unpack_from("<4sII", data, 0)
    if total != len(data):
        raise ValueError(f"GLB header length {total} != file length {len(data)}")
    off, js, binc = 12, None, b""
    while off + 8 <= len(data):
        ln, typ = struct.unpack_from("<I4s", data, off)
        chunk = data[off + 8:off + 8 + ln]
        if typ == b"JSON":
            js = json.loads(chunk)
        elif typ[:3] == b"BIN":
            binc = chunk
        off += 8 + ln + (-ln % 4)               # chunks are 4-byte padded
    if js is None:
        raise ValueError("GLB has no JSON chunk")
    return js, binc


def load_glb(path):
    with open(path, "rb") as f:
        return parse_glb(f.read())


def _accessor(g, binc, idx):
    import numpy as np
    acc = g["accessors"][idx]
    n, ncomp = acc["count"], _NCOMP[acc["type"]]
    dt = np.dtype("<" + _COMP[acc["componentType"]])
    if "bufferView" not in acc:                 # sparse-only / zero-filled
        return np.zeros((n, ncomp), dtype=dt)
    bv = g["bufferViews"][acc["bufferView"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or dt.itemsize * ncomp
    if stride == dt.itemsize * ncomp:
        return np.frombuffer(binc, dtype=dt, count=n * ncomp,
                             offset=base).reshape(n, ncomp)
    raw = np.frombuffer(binc, dtype=np.uint8, count=stride * n, offset=base)
    return (raw.reshape(n, stride)[:, :dt.itemsize * ncomp]
            .copy().view(dt).reshape(n, ncomp))


def _matrix(node):
    import numpy as np
    if "matrix" in node:                        # glTF matrices are column-major
        return np.array(node["matrix"], float).reshape(4, 4).T
    m = np.eye(4)
    if "scale" in node:
        m = np.diag(list(node["scale"]) + [1.0]) @ m
    if "rotation" in node:
        x, y, z, w = node["rotation"]
        m = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0],
            [0, 0, 0, 1]], float) @ m
    if "translation" in node:
        t = np.eye(4)
        t[:3, 3] = node["translation"]
        m = t @ m
    return m


def export_glb(spec, tmp_path, kicad_cli, resolve: bool = True):
    """Export a BadgeSpec to GLB. Returns the path.

    Deliberately ignores the return code: kicad-cli prints "Could not add 3D
    model for <ref>" and **exits 2** when a model file is missing, but still
    writes a complete, valid GLB with the part silently absent. Presence is
    asserted by :func:`check_present`, which gives a far better message than a
    generic "kicad-cli failed".
    """
    if resolve:
        spec = resolved_spec(spec)
    name = getattr(spec, "name", None) or "board"
    board = tmp_path / f"{name}.kicad_pcb"
    board.write_text(pcb.generate_pcb(spec))
    out = tmp_path / f"{name}.glb"
    subprocess.run([str(kicad_cli), *GLB_ARGS, "-o", str(out), str(board)],
                   capture_output=True, timeout=300, check=False)
    assert out.exists(), "kicad-cli wrote no GLB at all"
    return out


def board_geometry(src) -> dict:
    """World-space, board-mm summary of an exported GLB.

    -> ``{"layers": {role: box}, "components": {ref: box}, "materials": [...],
    "layer_material": {role: [material index, ...]}, "gltf": {...}}``

    ``box`` = ``{"min","max","ctr","size"}`` as 3-vectors in mm, ordered
    (board x, height, board y). Component boxes also carry ``mount_y`` (the model
    origin's height, i.e. the plane KiCad sat the part on) and ``model``.

    The body centre is the world AABB midpoint — tessellation-independent, unlike
    a vertex mean, and unlike the node translation, which is only the anchor
    (pin 1 for THT models). ``src`` may be a path or a parsed ``(gltf, bin)``.
    """
    import numpy as np
    g, binc = src if isinstance(src, tuple) else load_glb(src)
    layers, comps, lmat = {}, {}, {}

    def add(store, key, mn, mx, nv, extra=None):
        r = store.get(key)
        if r is None:
            store[key] = r = {"min": mn.copy(), "max": mx.copy(), "n_vert": 0}
            r.update(extra or {})
        r["min"] = np.minimum(r["min"], mn)
        r["max"] = np.maximum(r["max"], mx)
        r["n_vert"] += nv

    def walk(ni, parent):
        node = g["nodes"][ni]
        world = parent @ _matrix(node)
        if "mesh" in node:
            mesh = g["meshes"][node["mesh"]]
            mname = str(mesh.get("name", ""))
            role = mname.rsplit("_", 1)[-1]
            for prim in mesh.get("primitives", []):
                ai = prim.get("attributes", {}).get("POSITION")
                if ai is None:
                    continue
                p = _accessor(g, binc, ai).astype(float)
                v = (np.c_[p, np.ones(len(p))] @ world.T)[:, :3] * MM
                mn, mx = v.min(axis=0), v.max(axis=0)
                if role in BOARD_LAYERS:
                    add(layers, role, mn, mx, len(v))
                    lmat.setdefault(role, set()).add(prim.get("material"))
                else:
                    add(comps, str(node.get("name", f"node{ni}")), mn, mx, len(v),
                        {"model": mname, "mount_y": float(world[1, 3] * MM)})
        for c in node.get("children", []):
            walk(c, world)

    scene = g.get("scene", 0)
    for ni in g.get("scenes", [{}])[scene].get("nodes", []):
        walk(ni, np.eye(4))
    for store in (layers, comps):
        for r in store.values():
            r["ctr"] = (r["min"] + r["max"]) / 2.0
            r["size"] = r["max"] - r["min"]
    return {"layers": layers, "components": comps,
            "materials": g.get("materials", []),
            "layer_material": {k: sorted(v) for k, v in lmat.items()},
            "gltf": g}


def check_board_slab(geom, outline_mm=(0.16, 0.16, 20.16, 20.16),
                     origin=pcb.ORIGIN, tol=TOL_STACK) -> list[str]:
    """The dielectric slab matches Edge.Cuts, and the stack heights match.

    For a custom outline pass ``outline_mm=pcb.outline_polygon(spec).bounds``.
    Layers absent from the board (a via-less design has no ``via`` mesh) are
    skipped, not failed.
    """
    import numpy as np
    out = []
    slab = geom["layers"].get("PCB")
    if slab is None:
        return [("no board slab in the GLB (mesh '<project>_PCB' missing) — "
                 "the 3D view shows floating parts and no board")]
    x0, y0, x1, y1 = outline_mm
    want = ((slab["min"], np.array([origin + x0, CORE_Y[0], origin + y0]), "min"),
            (slab["max"], np.array([origin + x1, CORE_Y[1], origin + y1]), "max"))
    for got, exp, tag in want:
        d = np.abs(got - exp)
        if d.max() > tol:
            out.append(f"board slab {tag} {np.round(got, 4).tolist()} != "
                       f"{np.round(exp, 4).tolist()} (max |d| {d.max():.4f} mm) "
                       "— the 3D preview is not the board being fabricated")
    for role, (lo, hi) in LAYER_Y.items():
        r = geom["layers"].get(role)
        if r is None:
            continue
        if abs(r["min"][1] - lo) > tol or abs(r["max"][1] - hi) > tol:
            out.append(f"layer {role!r} height "
                       f"[{r['min'][1]:.4f},{r['max'][1]:.4f}] != [{lo},{hi}] — "
                       "the stackup the fab is quoted on has moved")
    return out


def check_present(geom, refs) -> list[str]:
    """Every expected reference has a 3D model in the export.

    kicad-cli prints 'Could not add 3D model for <ref>' and EXITS 2 when a model
    file is missing — but still writes a complete, valid GLB with the part
    silently absent. Never trust the exit code alone; assert the set of component
    node names on every 3D test.
    """
    have = set(geom["components"])
    return [(f"component {r!r} has no 3D model in the GLB "
             f"(have {sorted(have)}) — the user's 3D preview is missing a part "
             "that is on the board")
            for r in refs if r not in have]


def check_mount_plane(geom, ref, face, z_offset_mm=0.0, tol=TOL_EXACT) -> list[str]:
    """The part sits ON its face — not floating above, not sunk into it.

    Height defects move ``node.translation.y`` and nothing else in the plane, so
    a placement check is blind to them; this catches them with a 1.0 mm signal
    against a 1e-4 tolerance. Pass ``z_offset_mm`` for parts deliberately
    displaced (the connector headers carry ``offset z -1.6``).
    """
    c = geom["components"].get(ref)
    if c is None:
        return [f"component {ref!r} missing"]
    sign = 1 if face == "front" else -1
    want = MOUNT_PLANE[face] + sign * z_offset_mm
    d = abs(c["mount_y"] - want)
    if d <= tol:
        return []
    how = "floats above" if (c["mount_y"] - want) * sign > 0 else "sinks into"
    return [(f"{ref} {how} the {face} face by {d:.4f} mm (mount plane "
             f"{c['mount_y']:.4f}, expected {want:.4f}) — the part is not "
             "sitting on the board it is soldered to")]


def check_face(geom, ref, face) -> list[str]:
    """The part is mounted on the face the spec asked for.

    Scope this to ``D*``/``R*``: ``pcb._connector_footprint`` deliberately drops
    the headers below the board, so ``J1`` reads as 'back' on a front-side board.
    """
    c = geom["components"].get(ref)
    if c is None:
        return [f"component {ref!r} missing"]
    got = "front" if c["mount_y"] > BOARD_MIDPLANE else "back"
    return [] if got == face else [
        (f"{ref} is on the {got} face, spec says {face} (mount plane "
         f"{c['mount_y']:.4f} vs midplane {BOARD_MIDPLANE}) — the user placed "
         "the LED on one side and it is assembled on the other")]


def placement_error(geom, ref, page_xy):
    """(dx, dy, |d|) between the part's body centre and its footprint origin.

    Board mm, y counting DOWN like the rest of ``pcb.py``.
    """
    c = geom["components"][ref]
    dx = float(c["ctr"][0] - page_xy[0])
    dy = float(c["ctr"][2] - page_xy[1])
    return dx, dy, math.hypot(dx, dy)


def check_placement(geom, ref, page_xy, tol=TOL_PLACE) -> list[str]:
    """The part's body landed on its pads.

    Never assert this is ~0: ``LED_D3.0mm`` is legitimately 0.1919 mm off because
    its flange is not symmetric about the pin-1 anchor. The tolerance must clear
    the worst *stock model* asymmetry.
    """
    if ref not in geom["components"]:
        return [f"component {ref!r} missing"]
    dx, dy, d = placement_error(geom, ref, page_xy)
    return [] if d <= tol else [
        (f"{ref} body centre is {d:.4f} mm off its footprint origin "
         f"(dx {dx:+.4f}, dy {dy:+.4f}); tolerance {tol} — the part straddles "
         "bare laminate and one pad is left exposed")]


def canonical_residual(dx, dy, rot, face):
    """Fold out the unit rotation and the back-face mirror.

    The stock 3D models are not all symmetric about their pin-1 anchor, so the
    raw error is package-dependent. De-rotated and un-mirrored it is a CONSTANT
    for a given (package, layout) — measured identical to 0.0000 mm across all
    four rotations on both faces.
    """
    t = math.radians(-float(rot) % 360)
    ux = dx * math.cos(t) - dy * math.sin(t)
    uy = dx * math.sin(t) + dy * math.cos(t)
    return (ux, -uy if face == "back" else uy)


def check_rotation_invariance(samples, tol=TOL_CANON) -> list[str]:
    """``samples``: ``[(label, dx, dy, rot, face), ...]`` for ONE (package, layout).

    **This is the assertion that catches the historical LED-lens bug, and the
    absolute check alone does not.** A sign error in the model offset or rotation
    is INVISIBLE at rot 0 and 180 (measured: error 0.0000 mm there, 2.5400 mm at
    90/270), so a per-board absolute check on a 0-degree board passes a broken
    build. On the injected y-sign defect this fired on 4/4 affected package groups
    where the absolute check fired on only 16/32 boards.

    Any change to ``pcb._smd``'s model block — ``offset``, ``rotate``, ``mz``,
    ``f``, or the ``drill`` branch — needs the full rotation x face sweep, not one
    board.
    """
    import numpy as np
    canon = [(lab, *canonical_residual(dx, dy, rot, face))
             for lab, dx, dy, rot, face in samples]
    if not canon:
        return []
    ref = np.array(canon[0][1:])
    spread = max(float(np.abs(np.array(c[1:]) - ref).max()) for c in canon)
    if spread <= tol:
        return []
    return [f"model placement is not rotation/face invariant: residual spread "
            f"{spread:.4f} mm > {tol} — the 3D model sits on its pads at some "
            "rotations and off them at others: " +
            ", ".join(f"{lab}=({ux:+.4f},{uy:+.4f})" for lab, ux, uy in canon)]


def material_roles(geom) -> dict:
    """``{role: material index}``, using the same mesh-suffix mapping
    ``webapp._GLB_LAYER_ROLES`` uses — so a regression in ``_tag_glb_layers``'s
    role attribution shows up here too."""
    return {k: v[0] for k, v in geom["layer_material"].items() if v}


MASK_RGB = {                       # measured baseColorFactor, KiCad 9.0.4
    "green": (0.0627, 0.16, 0.1129),
    "black": (0.0345, 0.0345, 0.0345),
    "red": (0.5678, 0.0596, 0.0659),
    "blue": (0.0063, 0.1851, 0.5082),
    "white": (0.7686, 0.7686, 0.7686),
    "purple": (0.1004, 0.0063, 0.1663),
    "yellow": (0.6086, 0.6118, 0.0),
}


def check_mask_material(geom, rgb, opaque, tol=0.01) -> list[str]:
    """Soldermask colour comes from the stackup; alpha comes from kicad-cli.

    Raw export: ``alphaMode`` BLEND with a fixed alpha 0.83 whatever the colour.
    After ``webapp._tag_glb_layers``: OPAQUE, alpha 1.0, name ``"soldermask:<i>"``.
    Pass ``opaque=False`` to pin the raw-export contract (so a KiCad change that
    silently drops the BLEND is noticed), ``opaque=True`` for served bytes.
    """
    idx = material_roles(geom).get("soldermask")
    if idx is None:
        return ["no soldermask material in the GLB — the board renders bare"]
    m = geom["materials"][idx]
    f = m["pbrMetallicRoughness"]["baseColorFactor"]
    out = []
    if max(abs(a - b) for a, b in zip(f[:3], rgb)) > tol:
        out.append(f"soldermask colour {[round(v, 4) for v in f[:3]]} != {rgb} — "
                   "the user picks a colour and the preview shows another")
    if opaque:
        if m.get("alphaMode", "OPAQUE") != "OPAQUE" or f[3] < 1.0 - _EPS:
            out.append(f"soldermask not opaque after tagging: "
                       f"alphaMode={m.get('alphaMode')} alpha={f[3]} — copper "
                       "ghosts through the mask in the 3D view")
        if not str(m.get("name", "")).startswith("soldermask:"):
            out.append(f"soldermask material not tagged: name={m.get('name')!r} — "
                       "the viewer's layer toggles cannot find it")
    elif m.get("alphaMode") != "BLEND" or abs(f[3] - 0.83) > 1e-3:
        out.append(f"raw export mask alpha changed: alphaMode="
                   f"{m.get('alphaMode')} alpha={f[3]} (was BLEND/0.83) — "
                   "_tag_glb_layers is fixing a problem that no longer exists")
    return out


# ===========================================================================
# 10. Raster metrics over false-colour renders
#
# Every pixel in a false-colour map render belongs to one of a handful of
# classes whose authored colours are >=100/255 apart in at least two channels,
# so classification is a nearest-of-N lookup with a huge margin: no thresholds
# to tune, and a lighting or shading change in a future KiCad cannot flip a
# class. Never golden a PNG — five identical render invocations produce five
# different files, with anti-aliasing disabled as well as on.
# ===========================================================================

#: Authored false colours -> rendered, via ``out = 35 + 0.859*c`` for lit
#: surfaces. Background is drawn unlit. Measured values, not predicted ones.
CLASSES = {
    "offboard": (254, 0, 254),     # magenta background (also drill holes)
    "laminate": (22, 19, 254),     # blue  FR4 with no copper on it
    "copper": (254, 35, 35),       # red   copper (pads, art, exposed pour)
    "pour": (254, 177, 106),       # pour copper seen with the mask hidden.
                                   # KiCad shades it differently from pad copper
                                   # and the value is NOT ours to set, so never
                                   # assert on this class directly — use
                                   # has_copper().
    "mask": (48, 254, 48),         # green soldermask
    "silk": (254, 254, 254),       # white silkscreen ink
}
ORDER = list(CLASSES)

#: Gate thresholds, each calibrated against a deliberately broken artifact.
COPPER_ISLAND_MIN_FRAC = 0.05   # ignore isolated pads; big islands only
SILK_ON_OPENING_MAX = 5e-4      # 7x the measured noise, 6x below the weakest
                                # real defect
PLACEMENT_TOL_MM = 0.4          # 3x the measured systematic bbox bias
MAX_UNKNOWN_FRAC = 0.02         # AA/perimeter budget


def _ref_colors():
    import numpy as np
    return np.array([CLASSES[k] for k in ORDER], dtype=np.int16)


def classify(path, *, max_unknown: float = MAX_UNKNOWN_FRAC):
    """RGB PNG -> ((H,W) int8 array of class indices into ORDER, unknown frac).

    Pixels are assigned to the nearest class colour. Anti-aliased edge pixels sit
    between two classes and land on one of them, which is why every metric below
    is an area *fraction* with a tolerance far larger than the perimeter's share
    of the image.

    Raises if more than ``max_unknown`` of the image is further than 60 units
    (L-inf) from every class: that is the signal that the appearance preset did
    not apply, or that a layer rendered in a colour we do not know about — the
    failure mode where a whole feature class is invisible and every area metric
    quietly reads zero.
    """
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(path).convert("RGB"), dtype=np.int16)
    d = np.abs(a[:, :, None, :] - _ref_colors()[None, None, :, :]).max(axis=3)
    idx = d.argmin(axis=2)
    unknown = float((d.min(axis=2) > 60).mean())
    if unknown > max_unknown:
        raise AssertionError(
            f"{path}: {unknown:.3%} of pixels match no known class (limit "
            f"{max_unknown:.1%}) — the appearance preset did not apply, so every "
            "area metric taken from this render is meaningless")
    return idx.astype(np.int8), unknown


def mask_of(idx, name):
    return idx == ORDER.index(name)


def onboard(idx):
    """Boolean mask of pixels that are part of the board (not background)."""
    return idx != ORDER.index("offboard")


def has_copper(idx):
    """Copper on the *copper* map.

    Robust definition: on that map every on-board pixel is either laminate (a
    colour we chose) or copper (a colour KiCad chose). Assert on the former.
    """
    return onboard(idx) & ~mask_of(idx, "laminate")


def frac(m, denom=None) -> float:
    """Area of ``m`` as a fraction of ``denom`` (default: the whole image)."""
    return float(m.sum()) / float(denom.sum() if denom is not None else m.size)


def components(m, min_px: int = 0) -> list[int]:
    """Sizes of 4-connected True regions in ``m``, descending.

    NOTE: do NOT reach for ``PIL.ImageDraw.floodfill`` as a substitute. On Pillow
    12.3.0 it is a **silent no-op** on a mode-"L" image built with
    ``Image.fromarray`` — it returns without error and changes nothing, so a
    component count comes back as 0 and a naive test passes forever. That is why
    every metric in this section is calibrated against a deliberately broken
    artifact before it is trusted.

    This is a row-run union-find: each row is reduced to runs of True, and runs
    are merged with any overlapping run in the row above. Cost is proportional to
    the number of runs (hundreds), not to the pixel count.
    """
    import numpy as np
    parent: list[int] = []
    size: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if size[ra] < size[rb]:
                ra, rb = rb, ra
            parent[rb] = ra
            size[ra] += size[rb]

    prev: list[tuple[int, int, int]] = []      # (start, end_exclusive, label)
    h, _w = m.shape
    for y in range(h):
        row = m[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts = np.flatnonzero(d == 1)
        ends = np.flatnonzero(d == -1)
        cur = []
        j = 0
        for s, e in zip(starts.tolist(), ends.tolist()):
            lbl = len(parent)
            parent.append(lbl)
            size.append(e - s)
            while j < len(prev) and prev[j][1] <= s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] < e:
                union(lbl, prev[k][2])
                k += 1
            cur.append((s, e, lbl))
        prev = cur

    if not parent:
        return []
    roots = {find(i) for i in range(len(parent))}
    return sorted((size[r] for r in roots if size[r] >= min_px), reverse=True)


def label(m):
    """(labelled int array, {label: size}) using the same run union-find."""
    import numpy as np
    parent: list[int] = []
    size: list[int] = []

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            if size[ra] < size[rb]:
                ra, rb = rb, ra
            parent[rb] = ra
            size[ra] += size[rb]

    out = np.zeros(m.shape, dtype=np.int32)
    runs: list[list[tuple[int, int, int]]] = []
    prev: list[tuple[int, int, int]] = []
    for y in range(m.shape[0]):
        row = m[y]
        if not row.any():
            runs.append([])
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        cur = []
        j = 0
        for s, e in zip(np.flatnonzero(d == 1).tolist(),
                        np.flatnonzero(d == -1).tolist()):
            lbl = len(parent)
            parent.append(lbl)
            size.append(e - s)
            while j < len(prev) and prev[j][1] <= s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] < e:
                union(lbl, prev[k][2])
                k += 1
            cur.append((s, e, lbl))
        runs.append(cur)
        prev = cur
    for y, cur in enumerate(runs):
        for s, e, lbl in cur:
            out[y, s:e] = find(lbl)
    sizes = {int(r): int(size[r]) for r in {find(i) for i in range(len(parent))}}
    return out, sizes


def largest_component(m):
    """Boolean mask of the single biggest 4-connected region in ``m``."""
    import numpy as np
    lab, sizes = label(m)
    if not sizes:
        return np.zeros_like(m)
    return lab == max(sizes, key=sizes.get)


def centroid(m):
    import numpy as np
    ys, xs = np.nonzero(m)
    if not len(xs):
        return (float("nan"), float("nan"))
    return (float(xs.mean()), float(ys.mean()))


def bbox(m):
    import numpy as np
    ys, xs = np.nonzero(m)
    if not len(xs):
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


def copper_islands(copper_png, min_frac: float = COPPER_ISLAND_MIN_FRAC):
    """(number of big copper islands, largest island's share of all copper).

    The strongest measurement in the visual lane: categorical, **1** on every one
    of 42 good faces and **3** / **2** on a severed board, with zero variance over
    five repeats of the same render. ``min_frac`` drops isolated pads, which are
    legitimately their own islands.
    """
    cu = has_copper(classify(copper_png)[0])
    sizes = components(cu, min_px=int(min_frac * cu.size))
    if not sizes:
        return 0, 0.0
    return len(sizes), sizes[0] / sum(sizes)


def assert_pour_is_one_island(copper_png, net_layer: str,
                              min_frac: float = COPPER_ISLAND_MIN_FRAC) -> None:
    """The rendered copper on this face is a single connected plane."""
    n, share = copper_islands(copper_png, min_frac)
    assert n == 1, (
        f"{net_layer} pour split into {n} islands (expected 1; largest holds "
        f"{share:.1%} of the copper) — every LED on a severed island is wired to "
        "nothing and will never light")


def silk_on_opening(materials_png, materials_clipped_png) -> float:
    """Fraction of the board carrying silkscreen ink that lands on a mask opening.

    Measured as authored-silk-area minus mask-clipped-silk-area, from two renders
    that differ ONLY in ``subtract_mask_from_silk``. Everything else — camera,
    lighting, geometry, anti-aliasing — is identical, so the difference is signal,
    not noise. Gate at :data:`SILK_ON_OPENING_MAX`.
    """
    a = classify(materials_png)[0]
    b = classify(materials_clipped_png)[0]
    on = onboard(a)
    return frac(mask_of(a, "silk"), on) - frac(mask_of(b, "silk"), on)


def assert_silk_stays_off_openings(materials_png, materials_clipped_png,
                                   limit: float = SILK_ON_OPENING_MAX) -> None:
    v = silk_on_opening(materials_png, materials_clipped_png)
    assert v <= limit, (
        f"{v:.2e} of the board is silkscreen printed onto a mask opening "
        f"(limit {limit:.0e}) — ink on bare copper does not adhere and the "
        "legend rubs off, or the pad will not take solder")


def assert_board_is_one_piece(board_png) -> None:
    """The board silhouette is a single connected shape."""
    on = onboard(classify(board_png)[0])
    n = len(components(on, min_px=int(0.001 * on.size)))
    assert n == 1, (
        f"the board silhouette is {n} piece(s), expected 1 — the outline did not "
        "close, or a cut-out ate the board and it falls apart on the panel")


def frame_mm(board_png, side, board_mm):
    """A px -> board-mm converter derived from the board silhouette bbox.

    ``board_mm`` is the outline's ``(x0, y0, x1, y1)`` in board millimetres, which
    the test already knows because it wrote the spec. ``kicad-cli pcb render
    --side bottom`` mirrors the bottom view in x, so it is un-mirrored here.
    Accurate to ~0.13 mm at 600 px; gate placement at
    :data:`PLACEMENT_TOL_MM`.
    """
    on = onboard(classify(board_png)[0])
    x0, y0, x1, y1 = bbox(on)
    bx0, by0, bx1, by1 = board_mm

    def conv(px, py):
        u = (px - x0) / (x1 - x0)
        v = (py - y0) / (y1 - y0)
        if side == "bottom":
            u = 1.0 - u
        return bx0 + u * (bx1 - bx0), by0 + v * (by1 - by0)
    return conv
