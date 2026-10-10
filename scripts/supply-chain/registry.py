"""infra/supply-chain/images.json: the registry of first-party images and CI test suites.

Stdlib only (runs on GitHub runners, in ``python:3.14.8-slim-trixie`` and from Git Bash). Shared by
``check_pins.py`` (coverage rules), ``matrix.py`` (GitHub Actions matrices), ``ci_run_suite.py``
(local == CI suite runs) and ``release_manifest.py`` (signed release file). Spec:
docs/contracts/129-m8-supply-chain.md §3, how-to: docs/supply-chain.md.

Every Dockerfile in the repository is registered here exactly once. ``load()`` validates the file
strictly and raises :class:`RegistryError` with every problem it finds.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "keelson.images/1"
REGISTRY_PATH = Path("infra") / "supply-chain" / "images.json"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}[a-z0-9]$")
SUITE_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}[a-z0-9]$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DEPLOY_VAR_RE = re.compile(r"^IMAGE_[A-Z0-9_]+$")
ARG_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# name:tag@sha256:<64 hex> - an exact tag plus the immutable index digest
PINNED_REF_RE = re.compile(
    r"^(?P<name>[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})@(?P<digest>sha256:[a-f0-9]{64})$"
)
SUITE_DBS = ("pg-core",)
SUITE_KINDS = ("run", "build")


class RegistryError(ValueError):
    """images.json is malformed; ``problems`` lists every finding (one sentence each)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Deselect:
    nodeid: str
    reason: str


@dataclass(frozen=True)
class Suite:
    """One CI test suite. ``image`` is set for image suites (a Dockerfile target), ``tool_image``
    for suites that run in a pinned toolchain image with the repository mounted read-only."""

    id: str
    kind: str  # run | build
    image: str | None = None
    target: str | None = None
    tool_image: str | None = None
    workdir: str | None = None
    db: str | None = None
    valkey: bool = False
    memory: str = "1500m"
    env: dict[str, str] = field(default_factory=dict)
    cmd: tuple[str, ...] | None = None
    deselect: tuple[Deselect, ...] = ()


@dataclass(frozen=True)
class Image:
    name: str
    dockerfile: str
    context: str
    target: str | None
    release: bool
    deploy_var: str | None = None
    overlay_of: str | None = None
    overlay_arg: str | None = None
    domain_overlay: bool = False
    verify_upstream: dict[str, str] | None = None
    suites: tuple[Suite, ...] = ()

    @property
    def release_key(self) -> str:
        """KEY suffix in keelson-release.env: RELEASE_IMAGE_<KEY>."""
        return self.name.upper().replace("-", "_")


@dataclass(frozen=True)
class Registry:
    images: tuple[Image, ...]
    tool_suites: tuple[Suite, ...]

    def image(self, name: str) -> Image:
        for img in self.images:
            if img.name == name:
                return img
        raise KeyError(name)

    @property
    def suites(self) -> tuple[Suite, ...]:
        return tuple(s for img in self.images for s in img.suites) + self.tool_suites

    def suite(self, suite_id: str) -> Suite:
        for s in self.suites:
            if s.id == suite_id:
                return s
        raise KeyError(suite_id)

    @property
    def release_images(self) -> tuple[Image, ...]:
        return tuple(i for i in self.images if i.release)


def _str(d: dict[str, Any], key: str, where: str, problems: list[str], *, required: bool = True) -> str | None:
    v = d.get(key)
    if v is None:
        if required:
            problems.append(f"{where}: '{key}' is required")
        return None
    if not isinstance(v, str) or not v:
        problems.append(f"{where}: '{key}' must be a non-empty string")
        return None
    return v


def _suite(raw: Any, where: str, problems: list[str], *, image: str | None) -> Suite | None:
    if not isinstance(raw, dict):
        problems.append(f"{where}: a suite must be an object")
        return None
    allowed = {"id", "kind", "target", "image", "workdir", "db", "valkey", "memory", "env", "cmd", "deselect", "comment"}
    for k in set(raw) - allowed:
        problems.append(f"{where}: unknown key '{k}'")
    sid = _str(raw, "id", where, problems)
    where = f"{where} '{sid}'"
    if sid and not SUITE_RE.match(sid):
        problems.append(f"{where}: id must match {SUITE_RE.pattern}")
    kind = raw.get("kind", "run")
    if kind not in SUITE_KINDS:
        problems.append(f"{where}: kind must be one of {SUITE_KINDS}")
    target = tool_image = workdir = None
    if image is not None:
        target = _str(raw, "target", where, problems)
        if "image" in raw or "workdir" in raw:
            problems.append(f"{where}: image suites take 'target', not 'image'/'workdir'")
    else:
        tool_image = _str(raw, "image", where, problems)
        workdir = _str(raw, "workdir", where, problems)
        if tool_image and not PINNED_REF_RE.match(tool_image):
            problems.append(f"{where}: image must be pinned as name:tag@sha256:<digest>")
        if kind != "run":
            problems.append(f"{where}: tool suites must be kind 'run'")
    db = raw.get("db")
    if db is not None and db not in SUITE_DBS:
        problems.append(f"{where}: db must be null or one of {SUITE_DBS}")
    valkey = raw.get("valkey", False)
    if not isinstance(valkey, bool):
        problems.append(f"{where}: valkey must be a boolean")
    memory = raw.get("memory", "1500m")
    if not isinstance(memory, str) or not re.fullmatch(r"[0-9]+[mg]", memory):
        problems.append(f"{where}: memory must look like 1500m or 2g")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and ENV_KEY_RE.match(k) and isinstance(v, str)
                                            for k, v in env.items()):
        problems.append(f"{where}: env must map UPPER_CASE names to strings")
        env = {}
    cmd = raw.get("cmd")
    if cmd is not None and (not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd)):
        problems.append(f"{where}: cmd must be a non-empty list of strings (or omitted: the image CMD)")
        cmd = None
    if image is None and cmd is None:
        problems.append(f"{where}: tool suites need a cmd")
    if kind == "build" and (cmd is not None or db or valkey):
        problems.append(f"{where}: build suites only build their target (no cmd/db/valkey)")
    deselect = []
    for i, d in enumerate(raw.get("deselect", [])):
        if (not isinstance(d, dict) or not isinstance(d.get("nodeid"), str) or "::" not in d["nodeid"]
                or not isinstance(d.get("reason"), str) or len(d["reason"]) < 10):
            problems.append(f"{where}: deselect[{i}] needs a pytest nodeid and a reason (>= 10 chars)")
            continue
        deselect.append(Deselect(d["nodeid"], d["reason"]))
    if not sid:
        return None
    return Suite(id=sid, kind=kind, image=image, target=target, tool_image=tool_image, workdir=workdir,
                 db=db, valkey=bool(valkey), memory=memory if isinstance(memory, str) else "1500m",
                 env=dict(env), cmd=tuple(cmd) if cmd else None, deselect=tuple(deselect))


def parse(data: Any) -> Registry:
    """Validate the decoded images.json document; raise RegistryError listing every problem."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise RegistryError(["images.json must be a JSON object"])
    if data.get("schema") != SCHEMA:
        problems.append(f"schema must be '{SCHEMA}'")
    for k in set(data) - {"schema", "comment", "images", "suites"}:
        problems.append(f"unknown top-level key '{k}'")
    images: list[Image] = []
    raw_images = data.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        problems.append("'images' must be a non-empty list")
        raw_images = []
    allowed = {"name", "dockerfile", "context", "target", "release", "deploy_var", "overlay_of", "overlay_arg",
               "domain_overlay", "verify_upstream", "suites", "comment"}
    for idx, raw in enumerate(raw_images):
        where = f"images[{idx}]"
        if not isinstance(raw, dict):
            problems.append(f"{where}: must be an object")
            continue
        for k in set(raw) - allowed:
            problems.append(f"{where}: unknown key '{k}'")
        name = _str(raw, "name", where, problems)
        where = f"image '{name or idx}'"
        if name and not NAME_RE.match(name):
            problems.append(f"{where}: name must match {NAME_RE.pattern}")
        dockerfile = _str(raw, "dockerfile", where, problems)
        context = _str(raw, "context", where, problems)
        target = _str(raw, "target", where, problems, required=False)
        release = raw.get("release")
        if not isinstance(release, bool):
            problems.append(f"{where}: release must be true or false")
            release = False
        deploy_var = _str(raw, "deploy_var", where, problems, required=False)
        if deploy_var and not DEPLOY_VAR_RE.match(deploy_var):
            problems.append(f"{where}: deploy_var must look like IMAGE_<NAME>")
        if deploy_var and not release:
            problems.append(f"{where}: only release images can feed a deploy_var")
        overlay_of = _str(raw, "overlay_of", where, problems, required=False)
        overlay_arg = _str(raw, "overlay_arg", where, problems, required=False)
        if bool(overlay_of) != bool(overlay_arg):
            problems.append(f"{where}: overlay_of and overlay_arg go together")
        if overlay_arg and not ARG_RE.match(overlay_arg):
            problems.append(f"{where}: overlay_arg must be a Dockerfile ARG name")
        domain_overlay = raw.get("domain_overlay", False)
        if not isinstance(domain_overlay, bool):
            problems.append(f"{where}: domain_overlay must be a boolean")
            domain_overlay = False
        vu = raw.get("verify_upstream")
        if vu is not None and (not isinstance(vu, dict) or set(vu) != {"versions_env_key", "pubkey"}
                               or not all(isinstance(v, str) and v for v in vu.values())):
            problems.append(f"{where}: verify_upstream must be {{versions_env_key, pubkey}}")
            vu = None
        suites = []
        raw_suites = raw.get("suites", [])
        if not isinstance(raw_suites, list):
            problems.append(f"{where}: suites must be a list")
            raw_suites = []
        for sidx, rs in enumerate(raw_suites):
            s = _suite(rs, f"{where} suites[{sidx}]", problems, image=name)
            if s:
                suites.append(s)
        if name and dockerfile and context:
            images.append(Image(name=name, dockerfile=dockerfile, context=context, target=target, release=release,
                                deploy_var=deploy_var, overlay_of=overlay_of, overlay_arg=overlay_arg,
                                domain_overlay=domain_overlay, verify_upstream=vu, suites=tuple(suites)))
    tool_suites = []
    raw_tool = data.get("suites", [])
    if not isinstance(raw_tool, list):
        problems.append("'suites' must be a list")
        raw_tool = []
    for sidx, rs in enumerate(raw_tool):
        s = _suite(rs, f"suites[{sidx}]", problems, image=None)
        if s:
            tool_suites.append(s)
    reg = Registry(images=tuple(images), tool_suites=tuple(tool_suites))
    names = [i.name for i in reg.images]
    for n in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"image name '{n}' is registered more than once")
    ids = [s.id for s in reg.suites]
    for n in sorted({n for n in ids if ids.count(n) > 1}):
        problems.append(f"suite id '{n}' is used more than once")
    dfs = [(i.dockerfile, i.target, i.overlay_of) for i in reg.images]
    for d in sorted({d for d in dfs if dfs.count(d) > 1}, key=str):
        problems.append(f"Dockerfile {d[0]} (target {d[1]}) is registered more than once")
    deploy_vars = [i.deploy_var for i in reg.images if i.deploy_var]
    for v in sorted({v for v in deploy_vars if deploy_vars.count(v) > 1}):
        problems.append(f"deploy_var {v} is fed by more than one image")
    for img in reg.images:
        if img.overlay_of:
            if img.overlay_of not in names:
                problems.append(f"image '{img.name}': overlay_of '{img.overlay_of}' is not a registered image")
            elif not reg.image(img.overlay_of).release or not img.release:
                problems.append(f"image '{img.name}': an overlay and its base must both be release images")
    if problems:
        raise RegistryError(problems)
    return reg


def load(root: Path) -> Registry:
    path = root / REGISTRY_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError([f"{REGISTRY_PATH} not found"]) from exc
    except json.JSONDecodeError as exc:
        raise RegistryError([f"{REGISTRY_PATH}: invalid JSON ({exc})"]) from exc
    return parse(data)
