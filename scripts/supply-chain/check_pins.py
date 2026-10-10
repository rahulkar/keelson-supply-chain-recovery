#!/usr/bin/env python3
"""Supply-chain pin checker (docs/contracts/129-m8-supply-chain.md §2, docs/supply-chain.md).

    python3 scripts/supply-chain/check_pins.py [--root DIR] [--rule P1,P2,...]

Stdlib only. Prints one JSON line per violation ``{"rule", "file", "line", "message"}`` (sorted) and
a per-rule summary on stderr. Exit status = number of violations (capped at 125).

Rules
  P1  Dockerfiles: ``# syntax=``, ``FROM``, ``COPY/RUN --from=`` and the ARG defaults used by them are
      ``name:<exact tag>@sha256:<digest>`` (stage names and local ``keelson*`` images excepted).
  P2  compose files: every ``image:`` is pinned the same way.
  P3  infra/cloudrun/versions.env and infra/supply-chain/signing.env: every ``*_IMAGE`` is pinned.
  P4  scripts (``scripts/``, ``infra/``, ``e2e/`` shell + PowerShell) and GitHub workflows/actions:
      image assignments and every literal of a known image repository are pinned.
  P5  workflows: every ``uses:`` is a local ``./`` action or ``owner/repo[/path]@<40-hex sha>  # vX``;
      ``docker://`` actions are forbidden.
  P6  no floating tags (latest, stable, edge, ...) and no inexact tags (``2-alpine``) anywhere above.
  P7  version caps from docs/contracts/60-build-guide.md, checked in the lockfiles and image tags
      (motion <14, typescript <6.1, litellm never 1.82.7/1.82.8, ...).
  P8  cross-file consistency (pnpm version and minimumReleaseAge = Renovate's, uv, litellm, keycloak,
      caddy, python/node bases, overlay ARG defaults = versions.env, pydantic/mcp/fastmcp across the uv
      projects, AG-UI, Playwright).
  P9  infra/supply-chain/images.json coverage: every Dockerfile, compose ``build:`` and Dockerfile
      ``test`` stage is registered; every registered path/target exists; deploy.sh's image variables
      equal the registry's ``deploy_var`` set.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as reg  # noqa: E402

SKIP_DIRS = {".git", "node_modules", ".native", ".venv", "venv", "__pycache__", ".next", ".turbo",
             ".pnpm-store", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", "coverage"}
# Test fixtures of this checker are deliberately broken; generated release overrides are git-ignored.
SKIP_PATHS = ("scripts/supply-chain/tests/fixtures",)
SKIP_FILES = {"compose.release.yaml"}

FLOATING_TAGS = {"latest", "stable", "edge", "nightly", "main", "master", "rolling", "dev", "beta", "next"}
# Numeric version components an exact tag needs (default 2: 18.6-trixie, 6.6-24.04_beta); repositories
# whose minor version still floats need the patch too (python:3.14-slim moves with every 3.14.x).
MIN_TAG_PARTS = {"python": 3, "node": 3, "ghcr.io/astral-sh/uv": 3, "caddy": 3, "valkey/valkey": 3}
# Repositories whose most exact published tag has a single numeric component (the digest still pins it).
INEXACT_TAG_OK = {
    "postgis/postgis": "PostGIS only publishes <postgres major>-<postgis major.minor> tags",
}
# Tag prefixes stripped before reading the version (arizephoenix/phoenix:version-20.20.0).
TAG_PREFIXES = ("version-",)
# Local images built from this repository (never pulled, so nothing to pin).
LOCAL_RE = re.compile(r"^(?:keelson[-_/][a-z0-9._/-]*|keelson|scratch)(?::[A-Za-z0-9._-]+)?$")
# Image repositories that scripts and workflows use as literals (besides every repository pinned in a
# Dockerfile, compose file or pin file, which are added automatically).
TOOL_REPOS = {
    "koalaman/shellcheck", "rhysd/actionlint", "ghcr.io/zizmorcore/zizmor", "ghcr.io/renovatebot/renovate",
    "ghcr.io/sigstore/cosign/cosign", "registry", "mcr.microsoft.com/playwright", "ghcr.io/astral-sh/uv",
    "python", "node", "postgres", "postgis/postgis", "valkey/valkey", "redis", "caddy", "alpine", "busybox",
    "debian", "ubuntu", "docker", "nginx", "curlimages/curl", "ghcr.io/anchore/syft", "anchore/grype",
}

REF_TOKEN = r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
REFISH_RE = re.compile(rf"^(?P<name>{REF_TOKEN})(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{{0,127}}))?"
                       r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?$")
SHA_USES_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")
VERSION_COMMENT_RE = re.compile(r"#\s*v?\d+(?:\.\d+)*")

# P7: (lockfile glob, package, spec, reason, condition or None). Condition = (package, spec) that
# must hold in the same lockfile for the cap to apply.
LOCK_CAPS: tuple[tuple[str, str, str, str, tuple[str, str] | None], ...] = (
    ("pnpm-lock.yaml", "motion", "<14", "60-build-guide: motion 13.x, NOT 14", None),
    ("pnpm-lock.yaml", "@tanstack/react-table", "<9", "60-build-guide: @tanstack/react-table 8.21.x, NOT 9", None),
    ("pnpm-lock.yaml", "typescript", "<6.1", "typescript-eslint requires TypeScript <6.1", None),
    ("pnpm-lock.yaml", "maplibre-gl", ">=6.4.1", "60-build-guide: maplibre-gl >= 6.4.1", None),
    ("apps/merxa-web/pnpm-lock.yaml", "typescript", "<6.1", "typescript-eslint requires TypeScript <6.1", None),
    ("services/engine/uv.lock", "fastapi", "<0.142", "google-adk 2.11 caps opentelemetry-api at 1.42.1 (60-build-guide)",
     ("google-adk", "==2.11.0")),
    ("services/engine/uv.lock", "opentelemetry-api", "<=1.42.1", "google-adk 2.11 caps opentelemetry-api at 1.42.1",
     ("google-adk", "==2.11.0")),
    ("*/uv.lock", "litellm", "!=1.82.7", "plan §14: litellm 1.82.7/1.82.8 are banned (compromised releases)", None),
    ("*/uv.lock", "litellm", "!=1.82.8", "plan §14: litellm 1.82.7/1.82.8 are banned (compromised releases)", None),
)
IMAGE_CAPS: tuple[tuple[str, str, str], ...] = (
    ("python", "<3.15", "requires-python <3.15 in every uv project"),
    ("node", "<25", "Node >= 25 no longer bundles corepack (the Dockerfiles run corepack enable)"),
    ("postgres", "<19", "Cloud SQL runs POSTGRES_18 (infra/cloudrun/versions.env)"),
    ("valkey/valkey", "<9", "Memorystore runs VALKEY_8_0 (infra/cloudrun/versions.env)"),
    ("quay.io/keycloak/keycloak", "<27", "realm files and keycloak-config-cli target Keycloak 26"),
    ("caddy", "<3", "Caddyfile v2 syntax"),
    ("ghcr.io/berriai/litellm-non_root", "!=1.82.7", "plan §14: litellm 1.82.7/1.82.8 are banned"),
    ("ghcr.io/berriai/litellm-non_root", "!=1.82.8", "plan §14: litellm 1.82.7/1.82.8 are banned"),
)


@dataclass(frozen=True, order=True)
class Finding:
    rule: str
    file: str
    line: int
    message: str

    def as_json(self) -> str:
        return json.dumps({"rule": self.rule, "file": self.file, "line": self.line, "message": self.message},
                          ensure_ascii=False)


@dataclass(frozen=True)
class Ref:
    name: str
    tag: str | None
    digest: str | None
    file: str
    line: int


# ───────────────────────────── versions ─────────────────────────────
def version_tuple(text: str) -> tuple[int, ...]:
    for p in TAG_PREFIXES:
        if text.startswith(p):
            text = text[len(p):]
    m = re.match(r"v?(\d+(?:\.\d+)*)", text)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def satisfies(version: str, spec: str) -> bool:
    m = re.fullmatch(r"(<=|>=|==|!=|<|>)\s*(\S+)", spec)
    if not m:
        raise ValueError(f"bad version spec {spec!r}")
    op, want = m.groups()
    if op in ("==", "!="):
        eq = version_tuple(version) == version_tuple(want) and len(version_tuple(version)) >= len(version_tuple(want))
        return eq if op == "==" else not eq
    c = _cmp(version_tuple(version), version_tuple(want))
    return {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0}[op]


def tag_problem(name: str, tag: str | None) -> str | None:
    if tag is None:
        return "has no tag (pin an exact tag plus digest: name:tag@sha256:...)"
    if tag in FLOATING_TAGS:
        return f"uses the floating tag '{tag}'"
    if len(version_tuple(tag)) < MIN_TAG_PARTS.get(name, 2) and name not in INEXACT_TAG_OK:
        return f"tag '{tag}' is not an exact version (use e.g. 2.11.7-alpine, not 2-alpine)"
    return None


# ───────────────────────────── files ─────────────────────────────
class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: list[str] | None = None

    def files(self) -> list[str]:
        if self._files is None:
            out = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                rel_dir = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
                rel_dir = "" if rel_dir == "." else rel_dir
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                                     and not any(f"{rel_dir}/{d}".lstrip("/").startswith(s) for s in SKIP_PATHS))
                for f in sorted(filenames):
                    out.append(f"{rel_dir}/{f}".lstrip("/"))
            self._files = out
        return self._files

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8", errors="replace")

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def dockerfiles(self) -> list[str]:
        return [f for f in self.files() if (Path(f).name == "Dockerfile" or f.endswith(".Dockerfile"))]

    def compose_files(self) -> list[str]:
        return [f for f in self.files() if "/" not in f and f not in SKIP_FILES
                and re.fullmatch(r"(docker-)?compose(\.[a-z0-9-]+)?\.ya?ml", f)]

    def script_files(self) -> list[str]:
        out = []
        for f in self.files():
            if re.match(r"^(scripts|infra|e2e)/", f) and f.endswith((".sh", ".ps1")):
                out.append(f)
            elif re.match(r"^\.github/(workflows/[^/]+\.ya?ml|actions/.+/action\.ya?ml)$", f):
                out.append(f)
        return out


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Dockerfile lines with backslash continuations joined (line number of the first line)."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not buf:
            start = i
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append((start, buf + line))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def parse_ref(value: str, file: str, line: int) -> Ref | None:
    value = value.strip().strip("'\"")
    m = REFISH_RE.match(value)
    if not m:
        return None
    return Ref(m.group("name"), m.group("tag"), m.group("digest"), file, line)


def strip_registry_default(name: str) -> str:
    for p in ("docker.io/library/", "docker.io/", "index.docker.io/library/"):
        if name.startswith(p):
            return name[len(p):]
    return name


# ───────────────────────────── checker ─────────────────────────────
class Checker:
    def __init__(self, root: Path) -> None:
        self.repo = Repo(root)
        self.findings: list[Finding] = []
        self.refs: list[Ref] = []  # every pinned/unpinned external reference seen (for P7/P8)
        self.dockerfile_info: dict[str, dict] = {}
        self.registry: reg.Registry | None = None

    def add(self, rule: str, file: str, line: int, message: str) -> None:
        self.findings.append(Finding(rule, file, line, message))

    def check_ref(self, rule: str, ref: Ref, what: str) -> None:
        name = strip_registry_default(ref.name)
        full = f"{ref.name}{':' + ref.tag if ref.tag else ''}{'@' + ref.digest if ref.digest else ''}"
        if LOCAL_RE.match(full):
            return
        self.refs.append(Ref(name, ref.tag, ref.digest, ref.file, ref.line))
        problem = tag_problem(name, ref.tag)
        if problem:
            self.add("P6", ref.file, ref.line, f"{what} {full} {problem}")
        if not ref.digest:
            hint = f"scripts/supply-chain/pin-image.sh {ref.name}:{ref.tag}" if ref.tag else "pin-image.sh"
            self.add(rule, ref.file, ref.line, f"{what} {full} is not pinned by digest (run: {hint})")

    # P1 ─────────────────────────────
    def dockerfiles(self) -> None:
        for f in self.repo.dockerfiles():
            text = self.repo.read(f)
            args: dict[str, str] = {}
            stages: list[str] = []
            targets: list[tuple[str, int]] = []
            arg_lines: dict[str, int] = {}
            used_args: set[str] = set()
            for n, line in logical_lines(text):
                s = line.strip()
                m = re.match(r"^#\s*syntax\s*=\s*(\S+)", s, re.I)
                if m and n <= 3:
                    ref = parse_ref(m.group(1), f, n)
                    if ref:
                        self.check_ref("P1", ref, "# syntax frontend")
                    else:
                        self.add("P1", f, n, f"cannot parse the # syntax reference {m.group(1)}")
                    continue
                if not s or s.startswith("#"):
                    continue
                m = re.match(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?$", s, re.I)
                if m:
                    if m.group(2) is not None:
                        args[m.group(1)] = m.group(2).strip().strip("'\"")
                        arg_lines[m.group(1)] = n
                    continue
                m = re.match(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", s, re.I)
                if m:
                    src, alias = m.group(1), m.group(2)
                    if alias:
                        stages.append(alias.lower())
                        targets.append((alias.lower(), n))
                    self._dockerfile_source(f, n, src, stages[:-1] if alias else stages, args, arg_lines, used_args,
                                            "FROM")
                    continue
                if re.match(r"^(COPY|ADD)\b", s, re.I):
                    for m in re.finditer(r"--from=(\S+)", s):
                        self._dockerfile_source(f, n, m.group(1), stages, args, arg_lines, used_args, "COPY --from")
                elif re.match(r"^RUN\b", s, re.I):
                    for m in re.finditer(r"--mount=\S*?\bfrom=([^,\s]+)", s):
                        self._dockerfile_source(f, n, m.group(1), stages, args, arg_lines, used_args, "RUN --mount from")
            self.dockerfile_info[f] = {"stages": [t for t, _ in targets], "args": args, "targets": targets}

    def _dockerfile_source(self, f: str, n: int, src: str, stages: list[str], args: dict[str, str],
                           arg_lines: dict[str, int], used_args: set[str], what: str) -> None:
        if src.lower() in stages or src.isdigit():
            return
        m = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", src)
        if m:
            var = m.group(1)
            if var not in args:
                self.add("P1", f, n, f"{what} ${{{var}}} has no ARG default (give it a pinned default)")
                return
            if var in used_args:
                return
            used_args.add(var)
            src, n, what = args[var], arg_lines[var], f"ARG {var} default (used by {what})"
            if src.lower() in stages:
                return
        if "$" in src:
            self.add("P1", f, n, f"{what} {src} is built from variables and cannot be checked; use one ARG with a pinned default")
            return
        ref = parse_ref(src, f, n)
        if not ref:
            self.add("P1", f, n, f"{what} {src} is not a valid image reference")
            return
        self.check_ref("P1", ref, what)

    # P2 / compose structure ─────────────────────────────
    def compose(self) -> dict[str, dict[str, dict]]:
        """Return {compose file: {service: {image, build_context, build_dockerfile, line}}}."""
        result: dict[str, dict[str, dict]] = {}
        for f in self.repo.compose_files():
            services: dict[str, dict] = {}
            in_services, current, in_build = False, None, False
            for n, raw in enumerate(self.repo.read(f).splitlines(), 1):
                line = raw.split(" #")[0].rstrip() if not raw.lstrip().startswith("#") else ""
                if not line.strip():
                    continue
                indent = len(line) - len(line.lstrip())
                if indent == 0:
                    in_services = line.startswith("services:")
                    current = None
                    continue
                if not in_services:
                    continue
                if indent == 2 and re.match(r"^  [A-Za-z0-9._-]+:\s*$", line):
                    current = line.strip()[:-1]
                    services[current] = {"line": n}
                    in_build = False
                    continue
                if current is None:
                    continue
                m = re.match(r"^    image:\s*(\S+)", line)
                if m:
                    services[current]["image"] = m.group(1).strip("'\"")
                    services[current]["image_line"] = n
                    ref = parse_ref(m.group(1), f, n)
                    if ref is None:
                        if "$" not in m.group(1):
                            self.add("P2", f, n, f"service {current}: cannot parse image {m.group(1)}")
                        else:
                            self.add("P2", f, n, f"service {current}: image {m.group(1)} is built from variables; pin it literally")
                    else:
                        self.check_ref("P2", ref, f"service {current} image")
                    in_build = False
                    continue
                m = re.match(r"^    build:\s*(\S*)\s*$", line)
                if m:
                    services[current]["build_line"] = n
                    if m.group(1):
                        services[current]["build_context"] = m.group(1).strip("'\"")
                        services[current]["build_dockerfile"] = "Dockerfile"
                        in_build = False
                    else:
                        services[current].setdefault("build_dockerfile", "Dockerfile")
                        in_build = True
                    continue
                if indent == 4:
                    in_build = False
                if in_build:
                    m = re.match(r"^      (context|dockerfile):\s*(\S+)", line)
                    if m:
                        services[current][f"build_{m.group(1)}"] = m.group(2).strip("'\"")
            result[f] = services
        return result

    # P3 ─────────────────────────────
    def env_files(self) -> dict[str, dict[str, tuple[str, int]]]:
        out: dict[str, dict[str, tuple[str, int]]] = {}
        for f in ("infra/cloudrun/versions.env", "infra/supply-chain/signing.env"):
            if not self.repo.exists(f):
                continue
            vals: dict[str, tuple[str, int]] = {}
            for n, line in enumerate(self.repo.read(f).splitlines(), 1):
                m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
                if not m:
                    continue
                vals[m.group(1)] = (m.group(2), n)
                if m.group(1).endswith("_IMAGE"):
                    ref = parse_ref(m.group(2), f, n)
                    if ref is None:
                        self.add("P3", f, n, f"{m.group(1)}={m.group(2)} is not an image reference")
                    else:
                        self.check_ref("P3", ref, m.group(1))
            out[f] = vals
        return out

    # P4 / P5 ─────────────────────────────
    def scripts(self) -> None:
        known = {r.name for r in self.refs} | TOOL_REPOS
        names = "|".join(sorted((re.escape(k) for k in known), key=len, reverse=True))
        literal_re = re.compile(rf"(?<![\w./@:-])({names}):([A-Za-z0-9_][A-Za-z0-9._-]{{0,127}})"
                                r"(?:@(sha256:[a-f0-9]{64}))?(?![\w.-])")
        assign_re = re.compile(r"(?i)(?:^|[\s;(])(?:export\s+|local\s+)?\$?([a-z0-9_]*image[a-z0-9_]*)\s*=\s*"
                               r"(\"[^\"]*\"|'[^']*'|[^\s;)]+)")
        for f in self.repo.script_files():
            is_workflow = f.startswith(".github/")
            seen: set[tuple[str, int]] = set()
            in_block_comment = False  # PowerShell <# ... #>
            for n, raw in enumerate(self.repo.read(f).splitlines(), 1):
                s = raw.strip()
                if f.endswith(".ps1"):
                    if in_block_comment or s.startswith("<#"):
                        in_block_comment = "#>" not in s
                        continue
                if not s or s.startswith("#"):
                    continue
                if is_workflow:
                    self._uses(f, n, raw)
                for m in assign_re.finditer(raw):
                    value = m.group(2).strip("'\"")
                    if "$" in value or (":" not in value and "@" not in value):
                        continue
                    ref = parse_ref(value, f, n)
                    if ref and (ref.name, n) not in seen:
                        seen.add((ref.name, n))
                        self.check_ref("P4", ref, f"{m.group(1)}=")
                for m in literal_re.finditer(raw):
                    if (m.group(1), n) in seen:
                        continue
                    seen.add((m.group(1), n))
                    self.check_ref("P4", Ref(m.group(1), m.group(2), m.group(3), f, n), "image literal")
                if is_workflow:
                    m = re.match(r"^\s*image:\s*(\S+)", raw)
                    if m and "${{" not in m.group(1):
                        ref = parse_ref(m.group(1), f, n)
                        if ref and (ref.name, n) not in seen:
                            seen.add((ref.name, n))
                            self.check_ref("P4", ref, "workflow image")

    def _uses(self, f: str, n: int, raw: str) -> None:
        m = re.match(r"^\s*(?:-\s+)?uses:\s*(['\"]?)([^\s'\"#]+)\1\s*(#.*)?$", raw)
        if not m:
            return
        target, comment = m.group(2), m.group(3) or ""
        if target.startswith("./"):
            return
        if target.startswith("docker://"):
            self.add("P5", f, n, f"uses: {target}: docker:// actions are forbidden (run a pinned image with docker run)")
            return
        if not SHA_USES_RE.match(target):
            self.add("P5", f, n, f"uses: {target} is not pinned to a full 40-hex commit SHA")
        elif not VERSION_COMMENT_RE.search(comment):
            self.add("P5", f, n, f"uses: {target} needs a '# vX.Y.Z' version comment (Renovate keeps both in sync)")

    # P7 ─────────────────────────────
    @staticmethod
    def uv_lock(text: str) -> dict[str, str]:
        return {m.group(1): m.group(2) for m in
                re.finditer(r'^\[\[package\]\]\s*\nname = "([^"]+)"\s*\nversion = "([^"]+)"', text, re.M)}

    @staticmethod
    def pnpm_lock(text: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        section = None
        for line in text.splitlines():
            if line and not line.startswith(" "):
                section = line.rstrip(":")
                continue
            if section != "packages":
                continue
            m = re.match(r"^  '?(@?[^@'\s]+)@([0-9][^:'\s(]*)", line)
            if m:
                out.setdefault(m.group(1), set()).add(m.group(2))
        return out

    def caps(self) -> None:
        locks = [f for f in self.repo.files() if Path(f).name in ("uv.lock", "pnpm-lock.yaml")]
        for f in locks:
            text = self.repo.read(f)
            if f.endswith("uv.lock"):
                pkgs = {k: {v} for k, v in self.uv_lock(text).items()}
            else:
                pkgs = self.pnpm_lock(text)
            for glob, pkg, spec, why, cond in LOCK_CAPS:
                if not (Path(f).match(glob) if "*" in glob else f == glob):
                    continue
                if cond and not any(satisfies(v, cond[1]) for v in pkgs.get(cond[0], set())):
                    continue
                for v in sorted(pkgs.get(pkg, set())):
                    if not satisfies(v, spec):
                        self.add("P7", f, self._line_of(text, pkg, v), f"{pkg} {v} violates the cap {spec}: {why}")
        for ref in self.refs:
            for name, spec, why in IMAGE_CAPS:
                if ref.name == name and ref.tag and version_tuple(ref.tag) and not satisfies(ref.tag, spec):
                    self.add("P7", ref.file, ref.line, f"{name}:{ref.tag} violates the cap {spec}: {why}")

    @staticmethod
    def _line_of(text: str, pkg: str, version: str) -> int:
        for n, line in enumerate(text.splitlines(), 1):
            if pkg in line and version in line:
                return n
        return 0

    # P8 ─────────────────────────────
    def psd1(self) -> dict[str, str]:
        f = "scripts/native/versions.psd1"
        if not self.repo.exists(f):
            return {}
        text = self.repo.read(f)
        out = {}
        for m in re.finditer(r"^\s*(\w+)\s*=\s*@\{(.*?)\}", text, re.M | re.S):
            v = re.search(r"Version\s*=\s*'([^']*)'", m.group(2))
            if v:
                out[m.group(1)] = v.group(1)
        return out

    def consistency(self, compose: dict[str, dict[str, dict]], env: dict[str, dict[str, tuple[str, int]]]) -> None:
        psd1 = self.psd1()
        by_name: dict[str, list[Ref]] = {}
        for r in self.refs:
            by_name.setdefault(r.name, []).append(r)

        def same(name: str, what: str) -> None:
            refs = by_name.get(name, [])
            variants = sorted({f"{r.tag}@{r.digest}" for r in refs if r.digest})
            if len(variants) > 1:
                where = ", ".join(sorted({f"{r.file}:{r.line} ({r.tag})" for r in refs}))
                self.add("P8", refs[0].file, refs[0].line,
                         f"{what}: {name} is pinned to {len(variants)} different tag/digest pairs: {where}")

        for name in sorted(by_name):
            same(name, "one pin per image")
        # tool versions shared with the Docker-free Windows mode (scripts/native/versions.psd1)
        uv_tags = {r.tag for r in by_name.get("ghcr.io/astral-sh/uv", []) if r.tag and "-python" not in r.tag}
        if psd1.get("uv") and uv_tags and uv_tags != {psd1["uv"]}:
            self.add("P8", "scripts/native/versions.psd1", 0, f"uv {psd1['uv']} differs from the Dockerfile uv image tags {sorted(uv_tags)}")
        pnpm = self._pnpm_versions()
        if psd1.get("pnpm"):
            pnpm.setdefault(psd1["pnpm"], []).append("scripts/native/versions.psd1")
        if len(pnpm) > 1:
            self.add("P8", "package.json", 0, "pnpm versions differ: " + "; ".join(f"{v}: {', '.join(sorted(set(w)))}" for v, w in sorted(pnpm.items())))
        self._release_age()
        engine_lock = self.uv_lock(self.repo.read("services/engine/uv.lock")) if self.repo.exists("services/engine/uv.lock") else {}
        litellm_versions = {"services/engine/uv.lock": engine_lock.get("litellm"), "scripts/native/versions.psd1": psd1.get("litellm")}
        for r in by_name.get("ghcr.io/berriai/litellm-non_root", []):
            litellm_versions[f"{r.file}:{r.line}"] = r.tag
        self._all_equal("litellm (python package = proxy image tag)", litellm_versions)
        kc = {f"{r.file}:{r.line}": r.tag for r in by_name.get("quay.io/keycloak/keycloak", [])}
        kc["scripts/native/versions.psd1"] = psd1.get("keycloak")
        self._all_equal("keycloak", kc)
        for r in by_name.get("adorsys/keycloak-config-cli", []):
            if psd1.get("kcconfig") and r.tag and not psd1["kcconfig"].startswith(r.tag + "."):
                self.add("P8", r.file, r.line, f"keycloak-config-cli {r.tag} differs from versions.psd1 kcconfig {psd1['kcconfig']}")
        for r in by_name.get("caddy", []):
            if psd1.get("caddy") and r.tag and r.tag.split("-")[0] != psd1["caddy"]:
                self.add("P8", r.file, r.line, f"caddy {r.tag} differs from versions.psd1 caddy {psd1['caddy']}")
        # overlay ARG defaults = versions.env (deploy.sh passes versions.env, local builds use the default)
        venv = env.get("infra/cloudrun/versions.env", {})
        for f, info in sorted(self.dockerfile_info.items()):
            if not f.startswith("infra/cloudrun/images/"):
                continue
            for arg, default in sorted(info["args"].items()):
                if arg in venv and venv[arg][0] != default:
                    self.add("P8", f, 0, f"ARG {arg} default {default} differs from versions.env {venv[arg][0]}")
        # one version of the shared Python packages across the uv projects
        uv_locks = {f: self.uv_lock(self.repo.read(f)) for f in self.repo.files() if Path(f).name == "uv.lock"}
        for pkg, scope in (("pydantic", None), ("mcp", ("engine", "merxa-api", "signal-hub")),
                           ("fastmcp", ("engine", "merxa-api", "signal-hub"))):
            versions = {f: lk.get(pkg) for f, lk in uv_locks.items()
                        if lk.get(pkg) and (scope is None or any(f"services/{s}/" in f for s in scope))}
            self._all_equal(f"{pkg} across the uv projects", versions)
        # AG-UI protocol: Python ag-ui-protocol and TS @ag-ui/core share major.minor
        agui_py = engine_lock.get("ag-ui-protocol")
        agui_ts = self.pnpm_lock(self.repo.read("pnpm-lock.yaml")).get("@ag-ui/core", set()) if self.repo.exists("pnpm-lock.yaml") else set()
        for v in sorted(agui_ts):
            if agui_py and version_tuple(v)[:2] != version_tuple(agui_py)[:2]:
                self.add("P8", "pnpm-lock.yaml", 0, f"@ag-ui/core {v} and ag-ui-protocol {agui_py} differ in major.minor")
        # Playwright: the npm package and the browser image move together
        pw_pkg = "e2e/playwright/package.json"
        if self.repo.exists(pw_pkg):
            want = json.loads(self.repo.read(pw_pkg)).get("devDependencies", {}).get("@playwright/test")
            for r in by_name.get("mcr.microsoft.com/playwright", []):
                if want and r.tag and version_tuple(r.tag) != version_tuple(want):
                    self.add("P8", r.file, r.line, f"playwright image {r.tag} differs from @playwright/test {want}")
        # Memorystore major = local Valkey major
        vev = venv.get("VALKEY_ENGINE_VERSION")
        if vev:
            major = re.match(r"VALKEY_(\d+)", vev[0])
            for r in by_name.get("valkey/valkey", []):
                if major and r.tag and version_tuple(r.tag) and version_tuple(r.tag)[:1] != (int(major.group(1)),):
                    self.add("P8", r.file, r.line, f"valkey {r.tag} differs from Memorystore {vev[0]} (versions.env)")

    def _pnpm_versions(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for f in ("package.json", "apps/merxa-web/package.json"):
            if self.repo.exists(f):
                pm = json.loads(self.repo.read(f)).get("packageManager", "")
                if pm.startswith("pnpm@"):
                    out.setdefault(pm.split("@", 1)[1].split("+")[0], []).append(f)
        for f in self.repo.dockerfiles():
            for m in re.finditer(r"corepack prepare pnpm@(\d+\.\d+\.\d+)", self.repo.read(f)):
                out.setdefault(m.group(1), []).append(f)
        return out

    PNPM_PROJECTS = ("package.json", "apps/merxa-web/package.json")

    def _release_age(self) -> None:
        """pnpm's ``minimumReleaseAge`` (minutes; in the pnpm-workspace.yaml next to each pnpm project)
        equals renovate.json ``minimumReleaseAge`` ("N days"), so manual installs cool off like updates."""
        if not self.repo.exists("renovate.json"):
            return
        try:
            age = json.loads(self.repo.read("renovate.json")).get("minimumReleaseAge")
        except json.JSONDecodeError:
            return  # malformed JSON is the renovate-config-validator's finding
        m = re.fullmatch(r"(\d+) days?", str(age or "").strip())
        if not m:
            self.add("P8", "renovate.json", 0, f"minimumReleaseAge {age!r} is not 'N days' (pnpm needs the same minimum)")
            return
        want = str(int(m.group(1)) * 24 * 60)
        for pkg in self.PNPM_PROJECTS:
            if not self.repo.exists(pkg) or not json.loads(self.repo.read(pkg)).get("packageManager", "").startswith("pnpm@"):
                continue
            ws = posixpath.join(posixpath.dirname(pkg), "pnpm-workspace.yaml")
            text = self.repo.read(ws) if self.repo.exists(ws) else ""
            got = re.search(r"^minimumReleaseAge:\s*(\S+?)\s*(?:#.*)?$", text, re.M)
            if not got:
                self.add("P8", ws, 0, f"pnpm minimumReleaseAge is not set (renovate.json minimumReleaseAge {age} = {want} minutes)")
            elif got.group(1) != want:
                self.add("P8", ws, text[:got.start()].count("\n") + 1, f"pnpm minimumReleaseAge {got.group(1)} differs "
                                                                        f"from renovate.json minimumReleaseAge {age} (= {want} minutes)")

    def _all_equal(self, what: str, values: dict[str, str | None]) -> None:
        present = {k: v for k, v in values.items() if v}
        if len(set(present.values())) > 1:
            self.add("P8", sorted(present)[0].split(":")[0], 0,
                     f"{what} differ: " + ", ".join(f"{k}={v}" for k, v in sorted(present.items())))

    # P9 ─────────────────────────────
    def registry_coverage(self, compose: dict[str, dict[str, dict]]) -> None:
        rp = str(reg.REGISTRY_PATH).replace(os.sep, "/")
        try:
            self.registry = reg.load(self.repo.root)
        except reg.RegistryError as exc:
            for p in exc.problems:
                self.add("P9", rp, 0, p)
            return
        r = self.registry
        registered = {i.dockerfile for i in r.images}
        for f in self.repo.dockerfiles():
            if f not in registered:
                self.add("P9", f, 0, f"Dockerfile {f} is not registered in {rp}: add an entry "
                                     "(name, dockerfile, context, target, release, suites; docs/supply-chain.md)")
        for img in r.images:
            if not self.repo.exists(img.dockerfile):
                self.add("P9", rp, 0, f"image '{img.name}': dockerfile {img.dockerfile} does not exist")
                continue
            if not (self.repo.root / img.context).is_dir():
                self.add("P9", rp, 0, f"image '{img.name}': context {img.context} is not a directory")
            stages = self.dockerfile_info.get(img.dockerfile, {}).get("stages", [])
            for t in [img.target] + [s.target for s in img.suites]:
                if t and t not in stages:
                    self.add("P9", rp, 0, f"image '{img.name}': target '{t}' is not a stage of {img.dockerfile}")
            covered = {s.target for s in img.suites}
            for stage, line in self.dockerfile_info.get(img.dockerfile, {}).get("targets", []):
                if (stage == "test" or stage.endswith("-test")) and stage not in covered:
                    self.add("P9", img.dockerfile, line, f"stage '{stage}' has no CI suite in {rp} (image '{img.name}')")
            if img.verify_upstream:
                if not self.repo.exists(img.verify_upstream["pubkey"]):
                    self.add("P9", rp, 0, f"image '{img.name}': verify_upstream key {img.verify_upstream['pubkey']} does not exist")
        # compose builds -> registered Dockerfiles; release images -> compose-release.sh map
        by_dockerfile = {i.dockerfile: i for i in r.images if not i.overlay_of}
        release_services: dict[str, str] = {}
        for f, services in sorted(compose.items()):
            for svc, info in sorted(services.items()):
                if "build_line" not in info:
                    continue
                ctx = os.path.normpath(info.get("build_context", ".")).replace(os.sep, "/")
                df = os.path.normpath(os.path.join(ctx, info.get("build_dockerfile", "Dockerfile"))).replace(os.sep, "/")
                if df not in registered:
                    self.add("P9", f, info["build_line"], f"service {svc} builds {df}, which is not registered in {rp}")
                elif df in by_dockerfile and by_dockerfile[df].release and f == "compose.yaml":
                    release_services[svc] = by_dockerfile[df].release_key
        crm = self._compose_release_map()
        if crm is not None and crm != release_services:
            diff = sorted(set(crm.items()) ^ set(release_services.items()))
            self.add("P9", "scripts/supply-chain/compose-release.sh", 0,
                     f"COMPOSE_RELEASE_MAP differs from the compose.yaml services that build release images: {diff}")
        # deploy.sh image variables <-> deploy_var
        deploy_vars = self._deploy_image_vars()
        if deploy_vars is not None:
            want = {i.deploy_var for i in r.images if i.deploy_var}
            for v in sorted(deploy_vars - want):
                self.add("P9", "scripts/cloudrun/deploy.sh", 0, f"{v} is deployed but no release image in {rp} feeds it (deploy_var)")
            for v in sorted(want - deploy_vars):
                self.add("P9", rp, 0, f"deploy_var {v} is not an image variable of scripts/cloudrun/deploy.sh")
        release_map = self._deploy_release_map()
        if release_map is not None:
            want_map = {i.deploy_var: i.release_key for i in r.images if i.deploy_var}
            if release_map != want_map:
                diff = sorted(set(release_map.items()) ^ set(want_map.items()))
                self.add("P9", "scripts/cloudrun/deploy.sh", 0,
                         f"RELEASE_IMAGE_MAP differs from the deploy_var/name pairs in {rp}: {diff}")

    def _deploy_image_vars(self) -> set[str] | None:
        f = "scripts/cloudrun/deploy.sh"
        if not self.repo.exists(f):
            return None
        text = self.repo.read(f)
        m = re.search(r"^RENDER_VARS=\((.*?)\)", text, re.S | re.M)
        out = set(re.findall(r"\bIMAGE_[A-Z0-9_]+\b", m.group(1))) if m else set()
        # deploy.sh hooks (infra/cloudrun/signal-hub.sh) append their own image variables
        for hook in self.repo.files():
            if re.fullmatch(r"infra/cloudrun/[^/]+\.sh", hook):
                for block in re.findall(r"RENDER_VARS\+=\(([^)]*)\)", self.repo.read(hook)):
                    out |= set(re.findall(r"\bIMAGE_[A-Z0-9_]+\b", block))
        # third-party images mirrored from a versions.env pin (no first-party build, e.g. Phoenix)
        mirrored = set(re.findall(r"\bmirror (IMAGE_[A-Z0-9_]+)", text))
        return out - mirrored

    def _compose_release_map(self) -> dict[str, str] | None:
        f = "scripts/supply-chain/compose-release.sh"
        if not self.repo.exists(f):
            return None
        m = re.search(r"^COMPOSE_RELEASE_MAP=\((.*?)\)", self.repo.read(f), re.S | re.M)
        return dict(re.findall(r"\b([a-z0-9-]+):([A-Z0-9_]+)\b", m.group(1))) if m else None

    def _deploy_release_map(self) -> dict[str, str] | None:
        f = "scripts/cloudrun/deploy.sh"
        if not self.repo.exists(f):
            return None
        m = re.search(r"^RELEASE_IMAGE_MAP=\((.*?)\)", self.repo.read(f), re.S | re.M)
        if not m:
            return None
        pairs = re.findall(r"\b(IMAGE_[A-Z0-9_]+):([A-Z0-9_]+)\b", m.group(1))
        return dict(pairs)

    # ─────────────────────────────
    def run(self) -> list[Finding]:
        self.dockerfiles()
        compose = self.compose()
        env = self.env_files()
        self.scripts()
        self.caps()
        self.consistency(compose, env)
        self.registry_coverage(compose)
        return sorted(set(self.findings))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2], help="repository root")
    ap.add_argument("--rule", default="", help="only report these rules (comma separated, e.g. P1,P9)")
    args = ap.parse_args(argv)
    findings = Checker(args.root.resolve()).run()
    if args.rule:
        keep = {r.strip().upper() for r in args.rule.split(",") if r.strip()}
        findings = [f for f in findings if f.rule in keep]
    for f in findings:
        print(f.as_json())
    counts = Counter(f.rule for f in findings)
    summary = ", ".join(f"{r}: {counts[r]}" for r in sorted(counts)) or "none"
    print(f"check_pins: {len(findings)} violation(s) ({summary})", file=sys.stderr)
    return min(len(findings), 125)


if __name__ == "__main__":
    sys.exit(main())
