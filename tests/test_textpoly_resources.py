"""What `textpoly` still holds after it has drawn a string.

Every other font test in this suite asks whether the *outline* is right. This
file asks a different question -- what the module keeps hold of once it is
done -- because `_load_font` is an unbounded `lru_cache` inside a Flask
process that is expected to stay up for days. Anything it fails to hand back
is never handed back at all.

Not covered here (and deliberately): `webapp.py`'s `/fonts/<key>.ttf` route
leaks the `send_file` response's file wrapper under the werkzeug *test*
client, which is what actually raises ResourceWarning in this suite today. A
PEP 3333 server closes that iterable, so it is a harness artefact, not a
descriptor a deployed worker loses -- and it lives in a file this file does
not own.
"""

from __future__ import annotations

import gc
import os

import pytest

from minibadge_designer import textpoly

#: A POSIX system exposes the calling process's own descriptors here; on Linux
#: it is a symlink to /proc/self/fd. Anything else (Windows) cannot answer the
#: question this file asks, so it says so rather than passing vacuously.
FD_DIR = "/dev/fd"

#: Mixed case plus a digit, so the cap-height path, the descender path and the
#: numeral path all resolve a glyph. Every bundled face carries basic Latin.
SAMPLE_TEXT = "Ag8"

#: Cap height in mm. Deliberately not the app's default text size -- nothing
#: about descriptor ownership may depend on how big the letters are.
SAMPLE_SIZE_MM = 2.4


def _open_descriptors() -> set[str]:
    """The numbers of every file descriptor this process currently holds."""
    return set(os.listdir(FD_DIR))


def _clear_font_memos() -> int:
    """Empty every memo in `textpoly`; return how many were found.

    Discovered rather than hard-coded. Naming `_load_font.cache_clear()` here
    would make renaming a private helper -- a refactor with no user-visible
    effect whatsoever -- turn this test red for a reason that has nothing to
    do with file descriptors, which is the failure mode this suite has the
    most of already.
    """
    memos = [c for c in vars(textpoly).values()
             if callable(getattr(c, "cache_clear", None))]
    for memo in memos:
        memo.cache_clear()
    return len(memos)


@pytest.mark.needs("assets")
@pytest.mark.parametrize("font_key", sorted(textpoly.FONTS))
def test_drawing_a_string_hands_back_every_descriptor_it_opened(font_key):
    """Rendering text in a face leaves the process no file open for that .ttf.

    `_load_font` is cached with `maxsize=None`, so whatever the cached value
    still references is pinned for the life of the interpreter. If it pins an
    open TTF, a long-running worker that has served text in all the bundled
    faces has quietly spent one descriptor per face and can never get them
    back; add the descriptors held by uploaded logos and by the zip it builds
    per request and the worker eventually cannot open a file at all, so
    /generate stops handing anyone a project. Descriptor *count* is the check
    because it is the thing that runs out.

    Parametrised over every bundled face rather than the one face the rest of
    the suite reaches for: the leak, if it comes back, comes back per face,
    and a single-face check would under-report it twelvefold.
    """
    if not os.path.isdir(FD_DIR):
        pytest.skip(f"{FD_DIR} is not available on this platform")

    # Warm the lazy `import fontTools` / `import shapely` inside the module so
    # their own one-off descriptors are not mistaken for the font's.
    textpoly.text_geometry(SAMPLE_TEXT, font_key, SAMPLE_SIZE_MM)

    # Drop the cached face and let the collector finalise it, so the baseline
    # is a process that is holding nothing on this font's behalf.
    memos = _clear_font_memos()
    gc.collect()

    # Without a cold cache the render below re-uses a face that is already
    # loaded, opens nothing, and the descriptor check passes for free.
    assert memos, ("textpoly exposes no clearable cache, so this test cannot "
                   "force the cold load it needs to measure")

    before = _open_descriptors()
    geom = textpoly.text_geometry(SAMPLE_TEXT, font_key, SAMPLE_SIZE_MM)
    after = _open_descriptors()

    # Guard against a vacuous pass: a face that rendered nothing also opens
    # nothing, and would sail through the assertion below.
    assert geom is not None and not geom.is_empty, (
        f"{font_key!r} produced no outline for {SAMPLE_TEXT!r}, so this test "
        f"never exercised the font-loading path it is here to measure")

    leaked = after - before
    assert not leaked, (
        f"rendering {SAMPLE_TEXT!r} in {font_key!r} left {len(leaked)} file "
        f"descriptor(s) open ({sorted(leaked)}) and the cached font pins them "
        f"for the life of the process; a worker doing this once per bundled "
        f"face bleeds descriptors it can never reclaim")
