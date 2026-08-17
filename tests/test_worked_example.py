"""The tests that `references/worked-example.md` walks through, step by step.

This file exists so the worked example is *executable*. The example takes one
real test through all ten steps of the skill and shows the actual output of
each: the value-bar sentence, the parameter matrix, the mutation, the pasted
`--tb=short` failure, the three refactor probes. It cited this module before the
module existed, which made the one document arguing against fabricated evidence
the only one carrying some. `tests/test_meta.py` now asserts that every test the
example names is really here.

What they guard, in user terms: the mask colour and the surface finish are the
two fabrication choices that reach the fab *only* through the stackup block. The
UI shows the user a colour, the zip downloads, the board opens, and nothing
anywhere reveals that the file asked for the wrong one until the badges arrive.
"""

import pytest

from invariants import _kid, _kids, _val, assert_parses


def _mask_layer_colours(text):
    """The colour recorded on every solder-mask layer of the stackup."""
    b = assert_parses(text)
    assert b.setup is not None, "no (setup ...) block; the fab gets no stackup"
    stack = _kid(b.setup, "stackup")
    assert stack is not None, (
        "no (stackup ...) block; the fab is told nothing about mask colour "
        "and ships whatever is on the panel")
    return {str(ly[1]): str(_val(ly, "color"))
            for ly in _kids(stack, "layer") if "Mask" in str(ly[1])}


# A user who picks purple must be shipped purple. Nothing else in the .kicad_pcb
# records the choice, so a regression here is invisible until the fab order.
# Every case deliberately moves off the defaults (green mask, 0805, front, rot 0)
# because a board-shorting defect once hid in exactly the size parameter.
@pytest.mark.parametrize("colour,spec_kwargs", [
    ("purple", {"leds": [{"x": 6.5, "y": 6.0, "size": "1206", "rot": 90}]}),
    ("black", {"leds": [{"x": 10.0, "y": 8.0, "size": "0603", "side": "back"}],
               "finish": "hasl"}),
    ("white", {"leds": [{"x": 6.5, "y": 6.0, "reverse": True, "size": "1206"}]}),
    ("red", {"leds": [{"x": 6.5, "y": 6.0, "size": "0603", "layout": "inline"}],
             "pins": ("1", "2", "7", "8")}),
    # light-path addition: a through-hole package, a different code path
    # through generate_pcb entirely, on a non-default finish.
    ("blue", {"leds": [{"x": 8.0, "y": 8.0, "size": "3mm"}], "finish": "hasl"}),
    ("green", {}),  # the default, kept as the contrast case
])
def test_the_stackup_tells_the_fab_the_mask_colour_the_user_picked(
        board, colour, spec_kwargs):
    colours = _mask_layer_colours(board(mask_color=colour, **spec_kwargs))
    assert set(colours) == {"F.Mask", "B.Mask"}, (
        f"stackup carries mask layers {sorted(colours)}; a badge with only "
        "one mask layer described is not a two-sided board to the fab")
    # Independently stated (D2): the file must name the colour the user picked,
    # capitalised the way KiCad's stackup dialog writes it, on *both* faces.
    assert set(colours.values()) == {colour.capitalize()}, (
        f"user picked {colour!r} but the stackup says {colours}; the badge "
        "is fabricated in the wrong colour, and nothing in the UI says so")


@pytest.mark.parametrize("finish,spec_kwargs", [
    ("enig", {"leds": [{"x": 6.5, "y": 6.0, "size": "0603"}]}),
    ("hasl", {"leds": [{"x": 10.0, "y": 10.0, "size": "1206", "side": "back"}]}),
    ("hasl", {"leds": [], "mask_color": "black"}),
    ("enig", {"leds": [{"x": 8.0, "y": 8.0, "size": "3mm"}]}),
])
def test_the_stackup_tells_the_fab_the_surface_finish_the_user_picked(
        board, finish, spec_kwargs):
    """ENIG and HASL are different prices and different solderability.

    Shipping the wrong one is a purchasing error the user cannot see: the board
    opens, the 3D view is unchanged, and only the fab quote differs.
    """
    b = assert_parses(board(finish=finish, **spec_kwargs))
    stack = _kid(b.setup, "stackup")
    assert stack is not None, (
        "no (stackup ...) block; the fab is told nothing about the finish")
    got = str(_val(stack, "copper_finish"))
    # Stated independently of pcb's own mapping (D2), which means these two
    # strings had to be read off a real board once rather than imported. Worth
    # knowing that the first draft guessed "HAL" and the artifact said
    # "HAL lead-free"; an assertion that had sourced the value from `pcb` would
    # have agreed with the code and told us nothing.
    want = {"enig": "ENIG", "hasl": "HAL lead-free"}[finish]
    assert got == want, (
        f"user picked {finish!r} so the stackup should say {want!r}, not "
        f"{got!r}; the fab quotes and plates the wrong finish")
