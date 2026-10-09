#!/usr/bin/env python3
"""Writes a tiny, deterministic stand-in of the `basemap` volume for the Caddy route test (run.sh).

Usage: ``python make_basemap_fixture.py <dir>``. Layout as written by scripts/basemap/fetch-basemap.mjs
(docs/contracts/127-m8-basemap.md), with synthetic content (no map data in the repository):

* ``manifest.json``
* ``v4/test.pmtiles`` - 4096 bytes starting with the PMTiles v3 magic (``PMTiles\\x03``)
* ``fonts/Noto Sans Regular/0-255.pbf`` - 2048 compressible bytes
* ``sprites/v4/light.json`` / ``light.png``
* ``.staging-1-2/partial.pmtiles`` and ``.hidden.json`` - must never be served
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ARCHIVE_SIZE = 4096


def main(root: Path) -> None:
    files: dict[str, bytes] = {
        "manifest.json": json.dumps({"schema": "keelson.basemap/1", "archive": {"path": "v4/test.pmtiles"}}).encode(),
        "v4/test.pmtiles": (b"PMTiles\x03" + bytes(i % 251 for i in range(ARCHIVE_SIZE - 8))),
        "fonts/Noto Sans Regular/0-255.pbf": b"glyphs " * 292 + b"pbf!",
        "sprites/v4/light.json": json.dumps({"townspot": {"x": 0, "y": 0, "width": 8, "height": 8, "pixelRatio": 1}}).encode(),
        "sprites/v4/light.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 56,
        ".staging-1-2/partial.pmtiles": b"PMTiles\x03partial",
        ".hidden.json": b'{"secret": true}',
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    assert len(files["v4/test.pmtiles"]) == ARCHIVE_SIZE


if __name__ == "__main__":
    main(Path(sys.argv[1]))
