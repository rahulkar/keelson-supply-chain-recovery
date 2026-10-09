#!/usr/bin/env python3
"""GitHub Actions matrices from infra/supply-chain/images.json (docs/contracts/129-m8-supply-chain.md §3).

    python3 scripts/supply-chain/matrix.py                    # print name=<json> lines
    python3 scripts/supply-chain/matrix.py --github-output    # append them to $GITHUB_OUTPUT

Matrices (each a JSON list used as ``strategy.matrix.include``):
  python    test suites that run a container (Dockerfile ``test`` targets + toolchain suites)
  web       suites whose check is building a Dockerfile target (keelson-web test/workspace-test, merxa-web)
  images    release images built, scanned and signed first (no domain overlays)
  overlays  domain overlays (built from a signed base image when KEELSON_PUBLIC_DOMAIN is set)
Stdlib only; no network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as reg  # noqa: E402


def matrices(r: reg.Registry) -> dict[str, list[dict[str, object]]]:
    python: list[dict[str, object]] = []
    web: list[dict[str, object]] = []
    for img in r.images:
        for s in img.suites:
            row: dict[str, object] = {"suite": s.id, "image": img.name, "dockerfile": img.dockerfile,
                                      "context": img.context, "target": s.target or ""}
            if s.kind == "build":
                web.append(row)
            else:
                python.append(dict(row, db=s.db or ""))
    for s in r.tool_suites:
        python.append({"suite": s.id, "image": "", "dockerfile": "", "context": "", "target": "", "db": s.db or ""})
    images: list[dict[str, object]] = []
    overlays: list[dict[str, object]] = []
    for img in r.release_images:
        row = {"name": img.name, "dockerfile": img.dockerfile, "context": img.context, "target": img.target or "",
               "release_key": img.release_key,
               "upstream_key": (img.verify_upstream or {}).get("versions_env_key", ""),
               "upstream_pubkey": (img.verify_upstream or {}).get("pubkey", "")}
        if img.domain_overlay:
            overlays.append(dict(row, overlay_of=img.overlay_of or "", overlay_arg=img.overlay_arg or ""))
        else:
            images.append(row)
    return {"python": python, "web": web, "images": images, "overlays": overlays}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GitHub Actions matrices from infra/supply-chain/images.json")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--github-output", action="store_true", help="append name=<json> lines to $GITHUB_OUTPUT")
    args = ap.parse_args(argv)
    try:
        r = reg.load(args.root)
    except reg.RegistryError as exc:
        for p in exc.problems:
            print(f"matrix.py: {p}", file=sys.stderr)
        return 2
    lines = [f"{name}={json.dumps(rows, separators=(',', ':'), sort_keys=True)}"
             for name, rows in matrices(r).items()]
    if args.github_output:
        out = os.environ.get("GITHUB_OUTPUT")
        if not out:
            print("matrix.py: GITHUB_OUTPUT is not set", file=sys.stderr)
            return 2
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
