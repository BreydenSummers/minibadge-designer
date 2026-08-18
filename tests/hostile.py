"""Hostile-payload corpus for the HTTP boundary (`/generate`, `/outline`, `/model3d`).

**The one rule.** Every case in this corpus asserts exactly two things:

1. the request did not crash the server (no 5xx, no propagated exception), and
2. it finished inside its wall-clock budget.

Nothing else. Never a specific status number, never an error string, never a
response length. That is what lets this file grow without bound: adding a
payload can only ever catch a new crash or a new hang; it can never go red
because someone legitimately changed what the app accepts or rejects.

The rule is enforced by the API, not by good intentions: `probe()` performs the
request, makes those two assertions itself, and returns `None`. A test module
driving this corpus never sees a status code, so it cannot assert on one.
`Case` objects carry payloads only; they carry no expected status.

**Known-crashing cases are marked `xfail(strict=True)`.** A payload that 500s
against a defect nobody is fixing right now carries `crashes={route: "#N"}`;
`route_cases()` turns that into a strict xfail naming the defect, so the case
flips to a *failure* the moment the defect is fixed and the marker cannot
outlive it. Do not weaken an assertion to make the suite green; either fix the
defect and drop the marker, or leave both. `DEFECTS` is empty today: the five
defects the corpus pinned are repaired, and all seventeen markers naming them
went red as `[XPASS(strict)]` on cue. The payloads stayed.

Usage (the whole consuming test module)::

    import hostile
    import pytest

    @pytest.mark.webapp
    @pytest.mark.parametrize("route,case", hostile.route_cases())
    def test_hostile_input_never_crashes(client, route, case):
        hostile.probe(client, route, case)

`client` is the Flask test client from `conftest.py`. This module deliberately
imports nothing from the app or from `conftest`, so it stays usable against a
live server binding too (anything with a `.post(path, data=..., content_type=...)`).
"""

from __future__ import annotations

import io
import json
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache

import pytest

# ---------------------------------------------------------------------------
# Routes and budgets
# ---------------------------------------------------------------------------

GENERATE = "/generate"
OUTLINE = "/outline"
MODEL3D = "/model3d"

#: Routes probed by default. `/model3d` is opt-in: it shares `_generate_impl`
#: with `/generate`, so it adds nothing at the parse layer, and every payload it
#: *accepts* pays a multi-second kicad-cli export. Cases that exercise the
#: render path itself set `model3d=True` and are probed there as well.
DEFAULT_ROUTES = (GENERATE, OUTLINE)

#: Ceiling on a hang, not a benchmark. A slow CI box must not turn this red;
#: the point is that a request which takes half a minute has failed the user
#: whatever it eventually returns. Individual cases tighten it where the
#: tighter number *documents an intent* (see SVG_BUDGET_S).
DEFAULT_BUDGET_S = 20.0

#: A board-shape or artwork SVG is a logo, not a mesh. Auto-traced art routinely
#: carries tens of thousands of points, and a quarter-megabyte file has to come
#: back while the user is still looking at the preview; /outline runs on every
#: edit. A hang is invisible to a status check, so for these cases the clock is
#: the assertion.
#:
#: Measured before `webapp.MAX_SVG_COMPLEXITY` landed, every one returning 200:
#: an N-vertex <path> cost 1k = 0.05 s, 20k = 1.3 s, 40k = 5.2 s, 80k = 25.7 s,
#: 200k = 493 s, and separate cubic-Bezier segments cost ~4 ms each on top
#: (4 000 of them = 15.5 s at 200 KB). With the cap, the over-limit cases below
#: are refused (or, when the client sent a `*_raster`, quietly served from it)
#: in well under 0.2 s, so this budget keeps 10x+ headroom on every passing case
#: while sitting far under the seconds a regression would cost.
SVG_BUDGET_S = 2.0


# ---------------------------------------------------------------------------
# Live defects, keyed by the id in the decisions-file ledger, referenced by
# `Case.crashes` and turned into strict xfail markers by `route_cases()`.
#
# It is empty, and that is the point of the mechanism rather than a sign the
# mechanism is unused. The five entries it used to hold (#1 novia NameError,
# #5 uncapped SVG complexity, #6 NaN geometry raised outside every `try`, #12
# non-object `params`, #13 non-iterable `pins`/`rows`) were all repaired in
# webapp.py, and every one of the seventeen `crashes=` markers naming them
# failed the run with `[XPASS(strict)]` the moment the fixes landed, which is
# exactly the property that stops a marker outliving its defect. The payloads
# stayed; only the markers went.
#
# Add an entry here, and a `crashes=` on the case, when a payload is found to
# 500 and the fix is not being taken now. Never weaken an assertion instead.
# ---------------------------------------------------------------------------

DEFECTS: Mapping[str, str] = {}


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class Case:
    """One hostile payload. Carries no expected status, on purpose."""

    id: str
    #: Params object, serialised with json.dumps. Mutually exclusive with `raw`.
    params: dict | None = None
    #: Exact bytes of the `params` form field, bypassing json.dumps. Use for
    #: payloads Python cannot express as a dict (NaN, Infinity, malformed JSON).
    raw: str | None = None
    #: Omit the `params` form field entirely.
    omit_params: bool = False
    #: (form field, filename, zero-arg byte factory). Factories are re-invoked
    #: per request so a consumed stream never leaks between routes.
    files: Sequence[tuple[str, str, Callable[[], bytes]]] = ()
    budget: float = DEFAULT_BUDGET_S
    #: route -> DEFECTS key, for routes where this payload crashes today.
    crashes: Mapping[str, str] = field(default_factory=dict)
    #: Also probe `/model3d` (costs a kicad-cli export when accepted).
    model3d: bool = False
    #: Measured over the fast-tier budget: gets `@pytest.mark.slow`.
    slow: bool = False

    def form_data(self) -> dict:
        data: dict = {}
        if not self.omit_params:
            data["params"] = self.raw if self.raw is not None else json.dumps(self.params or {})
        for fieldname, filename, factory in self.files:
            data[fieldname] = (io.BytesIO(factory()), filename)
        return data

    def routes(self) -> tuple[str, ...]:
        return (*DEFAULT_ROUTES, MODEL3D) if self.model3d else DEFAULT_ROUTES


# ---------------------------------------------------------------------------
# The only assertion this corpus is allowed to make
# ---------------------------------------------------------------------------

def probe(client, route: str, case: Case) -> None:
    """Send one hostile case to one route and assert survival.

    Asserts exactly: the response is not a 5xx (an exception propagated out of
    the app counts as a crash), and the request finished inside `case.budget`.

    Returns None deliberately: the caller never gets a status code, so no test
    over this corpus can grow an assertion about one.
    """
    data = case.form_data()
    started = time.monotonic()
    try:
        resp = client.post(route, data=data, content_type="multipart/form-data")
    except Exception:  # noqa: BLE001; PROPAGATE_EXCEPTIONS=True raises instead of 500ing
        elapsed = time.monotonic() - started
        status, detail = 500, traceback.format_exc()[-700:]
    else:
        elapsed = time.monotonic() - started
        status = resp.status_code
        # A success body is a zip or a GLB: only decode when we are about to
        # complain, and never let the decode itself masquerade as a crash.
        detail = (resp.get_data()[:300].decode("utf-8", "replace")
                  if status >= 500 else "")

    assert status < 500, (
        f"{route} [{case.id}] crashed the server: status {status}\n{detail}")
    assert elapsed <= case.budget, (
        f"{route} [{case.id}] took {elapsed:.1f}s, budget {case.budget}s; "
        f"a hang is invisible to a status check, so the budget is the assertion")


def route_cases(routes: Sequence[str] | None = None,
                cases: Sequence[Case] | None = None) -> list:
    """pytest params of (route, case), carrying their own marks.

    Each param arrives already wearing what it needs, so the consuming module
    stays a three-liner:

    * `xfail(strict=True)` naming the defect, for routes where the payload
      crashes today (`Case.crashes`);
    * `slow`, for the handful of cases measured over the fast-tier budget;
    * `kicad` + `needs("kicad")` on every `/model3d` param: without kicad-cli
      that route answers an accepted design with a deliberate **501**, which is
      not a crash but would trip the `< 500` rule. Gating is the honest fix;
      loosening the rule to "not a 500, except 501" is not.

    Pass `routes` to restrict the sweep (e.g. `(GENERATE,)` for a fast subset).
    """
    out = []
    for case in (cases if cases is not None else CORPUS):
        for route in case.routes():
            if routes is not None and route not in routes:
                continue
            marks = []
            defect = case.crashes.get(route)
            if defect is not None:
                marks.append(pytest.mark.xfail(strict=True, reason=DEFECTS[defect]))
            if case.slow or route == MODEL3D:
                marks.append(pytest.mark.slow)
            if route == MODEL3D:
                marks.extend([pytest.mark.kicad, pytest.mark.needs("kicad")])
            out.append(pytest.param(route, case, id=f"{route.lstrip('/')}-{case.id}",
                                    marks=marks))
    return out


# ---------------------------------------------------------------------------
# Payload builders (lazy + memoised: the big ones cost real memory)
# ---------------------------------------------------------------------------

@cache
def png_bytes(w: int = 120, h: int = 120) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), "white")
    ImageDraw.Draw(img).ellipse((w // 6, h // 6, w * 5 // 6, h * 5 // 6), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@cache
def jpeg_bytes(w: int = 80, h: int = 80) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, "JPEG")
    return buf.getvalue()


@cache
def svg_bytes(points: int = 0) -> bytes:
    if not points:
        return (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                b'<rect width="10" height="10" fill="#000"/></svg>')
    d = b" ".join(b"L%d %d" % (i % 100, (i * 7) % 100) for i in range(points))
    return (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            b'<path fill="#000" d="M0 0 ' + d + b' Z"/></svg>')


@cache
def cubic_svg(segments: int = 1000) -> bytes:
    """Separate cubic-Bezier paths: the costliest shape per byte, because each
    long curve flattens to up to 256 chords."""
    ps = b"".join(b'<path fill="#000" d="M%d %d C%d %d %d %d %d %d Z"/>'
                  % (i % 97, (i * 3) % 97, i % 97, (i * 13) % 97,
                     (i * 7) % 97, (i * 29) % 97, (i * 3) % 97, (i * 11) % 97)
                  for i in range(segments))
    return (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            + ps + b"</svg>")


@cache
def nested_svg(depth: int = 5000) -> bytes:
    return (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            + b"<g>" * depth
            + b'<rect width="10" height="10" fill="#000"/>'
            + b"</g>" * depth + b"</svg>")


@cache
def xxe_svg() -> bytes:
    return (b'<?xml version="1.0"?><!DOCTYPE svg ['
            b'<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            b'<rect width="10" height="10" fill="#000"/><desc>&xxe;</desc></svg>')


@cache
def billion_laughs_svg() -> bytes:
    ents = b"".join(
        b'<!ENTITY l%d "&l%d;&l%d;&l%d;&l%d;&l%d;&l%d;&l%d;&l%d;&l%d;&l%d;">'
        % (i, i - 1, i - 1, i - 1, i - 1, i - 1, i - 1, i - 1, i - 1, i - 1, i - 1)
        for i in range(1, 10))
    return (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY l0 "haha">' + ents + b"]>"
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            b'<rect width="10" height="10" fill="#000"/><desc>&l9;</desc></svg>')


def blank() -> bytes:
    return b""


def junk() -> bytes:
    return b"this is definitely not an image"


def huge_png() -> bytes:
    return png_bytes(8000, 8000)


def art_file(i: int = 0, name: str = "a.png", factory: Callable[[], bytes] = png_bytes):
    return (f"art{i}", name, factory)


def shape_file(name: str = "s.png", factory: Callable[[], bytes] = png_bytes):
    return ("shape", name, factory)


# Shorthand payload fragments.
def L(**kw) -> dict:
    return dict({"x": 10, "y": 10, "color": "red"}, **kw)


def T(**kw) -> dict:
    return dict({"x": 10, "y": 10, "text": "hi", "size": 2}, **kw)


def A(**kw) -> dict:
    return dict({"material": "silk", "cx": 10.16, "cy": 10.16, "w": 10}, **kw)


def IMAGE_SHAPE(**kw) -> dict:
    return dict({"mode": "image", "cx": 10.16, "cy": 10.16, "w": 18}, **kw)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

# -- the params envelope ----------------------------------------------------
ENVELOPE = [
    Case("params-omitted", omit_params=True, model3d=True),
    Case("params-empty-string", raw=""),
    Case("params-not-json", raw="not json at all"),
    Case("params-json-null", raw="null",
         model3d=True),
    Case("params-json-list", raw="[]",
         model3d=True),
    Case("params-json-number", raw="42"),
    Case("params-json-string", raw='"hello"'),
    Case("params-json-true", raw="true"),
    Case("params-empty-object", params={}),
    Case("params-truncated-json", raw='{"name": "x"'),
    Case("params-nested-50-deep", raw="{\"a\":" * 50 + "1" + "}" * 50),
    Case("params-unknown-keys", params={"totally": "unknown", "keys": [1, 2, 3]}),
]

# -- connector pins (_parse_pins is called outside every try) ----------------
PINS = [
    Case("pins-int-5", params={"pins": 5},
         model3d=True),
    Case("rows-int-5", params={"rows": 5}),
    Case("pins-string", params={"pins": "abc"}),
    Case("pins-dict", params={"pins": {"1": True}}),
    Case("pins-objects", params={"pins": [{}]}),
    Case("pins-unknown-values", params={"pins": ["99", "abc", None]}),
    Case("pins-empty-with-leds", params={"pins": [], "leds": [L()]}),
    Case("pins-empty-no-leds", params={"pins": []}),
    Case("rows-substring", params={"rows": "topbottom"}),
    Case("rows-dict", params={"rows": {"top": True}}),
]

# -- out-of-range numbers, NaN and Infinity ---------------------------------
NUMERIC = [
    Case("led-x-nan", raw='{"leds":[{"x":NaN,"y":10,"color":"red"}]}'),
    Case("led-rot-nan", raw='{"leds":[{"x":10,"y":10,"color":"red","rot":NaN}]}'),
    Case("led-adv-rrot-nan",
         raw='{"leds":[{"x":10,"y":10,"color":"red","adv":{"rrot":NaN}}]}'),
    Case("text-rot-nan",
         raw='{"texts":[{"x":10,"y":10,"text":"hi","size":2,"rot":NaN}]}'),
    Case("shape-circle-nan",
         raw='{"shape":{"mode":"custom","elements":'
             '[{"kind":"circle","cx":NaN,"cy":10,"w":8,"op":"add"}]}}'),
    Case("led-x-infinity", raw='{"leds":[{"x":Infinity,"y":10,"color":"red"}]}'),
    Case("led-x-neg-infinity", raw='{"leds":[{"x":-Infinity,"y":10,"color":"red"}]}'),
    Case("led-x-1e308", params={"leds": [L(x=1e308, y=-99999)]}),
    Case("led-x-10e40", params={"leds": [L(x=10 ** 40, y=10 ** 40)]}),
    Case("text-size-zero", params={"texts": [T(size=0)]}),
    Case("text-size-negative", params={"texts": [T(size=-5)]}),
    Case("text-size-nan", raw='{"texts":[{"x":10,"y":10,"text":"hi","size":NaN}]}'),
    Case("art-w-huge", params={"art": [A(w=100000)]}, files=[art_file()]),
    Case("art-w-zero", params={"art": [A(w=0)]}, files=[art_file()]),
    Case("art-w-negative", params={"art": [A(w=-10)]}, files=[art_file()]),
    Case("art-threshold-huge", params={"art": [A(threshold=99999)]}, files=[art_file()]),
    Case("art-sides-1e9", params={"art": [A(kind="polygon", sides=10 ** 9)]}),
    Case("art-rgb-out-of-range",
         params={"art": [A(palette=[{"rgb": [99999, -5, 3], "material": "silk"}])]},
         files=[art_file()]),
]

# -- wrong types where a list/dict/number was expected ----------------------
WRONG_TYPES = [
    Case("leds-string", params={"leds": "abc"}),
    Case("leds-dict", params={"leds": {"a": 1}}),
    Case("leds-null-entry", params={"leds": [None]}),
    Case("leds-string-entry", params={"leds": ["s"]}),
    Case("leds-nested-list", params={"leds": [[1, 2]]}),
    Case("led-x-string", params={"leds": [L(x="abc")]}),
    Case("led-nodes-scalar", params={"leds": [L(nodes=5)]}),
    Case("led-adv-string", params={"leds": [L(adv="str")]}),
    Case("led-clk-dict", params={"leds": [L(clk={"a": 1})]}),
    Case("led-cnodes-scalar", params={"leds": [L(clk=True, cnodes=7)]}),
    Case("clk-string", params={"leds": [L(clk=True)], "clk": "abc"}),
    Case("clk-list", params={"leds": [L(clk=True)], "clk": [1, 2]}),
    Case("clk-x-string", params={"leds": [L(clk=True)],
                                 "clk": {"x": "abc", "y": 5}}),
    Case("clk-x-nan", params={"leds": [L(clk=True)],
                              "clk": {"x": float("nan"), "y": float("nan")}}),
    Case("clk-rot-huge", params={"leds": [L(clk=True)],
                                 "clk": {"x": 5, "y": 5, "rot": 1e308}}),
    Case("clk-jumper-dict", params={"leds": [L(clk=True)],
                                    "clk": {"jumper": {"a": 1}}}),
    Case("clk-side-garbage", params={"leds": [L(clk=True)],
                                     "clk": {"side": ["x"], "via": "no"}}),
    Case("clk-nodes-garbage", params={"leds": [L(clk=True)],
                                      "clk": {"nodes": [[1], "x", [None, 2]],
                                              "v3nodes": 5}}),
    Case("clk-v3pin-garbage", params={"leds": [L(clk=True)],
                                      "clk": {"side": "back", "via": False,
                                              "v3pin": {"a": 1}}}),
    Case("texts-string", params={"texts": "abc"}),
    Case("texts-null-entry", params={"texts": [None]}),
    Case("text-body-dict", params={"texts": [T(text={"a": 1})]}),
    Case("text-size-string", params={"texts": [T(size="big")]}),
    Case("art-dict", params={"art": {"a": 1}}),
    Case("art-null-entry", params={"art": [None]}, files=[art_file()]),
    Case("art-overrides-strings", params={"art": [A(overrides=["x"])]}, files=[art_file()]),
    Case("art-palette-null", params={"art": [A(palette=None)]}, files=[art_file()]),
    Case("name-dict", params={"name": {"a": 1}}),
    Case("name-null", params={"name": None}),
    Case("finish-dict", params={"finish": {"a": 1}}),
    Case("mask-color-list", params={"mask_color": [1, 2, 3]}),
    Case("shape-string", params={"shape": "abc"}),
    Case("shape-list", params={"shape": [1, 2]}),
    Case("shape-elements-string", params={"shape": {"mode": "custom", "elements": "abc"}}),
]

# -- empty and enormous arrays ---------------------------------------------
ARRAYS = [
    Case("arrays-all-empty", params={"leds": [], "texts": [], "art": [], "pins": []}),
    Case("shape-elements-empty", params={"shape": {"mode": "custom", "elements": []}}),
    Case("leds-500", params={"leds": [L(x=2 + (i % 16), y=2 + (i // 16) % 16)
                                      for i in range(500)]}, slow=True),
    Case("texts-100", params={"texts": [T(x=2 + (i % 18), y=2 + (i // 18), text=f"t{i}")
                                        for i in range(100)]}),
    Case("art-30-declared", params={"art": [A() for _ in range(30)]},
         files=[art_file(i, f"{i}.png") for i in range(30)]),
    Case("led-nodes-500", params={"leds": [L(novia=True,
                                             nodes=[[10 + i * 0.01, 10] for i in range(500)])]}),
    Case("shape-elements-200",
         params={"shape": {"mode": "custom",
                           "elements": [{"kind": "circle", "cx": 10, "cy": 10,
                                         "w": 5, "op": "add"} for _ in range(200)]}}),
    Case("palette-500", params={"art": [A(palette=[{"rgb": [i % 256, 0, 0],
                                                    "material": "silk"}
                                                   for i in range(500)])]},
         files=[art_file()]),
]

# -- unknown enum values ----------------------------------------------------
ENUMS = [
    Case("led-color-unknown", params={"leds": [L(color="plutonium")]}),
    Case("led-size-unknown", params={"leds": [L(size="9999")]}),
    Case("led-layout-unknown", params={"leds": [L(layout="sideways")]}),
    Case("text-font-unknown", params={"texts": [T(font="nosuchfont")]}),
    Case("text-material-unknown", params={"texts": [T(material="plutonium")]}),
    Case("art-material-unknown", params={"art": [A(material="plutonium")]}, files=[art_file()]),
    Case("art-side-unknown", params={"art": [A(side="plutonium")]}, files=[art_file()]),
    Case("art-kind-unknown", params={"art": [A(kind="plutonium")]}, files=[art_file()]),
    Case("shape-mode-unknown", params={"shape": {"mode": "plutonium"}}),
    Case("finish-unknown", params={"finish": "chrome"}),
]

# -- strings: unicode, control chars, traversal, s-expression injection -----
STRINGS = [
    Case("name-traversal", params={"name": "../../../../etc/passwd"}),
    Case("name-absolute-path", params={"name": "/etc/passwd"}),
    Case("name-nul-byte", params={"name": "a\x00b"}),
    Case("name-emoji", params={"name": "\U0001f525badge\U0001f480"}),
    Case("name-punctuation-only", params={"name": "!!!!!!"}),
    Case("name-5000-chars", params={"name": "A" * 5000}),
    # s-expression injection. `name` is whitelisted by _slug and text content is
    # escaped by pcb._esc, both verified balanced. `mask_color` is neither
    # (defect #4): it does not crash, it ships a 200 whose board KiCad cannot
    # open, which only a balanced-s-expr assertion on the zip can see; the
    # corpus's job here is just to prove none of the three 500s.
    Case("name-sexpr-injection", params={"name": 'x") (gr_text "PWN'}),
    Case("text-sexpr-injection", params={"texts": [T(text='x") (gr_text "PWN')]}),
    Case("mask-color-sexpr-injection", params={"mask_color": 'green")'}),
    Case("mask-color-trailing-backslash", params={"mask_color": "g\\"}),
    Case("mask-color-unknown", params={"mask_color": "plutonium"}),
    Case("text-control-chars", params={"texts": [T(text="\x01\x02\x03\x7f")]}),
    Case("text-rtl-and-cjk", params={"texts": [T(text="\u202e\u0645\u0631\u062d\u0628\u0627\u4f60\u597d")]}),
    Case("text-5000-chars", params={"texts": [T(text="A" * 5000)]}),
    Case("font-traversal", params={"texts": [T(font="../../etc/passwd")]}),
    Case("font-int", params={"texts": [T(font=5)]}),
]

# -- uploads ----------------------------------------------------------------
UPLOADS = [
    Case("art-zero-byte", params={"art": [A()]}, files=[art_file(0, "z.png", blank)]),
    Case("art-not-an-image", params={"art": [A()]}, files=[art_file(0, "x.png", junk)]),
    Case("art-png-named-svg", params={"art": [A()]}, files=[art_file(0, "x.svg", png_bytes)]),
    Case("art-jpeg-named-png", params={"art": [A()]}, files=[art_file(0, "x.png", jpeg_bytes)]),
    Case("art-svg-named-png", params={"art": [A()]}, files=[art_file(0, "x.png", svg_bytes)]),
    Case("art-empty-filename", params={"art": [A()]}, files=[art_file(0, "", png_bytes)]),
    Case("art-8000px", params={"art": [A()]}, files=[art_file(0, "big.png", huge_png)],
         slow=True),
    # More art files than declared, and fewer: the fewer case is a silent
    # partial success today (lane-02 F7), which is a 200; the corpus only says
    # it must not crash.
    Case("art-3-declared-1-sent", params={"art": [A(), A(), A()]}, files=[art_file()]),
    Case("art-1-declared-3-sent", params={"art": [A()]},
         files=[art_file(0), art_file(1, "b.png"), art_file(2, "c.png")]),
    Case("art-declared-none-sent", params={"art": [A()]}),
    Case("shape-image-garbage", params={"shape": IMAGE_SHAPE()},
         files=[shape_file("s.png", junk)]),
    Case("shape-image-zero-byte", params={"shape": IMAGE_SHAPE()},
         files=[shape_file("s.png", blank)]),
    Case("shape-image-no-upload", params={"shape": IMAGE_SHAPE()}),
]

# -- SVG: security invariants and the complexity budget ---------------------
SVG = [
    Case("svg-xxe-file-read", params={"art": [A()]},
         files=[art_file(0, "x.svg", xxe_svg)], budget=SVG_BUDGET_S),
    Case("svg-billion-laughs", params={"art": [A()]},
         files=[art_file(0, "x.svg", billion_laughs_svg)], budget=SVG_BUDGET_S),
    Case("svg-5000-nested-groups", params={"art": [A()]},
         files=[art_file(0, "x.svg", nested_svg)], budget=SVG_BUDGET_S),
    Case("svg-1k-point-art", params={"art": [A()]},
         files=[art_file(0, "p.svg", lambda: svg_bytes(1000))], budget=SVG_BUDGET_S),
    # The four cases below are the whole measurement of the complexity cap.
    # 40k vertices is 272 KB (an ordinary auto-traced logo, far under the
    # 24 MiB upload cap) and used to chew ~5.3 s on both routes; 60k ran ~12 s
    # and 200k ran 493 s. The curve case is the shape that costs most per byte.
    #
    # Payloads stay under 500 KB deliberately: over that, werkzeug's *test
    # client* spools the request body to a temp file it never closes, and the
    # resulting ResourceWarning is raised against whichever test next triggers
    # a GC. That is a harness artefact, but it makes any test that carries such
    # a payload a false-positive generator for its neighbours.
    Case("svg-40k-point-shape", params={"shape": IMAGE_SHAPE()},
         files=[shape_file("s.svg", lambda: svg_bytes(40000))],
         budget=SVG_BUDGET_S),
    Case("svg-60k-point-shape", params={"shape": IMAGE_SHAPE()},
         files=[shape_file("s.svg", lambda: svg_bytes(60000))],
         budget=SVG_BUDGET_S),
    Case("svg-60k-point-art", params={"art": [A()]},
         files=[art_file(0, "p.svg", lambda: svg_bytes(60000))],
         budget=SVG_BUDGET_S),
    # Over the cap AND carrying the raster the UI always sends: the exact
    # pipeline is skipped and the raster is used, so this must be fast too.
    Case("svg-60k-point-shape-with-raster", params={"shape": IMAGE_SHAPE()},
         files=[shape_file("s.svg", lambda: svg_bytes(60000)),
                ("shape_raster", "s.png", png_bytes)],
         budget=SVG_BUDGET_S),
    # Cubic segments, not vertices: 4 000 of them is only 200 KB but flattens
    # to up to a quarter-million chords, which is why the cap weights curves.
    Case("svg-4k-cubic-art", params={"art": [A()]},
         files=[art_file(0, "c.svg", lambda: cubic_svg(4000))],
         budget=SVG_BUDGET_S),
    Case("svg-4k-cubic-shape", params={"shape": IMAGE_SHAPE()},
         files=[shape_file("c.svg", lambda: cubic_svg(4000))],
         budget=SVG_BUDGET_S),
]

# -- via-less LEDs driven to their failure path (defect #1) ----------------
NOVIA = [
    Case("novia-bent-nodes",
         params={"leds": [L(novia=True, nodes=[[10.2, 10.2], [10.3, 10.25]])]}, model3d=True),
    Case("novia-crowded-row",
         params={"leds": [L(x=3 + 3.2 * i, y=10, novia=True) for i in range(6)]}, slow=True),
    Case("novia-two-rows",
         params={"leds": [L(x=4 + 4 * i, y=5, novia=True) for i in range(4)]
                         + [L(x=4 + 4 * i, y=15, novia=True) for i in range(4)]}, slow=True),
    Case("novia-top-pins-only",
         params={"pins": ["1", "2", "7", "8"],
                 "leds": [L(x=3 + 3.2 * i, y=16, novia=True) for i in range(5)]}, slow=True),
]

CORPUS: tuple[Case, ...] = tuple(
    ENVELOPE + PINS + NUMERIC + WRONG_TYPES + ARRAYS + ENUMS + STRINGS
    + UPLOADS + SVG + NOVIA
)

CATEGORIES: Mapping[str, Sequence[Case]] = {
    "envelope": ENVELOPE, "pins": PINS, "numeric": NUMERIC,
    "wrong_types": WRONG_TYPES, "arrays": ARRAYS, "enums": ENUMS,
    "strings": STRINGS, "uploads": UPLOADS, "svg": SVG, "novia": NOVIA,
}
