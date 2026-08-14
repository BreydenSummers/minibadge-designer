#!/usr/bin/env python3
"""Download the third-party assets minibadge-designer serves at runtime.

These are other people's files under other people's licences, so they are not
kept in this repository — they are fetched, by exact version and verified
against a SHA-256, into paths git ignores. Run this once after cloning (the
Docker build runs it for you):

    python3 scripts/fetch_assets.py

Nothing here is modified or redistributed by this project; each asset keeps
the licence it ships under, recorded below and in the generated NOTICE file.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "minibadge_designer"

# google/fonts is pinned to a commit so a re-fetch cannot silently change a
# glyph — the checksums below are what that commit serves.
FONTS_COMMIT = "73fc2ff52147e34a74804b500cf89ca219eac55d"
FONTS_BASE = f"https://raw.githubusercontent.com/google/fonts/{FONTS_COMMIT}"

# (url, destination, sha256, licence)
ASSETS: list[tuple[str, Path, str, str]] = [
    (f"{FONTS_BASE}/ofl/archivoblack/ArchivoBlack-Regular.ttf",
     PKG / "fonts/ArchivoBlack-Regular.ttf",
     "dd9a89a019b4849f66ab75455fe7bdf931311042cbb0f0f97acc061539703180", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/audiowide/Audiowide-Regular.ttf",
     PKG / "fonts/Audiowide-Regular.ttf",
     "c7c0f2b0f6fad8c623e31772ce79f94a4edb9321ffce9fce978ea892d20ae730", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/bangers/Bangers-Regular.ttf",
     PKG / "fonts/Bangers-Regular.ttf",
     "4160a7311de9342674cce9160cde9fcbb30f48190397d86ff1b70b455af65824", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/blackopsone/BlackOpsOne-Regular.ttf",
     PKG / "fonts/BlackOpsOne-Regular.ttf",
     "282a825b5f294377387e3969f765408157dbea8da0f5d0aae68c6bc704b145b3", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/creepster/Creepster-Regular.ttf",
     PKG / "fonts/Creepster-Regular.ttf",
     "402aeb734586c74aecd3dbdc454589b1fb12e2e1c71f782fd019ae68066d9f44", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/dmserifdisplay/DMSerifDisplay-Regular.ttf",
     PKG / "fonts/DMSerifDisplay-Regular.ttf",
     "8cc3643535edf039aa5d95440a8542735e9197e4f4b8d9303e980fefbf5ab616", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/pacifico/Pacifico-Regular.ttf",
     PKG / "fonts/Pacifico-Regular.ttf",
     "5b6c0d5334a7bf77dea52b975c5a0c408878c0f7115ed5b6fb151f634b7bf701", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/pirataone/PirataOne-Regular.ttf",
     PKG / "fonts/PirataOne-Regular.ttf",
     "5347a2e155589ecf667d4b766613c8ee003edde9f83717fd24c09599a4b1ecc0", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/pressstart2p/PressStart2P-Regular.ttf",
     PKG / "fonts/PressStart2P-Regular.ttf",
     "034c77f1f05ec89421e4a63f0e3a4ca1ecf852cc6d2bf611f126f275728e017d", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/righteous/Righteous-Regular.ttf",
     PKG / "fonts/Righteous-Regular.ttf",
     "2ffb3fe5c27d7e6571210b800448c4e234e651b46c6b4426c1bb567e5341348a", "OFL-1.1"),
    (f"{FONTS_BASE}/ofl/vt323/VT323-Regular.ttf",
     PKG / "fonts/VT323-Regular.ttf",
     "cf4de751ada78ceac033dbe16a687742939995b77bc2a052ae17a4957958594d", "OFL-1.1"),
    (f"{FONTS_BASE}/apache/specialelite/SpecialElite-Regular.ttf",
     PKG / "fonts/SpecialElite-Regular.ttf",
     "a776fcb4ceb8bdf03e2967688ebdad42680de5b91a7e62c17e718ae212d14bc4", "Apache-2.0"),
    ("https://unpkg.com/@google/model-viewer@4.0.0/dist/model-viewer.min.js",
     PKG / "static/vendor/model-viewer.min.js",
     "774edda21e1be2a0934e460ca5943af1fe3f88da130a9f98bd6a9d611576cacf", "BSD-3-Clause"),
]

NOTICE = """\
Third-party assets fetched by scripts/fetch_assets.py
=====================================================

These files are NOT part of minibadge-designer and are not distributed in its
repository. They are downloaded at setup time and remain under their own
licences and copyrights.

Fonts — from the Google Fonts collection (https://github.com/google/fonts),
pinned to commit {commit}:

  SIL Open Font License 1.1 (https://openfontlicense.org)
    Archivo Black, Audiowide, Bangers, Black Ops One, Creepster,
    DM Serif Display, Pacifico, Pirata One, Press Start 2P, Righteous, VT323

  Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0)
    Special Elite

  Each family's full licence and copyright notice lives beside the font in
  the upstream repository, under the same pinned commit.

<model-viewer> (https://github.com/google/model-viewer), version 4.0.0:

  BSD 3-Clause, Copyright 2017 Google LLC. The copyright notice is retained
  verbatim in the header of the downloaded file.
"""


def fetch(url: str, dest: Path, want: str, force: bool) -> str:
    if dest.exists() and not force and hashlib.sha256(dest.read_bytes()).hexdigest() == want:
        return "ok (cached)"
    with urllib.request.urlopen(url, timeout=120) as r:
        blob = r.read()
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        raise SystemExit(
            f"checksum mismatch for {url}\n  expected {want}\n  got      {got}\n"
            "Refusing to install an asset that is not the pinned version.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return f"downloaded ({len(blob) // 1024} KB)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="re-download even when the local copy already matches")
    ap.add_argument("--check", action="store_true",
                    help="verify what is present without downloading anything")
    args = ap.parse_args()

    if args.check:
        missing = [d for _u, d, s, _l in ASSETS
                   if not d.exists() or hashlib.sha256(d.read_bytes()).hexdigest() != s]
        for d in missing:
            print(f"MISSING/STALE {d.relative_to(ROOT)}")
        print(f"{len(ASSETS) - len(missing)}/{len(ASSETS)} assets present and verified")
        return 1 if missing else 0

    for url, dest, want, licence in ASSETS:
        status = fetch(url, dest, want, args.force)
        print(f"  {dest.relative_to(ROOT)}  [{licence}]  {status}")

    notice = PKG / "fonts" / "THIRD-PARTY-NOTICE.txt"
    notice.write_text(NOTICE.format(commit=FONTS_COMMIT))
    print(f"  {notice.relative_to(ROOT)}  written")
    print(f"\n{len(ASSETS)} assets ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
