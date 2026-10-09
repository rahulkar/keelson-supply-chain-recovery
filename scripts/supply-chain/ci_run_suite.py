#!/usr/bin/env python3
"""Run one CI test suite of infra/supply-chain/images.json exactly as CI does (129 §3).

    python3 scripts/supply-chain/ci_run_suite.py --list
    python3 scripts/supply-chain/ci_run_suite.py optimizer
    python3 scripts/supply-chain/ci_run_suite.py engine-platform --no-build --image keelson-ci/engine:test
    python3 scripts/supply-chain/ci_run_suite.py signal-hub --dry-run      # print the docker commands only

Image suites build their Dockerfile target (tag ``keelson-ci/<image>:<target>``; ``--no-build`` uses
an existing image, e.g. one CI built with its layer cache) and run it; ``kind: build`` suites pass when
the target builds. Suites with ``db: pg-core`` / ``valkey: true`` get a throwaway pg-core
(``keelson-ci/pg-core:latest``, PostGIS + pgvector, tmpfs, fsync off) and Valkey on a private network,
reachable as ``e1-test-pg`` / ``e1-test-valkey`` (the names the service READMEs use). Toolchain suites
run a pinned image with the repository mounted read-only. Every container and the network are named
``keelson_ci_<suite>_<random>`` and removed afterwards (``--keep`` keeps them for debugging); each
container has a ``--memory`` cap. Exit status: the suite's own status (99 = the harness failed).

Stdlib only: runs on the GitHub runner and from Git Bash on Windows (docker CLI on PATH).
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as reg  # noqa: E402

PG_IMAGE = "keelson-ci/pg-core:latest"
PG_ENV = {"POSTGRES_PASSWORD": "test", "KEELSON_DB_PASSWORD": "ci-keelson", "ADK_DB_PASSWORD": "ci-adk",
          "KEYCLOAK_DB_PASSWORD": "ci-keycloak"}
HARNESS_FAILED = 99


class Harness:
    def __init__(self, root: Path, *, dry_run: bool, keep: bool) -> None:
        self.root = root
        self.dry_run = dry_run
        self.keep = keep
        self.cleanup: list[list[str]] = []

    def docker(self, *args: str, check: bool = True, capture: bool = False, quiet: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", *args]
        if not quiet or self.dry_run:
            print("+ " + shlex.join(cmd), file=sys.stderr if not self.dry_run else sys.stdout, flush=True)
        if self.dry_run:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        res = subprocess.run(cmd, cwd=self.root, text=True, capture_output=capture)
        if check and res.returncode != 0:
            raise HarnessError(f"{shlex.join(cmd)} failed ({res.returncode}): {(res.stderr or '').strip()[-500:]}")
        return res

    def remove_later(self, *args: str) -> None:
        self.cleanup.append(["docker", *args])

    def close(self) -> None:
        if self.keep:
            for c in self.cleanup:
                print(f"kept (--keep): {shlex.join(c)}", file=sys.stderr)
            return
        for c in reversed(self.cleanup):
            if self.dry_run:
                print("+ " + shlex.join(c))
            else:
                subprocess.run(c, cwd=self.root, capture_output=True, text=True)


class HarnessError(RuntimeError):
    pass


def compose_image(root: Path, service: str) -> str:
    """The pinned image of a compose service (single source of truth for Valkey)."""
    text = (root / "compose.yaml").read_text(encoding="utf-8")
    m = re.search(rf"^  {re.escape(service)}:\s*\n(?:    .*\n|\s*\n)*?    image:\s*(\S+)", text, re.M)
    if not m:
        raise HarnessError(f"compose.yaml has no image for service {service}")
    return m.group(1)


def wait_healthy(h: Harness, container: str, timeout_s: int = 180) -> None:
    if h.dry_run:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        res = h.docker("inspect", "-f", "{{.State.Health.Status}}", container, check=False, capture=True, quiet=True)
        if res.stdout.strip() == "healthy":
            return
        time.sleep(1)
    logs = h.docker("logs", "--tail", "30", container, check=False, capture=True, quiet=True)
    raise HarnessError(f"{container} did not become healthy in {timeout_s}s:\n{logs.stdout}{logs.stderr}")


def image_cmd(h: Harness, image: str) -> list[str]:
    if h.dry_run:
        return ["<image CMD>"]
    res = h.docker("image", "inspect", "-f", "{{json .Config.Cmd}}", image, capture=True, quiet=True)
    cmd = json.loads(res.stdout.strip() or "null")
    if not cmd:
        raise HarnessError(f"{image} has no CMD; give the suite a cmd in images.json")
    return list(cmd)


def run_suite(r: reg.Registry, suite: reg.Suite, h: Harness, *, image_ref: str | None, build: bool) -> int:
    name = f"keelson_ci_{suite.id.replace('-', '_')}_{secrets.token_hex(3)}"
    env: dict[str, str] = dict(suite.env)
    run_args: list[str] = []
    if suite.image is not None:
        img = r.image(suite.image)
        ref = image_ref or f"keelson-ci/{img.name}:{suite.target}"
        if build and not image_ref:
            h.docker("build", "-f", img.dockerfile, "--target", suite.target or "", "-t", ref, img.context)
        if suite.kind == "build":
            return 0
    else:
        ref = suite.tool_image or ""
        run_args += ["-v", f"{h.root.as_posix()}:/work:ro", "-w", posixpath.normpath(f"/work/{suite.workdir}")]
    if suite.db or suite.valkey:
        net = f"{name}_net"
        h.docker("network", "create", net)
        h.remove_later("network", "rm", net)
        run_args += ["--network", net]
        if suite.db == "pg-core":
            pg = r.image("pg-core")
            if build:
                h.docker("build", "-q", "-f", pg.dockerfile, "-t", PG_IMAGE, pg.context)
            h.remove_later("rm", "-fv", f"{name}_pg")
            pg_env = [x for k, v in PG_ENV.items() for x in ("-e", f"{k}={v}")]
            h.docker("run", "-d", "--name", f"{name}_pg", "--network", net, "--network-alias", "e1-test-pg",
                     "--memory", "512m", "--tmpfs", "/var/lib/postgresql:size=768m", *pg_env, PG_IMAGE,
                     "postgres", "-c", "fsync=off", "-c", "max_connections=200", capture=True)
        if suite.valkey:
            h.remove_later("rm", "-fv", f"{name}_valkey")
            h.docker("run", "-d", "--name", f"{name}_valkey", "--network", net, "--network-alias", "e1-test-valkey",
                     "--memory", "64m", compose_image(h.root, "valkey"), "valkey-server", "--requirepass", "test",
                     "--save", "", "--appendonly", "no", capture=True)
        if suite.db == "pg-core":
            wait_healthy(h, f"{name}_pg")
    cmd = list(suite.cmd) if suite.cmd else image_cmd(h, ref)
    for d in suite.deselect:
        cmd += ["--deselect", d.nodeid]
    env_args = [x for k, v in sorted(env.items()) for x in ("-e", f"{k}={v}")]
    res = h.docker("run", "--rm", "--name", name, "--memory", suite.memory, "-e", "PYTHONDONTWRITEBYTECODE=1",
                   *env_args, *run_args, ref, *cmd, check=False)
    return res.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one CI suite of infra/supply-chain/images.json locally or in CI")
    ap.add_argument("suite", nargs="?", help="suite id (see --list)")
    ap.add_argument("--list", action="store_true", help="list the suites and exit")
    ap.add_argument("--image", help="run this image instead of building the suite's target")
    ap.add_argument("--no-build", action="store_true", help="use existing images (keelson-ci/<image>:<target>, pg-core)")
    ap.add_argument("--keep", action="store_true", help="keep containers and the network for debugging")
    ap.add_argument("--dry-run", action="store_true", help="print the docker commands, run nothing")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        r = reg.load(root)
    except reg.RegistryError as exc:
        for p in exc.problems:
            print(f"ci_run_suite.py: {p}", file=sys.stderr)
        return 2
    if args.list:
        for s in r.suites:
            where = f"{s.image}:{s.target}" if s.image else s.tool_image
            extra = " ".join(x for x in (f"db={s.db}" if s.db else "", "valkey" if s.valkey else "") if x)
            print(f"{s.id:18} {s.kind:6} {where} {extra}".rstrip())
        return 0
    if not args.suite:
        ap.error("a suite id is required (see --list)")
    try:
        suite = r.suite(args.suite)
    except KeyError:
        print(f"ci_run_suite.py: unknown suite {args.suite!r} (see --list)", file=sys.stderr)
        return 2
    h = Harness(root, dry_run=args.dry_run, keep=args.keep)
    try:
        rc = run_suite(r, suite, h, image_ref=args.image, build=not args.no_build)
    except HarnessError as exc:
        print(f"ci_run_suite.py: {exc}", file=sys.stderr)
        rc = HARNESS_FAILED
    except KeyboardInterrupt:
        rc = 130
    finally:
        h.close()
    print(f"ci_run_suite.py: suite {suite.id}: {'PASS' if rc == 0 else f'FAIL ({rc})'}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
