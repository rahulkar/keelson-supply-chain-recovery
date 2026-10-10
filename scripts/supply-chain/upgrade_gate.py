#!/usr/bin/env python3
"""Upgrade gate for the LLM/agent stack (plan §21, docs/contracts/129-m8-supply-chain.md §5).

    python3 scripts/supply-chain/upgrade_gate.py --base <git ref> [--summary FILE]
    python3 scripts/supply-chain/upgrade_gate.py --base-dir DIR [--head-dir DIR]      # tests / local

Diffs the lockfiles (every ``uv.lock`` and ``pnpm-lock.yaml``, ``evals/requirements.txt``), ``models.lock``,
``infra/litellm/config.yaml`` and the LiteLLM proxy image pins between the base and the head of a pull
request, directly and transitively. ADK, LiteLLM and MCP release weekly and break often, so:

* a change of google-adk, google-genai, litellm (package or proxy image), mcp, fastmcp,
  ag-ui-protocol, @ag-ui/core or an opentelemetry-* package of the engine needs the PR label
  ``spike-verified`` (S1-S3 re-run, see the checklist written to the job summary);
* a change of models.lock, the LiteLLM config, google-adk, google-genai or litellm needs the label
  ``replays-rerecorded`` (LLM replays are keyed by request shape and model id, runtime/replay.py).

PR labels come from the environment variable ``PR_LABELS`` (a JSON list, e.g.
``toJSON(github.event.pull_request.labels.*.name)``); they are parsed as JSON and never reach a shell.
Writes a Markdown report to ``--summary`` (default: $GITHUB_STEP_SUMMARY when set). Exit status: 0 when
every required label is present, 1 otherwise, 2 on usage errors. Stdlib only; no network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SPIKE_LABEL = "spike-verified"
REPLAY_LABEL = "replays-rerecorded"
SPIKE_PACKAGES = {"google-adk", "google-genai", "litellm", "mcp", "fastmcp", "ag-ui-protocol", "@ag-ui/core"}
REPLAY_PACKAGES = {"google-adk", "google-genai", "litellm"}
ENGINE_LOCK = "services/engine/uv.lock"
REPLAY_FILES = ("models.lock", "infra/litellm/config.yaml")
LITELLM_IMAGE = "ghcr.io/berriai/litellm-non_root"
LITELLM_IMAGE_FILES = ("compose.yaml", "infra/cloudrun/versions.env", "infra/cloudrun/images/litellm.Dockerfile")
LOCK_NAMES = ("uv.lock", "pnpm-lock.yaml")
REQUIREMENTS = ("evals/requirements.txt",)

Reader = Callable[[str], str | None]


@dataclass(frozen=True, order=True)
class Change:
    file: str
    package: str
    before: str
    after: str


@dataclass
class Report:
    changes: list[Change] = field(default_factory=list)
    file_changes: list[str] = field(default_factory=list)
    spike: list[str] = field(default_factory=list)
    replay: list[str] = field(default_factory=list)

    def required(self) -> set[str]:
        out = set()
        if self.spike:
            out.add(SPIKE_LABEL)
        if self.replay:
            out.add(REPLAY_LABEL)
        return out


# ───────────────────────────── lockfile parsing ─────────────────────────────
def uv_lock(text: str) -> dict[str, str]:
    """name -> every locked version, comma separated (uv.lock repeats a package per resolution fork)."""
    out: dict[str, set[str]] = {}
    for m in re.finditer(r'^\[\[package\]\]\s*\nname = "([^"]+)"\s*\nversion = "([^"]+)"', text, re.M):
        out.setdefault(m.group(1), set()).add(m.group(2))
    return {k: ",".join(sorted(v)) for k, v in out.items()}


def pnpm_lock(text: str) -> dict[str, str]:
    out: dict[str, set[str]] = {}
    section = None
    for line in text.splitlines():
        if line and not line.startswith(" "):
            section = line.rstrip(":")
            continue
        if section == "packages":
            m = re.match(r"^  '?(@?[^@'\s]+)@([0-9][^:'\s(]*)", line)
            if m:
                out.setdefault(m.group(1), set()).add(m.group(2))
    return {k: ",".join(sorted(v)) for k, v in out.items()}


def requirements(text: str) -> dict[str, str]:
    """name (PEP 503 normalized: Google_ADK -> google-adk; extras dropped) -> pinned version."""
    return {re.sub(r"[-_.]+", "-", m.group(1)).lower(): m.group(2) for m in
            re.finditer(r"^[ \t]*([A-Za-z0-9_.-]+)[ \t]*(?:\[[^\]]*\])?[ \t]*==[ \t]*([^\s;#]+)", text, re.M)}


def litellm_image(text: str) -> str:
    refs = re.findall(rf"{re.escape(LITELLM_IMAGE)}:[A-Za-z0-9._-]+(?:@sha256:[a-f0-9]{{64}})?", text)
    return ",".join(sorted(set(refs)))


def parse(path: str, text: str) -> dict[str, str]:
    name = Path(path).name
    if name == "uv.lock":
        return uv_lock(text)
    if name == "pnpm-lock.yaml":
        return pnpm_lock(text)
    return requirements(text)


# ───────────────────────────── diff ─────────────────────────────
def gated_spike(path: str, package: str) -> bool:
    if package in SPIKE_PACKAGES:
        return True
    return path == ENGINE_LOCK and package.startswith("opentelemetry-")


def evaluate(paths: list[str], base: Reader, head: Reader) -> Report:
    rep = Report()
    for path in sorted(set(paths)):
        b, h = base(path), head(path)
        if b == h:
            continue
        if path in REPLAY_FILES:
            rep.file_changes.append(path)
            rep.replay.append(f"{path} changed")
            continue
        if path in LITELLM_IMAGE_FILES:
            bi, hi = litellm_image(b or ""), litellm_image(h or "")
            if bi != hi:
                rep.changes.append(Change(path, LITELLM_IMAGE, bi or "-", hi or "-"))
                rep.spike.append(f"{LITELLM_IMAGE} in {path}")
            continue
        bp, hp = parse(path, b or ""), parse(path, h or "")
        for pkg in sorted(set(bp) | set(hp)):
            if bp.get(pkg) == hp.get(pkg):
                continue
            rep.changes.append(Change(path, pkg, bp.get(pkg, "-"), hp.get(pkg, "-")))
            if gated_spike(path, pkg):
                rep.spike.append(f"{pkg} in {path}")
            if pkg in REPLAY_PACKAGES:
                rep.replay.append(f"{pkg} in {path}")
    return rep


def parse_labels(raw: str | None) -> set[str]:
    """PR labels from JSON (a list of names, or of {"name": ...} objects). Anything else -> empty."""
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    out = set()
    for item in data:
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            out.add(item["name"])
    return out


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("`", "'").replace("<", "&lt;").replace(">", "&gt;")


def summary(rep: Report, labels: set[str]) -> str:
    missing = sorted(rep.required() - labels)
    lines = ["## Upgrade gate (ADK / LiteLLM / MCP / AG-UI)", ""]
    if not rep.required():
        lines.append("No gated dependency, model or LiteLLM configuration changed: nothing to attest.")
    elif missing:
        lines.append(f"**Blocked**: add the label(s) {', '.join(f'`{m}`' for m in missing)} after completing the checklist.")
    else:
        lines.append("**Passed**: the required labels are present (" + ", ".join(f"`{x}`" for x in sorted(rep.required())) + ").")
    if rep.changes or rep.file_changes:
        lines += ["", "| File | Package | Base | Head | Gate |", "|---|---|---|---|---|"]
        for c in sorted(rep.changes):
            gate = [g for g, items in ((SPIKE_LABEL, rep.spike), (REPLAY_LABEL, rep.replay))
                    if f"{c.package} in {c.file}" in items]
            lines.append(f"| {md_escape(c.file)} | {md_escape(c.package)} | {md_escape(c.before)} | "
                         f"{md_escape(c.after)} | {', '.join(gate) or '-'} |")
        for f in rep.file_changes:
            lines.append(f"| {md_escape(f)} | (file) | | changed | {REPLAY_LABEL} |")
    if SPIKE_LABEL in rep.required():
        lines += ["", f"### `{SPIKE_LABEL}` checklist (plan §19 M0 spikes)", "",
                  "- [ ] S1 live: `pytest -m live tests/runtime/test_live_s1.py` against a live LiteLLM alias "
                  "(needs Gemini credits; while credits are depleted the upgrade stays frozen)",
                  "- [ ] S2 connectors: engine `tests/platform/test_admin_connectors.py`, `test_api_connectors.py`, "
                  "merxa-api `tests/test_mcp*.py` (real FastMCP 4)",
                  "- [ ] S3 durability: engine worker / replay / resume / crash suites, plus a manual `kill -9` of "
                  "engine-worker during investigate and execute in compose",
                  "- [ ] Fake-model suites green in CI (engine-platform, engine-runtime)"]
    if REPLAY_LABEL in rep.required():
        lines += ["", f"### `{REPLAY_LABEL}` checklist", "",
                  "- [ ] LLM replays re-recorded (`LLM_REPLAY_MODE=record`) where request shapes or model ids changed",
                  "- [ ] Offline e2e under `LLM_REPLAY_MODE=replay`: `e2e/api_m5_offline.py`, `e2e/api_m6_offline.py`, "
                  "`e2e/api_m7_offline.py` (and the M8 offline scripts)"]
    return "\n".join(lines) + "\n"


# ───────────────────────────── readers ─────────────────────────────
def dir_reader(root: Path) -> Reader:
    def read(path: str) -> str | None:
        p = root / path
        return p.read_text(encoding="utf-8") if p.is_file() else None
    return read


def dir_paths(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", ".native", ".next"}]
        for f in filenames:
            rel = (Path(dirpath) / f).relative_to(root).as_posix()
            if f in LOCK_NAMES or rel in REQUIREMENTS or rel in REPLAY_FILES or rel in LITELLM_IMAGE_FILES:
                out.append(rel)
    return out


def git_reader(repo: Path, ref: str) -> Reader:
    def read(path: str) -> str | None:
        res = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True)
        return res.stdout if res.returncode == 0 else None
    return read


def git_paths(repo: Path, ref: str) -> list[str]:
    res = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref], cwd=repo, capture_output=True, text=True,
                         check=True)
    return [p for p in res.stdout.splitlines()
            if Path(p).name in LOCK_NAMES or p in REQUIREMENTS or p in REPLAY_FILES or p in LITELLM_IMAGE_FILES]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ADK/LiteLLM/MCP/AG-UI upgrade gate")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--base", help="git ref of the PR base (e.g. the base commit SHA)")
    src.add_argument("--base-dir", type=Path, help="directory holding the base files (tests)")
    ap.add_argument("--head-dir", type=Path, default=Path(__file__).resolve().parents[2],
                    help="directory holding the head files (default: this checkout)")
    ap.add_argument("--summary", type=Path, default=None, help="Markdown report path (default $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)
    head_root = args.head_dir.resolve()
    head = dir_reader(head_root)
    paths = dir_paths(head_root)
    if args.base:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", args.base) or args.base.startswith("-"):
            print("upgrade_gate.py: invalid --base ref", file=sys.stderr)
            return 2
        try:
            paths += git_paths(head_root, args.base)
        except subprocess.CalledProcessError as exc:
            print(f"upgrade_gate.py: cannot list {args.base}: {exc.stderr.strip()}", file=sys.stderr)
            return 2
        base = git_reader(head_root, args.base)
    else:
        paths += dir_paths(args.base_dir.resolve())
        base = dir_reader(args.base_dir.resolve())
    rep = evaluate(paths, base, head)
    labels = parse_labels(os.environ.get("PR_LABELS"))
    text = summary(rep, labels)
    target = args.summary or (Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None)
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    missing = rep.required() - labels
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
