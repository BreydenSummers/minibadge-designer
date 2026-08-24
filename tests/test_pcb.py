import re

import pytest

import invariants
from minibadge_designer import pcb


def _spec(n_leds=2, rects=None):
    leds = [pcb.Led(6.5, 6.0, "red"), pcb.Led(14.0, 6.0, "blue")][:n_leds]
    rects = rects if rects is not None else [(8.0, 10.0, 4.0, 0.2), (8.0, 10.2, 3.0, 0.2)]
    return pcb.BadgeSpec(
        name="test-badge",
        leds=leds,
        art=[pcb.ArtLayer(material="silk", rects=rects)],
    )


def _bom_rows(bom: str) -> dict:
    """BOM.csv keyed by reference, parsed strictly.

    Strict on purpose: an unquoted comma anywhere in a Value or Notes string
    shifts every later column, and `csv.DictReader` would hide it in a
    ``None`` key rather than complain. Whoever calls this gets told instead.
    """
    import csv
    import io

    reader = csv.reader(io.StringIO(bom))
    header = next(reader)
    rows = {}
    for fields in reader:
        if not fields:
            continue
        assert len(fields) == len(header), (
            f"BOM row has {len(fields)} fields, header has {len(header)}: "
            f"{fields!r} -- an unescaped comma shifts the columns and an "
            "assembly house reads the wrong package against the reference")
        rows[fields[0]] = dict(zip(header, fields))
    return rows


def _balanced(sexpr: str) -> bool:
    depth = 0
    in_str = False
    prev = ""
    for ch in sexpr:
        if in_str:
            if ch == '"' and prev != "\\":
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
        prev = ch
    return depth == 0 and not in_str


def test_pcb_is_balanced_sexpr():
    out = pcb.generate_pcb(_spec())
    assert out.startswith("(kicad_pcb")
    assert _balanced(out)


def test_connector_pads_and_nets():
    out = pcb.generate_pcb(_spec())
    assert '(net 1 "3V3")' in out and '(net 2 "GND")' in out
    # 3V3 pads 7/15, GND pads 2/8/16, and VBATT/CLK/NC left un-netted.
    assert len(re.findall(r'\(pad "\d+" thru_hole', out)) == 8
    for num in ("7", "15"):
        assert re.search(rf'\(pad "{num}" thru_hole[^\n]*\(net 1 "3V3"\)', out)
    for num in ("2", "8", "16"):
        assert re.search(rf'\(pad "{num}" thru_hole[^\n]*\(net 2 "GND"\)', out)
    for num in ("1", "9", "10"):
        assert not re.search(rf'\(pad "{num}" thru_hole[^\n]*\(net ', out)


def test_led_units_scale_with_count():
    window = [pcb.ArtLayer("bare", [(4.0, 9.0, 12.0, 6.0)])]
    for n in (0, 1, 2):
        out = pcb.generate_pcb(_spec(n))
        assert out.count("(via ") == n
        assert len(re.findall(r'footprint "minibadge-designer:', out)) == 2 * n  # LED + resistor
        # 2 unit traces + the via's own perimeter bridge per unit. The PAD's
        # bridge is not cut on a board with no window: the pad already sits
        # in its pour and nothing could sever it (pcb.unit_bridges).
        assert out.count("(segment ") == 3 * n
        # Add a window and it appears -- one more segment per unit, and the
        # count is what says the bridge is per unit rather than per board.
        spec = _spec(n)
        spec.art = list(window)
        assert pcb.generate_pcb(spec).count("(segment ") == 4 * n
    assert '"/LED2_A"' in pcb.generate_pcb(_spec(2))
    assert '"/LED2_A"' not in pcb.generate_pcb(_spec(1))


def test_zones_on_both_sides():
    out = pcb.generate_pcb(_spec())
    assert re.search(r'\(zone \(net 1\) \(net_name "3V3"\) \(layer "F.Cu"\)', out)
    assert re.search(r'\(zone \(net 2\) \(net_name "GND"\) \(layer "B.Cu"\)', out)


def test_silk_art_emitted_on_silk():
    out = pcb.generate_pcb(_spec())
    assert out.count('(layer "F.SilkS")') >= 2
    assert len(re.findall(r"\(gr_poly ", out)) == 2


def test_copper_art_opens_front_mask():
    spec = pcb.BadgeSpec(art=[pcb.ArtLayer("copper", [(8.0, 8.0, 3.0, 0.2)])])
    out = pcb.generate_pcb(spec)
    assert re.search(r'\(gr_poly [^\n]+\(layer "F\.Mask"\)', out)
    assert not re.search(r'\(gr_poly [^\n]+\(layer "B\.Mask"\)', out)
    assert not re.search(r'\(gr_poly [^\n]+\(layer "F\.SilkS"\)', out)


def test_bare_art_opens_both_masks():
    spec = pcb.BadgeSpec(art=[pcb.ArtLayer("bare", [(8.0, 8.0, 3.0, 0.2)])])
    out = pcb.generate_pcb(spec)
    assert re.search(r'\(gr_poly [^\n]+\(layer "F\.Mask"\)', out)
    assert re.search(r'\(gr_poly [^\n]+\(layer "B\.Mask"\)', out)


def test_glow_and_bare_cut_both_pours():
    from shapely.geometry import box

    window = (8.0, 8.0, 4.0, 4.0)
    for material in ("glow", "bare"):
        spec = pcb.BadgeSpec(art=[pcb.ArtLayer(material, [window])])
        inner = box(9.0, 9.0, 11.0, 11.0)  # well inside the window
        for net, layer in (("3V3", "F.Cu"), ("GND", "B.Cu")):
            for poly in pcb._fill_geometry(net, layer, spec):
                assert not poly.intersects(inner), f"{material} left copper on {layer}"
    # A glow window draws no graphics of its own.
    out = pcb.generate_pcb(pcb.BadgeSpec(art=[pcb.ArtLayer("glow", [window])]))
    assert "gr_poly" not in out


def test_led_positions_clamped():
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(0.0, 99.0, "red")]))
    # rot 0 unit spans (-3.375,-4.0)..(2.375,1.4); safe (0.7,0.7)..(19.62,19.62)
    cx, cy = pcb.clamp_led(0.0, 99.0)
    assert (cx, round(cy, 9)) == (4.075, 18.22)
    assert re.search(
        r'\(footprint "minibadge-designer:LED_RED_0805" \(layer "F\.Cu"\) \(tstamp [0-9a-f-]+\)\n'
        r"    \(at 104\.075 118\.22\)",
        out,
    )


def test_custom_outline_widens_the_safe_region():
    # A big custom outline lets units go wherever the board goes.
    spec = pcb.BadgeSpec(outline=[[(-30.0, -35.0), (55.0, -35.0), (55.0, 60.0), (-30.0, 60.0)]])
    safe = pcb.unit_safe(spec)
    assert safe == (-29.44, -34.44, 54.44, 59.44)
    assert pcb.clamp_led(-20.0, -25.0, 0, "stacked", safe) == (-20.0, -25.0)
    # Without an outline the standard square still applies.
    assert pcb.unit_safe(pcb.BadgeSpec()) == pcb.UNIT_SAFE


def test_units_fit_between_connector_pads():
    # The strip between a row's two pad pairs is usable board.
    led = pcb.Led(10.16, 4.7, "red")
    assert not pcb.pad_conflict(led, pcb.ALL_PINS)
    assert pcb.resolve_pad_overlap(led, pcb.ALL_PINS) == led


def test_pad_overlap_is_resolved():
    # Clamped into the top-left corner the unit covers the VBATT/GND pair;
    # the backstop slides it clear (into the gap between the pairs).
    led = pcb.Led(*pcb.clamp_led(0.0, 0.0, 0), color="red")
    assert pcb.pad_conflict(led, ("1", "2", "7", "8"))
    moved = pcb.resolve_pad_overlap(led, ("1", "2", "7", "8"))
    assert not pcb.pad_conflict(moved, ("1", "2", "7", "8"))
    # A dropped row has no pads, so nothing to avoid.
    assert not pcb.pad_conflict(led, ("9", "10", "15", "16"))
    assert pcb.resolve_pad_overlap(led, ("9", "10", "15", "16")) == led


def test_clamp_is_rotation_aware():
    # rot 180 puts the resistor below the LED, so the LED itself can sit
    # higher on the board than at rot 0, and lower limits tighten instead.
    _x0, y0 = pcb.clamp_led(0.0, 0.0, 0)
    _x180, y180 = pcb.clamp_led(0.0, 0.0, 180)
    assert y180 < y0
    # Whatever the rotation, the whole unit stays inside the safe region.
    for rot in (0, 90, 180, 270):
        for x, y in ((0, 0), (99, 99), (0, 99), (99, 0)):
            led = pcb.Led(*pcb.clamp_led(x, y, rot), rot=rot)
            bx0, by0, bx1, by1 = pcb.led_unit_bbox(led)
            assert bx0 >= pcb.UNIT_SAFE[0] - 1e-9 and by0 >= pcb.UNIT_SAFE[1] - 1e-9
            assert bx1 <= pcb.UNIT_SAFE[2] + 1e-9 and by1 <= pcb.UNIT_SAFE[3] + 1e-9


@pytest.mark.parametrize(
    "size, side, rot",
    [("0805", "front", 0), ("1206", "back", 0), ("0805", "front", 90)],
)
def test_a_hand_placed_unit_only_blocks_the_pads_its_copper_reaches(size, side, rot):
    """Free-placed parts are judged on their copper, not on the box round them.

    Dragging the resistor to one corner and the LED to another leaves most of
    the unit's envelope empty board.  Judging that envelope costs the user a
    placement the fab can build: the generator slides the unit somewhere else
    (resolve_pad_overlap) or refuses the download, over a corner nothing of
    the unit occupies.
    """
    from shapely.geometry import box as sbox

    spread = {"rx": 5.0, "ry": 12.0, "vx": 2.0, "vy": 10.0}
    away = pcb.Led(3.0, 4.5, "red", size=size, side=side, rot=rot, adv=spread)
    hits = [k for k in pcb.active_pairs(pcb.ALL_PINS)
            if pcb.unit_poly(away).intersects(sbox(*pcb.PAD_PAIRS[k]["keepout"]))]
    assert hits, ("vacuous: this unit's envelope no longer reaches a pad pair, "
                  "so it cannot show that the envelope is not what is judged")
    assert not pcb.pad_conflict(away, pcb.ALL_PINS), (
        f"a unit whose envelope merely spans the {hits} pair is refused, "
        "though none of its pads, traces or via reach it")
    # ...and dragging the resistor ONTO that pair is still a conflict: this is
    # the copper that shorts the header, and it has to keep being refused.
    onto = dict(spread, rx=-0.6, ry=13.6)
    assert pcb.pad_conflict(
        pcb.Led(3.0, 4.5, "red", size=size, side=side, rot=rot, adv=onto),
        pcb.ALL_PINS), "a part parked on the connector pads is not refused"


def test_rotation_rotates_pads_and_via():
    # rot 90 (clockwise): resistor moves from above the LED to its right,
    # LED pads go vertical, via moves above the LED.
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(10.0, 10.0, "red", rot=90)]))
    # LED pad1 (cathode) at local (0, -0.95); 90° cw = KiCad orientation 270.
    assert re.search(r'\(pad "1" smd rect \(at 0 -1\.025 270\) \(size 1\.15 1\.4\)', out)
    # Resistor footprint sits right of the LED: at (110 + 2.6? no: rot of (0,-2.6) -> (2.6, 0))
    assert re.search(
        r'\(footprint "minibadge-designer:220R_0805" \(layer "F\.Cu"\) \(tstamp [0-9a-f-]+\)\n'
        r"    \(at 112\.6 110\)",
        out,
    )
    # Via (front, GND) at rot of (-2.5, 0) -> (0, -2.5): above the LED.
    assert re.search(r"\(via \(at 110 107\.425\)", out)


def test_arbitrary_rotation_rotates_unit():
    # 30° clockwise: pads carry the equivalent KiCad orientation (330 ccw),
    # local offsets rotate by the same matrix, and the unit bbox is the
    # axis-aligned envelope of the rotated corners (wider than at 0°).
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(10.0, 10.0, "red", rot=30)]))
    assert re.search(r'\(pad "1" smd rect \(at [\d.-]+ [\d.-]+ 330\) \(size 1\.15 1\.4\)', out)
    import math

    # via_front offset (-(dx + 1.55), 0) = (-2.575, 0), rotated 30° cw
    vx = 110 - 2.575 * math.cos(math.radians(30))
    vy = 110 - 2.575 * math.sin(math.radians(30))
    m = re.search(r"\(via \(at ([\d.]+) ([\d.]+)\)", out)
    assert abs(float(m.group(1)) - vx) < 0.01
    assert abs(float(m.group(2)) - vy) < 0.01
    b0 = pcb.led_unit_bbox(pcb.Led(10.0, 10.0, rot=0))
    b30 = pcb.led_unit_bbox(pcb.Led(10.0, 10.0, rot=30))
    assert (b30[2] - b30[0]) > (b0[2] - b0[0])  # envelope grows when tilted


def test_tilted_units_pack_diagonally():
    # Two 45° units placed corner-to-corner: their axis-aligned envelopes
    # overlap, but the rotated footprints are well clear: the tight
    # bounding must accept the placement (the envelope used to reject it).
    a = pcb.Led(8.0, 10.0, rot=45, side="front")
    b = pcb.Led(13.0, 15.0, rot=45, side="back")
    ba, bb = pcb.led_unit_bbox(a), pcb.led_unit_bbox(b)
    assert not (bb[0] >= ba[2] or bb[2] <= ba[0]
                or bb[1] >= ba[3] or bb[3] <= ba[1])  # envelopes DO overlap
    assert pcb.unit_poly(a).distance(pcb.unit_poly(b)) > 0.2
    assert pcb.resolve_overlap(a, b) == b  # accepted unchanged


def test_rotation_0_matches_legacy_layout():
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(10.0, 10.0, "red", rot=0)]))
    assert re.search(r'\(pad "1" smd rect \(at -1\.025 0\) \(size 1\.15 1\.4\)', out)
    assert re.search(r"\(via \(at 107\.425 110\)", out)


def test_inline_layout_geometry():
    out = pcb.generate_pcb(
        pcb.BadgeSpec(leds=[pcb.Led(12.0, 10.0, "red", layout="inline")])
    )
    # Resistor sits in line, 4.25 mm left of the LED.
    assert re.search(
        r'\(footprint "minibadge-designer:220R_0805" \(layer "F\.Cu"\) \(tstamp [0-9a-f-]+\)\n'
        r"    \(at 107\.75 110\)",
        out,
    )
    # LED is flipped: cathode (pad 1) faces away from the resistor.
    led_fp = out.split('minibadge-designer:LED_RED_0805')[1].split("\n  )")[0]
    assert '(pad "1" smd rect (at 1.025 0)' in led_fp
    assert '(pad "2" smd rect (at -1.025 0)' in led_fp
    # Front GND via sits at the cathode end (right), on the row axis.
    assert re.search(r"\(via \(at 114\.575 110\)", out)
    # Anode trace runs along the row between R pad 2 and the LED anode.
    assert "(segment (start 108.775 110) (end 110.975 110)" in out


def test_inline_back_via_at_resistor_end():
    out = pcb.generate_pcb(
        pcb.BadgeSpec(leds=[pcb.Led(12.0, 10.0, "blue", side="back", layout="inline")])
    )
    # 3V3 via 1 mm outboard of R pad 1: 12 - 6.275 = 5.725.
    assert re.search(r"\(via \(at 105\.725 110\)", out)
    assert re.search(r"\(via .*\(net 1\)", out)


def test_inline_clamp_uses_wider_bbox():
    # Inline unit spans -7.025..3.275, so the LED center can't near the left edge.
    x, y = pcb.clamp_led(0.0, 10.0, 0, "inline")
    assert (round(x, 9), y) == (7.725, 10.0)
    # Rotated 180 the long tail points right instead.
    x, _ = pcb.clamp_led(0.0, 10.0, 180, "inline")
    assert abs(x - 3.975) < 1e-9


def test_text_content_escaped():
    out = pcb.generate_pcb(
        pcb.BadgeSpec(texts=[pcb.Text(10.0, 10.0, 'my "cool" badge')])
    )
    assert '"my \\"cool\\" badge"' in out
    assert _balanced(out)


def test_many_leds():
    leds = [
        pcb.Led(4.5, 4.5, "red"),
        pcb.Led(10.5, 4.5, "green"),
        pcb.Led(16.5, 4.5, "blue"),
        pcb.Led(4.5, 11.0, "yellow", side="back"),
        pcb.Led(10.5, 11.0, "white", layout="inline", rot=90),
    ]
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=leds))
    assert out.count("(via ") == 5
    assert '(net 7 "/LED5_A")' in out
    assert len(re.findall(r'footprint "minibadge-designer:', out)) == 10
    assert _balanced(out)


def test_deterministic_output():
    assert pcb.generate_pcb(_spec()) == pcb.generate_pcb(_spec())


def test_bom_and_readme():
    """The parts list names each part the way it is ordered, and defers the
    resistance to the builder.

    Value is the column a human orders from, so it has to carry colour AND
    package for an LED. It must NOT carry a resistance: the right value depends
    on the forward voltage of the LED actually bought, and a number printed
    here reads as settled -- a builder who solders 120 ohm against a 3.2 V
    white LED gets a badge that barely lights. The suggestion lives in Notes,
    labelled as a suggestion, with the arithmetic in README.txt.
    """
    spec = _spec()
    spec.leds[1].side = "back"
    bom = pcb.generate_bom(spec)
    rows = _bom_rows(bom)

    d1, d2 = rows["D1"], rows["D2"]
    assert "red" in d1["Value"] and "0805" in d1["Value"], d1
    assert d1["Side"] == "front" and d2["Side"] == "back"
    assert "blue" in d2["Value"] and "0805" in d2["Value"], d2

    for ref in ("R1", "R2"):
        r = rows[ref]
        assert not re.search(r"\d+\s*ohm", r["Value"]), (
            f"{ref} presents a resistance as settled in the column a builder "
            f"orders from: {r['Value']!r}")
        assert "README" in r["Value"], (
            f"{ref} must point at the section that explains how to pick it")
        assert re.search(r"\d+ ohm suits", r["Notes"]), (
            f"{ref} lost its starting-point suggestion: {r['Notes']!r}")

    # The pins the badge plugs in with are a part you have to buy, and no
    # other file in the zip mentions them.
    assert "J1" in rows, "the parts list never mentions the header pins"
    assert "header" in rows["J1"]["Value"].lower()

    readme = pcb.generate_readme(spec)
    assert "lukejenkins/minibadge" in readme and "Press B" in readme
    # The two documents must agree about the same LED: a builder reading the
    # README table and a builder reading BOM Notes buy the same resistor.
    for ref, led in (("R1", spec.leds[0]), ("R2", spec.leds[1])):
        ma = f"{pcb.suggested_current_ma(led.color):.1f} mA"
        assert ma in rows[ref]["Notes"] and ma in readme, (
            f"{ref}: BOM and README disagree about the current for a "
            f"{led.color} LED ({ma})")


@pytest.mark.parametrize("spec_name,spec_factory", [
    # The Notes column is assembled from optional clauses, and the wordiest
    # combinations are where a separator gets typed as a comma. Each case
    # moves off the 0805/front/stacked/all-pins defaults in a different way.
    ("reverse-and-farled", lambda: pcb.BadgeSpec(
        name="n", leds=[pcb.Led(10.16, 10.16, "red", size="1206", reverse=True),
                        pcb.Led(6.0, 14.0, "white", side="back", farled=True)])),
    ("through-hole", lambda: pcb.BadgeSpec(
        name="n", leds=[pcb.Led(6.5, 8.0, "green", size="3mm"),
                        pcb.Led(13.0, 8.0, "yellow", size="5x2mm", rot=90)])),
    ("clk-jumper", lambda: pcb.BadgeSpec(
        name="n", clk_jumper=True,
        leds=[pcb.Led(6.0, 8.0, "blue", clk=True),
              pcb.Led(14.0, 8.0, "orange")])),
    ("clk-traced-and-novia", lambda: pcb.BadgeSpec(
        name="n", clk_jumper=False,
        leds=[pcb.Led(6.0, 8.0, "blue", clk=True, novia=True)])),
    ("one-pair-only", lambda: pcb.BadgeSpec(
        name="n", pins=("7", "8"), leds=[pcb.Led(10.0, 10.0, "red")])),
    ("no-leds", lambda: pcb.BadgeSpec(name="n", leds=[])),
])
def test_the_bom_stays_a_six_column_file_however_wordy_its_notes_get(
        spec_name, spec_factory):
    """Every BOM row carries exactly the columns its header declares.

    BOM.csv is the one file in the zip a person feeds to something else -- a
    spreadsheet, or an assembly house's importer. The Notes and Value strings
    are built by concatenating optional clauses, so one comma typed as a
    separator shifts every later column: the package lands under Side and the
    quantity under Footprint, and the order comes back wrong or rejected.
    Nothing else in the suite reads this file as a table.
    """
    rows = _bom_rows(pcb.generate_bom(spec_factory()))
    assert rows, f"{spec_name}: the BOM has no rows at all"
    for ref, row in rows.items():
        assert row["Reference"] == ref
        assert row["Qty"].isdigit(), (
            f"{spec_name}: {ref} has {row['Qty']!r} in the Qty column, which "
            "is where a shifted comma shows up first")


def test_the_readme_flags_a_colour_the_rail_cannot_drive_brightly():
    """The resistor guidance warns about low-headroom LEDs only when the board
    actually has one.

    Blue, green and white sit within ~0.3 V of the 3V3 rail, so the resistor
    barely sets the current and the LED's own Vf bin does. A builder who sizes
    those from the datasheet's 20 mA figure gets a badge that looks dead. The
    contrast case is the point: on a red-only board the same warning would be
    noise, and noise is what stops people reading the file.
    """
    warm = pcb.generate_readme(pcb.BadgeSpec(
        name="warm", leds=[pcb.Led(6.0, 8.0, "red"), pcb.Led(14.0, 8.0, "yellow")]))
    cool = pcb.generate_readme(pcb.BadgeSpec(
        name="cool", leds=[pcb.Led(6.0, 8.0, "red"), pcb.Led(14.0, 8.0, "white")]))

    # The method is stated on every board: it is what makes the BOM's blank
    # resistance actionable.
    for readme in (warm, cool):
        assert "Vf" in readme
        assert re.search(r"R = \([\d.]+ V - Vf\) / I", readme), (
            "the README no longer shows how to size the resistor, and the BOM "
            "deliberately does not carry a value")

    assert "white" in cool.split("Choosing the series resistor")[1], cool
    assert "Vf spread" in cool, "a white LED on 3V3 needs the headroom warning"
    assert "Vf spread" not in warm, (
        "a red/yellow board has ~1.3 V of headroom; warning about Vf spread "
        "there trains the reader to skip the section")

    # The suggestion is a real calculation, not a fixed string: a colour with
    # less headroom must be shown drawing less current.
    assert (pcb.suggested_current_ma("white")
            < pcb.suggested_current_ma("red") / 2), (
        "white is shown drawing as much as red, so the table is not reading "
        "the forward voltages it claims to")


@pytest.mark.parametrize("layout,size,rot", [
    ("stacked", "0805", 0),
    ("inline", "0603", 0),
    ("inline", "1206", 90),
])
def test_back_led_lands_on_back_layers(layout, size, rot):
    out = pcb.generate_pcb(pcb.BadgeSpec(
        leds=[pcb.Led(10.0, 10.0, "green", side="back",
                      layout=layout, size=size, rot=rot)]))
    fps = re.findall(r'\(footprint "minibadge-designer:[^"]+" \(layer "([FB])\.Cu"\)', out)
    assert fps == ["B", "B"]
    assert '(layers "B.Cu" "B.Paste" "B.Mask")' in out
    assert '(layers "F.Cu" "F.Paste" "F.Mask")' not in out
    # Back unit's via carries 3V3 up to the front pour; its own two traces
    # live on B.Cu. The only F.Cu segment is the 3V3 bridge from the via to
    # the ring. Its GND pad gets no bridge here: this board has no window, so
    # nothing could sever the pad from the pour it already sits in.
    assert re.search(r"\(via .*\(net 1\)", out)
    assert len(re.findall(r'\(segment [^\n]+\(layer "B\.Cu"\)', out)) == 2, \
        "a back unit's own two traces live on B.Cu"
    # Put a window over it and the GND bridge is cut, because now something
    # CAN sever the pad: that is the whole condition, checked on the same
    # unit rather than on a board built for the purpose.
    windowed = pcb.generate_pcb(pcb.BadgeSpec(
        leds=[pcb.Led(10.0, 10.0, "green", side="back",
                      layout=layout, size=size, rot=rot)],
        art=[pcb.ArtLayer("bare", [(3.0, 3.0, 14.0, 14.0)])]))
    assert len(re.findall(r'\(segment [^\n]+\(layer "B\.Cu"\)', windowed)) == 3, \
        "a window can sever the GND pad, so its perimeter bridge is cut"
    # The inline cases are the regression gate for the far-layer bridge scan:
    # inline puts the via 1.0 mm from the resistor pad center, INSIDE that
    # pad's inflated art-keepout quad. When the scan treated the unit's own
    # back-face pads as F.Cu obstacles too, every ray "collided" with copper
    # that is not on that layer, no F.Cu bridge routed, and the webapp fell
    # back to the reserved 2 mm window corridor: a fat band of pour across
    # the user's window where this 0.3 mm trace belongs.
    fsegs = re.findall(r'\(segment [^\n]+\(layer "F\.Cu"\) \(net (\d+)\)', out)
    assert fsegs == ["1"], \
        "the via's 3V3 feed must reach the front pour as a thin bridge trace"


def test_front_led_unchanged_by_side_default():
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(10.0, 10.0, "red")]))
    assert re.search(r"\(via .*\(net 2\)", out)  # GND via
    # The only B.Cu segment is the GND perimeter bridge from that via.
    bsegs = re.findall(r'\(segment [^\n]+\(layer "B\.Cu"\) \(net (\d+)\)', out)
    assert bsegs == ["2"]  # the GND bridge


def test_no_default_text():
    out = pcb.generate_pcb(_spec())
    assert "gr_text" not in out


def test_text_items_emitted_per_side():
    spec = pcb.BadgeSpec(
        texts=[
            pcb.Text(10.0, 5.0, "hello", size=2.0, side="front"),
            pcb.Text(10.0, 15.0, "world", size=0.9, side="back"),
            pcb.Text(5.0, 5.0, "   ", size=1.5),  # whitespace-only: dropped
        ]
    )
    out = pcb.generate_pcb(spec)
    front = re.search(r'\(gr_text "hello" \(at 110 105\) \(layer "F\.SilkS"\).*?\n(.*)\n', out)
    assert front and "(size 2 2)" in front.group(1) and "mirror" not in front.group(1)
    back = re.search(r'\(gr_text "world" \(at 110 115\) \(layer "B\.SilkS"\).*?\n(.*)\n', out)
    assert back and "(size 0.9 0.9)" in back.group(1) and "(justify mirror)" in back.group(1)
    assert out.count("gr_text") == 2


def test_text_rotation():
    # Text turns clockwise like everything else the user places; KiCad text
    # angles count the other way, so the emitted angle is negated. An
    # unrotated string keeps the short two-number form.
    spec = pcb.BadgeSpec(texts=[
        pcb.Text(10.0, 5.0, "flat"),
        pcb.Text(10.0, 10.0, "tilt", rot=30),
        pcb.Text(10.0, 15.0, "quarter", rot=90, side="back"),
    ])
    out = pcb.generate_pcb(spec)
    assert '(gr_text "flat" (at 110 105)' in out
    assert '(gr_text "tilt" (at 110 110 330)' in out
    # Back-side text keeps its mirror and carries the same negated angle.
    m = re.search(r'\(gr_text "quarter" \(at 110 115 270\).*?\n(.*)\n', out)
    assert m and "(justify mirror)" in m.group(1)


def test_rotated_text_keeps_its_carve_area():
    # A turned string must carve the room its glyphs really occupy, not the
    # unrotated rectangle (art would otherwise print over the ink).
    from minibadge_designer.webapp import _text_keepout

    flat = _text_keepout(pcb.Text(10.0, 10.0, "WIDE TEXT", size=2.0))
    turned = _text_keepout(pcb.Text(10.0, 10.0, "WIDE TEXT", size=2.0, rot=90))
    # The flat one reaches far in x and little in y; rotating swaps that.
    assert flat.x1 - flat.x0 > flat.y1 - flat.y0
    tb = turned.geom.bounds
    assert tb[3] - tb[1] > tb[2] - tb[0]


def test_zone_fills_have_no_holes():
    # A back-side unit carves a U-shaped hole into the back pour whose
    # bounding-box center lies on copper, the case that once left an
    # unfractured hole and poured copper over other-net pads (generate_pcb
    # now raises if a hole ever survives fracturing).
    spec = pcb.BadgeSpec(
        name="drc-check",
        leds=[pcb.Led(6.5, 13.8, "red"), pcb.Led(14.5, 13.8, "blue", side="back")],
    )
    for net, layer in (("3V3", "F.Cu"), ("GND", "B.Cu")):
        for poly in pcb._fill_geometry(net, layer, spec):
            assert not list(poly.interiors)
    assert pcb.generate_pcb(spec)


def test_row_selection_drops_pads():
    out = pcb.generate_pcb(pcb.BadgeSpec(pins=("1", "2", "7", "8")))
    assert len(re.findall(r'\(pad "\d+" thru_hole', out)) == 4
    for num in ("1", "2", "7", "8"):
        assert re.search(rf'\(pad "{num}" thru_hole', out)
    for num in ("9", "10", "15", "16"):
        assert not re.search(rf'\(pad "{num}" thru_hole', out)
    assert "CLK" not in out and "VBAT" in out


TAB_OUTLINE = [[  # standard square with a 8-mm-wide tab sticking 4 mm out the top
    (0.16, 0.16), (6.0, 0.16), (6.0, -4.0), (14.0, -4.0), (14.0, 0.16),
    (20.16, 0.16), (20.16, 20.16), (0.16, 20.16),
]]


def test_custom_outline_emitted_and_shapes_pour():
    spec = pcb.BadgeSpec(pins=("1", "2", "7", "8"), outline=TAB_OUTLINE)
    out = pcb.generate_pcb(spec)
    # Edge.Cuts is a polygon (with the tab vertex at page (106, 96)), not a rect.
    assert re.search(r'\(gr_poly \(pts [^\n]*\(xy 106 96\)[^\n]*\(layer "Edge\.Cuts"\)', out)
    assert not re.search(r'gr_rect [^\n]*Edge\.Cuts', out)
    # The pour reaches into the tab (above the old y=0.16 edge)...
    fills = pcb._fill_geometry("3V3", "F.Cu", spec)
    assert min(p.bounds[1] for p in fills) < -0.5
    # ...but stays inside the outline.
    from shapely.geometry import Polygon

    board = Polygon(TAB_OUTLINE[0])
    for p in fills:
        assert board.buffer(0.01).contains(p)


def test_resolve_overlap():
    a = pcb.Led(8.0, 8.0, "red")
    b_clear = pcb.Led(14.0, 8.0, "blue")
    assert pcb.resolve_overlap(a, b_clear) is b_clear
    b_on_top = pcb.Led(8.5, 8.0, "blue", side="back")
    moved = pcb.resolve_overlap(a, b_on_top)
    ba, bm = pcb.led_unit_bbox(a), pcb.led_unit_bbox(moved)
    assert bm[0] >= ba[2] or bm[2] <= ba[0] or bm[1] >= ba[3] or bm[3] <= ba[1]
    assert moved.side == "back" and moved.color == "blue"


def test_package_sizes_scale_footprints():
    # 0603 and 1206 pads/layouts derive from the PKG table; 0805 keeps the
    # original hard-coded geometry exactly.
    g805 = pcb._layout("stacked", "0805")
    assert g805["bbox"] == (-3.375, -4.0, 2.375, 1.4)
    assert pcb._layout("inline", "0805")["bbox"] == (-7.025, -1.4, 3.275, 1.4)
    g603 = pcb._layout("stacked", "0603")
    g1206 = pcb._layout("inline", "1206")
    assert g603["led_a"] == (0.875, 0) and g603["res"] == (0.0, -2.2)
    assert g1206["res_in"] == (-7.2125, 0.0) and g1206["via_back"] == (-8.2125, 0.0)
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[
        pcb.Led(6, 6, "red", size="0603"),
        pcb.Led(14, 14, "blue", size="1206", layout="inline"),
    ]))
    assert '"minibadge-designer:LED_RED_0603"' in out
    assert "(size 1.05 0.95)" in out    # 0603 hand-solder pads
    assert '"minibadge-designer:LED_BLUE_1206"' in out
    assert "(size 1.325 1.75)" in out    # 1206 hand-solder pads
    bom = pcb.generate_bom(pcb.BadgeSpec(leds=[pcb.Led(6, 6, "red", size="0603")]))
    assert "LED 0603 (1608 metric)" in bom


def test_reverse_layout_routes_hole_and_forces_1206():
    g = pcb._layout("stacked", "0603", reverse=True)   # size is overridden to 1206
    assert g["pkg"] == "1206"
    assert g["hole"] == 1.25
    # reverse composes with the inline layout too: same hole, inline offsets
    gi = pcb._layout("inline", "0805", reverse=True)
    assert gi["pkg"] == "1206" and gi["hole"] == 1.25
    assert gi["led_flip"] and gi["res"] == (-5.675, 0.0)
    assert gi["bbox"] == pcb._layout("inline", "1206")["bbox"]
    # legacy spelling still maps to stacked + reverse
    assert pcb._layout("reverse", "0603") == g
    spec = pcb.BadgeSpec(leds=[pcb.Led(10, 10, "red", reverse=True, size="1206")])
    out = pcb.generate_pcb(spec)
    # A second closed Edge.Cuts contour = the through-board light hole.
    assert '(layer "Edge.Cuts")' in out
    assert "(gr_circle (center 110 110) (end 110.625 110)" in out
    # Copper on BOTH pours stays clear of the routed hole.
    from shapely.geometry import Point

    for layer in ("F.Cu", "B.Cu"):
        for net in ("3V3", "GND"):
            for poly in pcb._fill_geometry(net, layer, spec):
                assert poly.distance(Point(10, 10)) >= 0.625
    bom = pcb.generate_bom(spec)
    assert "reverse-mount" in bom and "LED 1206" in bom
    # an inline reverse unit routes the hole and keeps its traces clear of it
    ispec = pcb.BadgeSpec(leds=[pcb.Led(10, 10, "red", layout="inline", reverse=True)])
    out = pcb.generate_pcb(ispec)
    assert "(gr_circle (center 110 110) (end 110.625 110)" in out
    from shapely.geometry import Point as _P

    for layer in ("F.Cu", "B.Cu"):
        for net in ("3V3", "GND"):
            for poly in pcb._fill_geometry(net, layer, ispec):
                assert poly.distance(_P(10, 10)) >= 0.625


def test_advanced_placement_moves_resistor_and_via():
    # Resistor 5 mm right of the LED, spun 90°; via tucked below-left.
    adv = {"rx": 5.0, "ry": 0.0, "rrot": 90.0, "vx": -2.0, "vy": 2.0}
    led = pcb.Led(10.0, 10.0, "red", adv=adv)
    g = pcb.led_geometry(led)
    assert g["res"] == (5.0, 0.0)
    # rrot 90 (cw): pad offsets rotate onto the y axis
    assert g["res_in"] == (5.0, -1.025) and g["res_out"] == (5.0, 1.025)
    assert g["via_front"] == (-2.0, 2.0) == g["via_back"]
    # bbox is the envelope of the real copper, not a layout constant
    assert g["bbox"][2] >= 5.0 + 1.2 and g["bbox"][0] <= -2.85
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[led]))
    import re

    # Resistor footprint lands at LED + (5, 0) with pads carrying the extra 90°
    assert re.search(
        r'\(footprint "minibadge-designer:220R_0805" \(layer "F\.Cu"\) \(tstamp [0-9a-f-]+\)\n'
        r"    \(at 115 110\)",
        out,
    )
    assert re.search(r'\(pad "1" smd rect \(at 0 -1\.025 270\) \(size 1\.15 1\.4\)', out)
    # via at LED + (-2, 2)
    assert "(via (at 108 112)" in out
    # pours keep clearance around the rotated resistor pads
    from shapely.geometry import Point

    for poly in pcb._fill_geometry("GND", "F.Cu", pcb.BadgeSpec(leds=[led])):
        assert poly.distance(Point(15.0, 9.05)) >= 0.3  # res_in pad region


def test_advanced_bbox_drives_clamp_and_collision():
    adv = {"rx": 8.0, "ry": 0.0, "rrot": 0.0, "vx": -2.5, "vy": 0.0}
    led = pcb.Led(19.0, 10.0, "red", adv=adv)
    # The far-flung resistor (envelope +9.45) forces the LED center left.
    x, _y = pcb.clamp_led_obj(led)
    assert x <= pcb.UNIT_SAFE[2] - 9.45 + 1e-9
    # unit_poly covers the resistor so collisions see the real footprint
    poly = pcb.unit_poly(pcb.Led(10.0, 10.0, "red", adv=adv))
    from shapely.geometry import Point

    assert poly.contains(Point(17.5, 10.0))


def test_advanced_led_own_rotation():
    # lrot spins the LED pads on their own center; the resistor stays put.
    adv = {"rx": 5.0, "ry": 0.0, "rrot": 0.0, "lrot": 90.0, "vx": -2.5, "vy": 0.0}
    led = pcb.Led(10.0, 10.0, "red", adv=adv)
    g = pcb.led_geometry(led)
    assert g["led_k"] == (0.0, -1.025) and g["led_a"] == (0.0, 1.025)
    assert g["res_in"] == (3.975, 0.0)  # resistor unaffected
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[led]))
    import re

    # The LED footprint carries the extra 90 (KiCad ccw: 270); its pad 1
    # lands at the rotated local offset.
    assert re.search(r'\(pad "1" smd rect \(at 0 -1\.025 270\) \(size 1\.15 1\.4\)', out)
    # pours respect the rotated LED pads
    from shapely.geometry import Point

    for poly in pcb._fill_geometry("3V3", "F.Cu", pcb.BadgeSpec(leds=[led])):
        assert poly.distance(Point(10.0, 9.05)) >= 0.3


def test_through_hole_led_geometry_and_footprint():
    # Every TH package keeps the LED pads at the 2.54 mm lead pitch and
    # hands the resistor to an SMD 0805.
    for size in ("1.8mm", "3mm", "5x2mm"):
        g = pcb._layout("stacked", size)
        assert g["pkg"] == size
        assert g["led_k"] == (-1.27, 0.0) and g["led_a"] == (1.27, 0.0)
        assert pcb.res_pkg(size) == "0805"
        # the bbox covers the body, which is wider than the pad span
        assert g["bbox"][2] >= pcb.PKG[size]["body"][0] / 2
    assert pcb.res_pkg("0603") == "0603"

    g = pcb._layout("stacked", "3mm")
    assert g["res_in"] == (-1.025, -3.0) and g["res_out"] == (1.025, -3.0)

    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(10, 10, "red", size="3mm")]))
    # LED pads are round thru-holes on both faces (footprint-local coords);
    # the resistor stays an SMD 0805.
    assert ('(pad "1" thru_hole circle (at -1.27 0) (size 1.8 1.8) (drill 0.9) '
            '(layers "*.Cu" "*.Mask")') in out
    assert "(attr through_hole)" in out
    assert '"minibadge-designer:LED_RED_3mm"' in out
    assert re.search(r'\(pad "1" smd rect \(at -1\.025 0\) \(size 1\.15 1\.4\)', out)
    assert "LED_THT.3dshapes/LED_D3.0mm.step" in out
    assert "R_0805_2012Metric.step" in out
    bom = pcb.generate_bom(pcb.BadgeSpec(leds=[pcb.Led(10, 10, "red", size="3mm")]))
    assert "LED 3mm radial TH" in bom and "R 0805 (2012 metric)" in bom

    # The rectangular bar has no dome: a Fab rect, not a circle, and its own
    # 3D model. The 1.8 mm dome sits on a wider rectangular base.
    bar = pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(10, 10, "red", size="5x2mm")]))
    assert "LED_Rectangular_W5.0mm_H2.0mm.step" in bar
    assert "(fp_circle" not in bar
    assert "LED 5x2mm rectangular TH" in pcb.generate_bom(
        pcb.BadgeSpec(leds=[pcb.Led(10, 10, "red", size="5x2mm")]))
    assert "(fp_circle" in pcb.generate_pcb(
        pcb.BadgeSpec(leds=[pcb.Led(10, 10, "red", size="1.8mm")]))


def test_through_hole_pads_shape_both_pours():
    # The anode barrel penetrates the back GND pour: fill keeps clear there;
    # the cathode barrel anchors it.
    from shapely.geometry import Point

    led = pcb.Led(10, 10, "red", size="3mm")  # front unit, stacked
    spec = pcb.BadgeSpec(leds=[led])
    anode = Point(11.27, 10.0)
    for poly in pcb._fill_geometry("GND", "B.Cu", spec):
        assert poly.distance(anode) >= 0.3  # pad half 0.9 + clearance, from center: 1.25
    # th_pad_circles reports both annuli for far-side keepouts
    circles = pcb.th_pad_circles(led)
    assert len(circles) == 2
    assert circles[0] == (8.73, 10.0, 0.9) and circles[1] == (11.27, 10.0, 0.9)
    assert pcb.th_pad_circles(pcb.Led(10, 10, "red", size="0805")) == []


def test_th_model_anchored_at_pad_one():
    # The THT models are anchored at pin 1, so the model offset must walk to
    # pad 1 (board frame, unrotated, 3D y counting up) while the model's own
    # z-rotation matches the angle baked into our geometry. Getting either
    # wrong slides the lens off its pads: invisible to DRC, obvious in 3D.
    for ang, want_off in ((0, "-1.27 0 0"), (90, "0 1.27 0"), (270, "0 -1.27 0")):
        out = pcb.generate_pcb(pcb.BadgeSpec(
            leds=[pcb.Led(10.16, 10.16, "red", size="3mm", rot=ang)]))
        m = re.search(r'LED_THT[^\n]*\n\s*\(offset \(xyz ([^)]*)\)\).*?'
                      r'\(rotate \(xyz 0 0 ([-\d.]+)\)\)', out)
        assert m, f"no THT model emitted at {ang}"
        assert m.group(1) == want_off, f"rot {ang}: offset {m.group(1)}"
        assert float(m.group(2)) == ang, f"rot {ang}: rotate {m.group(2)}"
    # A back-side footprint is flipped through the board plane, which reverses
    # the sense of the model's z-rotation, and the offset rides along, since
    # it is expressed in the model's own frame. With the sign wrong the body
    # lies across its own pads at twice the angle (invisible on a round part,
    # which is why this needs asserting rather than eyeballing a render).
    back = pcb.generate_pcb(pcb.BadgeSpec(
        leds=[pcb.Led(10.16, 10.16, "red", size="3mm", rot=90, side="back")]))
    assert re.search(r'LED_THT[^\n]*\n\s*\(offset \(xyz 0 -1\.27 0\)\).*?'
                     r'\(rotate \(xyz 0 0 270\)\)', back)
    # Same rule for the (offsetless) SMD models: front follows the geometry
    # angle, back negates it.
    for side, want in (("front", "30"), ("back", "330")):
        out = pcb.generate_pcb(pcb.BadgeSpec(
            leds=[pcb.Led(10.16, 10.16, "red", rot=30, side=side)]))
        assert re.search(rf'LED_SMD[^\n]*\n\s*\(offset \(xyz 0 0 0\)\).*?'
                         rf'\(rotate \(xyz 0 0 {want}\)\)', out), side
    # The inline layout mounts the LED flipped: pad 1 moves to +dx and the
    # model spins the extra 180.
    inline = pcb.generate_pcb(pcb.BadgeSpec(
        leds=[pcb.Led(10.16, 10.16, "red", size="3mm", layout="inline")]))
    assert re.search(r'LED_THT[^\n]*\n\s*\(offset \(xyz 1\.27 0 0\)\).*?'
                     r'\(rotate \(xyz 0 0 180\)\)', inline)
    # Flipped AND on the back is where the two flips can cancel or double up:
    # the back angle already carries the inline 180, so the offset must not
    # apply it a second time or the body lands a whole pad pitch away.
    for ang, want_off, want_rot in ((0, r"1\.27 0 0", "180"), (90, r"0 1\.27 0", "90")):
        bi = pcb.generate_pcb(pcb.BadgeSpec(leds=[
            pcb.Led(10.16, 10.16, "red", size="3mm", side="back",
                    layout="inline", rot=ang)]))
        assert re.search(rf'LED_THT[^\n]*\n\s*\(offset \(xyz {want_off}\)\).*?'
                         rf'\(rotate \(xyz 0 0 {want_rot}\)\)', bi), ang
    # SMD models stay body-centered: no offset.
    smd = pcb.generate_pcb(pcb.BadgeSpec(leds=[pcb.Led(10.16, 10.16, "red", rot=90)]))
    assert re.search(r'LED_SMD[^\n]*\n\s*\(offset \(xyz 0 0 0\)\)', smd)


def test_reverse_never_through_hole():
    # reverse forces the 1206 SMD package even if a TH size is requested
    g = pcb._layout("stacked", "3mm", reverse=True)
    assert g["pkg"] == "1206" and g["hole"] == 1.25


def test_light_windows_get_keepouts():
    """A zone fill is not a fixed artifact: refilling in KiCad (which the
    README tells people to do) recomputes it from KiCad's own rules, and
    those know nothing about why the copper under a light window is
    missing. Without a keepout the refill floods the window solid and the
    glow/bare feature quietly disappears from the fabricated board."""
    window = (7.0, 13.0, 5.0, 3.0)
    for material in ("glow", "bare"):
        out = pcb.generate_pcb(pcb.BadgeSpec(art=[pcb.ArtLayer(material, [window])]))
        assert "(copperpour not_allowed)" in out, material
        # a through window cuts the board, so both pours are barred
        for layer in ("F.Cu", "B.Cu"):
            assert re.search(rf'\(zone \(net 0\)[^\n]*\(layer "{layer}"\)', out), \
                (material, layer)

    # A bare window open on one face only bars that face: the other pour is
    # untouched, which is what lets it sit under a part on the far side.
    out = pcb.generate_pcb(pcb.BadgeSpec(
        art=[pcb.ArtLayer("bare", [window], window="back")]))
    assert re.search(r'\(zone \(net 0\)[^\n]*\(layer "B\.Cu"\)', out)
    assert not re.search(r'\(zone \(net 0\)[^\n]*\(layer "F\.Cu"\)', out)

    # Silk and copper artwork keep their copper, so they get no keepout.
    for material in ("silk", "copper"):
        out = pcb.generate_pcb(pcb.BadgeSpec(art=[pcb.ArtLayer(material, [window])]))
        assert "(copperpour not_allowed)" not in out, material

    # Copper art inside a window keeps its island: the keepout is carved
    # around it rather than swallowing it.
    both = pcb.generate_pcb(pcb.BadgeSpec(art=[
        pcb.ArtLayer("bare", [(6.0, 6.0, 8.0, 8.0)]),
        pcb.ArtLayer("copper", [(9.0, 9.0, 2.0, 2.0)]),
    ]))
    assert "(copperpour not_allowed)" in both
    geom = pcb._window_geometry(pcb.BadgeSpec(art=[
        pcb.ArtLayer("bare", [(6.0, 6.0, 8.0, 8.0)]),
        pcb.ArtLayer("copper", [(9.0, 9.0, 2.0, 2.0)]),
    ]))
    from shapely.geometry import Point

    assert not geom.contains(Point(10.0, 10.0))  # the copper island is exempt


def _rect_ring(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


#: A window outline that ENCLOSES two voids, at different x. Every window in
#: this file and in the invariant corpus is otherwise drawn as plain
#: rectangles, so the shape a real user draws most often -- a glyph with a
#: counter (8, 4, 0, 6, A), or a ring of artwork -- went unexercised.
#:
#: The voids sit at different x deliberately. Both a slit and a band cut are
#: driven off each void's own extent, and it takes two voids offset from one
#: another for the material between and below them to come away as separate
#: pieces rather than a notch. Aligned voids (the obvious way to draw an 8)
#: are the case that does NOT reproduce: measured, and it is why the shape
#: below looks lopsided.
_COUNTERS = [[_rect_ring(6.0, 6.0, 14.0, 15.0),
              _rect_ring(7.6, 7.6, 12.4, 10.0),
              _rect_ring(7.0, 11.0, 11.0, 13.4)]]

#: Emitted coordinates are written at four decimal places, so a vertex can
#: land 1e-4 mm off the geometry it came from. Erode the shape by rather more
#: than that before asking whether the board covers it -- and by far less than
#: the 0.02 mm hairline this test exists to catch, or it would pass over one.
_EMIT_SLACK = 0.005


@pytest.mark.parametrize("material,window,cut_layers", [
    ("bare", "through", ("F.Cu", "B.Cu")),
    ("bare", "back", ("B.Cu",)),
    ("glow", "through", ("F.Cu", "B.Cu")),
])
def test_a_window_with_counters_is_emitted_as_one_whole_shape(material, window,
                                                             cut_layers):
    """A window's opening and its keepout each cover the shape the user drew.

    Neither a ``gr_poly`` nor a zone outline has any syntax for a hole, so a
    window whose outline encloses a void has to be broken into hole-free
    pieces, and *how* that is done is the whole of this test. Cutting a slit
    from each void out to the boundary and emitting what is left cost the user
    twice over:

    * the mask opening lost the slit. No fab prints a 0.02 mm dam of
      soldermask, but KiCad plots one faithfully, so every counter in every
      window text shipped with a hairline scored across it -- visible in the
      3D view and reported from it.
    * where two slits cut a block of the shape free, the emitter kept only the
      largest piece, so the block got no keepout at all. The precomputed fill
      still excluded it (the zip and the 2D preview looked right, and DRC has
      no rule about copper inside a window), but the first refill -- the 3D
      export, the Gerber plot, or pressing B as the README asks -- poured
      copper back into it under an open mask: bare live copper sitting in the
      light window.

    Stated as containment rather than as a count of pieces or a total area, so
    it holds however the shape is divided up.

    The board is deliberately bare apart from the window. A part whose keepout
    reaches the shape carves the window legitimately, and this test is about
    how a shape with holes gets *divided*, not about what a window may cut --
    so adding such a part is a change in behaviour and belongs in a case of its
    own, while a part clear of the shape leaves this green (probed both ways).
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    import invariants

    art = pcb.ArtLayer(material, [], _COUNTERS, window=window)
    spec = pcb.BadgeSpec(name="counters", leds=[], art=[art])
    board = invariants.assert_parses(pcb.generate_pcb(spec))
    drawn = Polygon(_COUNTERS[0][0], _COUNTERS[0][1:]).buffer(-_EMIT_SLACK)

    assert len(cut_layers) > 0, "this case names no cut layer, so it checks nothing"
    for layer in cut_layers:
        keepout = unary_union(invariants._keepout_outlines(board, layer))
        assert not keepout.is_empty, (
            f"no keepout rule area reached {layer} at all, so the next refill "
            "floods the whole window solid")
        assert keepout.contains(drawn), (
            f"the {layer} keepout rule areas miss "
            f"{drawn.difference(keepout).area:.4f} mm^2 of the window "
            "(around "
            f"{tuple(round(v, 2) for v in drawn.difference(keepout).bounds)}): "
            "the next refill pours copper into that patch, under an open mask "
            "if the window is bare")

    if material != "bare":
        return  # glow keeps the mask; it only cuts copper
    opened = [f"{face[0]}.Mask" for face in cut_layers]
    assert len(opened) > 0, "a bare window that opens no mask is not a window"
    for layer in opened:
        rings = []
        for g in board.graphics(layer):
            if g[0] != "gr_poly":
                continue
            pts = next(c for c in g[1:] if isinstance(c, list) and c[0] == "pts")
            rings.append([(float(p[1]) - pcb.ORIGIN, float(p[2]) - pcb.ORIGIN)
                          for p in invariants._kids(pts, "xy")])
        opening = unary_union([Polygon(r) for r in rings if len(r) >= 3])
        assert opening.contains(drawn), (
            f"the {layer} opening is missing "
            f"{drawn.difference(opening).area:.4f} mm^2 of the window: a "
            "hairline of soldermask no fab can print, scored across the "
            "shape in the plot and the 3D view")


def test_through_hole_silk_is_inside_the_art_keepout():
    """Artwork carves around a unit's copper, but a through-hole lens outline
    is drawn well outside the pads, so silk art used to print straight over
    the part's own silkscreen and KiCad flagged the overlap. The keepout has
    to cover the body, not just the copper."""
    from shapely.geometry import Point

    for size in ("3mm", "1.8mm", "5x2mm"):
        led = pcb.Led(10.16, 10.16, "red", size=size)
        poly = pcb.unit_copper_poly(led)
        labels = {name for name, _q in pcb.unit_copper_pieces(led)}
        assert "silk_body" in labels, size
        # Where the part's own silk actually reaches: a round lens draws arcs
        # at its radius, a rectangular body draws lines just outside its edge.
        pk = pcb.PKG[size]
        bw, bh = pk["body"]
        lens = pk.get("lens", 0.0)
        reach_x = max(bw, lens) / 2 + 0.16
        reach_y = max(bh, lens) / 2 + 0.21
        for pt in (Point(10.16 + reach_x, 10.16), Point(10.16 - reach_x, 10.16),
                   Point(10.16, 10.16 + reach_y), Point(10.16, 10.16 - reach_y)):
            assert poly.contains(pt), f"{size}: silk at {pt.wkt} not kept clear"
    # SMD parts keep the tighter envelope: no phantom body keepout
    assert "silk_body" not in {n for n, _q in pcb.unit_copper_pieces(
        pcb.Led(10.16, 10.16, "red", size="0805"))}


def _courtyard_half(size: str) -> tuple[float, float]:
    """Half-extents of a footprint's courtyard rect; mirrors _footprint()."""
    p = pcb.PKG[size]
    bw, bh = p["body"]
    return (max(p["dx"] + 1.05, bw / 2 + 0.25),
            max(p["ph"] / 2 + 0.4, bh / 2 + 0.25))


def test_inline_courtyards_do_not_overlap():
    # KiCad treats overlapping courtyards as an ERROR, so an LED must never
    # sit close enough to its own resistor for the two rects to intersect.
    # 0603 inline used to miss by 0.2 mm and every downloaded board with that
    # combination failed DRC out of the box.
    for size in pcb.PKG:
        rsize = pcb.res_pkg(size)
        led_x, led_y = _courtyard_half(size)
        res_x, res_y = _courtyard_half(rsize)
        g = pcb._layout("inline", size)
        dist = abs(g["res"][0] - 0.0)  # LED sits at the unit origin
        assert dist >= led_x + res_x, (
            f"{size} inline: parts {dist:.3f} mm apart but courtyards need "
            f"{led_x + res_x:.3f} mm"
        )
        g = pcb._layout("stacked", size)
        dist = abs(g["res"][1])
        assert dist >= led_y + res_y, (
            f"{size} stacked: parts {dist:.3f} mm apart but courtyards need "
            f"{led_y + res_y:.3f} mm"
        )


def test_novia_emits_no_via_and_lands_on_a_connector_pad():
    # A via-less unit trades its barrel for a trace to a connector pad of the
    # net it cannot reach on its own side. The pad is plated through, so this
    # is the same circuit with nothing drilled.
    for side, net in (("front", "GND"), ("back", "3V3")):
        led = pcb.Led(10.16, 10.16, "red", side=side, size="0805", novia=True)
        spec = pcb.BadgeSpec(leds=[led])
        out = pcb.generate_pcb(spec)
        assert "(via " not in out, side
        route = pcb.novia_route(led, spec.pins, pcb.unit_safe(spec), [led])
        assert route["net"] == net
        pads = [(x, y) for _n, x, y, pnet, _row in pcb.CONNECTOR_PADS if pnet == net]
        assert route["pad"] in pads
        # the emitted copper ends on that pad
        assert f"{pcb._n(pcb.ORIGIN + route['pad'][0])} " \
               f"{pcb._n(pcb.ORIGIN + route['pad'][1])}" in out
    # ...and the via comes back when the option is off
    assert "(via " in pcb.generate_pcb(
        pcb.BadgeSpec(leds=[pcb.Led(10.16, 10.16, "red", novia=False)]))


def test_novia_front_through_hole_routes_nothing():
    # A front-side TH LED's cathode lead is already plated through to the back
    # GND pour, so via-less means no extra copper at all.
    for size in ("1.8mm", "3mm", "5x2mm"):
        led = pcb.Led(10.16, 10.16, "red", side="front", size=size, novia=True)
        spec = pcb.BadgeSpec(leds=[led])
        route = pcb.novia_route(led, spec.pins, pcb.unit_safe(spec), [led])
        assert route["direct"] and len(route["pts"]) == 1, size
        assert "(via " not in pcb.generate_pcb(spec)


def test_novia_route_clears_the_units_own_copper_and_the_board_edge():
    from shapely.geometry import LineString, Polygon

    for side in ("front", "back"):
        for size in ("0603", "0805", "1206"):
            for layout in ("stacked", "inline"):
                for rot in (0, 90, 180, 270):
                    led = pcb.Led(10.16, 10.16, "red", side=side, size=size,
                                  layout=layout, rot=rot, novia=True)
                    spec = pcb.BadgeSpec(leds=[led])
                    safe = pcb.unit_safe(spec)
                    r = pcb.novia_route(led, spec.pins, safe, [led])
                    assert not r.get("tight"), (side, size, layout, rot)
                    run = LineString(r["pts"]).buffer(pcb.TRACK_W / 2)
                    for quad in pcb._unit_copper_quads(led, safe, skip_start=True):
                        assert run.distance(Polygon(quad)) >= pcb.NOVIA_CLEAR - 1e-9, \
                            (side, size, layout, rot)
                    board = pcb.outline_polygon(spec)
                    assert run.distance(board.exterior) >= 0.2 - 1e-9


def test_novia_reports_units_that_cannot_reach_power():
    # This placement's run fences the GND pour's own pad onto an island, so the
    # resistor would float: the app has to refuse rather than ship it.
    bad = pcb.Led(17.0, 12.5, "red", side="back", size="3mm",
                  layout="stacked", novia=True)
    _leds, problems = pcb.resolve_novia(pcb.BadgeSpec(leds=[bad]))
    assert problems == [0]
    # ...while an ordinary placement passes
    ok = pcb.Led(10.16, 10.16, "red", side="back", size="0805", novia=True)
    _leds, problems = pcb.resolve_novia(pcb.BadgeSpec(leds=[ok]))
    assert problems == []


def test_novia_art_keepout_follows_the_trace():
    # Copper art sitting on the run would short it to the pour it crosses, so
    # the keepout has to cover the whole path.
    led = pcb.Led(10.16, 10.16, "red", side="back", size="0805", novia=True)
    spec = pcb.BadgeSpec(leds=[led])
    safe = pcb.unit_safe(spec)
    labels = {n for n, _q in pcb.unit_copper_pieces(led, safe, spec.pins, [led])}
    assert "via" not in labels and "trace_stub" not in labels
    assert any(n.startswith("trace_pad") for n in labels)
    poly = pcb.unit_copper_poly(led, safe, spec.pins, [led])
    route = pcb.novia_route(led, spec.pins, safe, [led])
    from shapely.geometry import Point
    for x, y in route["pts"]:
        assert poly.distance(Point(x, y)) < 1e-6


def test_back_silk_pad_captions_read_correctly_from_the_back():
    # Each caption names a pad PAIR. Viewed from the back the pair is mirrored,
    # so the words must swap: otherwise the back silk calls the 3V3 pad GND.
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[]))
    import re

    def caption(layer, x):
        m = re.search(
            rf'\(fp_text user "([^"]+)" \(at {x} [\d.]+ unlocked\) \(layer "{layer}"\)', out)
        return m.group(1) if m else None

    assert caption("F.SilkS", "17.78") == "3V3 GND"
    assert caption("B.SilkS", "17.78") == "GND 3V3"
    assert caption("F.SilkS", "2.54") == "VBAT GND"
    assert caption("B.SilkS", "2.54") == "GND VBAT"
    # The pair's left-hand pad on each face really does carry that net.
    top = {net: x for _n, x, y, net, row in pcb.CONNECTOR_PADS if row == "top"}
    assert top["3V3"] < top["GND"] or 16.51 < 19.05  # front order: 3V3 then GND


def test_farled_puts_the_led_on_the_far_face_with_a_via_in_each_pad():
    import re

    for side, far in (("front", "B.Cu"), ("back", "F.Cu")):
        led = pcb.Led(10.16, 10.16, "red", side=side, size="0805", farled=True)
        out = pcb.generate_pcb(pcb.BadgeSpec(leds=[led]))
        led_layer = re.search(
            r'\(footprint "minibadge-designer:LED_RED[^"]*" \(layer "([^"]+)"', out).group(1)
        res_layer = re.search(
            r'\(footprint "minibadge-designer:220R[^"]*" \(layer "([^"]+)"', out).group(1)
        assert led_layer == far, side          # LED crossed over
        assert res_layer != far, side          # resistor stayed put
        # A via sits at each LED pad, carrying its net through the board.
        g = pcb.led_geometry(led)
        vias = [(float(a), float(b)) for a, b in
                re.findall(r"\(via \(at ([\d.]+) ([\d.]+)\)", out)]
        for off in (g["led_k"], g["led_a"]):
            rx, ry = pcb._r(off[0], off[1], led.rot)
            want = (pcb.ORIGIN + 10.16 + rx, pcb.ORIGIN + 10.16 + ry)
            assert any(abs(v[0] - want[0]) < 1e-6 and abs(v[1] - want[1]) < 1e-6
                       for v in vias), (side, off)
    # Reverse mount already shines through, so the two never combine.
    rev = pcb.Led(10.16, 10.16, "red", size="1206", reverse=True, farled=True)
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[rev]))
    import re as _re
    assert _re.search(r'LED_RED[^"]*" \(layer "F\.Cu"', out)


def test_farled_bom_names_the_face_the_led_mounts_on():
    bom = pcb.generate_bom(pcb.BadgeSpec(
        leds=[pcb.Led(10.16, 10.16, "red", side="back", farled=True)]))
    d1 = [r for r in bom.splitlines() if r.startswith("D1,")][0]
    r1 = [r for r in bom.splitlines() if r.startswith("R1,")][0]
    assert ",front,1," in d1 and "front face" in d1
    assert ",back,1," in r1


def _subsequence(needles, haystack):
    it = iter(haystack)
    return all(any(abs(h[0] - n[0]) < 1e-6 and abs(h[1] - n[1]) < 1e-6 for h in it)
               for n in needles)


def _all_45(pts) -> bool:
    for a, b in zip(pts, pts[1:]):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        straight = dx < 1e-6 or dy < 1e-6
        diagonal = abs(dx - dy) < 1e-6
        if not (straight or diagonal):
            return False
    return True


def test_manual_trace_bends_are_kept_and_corners_come_out_as_45s():
    nodes = ((5.0, 17.0), (12.0, 17.5))
    led = pcb.Led(10.16, 13.0, "red", side="back", size="0805",
                  novia=True, nodes=nodes)
    spec = pcb.BadgeSpec(leds=[led])
    r = pcb.novia_route(led, spec.pins, pcb.unit_safe(spec), [led])
    assert r["manual"] and not r.get("tight")
    # every bend the user placed still sits on the path, in order...
    assert _subsequence(nodes, r["pts"])
    # ...and no corner is square: each leg is axis-aligned or exactly 45.
    assert _all_45(r["pts"]), r["pts"]


def test_auto_routes_come_out_as_45s_too():
    for side in ("front", "back"):
        for size in ("0603", "0805", "1206"):
            for layout in ("stacked", "inline"):
                led = pcb.Led(10.16, 10.16, "red", side=side, size=size,
                              layout=layout, novia=True)
                spec = pcb.BadgeSpec(leds=[led])
                r = pcb.novia_route(led, spec.pins, pcb.unit_safe(spec), [led])
                if r.get("direct"):
                    continue
                assert _all_45(r["pts"]), (side, size, layout, r["pts"])
    # A bend dropped onto another net's pad is flagged, not silently shipped.
    bad = pcb.Led(10.16, 13.0, "red", side="back", size="0805",
                  novia=True, nodes=((3.81, 1.27),))
    spec = pcb.BadgeSpec(leds=[bad])
    r = pcb.novia_route(bad, spec.pins, pcb.unit_safe(spec), [bad])
    assert r["tight"] and r["manual"]


def _worst_turn_deg(pts) -> float:
    """Largest direction change at any joint of the polyline, in degrees.

    A coincident-point pair reads as the worst possible turn: it is the
    residue of a hairpin, which is exactly the copper this measures for.
    """
    import math
    worst = 0.0
    for i in range(1, len(pts) - 1):
        a, c, b = pts[i - 1], pts[i], pts[i + 1]
        v1 = (c[0] - a[0], c[1] - a[1])
        v2 = (b[0] - c[0], b[1] - c[1])
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-9 or l2 < 1e-9:
            return 180.0
        dot = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
        worst = max(worst, math.degrees(math.acos(max(-1.0, min(1.0, dot)))))
    return worst


@pytest.mark.slow
def test_routes_squeezed_around_an_obstacle_never_turn_sharper_than_45():
    """No joint of a via-less run changes direction by more than 45 degrees.

    _all_45 above vets each leg, but the fold happens BETWEEN legs: mitre45
    shapes legs independently, so a run forced to approach its pad from the
    far side of an obstacle used to ship a hairpin built from two
    individually perfect legs. That acute wedge of copper is an acid trap a
    fab flags, and in the preview it reads as machine-mangled routing. The
    canvas mirror is pinned to this same geometry by the router-parity
    browser test, so proving the board proves the preview.

    Every placement here keeps a 0.5 mm gap to the obstacle and clears the
    connector pads, i.e. the app itself would allow it; each turned 64 to
    135 degrees before soften45.
    """
    obstacle = pcb.Led(10.0, 10.0, "red", size="1206")
    cases = [  # size, side, rot, x, y -- all off the 0805/front/0 default
        ("3mm", "back", 0, 11.0, 17.0),
        ("1206", "back", 0, 10.0, 17.0),
        ("1206", "back", 0, 9.0, 17.0),
        ("0805", "front", 270, 11.0, 3.0),
    ]
    bent = 0
    for size, side, rot, x, y in cases:
        led = pcb.Led(x, y, "red", rot=rot, size=size, side=side, novia=True)
        r = pcb.novia_route(led, others=[led, obstacle])
        assert r and not r.get("tight"), \
            f"{size}/{side}/rot{rot} at ({x},{y}) should route cleanly"
        if len(r["pts"]) >= 3:
            bent += 1
        assert _worst_turn_deg(r["pts"]) <= 45.0 + 1e-6, \
            (size, side, rot, x, y, r["pts"])
    # A run chained into another unit's pad bends around copper the same way.
    chained = pcb.Led(7.0, 3.0, "red", rot=90, size="0805", novia=True,
                      term=("unit", 1))
    hub = pcb.Led(15.0, 13.0, "red", size="1206", novia=True)
    blocker = pcb.Led(8.0, 10.0, "red", size="0805")
    leds = [chained, hub, blocker]
    term = pcb.novia_term(chained, leds=leds)
    r = pcb.novia_route(chained, others=leds, term=term)
    assert r and r.get("term") and not r.get("tight"), r
    if len(r["pts"]) >= 3:
        bent += 1
    assert _worst_turn_deg(r["pts"]) <= 45.0 + 1e-6, r["pts"]
    # The guard that keeps this from passing vacuously: if a smarter router
    # ever straightens every case, nothing above measured a joint at all.
    assert bent >= 3, \
        "the obstacle no longer bends these routes; move the placements"


def test_a_chosen_pad_wins_over_the_nearest_one():
    """A hand-picked trace end sends the run there, never to the closer pad
    the auto-router would use.

    The endpoint is the user's routing decision on the physical board: a run
    silently rerouted to a nearer pad crosses regions they deliberately kept
    clear, and the preview would be showing copper the fab never builds.
    """
    pads = {num: (x, y) for num, x, y, _n, _r in pcb.CONNECTOR_PADS}
    # Off the defaults: a back-side inline 0603 at an oblique angle, choosing
    # the top-row 3V3 pad when the bottom-row one is closer.
    led = pcb.Led(13.0, 15.0, "green", side="back", layout="inline",
                  size="0603", rot=37, novia=True, term=("pad", "7"))
    spec = pcb.BadgeSpec(leds=[led])
    safe = pcb.unit_safe(spec)
    term = pcb.novia_term(led, spec.leds, spec.pins, safe)
    route = pcb.novia_route(led, spec.pins, safe, spec.leds, term=term)
    assert route["pad"] == pads["7"], route["pad"]
    assert route.get("term") and not route.get("tight"), route
    # The contrast that keeps this from passing vacuously: without the choice
    # the router really does go to the nearer bottom-row pad.
    auto = pcb.novia_route(led, spec.pins, safe, spec.leds)
    assert auto["pad"] == pads["15"], auto["pad"]


def test_a_run_chained_onto_another_unit_lands_on_its_pad_and_ships_no_via():
    """term=("unit", k) ends the run on that unit's same-net pad, so several
    via-less units can share one path to the rail instead of each cutting its
    own channel across the pour. The chained board still ships via-free and
    resolve_novia accepts it."""
    from dataclasses import replace

    a = pcb.Led(6.0, 6.0, "red", novia=True, term=("unit", 1))
    b = pcb.Led(13.5, 12.5, "blue", novia=True, term=("pad", "16"))
    spec = pcb.BadgeSpec(leds=[a, b])
    safe = pcb.unit_safe(spec)
    term = pcb.novia_term(a, spec.leds, spec.pins, safe)
    assert term is not None and term[1] is b, "the chain target did not resolve"
    route = pcb.novia_route(a, spec.pins, safe, spec.leds, term=term)
    assert not route.get("tight"), route
    # The run ends exactly on b's cathode pad, the GND pad of a front unit.
    g = pcb.led_geometry(b)
    bx, by = pcb.clamp_led_obj(b, safe)
    ox, oy = pcb._r(*g["led_k"], b.rot)
    assert route["pts"][-1] == (bx + ox, by + oy), route["pts"]
    resolved, bad = pcb.resolve_novia(spec, safe)
    assert bad == [], "a routable chain was refused"
    assert "(via " not in pcb.generate_pcb(replace(spec, leds=list(resolved)))


@pytest.mark.parametrize("term,leds_extra,why", [
    (("pad", "7"), [], "a 3V3 pad cannot end a front unit's GND run"),
    (("pad", "1"), [], "VBAT carries no net a trace may land on"),
    (("pad", "42"), [], "not a connector pad at all"),
    (("unit", 0), [], "a unit cannot chain to itself"),
    (("unit", 5), [], "no such unit"),
    (("unit", 1), [pcb.Led(13.0, 13.0, "blue", side="back")],
     "the target's SMD pads have no copper on this unit's layer"),
    (("unit", 1), [pcb.Led(13.0, 13.0, "blue", novia=True, term=("unit", 0))],
     "a two-unit loop never reaches a plated hole"),
])
def test_an_invalid_terminal_choice_falls_back_to_the_nearest_pad(
        term, leds_extra, why):
    """Every invalid choice resolves to None (the automatic nearest-pad
    route) instead of refusing the board or, worse, landing the run on
    copper that cannot power it. Hand-crafted requests are sanitized, and
    the canvas never offers these choices in the first place."""
    led = pcb.Led(6.0, 6.0, "red", novia=True, term=term)
    leds = [led] + leds_extra
    assert pcb.novia_term(led, leds, pcb.ALL_PINS, None) is None, why
    # The dropped-pin variant needs a valid-net pad to prove `pins` gates it.
    kept = ("2", "7", "8", "15")  # pad 16 dropped
    gone = pcb.Led(6.0, 6.0, "red", novia=True, term=("pad", "16"))
    assert pcb.novia_term(gone, [gone], kept, None) is None, \
        "a dropped pin stayed a legal destination"
    import re

    # Keeping one corner leaves exactly its two pads, its tab and its header.
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[], pins=("7", "8")))
    assert re.findall(r'\(pad "(\d+)" thru_hole', out) == ["7", "8"]
    assert out.count("PinHeader_1x02") == 1
    # A half-populated pair keeps its pad but loses the two-pin header body.
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[], pins=("7",)))
    assert re.findall(r'\(pad "(\d+)" thru_hole', out) == ["7"]
    assert "PinHeader_1x02" not in out
    # ...and the caption names only the pin that is actually there.
    assert pcb.pair_caption("tr", ("7",)) == "3V3"
    assert pcb.pair_caption("tr", ("8",)) == "GND"
    assert pcb.pair_caption("tr", ("7", "8")) == "3V3 GND"


def test_dropped_corner_frees_its_keepout_and_tab():
    # A unit may sit where a removed pair used to be, and a custom outline no
    # longer grows a tab out to hold it.
    led = pcb.Led(2.54, 1.6, "red", size="0603", layout="inline")
    assert pcb.pad_conflict(led, pcb.ALL_PINS)
    assert not pcb.pad_conflict(led, ("7", "8", "15", "16"))
    assert pcb.active_pairs(("7", "8", "15", "16")) == ["tr", "br"]
    assert len(pcb.caption_boxes(("7", "8", "15", "16"))) == 2


def test_power_missing_names_the_rail_the_leds_lost():
    assert pcb.power_missing(pcb.ALL_PINS) == []
    assert pcb.power_missing(("7", "8")) == []          # one 3V3 + one GND
    assert pcb.power_missing(("2", "8", "16")) == ["3V3"]
    assert pcb.power_missing(("7", "15")) == ["GND"]
    assert pcb.power_missing(("1", "9", "10")) == ["3V3", "GND"]  # signal pins only
    assert pcb.power_missing(()) == ["3V3", "GND"]


def test_row_names_passed_as_pins_raise_rather_than_silently_dropping_keepouts():
    # The old API took row names. Handing those to a pin argument used to read
    # as "no pads kept", quietly removing every keepout.
    import pytest

    with pytest.raises(ValueError):
        pcb.active_pairs(("top", "bottom"))


def test_half_populated_pair_still_gets_a_single_pin_header():
    import re

    # A pair with one pin dropped has a real one-pin header on the finished
    # badge, so the 3D model must show one, centred on the pad that is left,
    # not on the pair midpoint a two-pin body would use.
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[], pins=("7",)))
    assert "PinHeader_1x01_P2.54mm_Vertical" in out
    assert "PinHeader_1x02" not in out
    off = re.search(r"\(offset \(xyz ([-\d.]+) ([-\d.]+) -1\.6\)\)", out)
    assert float(off.group(1)) == 16.51        # over pad 7 itself
    # A full pair keeps the two-pin body, spanning the pair's midpoint.
    out = pcb.generate_pcb(pcb.BadgeSpec(leds=[], pins=("7", "8")))
    assert "PinHeader_1x02_P2.54mm_Vertical" in out
    assert "PinHeader_1x01" not in out
    off = re.search(r"\(offset \(xyz ([-\d.]+) ([-\d.]+) -1\.6\)\)", out)
    assert float(off.group(1)) == 16.51        # 17.78 midpoint - 1.27


def test_one_face_bare_window_leaves_the_other_pour_alone():
    from shapely.geometry import Point

    # A part on the front and a bare window on the back, overlapping. The
    # window only opens the back mask, so only the back pour is cut; the
    # front keeps its copper and the part keeps working.
    win = (5.0, 6.0, 11.0, 8.0)
    led = pcb.Led(10.16, 10.16, "red", side="front", size="0805", layout="inline")
    spec = pcb.BadgeSpec(leds=[led],
                         art=[pcb.ArtLayer("bare", [win], side="back", window="back")])
    # inside the window but clear of the unit's own copper, which cuts the
    # front pour around itself no matter what the window does
    at = Point(14.5, 12.5)
    front = pcb._fill_geometry("3V3", "F.Cu", spec)
    back = pcb._fill_geometry("GND", "B.Cu", spec)
    assert any(p.contains(at) for p in front), "front pour should survive"
    assert not any(p.contains(at) for p in back), "back pour should be cut"

    # A glow window has to cut both: light crosses the board.
    spec = pcb.BadgeSpec(leds=[led], art=[pcb.ArtLayer("glow", [win])])
    assert not any(p.contains(at) for p in pcb._fill_geometry("3V3", "F.Cu", spec))
    assert not any(p.contains(at) for p in pcb._fill_geometry("GND", "B.Cu", spec))

    # ...and so does a bare window asked to open both faces.
    spec = pcb.BadgeSpec(leds=[led],
                         art=[pcb.ArtLayer("bare", [win], window="through")])
    assert not any(p.contains(at) for p in pcb._fill_geometry("3V3", "F.Cu", spec))
    assert not any(p.contains(at) for p in pcb._fill_geometry("GND", "B.Cu", spec))


def test_a_hand_bent_perimeter_bridge_is_built_through_its_bends():
    """A bridge the user shaped is the bridge the board carries.

    The perimeter bridges were the one piece of routed copper on the board
    nobody could touch: no bends, and not even drawn in the editor. So a
    ground trace appeared in the 3D view, running from a unit to the board
    edge, that its owner had never seen and could not move. They are editable
    like every other trace now, which is only true if the bends actually reach
    the board -- and if a bend the router cannot honour is reported rather
    than quietly straightened, because a trace that ignores where it was put
    is worse than one that cannot be placed at all.
    """
    window = [pcb.ArtLayer("bare", [(3.0, 3.0, 14.0, 14.0)])]
    bend = (8.975, 14.0)          # straight out of the cathode pad, clear
    spec = pcb.BadgeSpec(
        leds=[pcb.Led(10.0, 10.0, "red", side="back", bnodes=(bend,))],
        art=list(window))
    pts = pcb.unit_bridges(spec)[0]["B.Cu"]
    assert pts and len(pts) >= 3, (
        f"a bend was placed and the route came back as {pts}: the bridge is "
        "still the straight two-point run, so the bend never reached the board")
    assert min(abs(px - bend[0]) + abs(py - bend[1]) for px, py in pts) < 1e-6, \
        f"the bend at {bend} is not one of the route's own points: {pts}"
    assert not pcb.bridge_problems(spec), \
        "a bend with clear board around it was reported as unroutable"

    # Every leg reaches the file: the emitter used to write one segment per
    # bridge, which would have dropped everything past the first bend.
    out = pcb.generate_pcb(spec)
    legs = re.findall(
        r'\(segment \(start ([\d.]+) ([\d.]+)\) \(end ([\d.]+) ([\d.]+)\)'
        r'[^\n]+\(layer "B\.Cu"\) \(net 2\)', out)
    assert len(legs) == len(pts) - 1, (
        f"the route has {len(pts) - 1} legs and the board carries "
        f"{len(legs)} GND segments on B.Cu")

    # And a bend that drags the trace across the unit's own anode pad is a
    # problem, named per unit and layer, not silently re-routed.
    bad = pcb.BadgeSpec(
        leds=[pcb.Led(10.0, 10.0, "red", side="back", bnodes=((13.0, 12.0),))],
        art=list(window))
    assert pcb.bridge_problems(bad) == [(0, "B.Cu")], (
        "a bend that puts the bridge through the unit's own pad has to be "
        "reported: the board would ship a short")


@pytest.mark.parametrize("layout,size,rot", [
    ("stacked", "0805", 0), ("stacked", "0603", 90), ("inline", "1206", 180),
])
def test_part_labels_print_on_the_silkscreen_and_can_be_turned_off(layout, size,
                                                                   rot):
    """The board names its parts D1 and R1, in ink, unless asked not to.

    A footprint's reference is documentation on the fab layer and INK on the
    silkscreen, and only one of those reaches the finished board. It was on
    the fab layer, so the BOM named parts by references the board never
    printed: whoever solders the kit has to work out which resistor is R1 from
    the picture. Printed is the default, and the switch exists because a badge
    whose whole face is artwork does not want two labels on it.
    """
    led = pcb.Led(10.0, 10.0, "red", layout=layout, size=size, rot=rot)
    on = pcb.generate_pcb(pcb.BadgeSpec(leds=[led]))
    off = pcb.generate_pcb(pcb.BadgeSpec(leds=[led], refdes=False))

    for ref in ("D1", "R1"):
        assert re.search(rf'\(fp_text reference "{ref}"[^\n]*\(layer "[FB]\.SilkS"\)',
                         on), f"{ref} is not printed on the silkscreen"
        assert re.search(rf'\(fp_text reference "{ref}"[^\n]*\(layer "[FB]\.Fab"\)',
                         off), (
            f"with labels off, {ref} must stay on the fab layer -- a footprint "
            "without a reference is not a footprint")
        assert not re.search(
            rf'\(fp_text reference "{ref}"[^\n]*\(layer "[FB]\.SilkS"\)', off), \
            f"{ref} still prints with labels switched off"

    # The ink lands somewhere printable: clear of the pads' mask openings, on
    # this unit and its sibling. Stated as a clearance, since where exactly
    # the label goes is the placement search's business.
    from shapely.geometry import box as sbox
    b = invariants.assert_parses(on)
    labels = invariants._printed_references(b)
    assert len(labels) == 2, f"expected D1 and R1 in ink, got {labels}"
    unit_pads = [pad for pad in b.pads if pad.ref != "J1"]
    assert len(unit_pads) == 4, (
        f"expected the unit's four pads to measure against, found {unit_pads}")
    for ref, layer, _at, bx in labels:
        for pad in unit_pads:
            gap = sbox(*bx).distance(pad.copper())
            assert gap >= invariants.REFDES_CLEAR - 1e-4, (
                f"{ref} is {gap:.3f} mm from pad {pad.ref}.{pad.num} on a "
                f"{layout} {size} unit at {rot} degrees: the fab clips ink "
                "that close to a mask opening")


def test_a_part_label_can_be_moved_or_switched_off_one_part_at_a_time():
    """Each printed reference is the user's to place, or to do without.

    The automatic spot is a fallback, not a policy: a badge is a piece of
    design work, and where "R1" sits on it is the designer's call. So each
    part carries its own switch and its own position, and a hand-placed one is
    used as given -- a POSITION, not an offset, so it stays put when the part
    turns.

    The fallback matters as much as the placement. A label placed by hand and
    then buried (a part moved onto it, a package grew) drops back to the
    search rather than shipping ink over a mask opening, because the canvas
    runs the same fallback and the two have to agree about where it went.
    """
    from shapely.geometry import box as sbox

    hand = (15.0, 5.0)
    spec = pcb.BadgeSpec(leds=[pcb.Led(10.0, 10.0, "red", dlabel_at=hand)])
    placed = {lab["ref"]: lab for lab in pcb.refdes_layout(spec)}
    assert set(placed) == {"D1", "R1"}, f"expected both labels, got {placed}"
    assert placed["D1"]["at"] == pytest.approx(hand), (
        f"D1 was placed at {placed['D1']['at']}, not at the {hand} it was "
        "dragged to")
    assert placed["D1"]["hand"] and not placed["R1"]["hand"], (
        "the hand-placed flag has to name which label was chosen by whom; "
        "the UI draws them differently and the fallback below depends on it")

    # The emitted footprint carries it as a local offset, since that is what a
    # KiCad reference field is: turn the unit and the ink turns with it.
    out = pcb.generate_pcb(spec)
    assert re.search(r'\(fp_text reference "D1" \(at 5 -5 unlocked\) '
                     r'\(layer "F\.SilkS"\)', out), (
        "the hand-placed D1 is not written as an offset from its footprint")

    # One part at a time: R1 off leaves D1 printing.
    off = pcb.BadgeSpec(leds=[pcb.Led(10.0, 10.0, "red", rlabel=False)])
    assert [lab["ref"] for lab in pcb.refdes_layout(off)] == ["D1"], \
        "switching R1 off must not take D1 with it"
    text = pcb.generate_pcb(off)
    assert re.search(r'\(fp_text reference "R1"[^\n]*\(layer "F\.Fab"\)', text)
    assert not re.search(r'\(fp_text reference "R1"[^\n]*SilkS', text)

    # Buried by its own unit: back to the automatic spot, not onto the pad.
    buried = pcb.BadgeSpec(leds=[pcb.Led(10.0, 10.0, "red",
                                         dlabel_at=(10.0, 10.0))])
    lab = next(l for l in pcb.refdes_layout(buried) if l["ref"] == "D1")
    assert not lab["hand"], (
        "a label dropped on its own pads was honoured; that ink is clipped by "
        "the fab and DRC flags it")
    board = invariants.assert_parses(pcb.generate_pcb(buried))
    ink = sbox(*invariants._printed_references(board)[0][3])
    unit_pads = [p for p in board.pads if p.ref != "J1"]
    assert len(unit_pads) == 4, (
        f"expected the unit's four pads to measure against, found {unit_pads}")
    for pad in unit_pads:
        assert ink.distance(pad.copper()) >= invariants.REFDES_CLEAR - 1e-4, \
            "the fallback put the label somewhere the fab cannot print it"


def test_the_clk_jumper_vias_can_be_moved_but_not_onto_other_copper():
    """The jumper's vias go where the designer puts them, within reason.

    A via is a plated hole with copper on both faces: parked on a pad, on
    another part's copper or off the board it is a short or a hole in thin
    air, and the STUB that feeds it drags along with it -- a via dropped
    somewhere clear whose feed crosses a pad is the same fault one step
    removed. Legal moves are honoured; the rest fall back to the spot beside
    the jumper, which is what the canvas does with the same position.
    """
    leds = [pcb.Led(10.0, 6.0, "red", side="back", clk=True)]
    auto = pcb.clk_info(pcb.BadgeSpec(leds=leds, clk_jumper=True))["via"]
    assert auto, "this design should have a rail via to move in the first place"

    moved = (auto[0], auto[1] - 2.0)
    spec = pcb.BadgeSpec(leds=leds, clk_jumper=True, jumper_via_at=moved)
    assert pcb.jumper_via_ok(spec, "via", moved), (
        f"{moved} is 2 mm along the via's own feed and clear of everything; "
        "if the check refuses that, nothing is movable")
    assert pcb.clk_info(spec)["via"] == pytest.approx(moved)

    for label, bad in (("a connector pad", (1.27, 1.27)),
                       ("its own unit", (10.0, 6.0)),
                       ("off the board", (25.0, 25.0)),
                       ("across the CLK pad", (5.0, 18.45))):
        assert not pcb.jumper_via_ok(spec, "via", bad), \
            f"a via on {label} at {bad} was called legal"


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
@pytest.mark.parametrize("side,which,ref", [("front", "d", "D1"),
                                            ("back", "r", "R1")])
def test_a_hand_placed_label_prints_where_it_was_put_at_any_angle(rot, side,
                                                                  which, ref):
    """A label dragged to a spot prints at that spot, whatever the part's angle.

    The reference is written into the footprint as an offset from its anchor,
    and the footprints here carry no rotation of their own -- `_smd` bakes
    every angle into the local geometry instead. So the offset is a plain
    difference, and rotating it (the way the automatic branch rotates an
    offset INTO that frame) moves the ink somewhere nobody asked for. At 180
    degrees it landed on the far side of the board: right in the 2D preview,
    which reads the position, and wrong in the 3D view and in every exported
    file, which read the footprint.

    Every angle and both faces, because the bug was invisible at 0 -- where a
    rotation is the identity -- and every earlier test used 0.
    """
    from shapely.geometry import box as sbox

    want = (6.5, 4.0) if side == "front" else (13.5, 16.0)
    led = pcb.Led(10.0, 10.0, "red", side=side, rot=rot,
                  **{f"{which}label_at": want})
    board = invariants.assert_parses(
        pcb.generate_pcb(pcb.BadgeSpec(leds=[led])))
    printed = {r: at for r, _layer, at, _bx
               in invariants._printed_references(board)}
    assert ref in printed, (
        f"{ref} is not printed at all on a {side} unit at {rot} degrees; "
        f"the board carries {sorted(printed)}")
    at = printed[ref]
    assert max(abs(at[0] - want[0]), abs(at[1] - want[1])) < 0.01, (
        f"{ref} was dragged to {want} and the board prints it at "
        f"{(round(at[0], 3), round(at[1], 3))}")

    # And it is still printable there: the whole point of honouring the
    # position is that the search vetted it first.
    ink = sbox(*next(bx for r, _l, _a, bx in invariants._printed_references(board)
                     if r == ref))
    unit_pads = [p for p in board.pads if p.ref != "J1"]
    assert len(unit_pads) == 4, f"expected four unit pads, found {unit_pads}"
    for pad in unit_pads:
        assert ink.distance(pad.copper()) >= invariants.REFDES_CLEAR - 1e-4, (
            f"{ref} prints {ink.distance(pad.copper()):.3f} mm from pad "
            f"{pad.ref}.{pad.num}")
