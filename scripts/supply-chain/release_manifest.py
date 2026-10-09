#!/usr/bin/env python3
"""Write keelson-release.env, the signed list of a release's image digests (129 §6, docs/supply-chain.md).

    python3 scripts/supply-chain/release_manifest.py --refs-dir DIR --out keelson-release.env \
        --repository OWNER/REPO --git-sha SHA --ref refs/heads/main --run-id N --created-at 2026-10-10T08:00:00Z \
        [--domain demo.example.com]

DIR holds one ``<RELEASE_KEY>=<registry>/<image>@sha256:<digest>`` line per ``*.ref`` file (written by
.github/actions/sign-attest after an image was pushed, scanned, signed and attested). The output is plain,
sorted ``KEY=value`` lines that scripts/cloudrun/deploy.sh and compose-release.sh parse with their safe
read loop (no jq/python on the deploy machine):

    RELEASE_SCHEMA=keelson.release/1, RELEASE_REPOSITORY, RELEASE_GIT_SHA, RELEASE_REF, RELEASE_RUN_ID,
    RELEASE_CREATED_AT, RELEASE_DOMAIN, then RELEASE_IMAGE_<KEY> for every image (sorted)

Every value is validated; every non-overlay release image of infra/supply-chain/images.json must be
present, the domain overlays exactly when --domain is given. release.yml signs the file with
``cosign sign-blob --bundle keelson-release.env.sigstore.json``. Stdlib only. Exit 0, or 2 with errors.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as reg  # noqa: E402

SCHEMA = "keelson.release/1"
HEADER = ("RELEASE_SCHEMA", "RELEASE_REPOSITORY", "RELEASE_GIT_SHA", "RELEASE_REF", "RELEASE_RUN_ID",
          "RELEASE_CREATED_AT", "RELEASE_DOMAIN")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REF_RE = re.compile(r"^refs/(heads/main|tags/v[0-9]+\.[0-9]+\.[0-9]+)$")
RUN_RE = re.compile(r"^[0-9]{1,20}$")
TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
REGISTRY_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+$")
LINE_RE = re.compile(r"^([A-Z0-9_]+)=(\S+)$")


def signing_registry(root: Path) -> str:
    for line in (root / "infra" / "supply-chain" / "signing.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("RELEASE_REGISTRY="):
            return line.split("=", 1)[1].strip()
    return ""


def build(registry: reg.Registry, refs: dict[str, str], *, release_registry: str, repository: str, git_sha: str,
          ref: str, run_id: str, created_at: str, domain: str) -> tuple[list[str], list[str]]:
    """Return (lines, errors)."""
    errors: list[str] = []
    for value, rx, what in ((repository, REPO_RE, "repository"), (git_sha, SHA_RE, "git sha (40 hex)"),
                            (ref, REF_RE, "ref (refs/heads/main or refs/tags/vX.Y.Z)"), (run_id, RUN_RE, "run id"),
                            (created_at, TIME_RE, "created-at (YYYY-MM-DDThh:mm:ssZ)"),
                            (release_registry, REGISTRY_RE, "release registry")):
        if not rx.match(value):
            errors.append(f"invalid {what}: {value!r}")
    if domain and not DOMAIN_RE.match(domain):
        errors.append(f"invalid domain: {domain!r}")
    by_key = {img.release_key: img for img in registry.release_images}
    for key, value in sorted(refs.items()):
        img = by_key.get(key)
        if img is None:
            errors.append(f"{key} is not a release image of {reg.REGISTRY_PATH}")
            continue
        want = re.compile(rf"^{re.escape(release_registry)}/{re.escape(img.name)}@sha256:[a-f0-9]{{64}}$")
        if not want.match(value):
            errors.append(f"{key}: {value!r} is not {release_registry}/{img.name}@sha256:<digest>")
        if img.domain_overlay and not domain:
            errors.append(f"{key}: a domain overlay needs --domain")
    for key, img in sorted(by_key.items()):
        if key not in refs and (not img.domain_overlay or domain):
            errors.append(f"{key} ({img.name}) is missing from the release")
    header = {"RELEASE_SCHEMA": SCHEMA, "RELEASE_REPOSITORY": repository, "RELEASE_GIT_SHA": git_sha,
              "RELEASE_REF": ref, "RELEASE_RUN_ID": run_id, "RELEASE_CREATED_AT": created_at, "RELEASE_DOMAIN": domain}
    lines = [f"{k}={header[k]}" for k in HEADER] + [f"RELEASE_IMAGE_{k}={refs[k]}" for k in sorted(refs)]
    return lines, errors


def read_refs(directory: Path) -> tuple[dict[str, str], list[str]]:
    refs: dict[str, str] = {}
    errors = []
    for p in sorted(directory.rglob("*.ref")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            m = LINE_RE.match(line.strip())
            if not m:
                errors.append(f"{p.name}: bad line {line[:80]!r}")
            elif m.group(1) in refs and refs[m.group(1)] != m.group(2):
                errors.append(f"{m.group(1)} appears twice with different digests")
            else:
                refs[m.group(1)] = m.group(2)
    return refs, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="write keelson-release.env from the signed image refs")
    ap.add_argument("--refs-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--git-sha", default=os.environ.get("GITHUB_SHA", ""))
    ap.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    ap.add_argument("--created-at", required=True, help="UTC commit time, e.g. 2026-10-10T08:00:00Z")
    ap.add_argument("--domain", default="")
    ap.add_argument("--registry", default=None, help="default: RELEASE_REGISTRY of infra/supply-chain/signing.env")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = ap.parse_args(argv)
    try:
        registry = reg.load(args.root)
    except reg.RegistryError as exc:
        for p in exc.problems:
            print(f"release_manifest.py: {p}", file=sys.stderr)
        return 2
    refs, errors = read_refs(args.refs_dir)
    lines, more = build(registry, refs, release_registry=args.registry or signing_registry(args.root),
                        repository=args.repository, git_sha=args.git_sha, ref=args.ref, run_id=args.run_id,
                        created_at=args.created_at, domain=args.domain.lower())
    errors += more
    if errors:
        for e in errors:
            print(f"release_manifest.py: {e}", file=sys.stderr)
        return 2
    tmp = args.out.with_name(args.out.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(args.out)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
