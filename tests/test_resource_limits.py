"""The ceilings that keep one upload from spending the whole server.

Every route here is unauthenticated and every one decodes a file the caller
chose, so "how much of this machine can one request buy" needs an answer. Four
of the guards below were added after measuring that it did not have one, and
each test states the measurement it exists to hold.

These are *relationship* tests, not benchmarks: a cost score against its own
declared ceiling, a refusal against an acceptance of the same shape, one
constant against another. The wall-clock assertions live in `hostile.py`, where
the budgets carry 10x headroom on purpose.
"""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image

from minibadge_designer import logo, webapp

# Grid width the board outline classifies at. Stated here rather than imported
# so that raising webapp.OUTLINE_COLS cannot silently rescale what these tests
# think a "dense" image is.
OUTLINE_GRID_COLS = 480

# The pixel ceiling, restated independently of logo.MAX_INPUT_PIXELS: a test
# that reads the production constant cannot notice an edit to it. 24 Mpx is
# ~6000x4000, and the grid above means a badge never needs more than a few.
PIXEL_CAP = 24_000_000


def _png(w: int, h: int, pattern=None) -> bytes:
    """A PNG of exactly w x h, optionally with a `pattern(xs, ys) -> mask`.

    Vectorised through numpy rather than a per-pixel Python loop: these
    fixtures need to be at least as wide as the 480-cell outline grid to keep
    their structure through the resample, and a 600x600 double loop is most of
    a second all by itself.
    """
    if pattern is None:
        arr = np.full((h, w), 255, dtype=np.uint8)
    else:
        ys, xs = np.indices((h, w))
        arr = np.where(pattern(xs, ys), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, "PNG")
    return buf.getvalue()


def _checkerboard(side: int = 600) -> bytes:
    """Alternating single pixels: the shape that maximises rectangle count.

    `grid_to_rects` merges each row's horizontal runs, so a checkerboard is the
    worst case by construction -- every cell stays its own rectangle. It has to
    be wider than the 480-cell grid it lands on, or the resample blurs the
    pattern into flat grey and there is nothing left to count.
    """
    return _png(side, side, lambda xs, ys: (xs + ys) % 2 == 0)


def _use_bomb(depth: int, container: str = "g") -> bytes:
    """A container holding two `<use>` of the previous one, nested `depth` deep.

    2**depth shapes from a payload that grows linearly, which is why this is
    scored before svgelements parses it: the expansion happens inside
    `SVG.parse`, so anything measured afterwards is measured too late.

    `container` is a parameter because `<symbol>` and `<g>` take different
    branches through the scorer. A `<symbol>` paints nothing where it sits and
    everything through a `<use>`, so scoring a `<use>` by what its target
    *draws in place* reads zero for the whole sprite -- the idiom every icon
    set on the web is built from.
    """
    defs = "".join(
        f'<{container} id="s{i}"><use href="#s{i - 1}" x="0.5"/>'
        f'<use href="#s{i - 1}" x="1"/></{container}>'
        for i in range(1, depth + 1))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000">'
            f'<defs><{container} id="s0"><circle cx="5" cy="5" r="3" fill="#000"/>'
            f'</{container}>{defs}</defs><use href="#s{depth}"/></svg>').encode()


def _repeated(tag: str, n: int) -> bytes:
    """`n` copies of one simple shape -- no `d`, no `points` to score."""
    body = "".join(
        f'<{tag} cx="{(i * 7) % 900}" cy="{(i * 13) % 900}" r="6" fill="#000"/>'
        if tag in ("circle", "ellipse") else
        f'<{tag} x="{(i * 7) % 900}" y="{(i * 11) % 900}" width="9" height="9" fill="#000"/>'
        for i in range(n))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000">'
            f'{body.decode() if isinstance(body, bytes) else body}</svg>').encode()


# ---------------------------------------------------------------------------
# The pixel ceiling on uploads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,meta_key,meta", [
    ("art0", "art", [{"w": 19, "rot": 37, "mode": "threshold", "material": "silk"}]),
    ("shape0", "shape", {"mode": "custom",
                         "elements": [{"kind": "image", "cx": 10, "cy": 10, "w": 100}]}),
])
@pytest.mark.webapp
def test_an_image_over_the_pixel_cap_is_refused_and_told_why(
        post_generate, field, meta_key, meta):
    """An oversized upload gets a message naming its size, not a dead worker.

    Pillow's own bomb check only *warns* below 179 Mpx, so a 20 KB / 169 Mpx
    PNG used to be decoded in full: RGBA at source resolution, then an expanded
    rotate on top, measured at 3 736 MB of peak RSS for one layer. One request
    may carry 40 image fields, and the container is now capped at 3 GB, so the
    worker would be killed mid-request and the download would simply never
    arrive.

    Both entry points are covered because they are separate call sites into
    `classify_image` -- artwork layers and board-outline elements -- and the
    board-shape path is the one that classifies at the wider grid.
    """
    side = 13_000  # 169 Mpx, comfortably over the cap, ~20 KB compressed
    assert side * side > PIXEL_CAP, "the fixture must actually exceed the cap"
    resp = post_generate({meta_key: meta}, files={field: (_png(side, side), "big.png")})
    body = resp.get_json()
    assert resp.status_code == 400, (
        f"a {side}x{side} upload was accepted on {field}; it must be refused "
        f"before it is decoded (got {resp.status_code})")
    assert "megapixel" in body["error"], (
        f"the refusal must tell the user what is wrong with their file, "
        f"got {body['error']!r}")
    resp.close()


@pytest.mark.webapp
def test_a_large_but_usable_image_is_still_accepted(post_generate):
    """The cap turns away the absurd, not the merely big.

    A 5000x4000 export off a camera or a vector editor is a normal thing to
    drop on a badge, and it sits under the cap while still being far over the
    working size the pipeline reduces to. If this goes red the cap has been
    tightened into people's real artwork.
    """
    art = [{"w": 19, "rot": 37, "mode": "threshold", "material": "silk"}]
    big = _png(5000, 4000)
    assert 5000 * 4000 < PIXEL_CAP, "the fixture must sit under the cap"
    resp = post_generate({"art": art}, files={"art0": (big, "photo.png")})
    assert resp.status_code == 200, (
        f"a 20 Mpx upload was refused: {resp.get_json()}")
    resp.close()


def test_the_pixel_cap_is_read_from_the_header_not_the_pixels():
    """The refusal happens on the size fields, before any row is decoded.

    This is the whole reason the guard sits where it does. Decoding a PNG is
    all-or-nothing: by the time a single pixel is readable the entire raster is
    already resident, so a check that needs pixel data cannot be a memory
    guard. Proved with a file whose IHDR is valid and whose image data is
    garbage -- it is undecodable, and it must still be refused *for its size*,
    which can only happen if nothing tried to decode it.
    """
    truncated = _png(13_000, 13_000)[:200]  # header intact, image data gone
    with pytest.raises(logo.ImageTooLarge):
        logo._open_rgba(truncated)


# ---------------------------------------------------------------------------
# The rectangle ceiling on board outlines
# ---------------------------------------------------------------------------

@pytest.mark.slow  # 0.46 s: the contrast case is an accepted 480-cell outline
@pytest.mark.webapp
def test_an_outline_too_speckled_to_route_is_refused_not_ground_through(
        post_generate):
    """Board art that cannot be cut is answered, not computed for a minute.

    A 1-pixel checkerboard classifies to 64 620 separate rectangles on the
    outline grid, and unioning that many boxes costs seconds to produce an
    outline no router could follow: 0.25 mm islands against a ~2 mm bit. The
    contrast case is the same request with the same dimensions carrying a real
    silhouette (610 rectangles), which must still be accepted -- the ceiling is
    on how broken-up the shape is, never on how big it is.
    """
    shape = {"mode": "custom",
             "elements": [{"kind": "image", "cx": 10, "cy": 10, "w": 100,
                           "threshold": 128}]}
    speckled = post_generate({"shape": shape},
                             files={"shape0": (_checkerboard(), "speckle.png")})
    assert speckled.status_code == 400, (
        "a checkerboard outline was accepted; it must be refused as uncuttable")
    assert "detail" in speckled.get_json()["error"], (
        f"the refusal must name the problem, got {speckled.get_json()['error']!r}")
    speckled.close()

    with open("scripts/help_fixtures/helmet.png", "rb") as fh:
        real = post_generate({"shape": shape}, files={"shape0": (fh.read(), "logo.png")})
    assert real.status_code == 200, (
        f"a real silhouette at the same size was refused: {real.get_json()}")
    real.close()


@pytest.mark.slow  # 1.9 s: proving the rule needs a request that is ACCEPTED
@pytest.mark.webapp
def test_one_outline_allowance_is_shared_across_all_its_elements(post_generate):
    """Twelve elements cannot each spend the ceiling.

    A composition may carry up to twelve image elements, so a per-element
    allowance would multiply by twelve -- which is how the original cost went
    from one slow request to a five-minute one. The budget belongs to the
    outline computation, not to the element, and that is what this holds.
    """
    # A 16 px dot grid, measured at 1 857 rectangles on the outline grid: three
    # copies sit inside the ceiling and four do not. The density is identical in
    # both requests, so element COUNT is the only thing that changes -- which is
    # precisely the rule under test.
    dots = _png(600, 600, lambda xs, ys: (xs % 16 < 4) & (ys % 16 < 4))

    def _request(count):
        elements = [{"kind": "image", "cx": 10, "cy": 10, "w": 100,
                     "threshold": 128} for _ in range(count)]
        return post_generate(
            {"shape": {"mode": "custom", "elements": elements}},
            files={f"shape{i}": (dots, "d.png") for i in range(count)})

    few = _request(3)
    assert few.status_code == 200, (
        f"three copies must fit, or this test proves nothing: {few.get_json()}")
    few.close()

    more = _request(4)
    assert more.status_code == 400, (
        "a fourth element of the same density was accepted; the allowance "
        "must belong to the outline, not to each element")
    more.close()


# ---------------------------------------------------------------------------
# The complexity ceiling on SVG
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("container", ["g", "symbol"])
@pytest.mark.parametrize("depth", [12, 17, 30, 100])
def test_use_expansion_is_priced_by_what_it_draws_not_by_its_byte_count(
        depth, container):
    """A tiny SVG that expands to a huge one is scored on the expansion.

    Doubling per nesting level means payload size grows linearly while the work
    grows as 2**depth: measured, depth 17 is 1 253 bytes and held a worker past
    120 s. The byte-level scan that used to be the only guard read `d` and
    `points` attributes, saw neither, and scored all of these zero.

    Both axes of the matrix pin a way this went wrong while being written.
    Depth 100 exceeds the scorer's relaxation round limit, and a partial answer
    is a LOWER bound -- an earlier version stopped early and returned zero for
    the deep cases, waving through the very files the guard exists to stop, so
    non-convergence now reports "over the limit" instead. `<symbol>` is the
    other: a container that paints nothing in place, so scoring a `<use>` by
    what its target draws where it sits reads zero for the whole sprite.
    """
    payload = _use_bomb(depth, container)
    score = webapp._svg_tree_complexity(payload)
    assert score > webapp.MAX_SVG_COMPLEXITY, (
        f"{len(payload)} bytes of <{container}> expanding to 2**{depth} shapes "
        f"scored {score}, inside the {webapp.MAX_SVG_COMPLEXITY} ceiling")
    with pytest.raises(webapp._TooComplex):
        webapp._check_svg_complexity(payload)


def test_reusing_one_symbol_a_few_times_is_not_mistaken_for_an_expansion_bomb():
    """`<use>` is a normal thing to write, and stays usable.

    The contrast case for the test above: a defs'd icon placed twenty times, and
    a chain of groups nine deep, are both ordinary SVG editor output. If the
    expansion guard cannot tell them from a bomb it has just banned `<use>`.
    """
    icon = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400">'
            '<defs><symbol id="ic"><circle cx="5" cy="5" r="4" fill="#000"/>'
            '<rect x="0" y="0" width="2" height="2" fill="#f00"/></symbol></defs>'
            + "".join(f'<use href="#ic" x="{i * 15}" y="{i * 7}"/>' for i in range(20))
            + "</svg>").encode()
    chain = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400"><defs>'
             '<g id="s0"><circle cx="5" cy="5" r="4" fill="#000"/></g>'
             + "".join(f'<g id="s{i}"><use href="#s{i - 1}"/></g>' for i in range(1, 9))
             + '</defs><use href="#s8"/></svg>').encode()
    for name, payload in (("twenty placements", icon), ("nine-deep chain", chain)):
        score = webapp._svg_tree_complexity(payload)
        assert score < webapp.MAX_SVG_COMPLEXITY, (
            f"ordinary <use> ({name}) scored {score}, over the "
            f"{webapp.MAX_SVG_COMPLEXITY} ceiling -- the guard has banned <use>")


@pytest.mark.parametrize("tag", ["circle", "rect", "ellipse"])
def test_shape_count_is_priced_even_when_every_shape_is_trivial(tag):
    """Many simple shapes cost as much as one intricate path, and now score it.

    The painter pass in `svg_color_regions` unions and differences a growing
    geometry once per *shape*, so shape count is the expensive axis. Vertices
    were the only thing priced: 6 000 four-vertex `<rect>` scored 24 000
    against a 40 000 ceiling -- comfortably inside it -- and took 12 s, while
    8 000 `<circle>` took 36 s. All returned 200.

    Three tags because they reach the scorer by different branches: `circle`
    and `ellipse` through the curve cost, `rect` through the straight-sided
    cost that used to be four.
    """
    payload = _repeated(tag, 6000)
    score = webapp._svg_tree_complexity(payload)
    assert score > webapp.MAX_SVG_COMPLEXITY, (
        f"6 000 <{tag}> scored {score}, inside the "
        f"{webapp.MAX_SVG_COMPLEXITY} ceiling")


def test_a_deeply_nested_but_simple_svg_is_not_treated_as_expensive():
    """5 000 nested `<g>` around one rect is a harmless file.

    It is also the file that crashed the first version of this scorer, which
    recursed once per nesting level. Depth is not cost; what is drawn is.
    """
    payload = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
               + b"<g>" * 5000
               + b'<rect width="10" height="10" fill="#000"/>'
               + b"</g>" * 5000 + b"</svg>")
    score = webapp._svg_tree_complexity(payload)
    assert score is not None, "the scorer failed to parse plain nested groups"
    assert score < webapp.MAX_SVG_COMPLEXITY, (
        f"one rect inside 5 000 groups scored {score}; nesting is not cost")


def test_an_svg_the_xml_parser_rejects_falls_back_to_the_byte_scan():
    """A file `xml.etree` cannot read still gets scored, not waved through.

    Documents with an internal DTD and undefined entities -- the XXE and
    billion-laughs shapes -- fail XML parsing outright. They must fall back to
    the attribute scan rather than returning "no cost", or the tree scorer would
    have opened a hole underneath the guard it replaced.
    """
    # The entity is referenced, not merely declared: xml.etree parses a DTD it
    # never has to expand, so an unused &xxe; would leave this fixture testing
    # the tree scorer instead of the fallback.
    hostile_dtd = (b'<?xml version="1.0"?><!DOCTYPE svg ['
                   b'<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                   b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
                   b'<desc>&xxe;</desc>'
                   b'<path fill="#000" d="M0 0 '
                   + b" ".join(b"L%d %d" % (i % 100, (i * 7) % 100)
                               for i in range(60_000))
                   + b' Z"/></svg>')
    assert webapp._svg_tree_complexity(hostile_dtd) is None, (
        "this fixture is meant to defeat the XML parser; if it now parses, "
        "the fallback below is no longer being exercised")
    with pytest.raises(webapp._TooComplex):
        webapp._check_svg_complexity(hostile_dtd)


# ---------------------------------------------------------------------------
# The subprocess deadline
# ---------------------------------------------------------------------------

def test_the_export_budget_leaves_the_worker_timeout_room_to_answer():
    """A timed-out export reports the timeout instead of dropping the socket.

    /gerbers runs three subprocesses in sequence -- the pcbnew zone refill, the
    plot, then the drill export -- and each has a carefully written 500 saying
    what timed out and what to do instead. The old per-step budgets were 120 s
    each, 360 s in total, against gunicorn's `timeout = 300`: in exactly the
    case those messages were written for, the worker was killed before it could
    send one and the user got a dropped connection.

    So this asserts the relationship the arithmetic has to satisfy, reading
    gunicorn's own config file rather than a copy of the number.
    """
    cfg: dict = {}
    with open("gunicorn.conf.py") as fh:
        exec(compile(fh.read(), "gunicorn.conf.py", "exec"), cfg)  # noqa: S102
    worker_timeout = cfg["timeout"]
    budget = webapp.EXPORT_BUDGET_S
    assert budget < worker_timeout, (
        f"the export budget ({budget} s) is not under gunicorn's worker "
        f"timeout ({worker_timeout} s), so a slow export is SIGKILLed before "
        f"its own error response can be sent")
    # The handler still has to zip and stream the result after the deadline.
    assert worker_timeout - budget >= 30, (
        f"only {worker_timeout - budget} s left between the export budget and "
        f"the worker timeout; that is not enough to zip and send the response")


def test_every_subprocess_in_one_export_draws_on_the_same_deadline():
    """Three sequential steps share one allowance rather than each taking it.

    The bug was not that any single budget was too large; it was that they
    were independent and added up past the worker timeout. An exhausted
    deadline hands out a floor rather than a negative or zero timeout, so the
    step fails through its own `TimeoutExpired` branch -- which is the branch
    that produces the useful message.
    """
    fresh = webapp._Deadline(webapp.EXPORT_BUDGET_S)
    assert fresh.left() > 1.0, "a fresh deadline must hand out real time"
    exhausted = webapp._Deadline(-1000)
    assert exhausted.left() > 0, (
        "an exhausted deadline handed out a non-positive timeout; subprocess "
        "budgets must stay positive so the timeout path is reached")


# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------

@pytest.mark.webapp
def test_the_browser_protections_ride_on_every_kind_of_response(client):
    """Page, JSON refusal and file download all carry the security headers.

    Worth covering all three shapes rather than the front page alone: they
    leave the app by different paths (`render_template`, a dict return, and
    `send_file`), and an `after_request` hook is exactly the thing that is easy
    to bypass with a `Response` built elsewhere.

    The header values themselves are not asserted -- tightening a CSP must not
    turn this red. What is asserted is that none of them is missing, because a
    missing security header is invisible: nothing errors, nothing looks wrong,
    and the protection is simply gone.
    """
    required = ("Content-Security-Policy", "X-Content-Type-Options",
                "X-Frame-Options", "Referrer-Policy",
                "Strict-Transport-Security")
    responses = {
        "html page": client.get("/"),
        "json refusal": client.post("/generate", data={"params": "{"},
                                    content_type="multipart/form-data"),
        "zip download": client.post(
            "/generate", data={"params": json.dumps({"name": "hdr"})},
            content_type="multipart/form-data"),
        "font file": client.get("/fonts/vt323.ttf"),
    }
    try:
        for what, resp in responses.items():
            missing = [h for h in required if h not in resp.headers]
            assert not missing, f"the {what} response is missing {missing}"
    finally:
        for resp in responses.values():
            resp.close()


@pytest.mark.webapp
def test_the_content_policy_still_allows_what_the_page_actually_loads(client):
    """The CSP permits the app's own machinery, blob URLs included.

    Measured in Chrome: without `blob:` in `connect-src` the 3D view dies with
    "Failed to fetch" and an empty viewer, because /model3d is fetched, turned
    into an object URL, and then fetched again by <model-viewer>. That failure
    is invisible to any test that only checks the header is present, so the
    directives the page depends on are named here.
    """
    resp = client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    resp.close()
    for directive, needed in (("connect-src", "blob:"),   # the GLB object URL
                              ("img-src", "blob:"),       # canvas art previews
                              ("worker-src", "blob:"),    # model-viewer workers
                              ("font-src", "'self'"),     # /fonts/<key>.ttf
                              ("script-src", "'self'")):  # /static/vendor
        section = next((p for p in csp.split(";") if p.strip().startswith(directive)), None)
        assert section is not None, f"the CSP declares no {directive}"
        assert needed in section, (
            f"{directive} does not allow {needed}: {section.strip()!r} -- the "
            f"page loads it, so this policy breaks the app")
