"""Eval results: the JSON metrics file and its Markdown summary (``evals/results/``)."""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .assertions import Check

__all__ = ["CaseResult", "TurnResult", "case_status", "render_markdown", "summarise", "write_results"]

SCHEMA = "keelson-evals/1"
LATENCY_KEYS = ("first_stage", "exposure", "options", "total")


@dataclass
class TurnResult:
    id: str
    kind: str
    run_ids: list[str] = field(default_factory=list)
    timings: dict[str, float | None] = field(default_factory=dict)
    plan_version: int | None = None
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    skipped: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "run_ids": self.run_ids,
            "timings_s": {k: (None if v is None else round(v, 2)) for k, v in self.timings.items()},
            "plan_version": self.plan_version, "skipped": self.skipped,
            "checks": [c.to_json() for c in self.checks], "metrics": self.metrics,
        }


@dataclass
class CaseResult:
    id: str
    title: str
    tags: list[str] = field(default_factory=list)
    case_id: str | None = None
    error: str | None = None
    duration_s: float = 0.0
    turns: list[TurnResult] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def all_checks(self) -> list[Check]:
        return [c for t in self.turns for c in t.checks] + list(self.checks)

    @property
    def status(self) -> str:
        return case_status(self.error, self.all_checks())

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "tags": self.tags, "status": self.status, "case_id": self.case_id,
            "error": self.error, "duration_s": round(self.duration_s, 1),
            "turns": [t.to_json() for t in self.turns], "checks": [c.to_json() for c in self.checks],
            "metrics": self.metrics,
        }


def case_status(error: str | None, checks: Sequence[Check]) -> str:
    """``error`` (the run could not complete) > ``fail`` (a hard check failed) > ``pass``."""
    if error:
        return "error"
    if any(not c.ok and not c.soft for c in checks):
        return "fail"
    return "pass"


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def summarise(results: Sequence[CaseResult]) -> dict[str, Any]:
    checks = [c for r in results for c in r.all_checks()]
    statuses = [r.status for r in results]
    recall = [float(r.metrics["entity_recall"]) for r in results if r.metrics.get("entity_recall") is not None]
    grounding = [float(r.metrics["numeric_grounding"]) for r in results
                 if r.metrics.get("numeric_grounding") is not None]
    latency: dict[str, dict[str, float | None]] = {}
    for key in LATENCY_KEYS:
        vals = [float(v) for r in results for t in r.turns if (v := t.timings.get(key)) is not None]
        latency[f"{key}_s"] = {
            "n": len(vals),
            "p50": round(statistics.median(vals), 2) if vals else None,
            "max": round(max(vals), 2) if vals else None,
        }
    tokens = [int(r.metrics["tokens"]) for r in results if isinstance(r.metrics.get("tokens"), int)]
    return {
        "cases": len(results),
        "passed": statuses.count("pass"),
        "failed": statuses.count("fail"),
        "errors": statuses.count("error"),
        "checks": len(checks),
        "checks_failed": sum(1 for c in checks if not c.ok and not c.soft),
        "soft_failed": sum(1 for c in checks if not c.ok and c.soft),
        "entity_recall_mean": _mean(recall),
        "numeric_grounding_mean": _mean(grounding),
        "latency": latency,
        "tokens_per_case_mean": _mean([float(t) for t in tokens]),
        "tokens_total": sum(tokens) if tokens else None,
    }


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.2f}{unit}"
    return f"{value}{unit}"


def render_markdown(doc: Mapping[str, Any]) -> str:
    s = doc["summary"]
    lines = [
        f"# Keelson live evals — {doc['started_at']}",
        "",
        f"Target `{doc['keelson_url']}` · {s['cases']} case(s): **{s['passed']} passed**, {s['failed']} failed, "
        f"{s['errors']} error(s) · checks {s['checks'] - s['checks_failed']}/{s['checks']} "
        f"(soft failures {s['soft_failed']})",
        "",
        "| case | status | entity recall | numeric grounding | first stage | exposure | options | tokens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in doc["cases"]:
        first: dict[str, Any] = next((t["timings_s"] for t in c["turns"] if t["timings_s"]), {})
        m = c["metrics"]
        lines.append(
            f"| `{c['id']}` | {c['status'].upper()} | {_fmt(m.get('entity_recall'))} | "
            f"{_fmt(m.get('numeric_grounding'))} | {_fmt(first.get('first_stage'), 's')} | "
            f"{_fmt(first.get('exposure'), 's')} | {_fmt(first.get('options'), 's')} | {_fmt(m.get('tokens'))} |"
        )
    lat = s["latency"]
    lines += [
        "",
        "Latency (s, p50 / max over turns): "
        + ", ".join(f"{k} {_fmt(v['p50'])} / {_fmt(v['max'])}" for k, v in lat.items()),
        f"Mean entity recall {_fmt(s['entity_recall_mean'])} · mean numeric grounding "
        f"{_fmt(s['numeric_grounding_mean'])} · tokens/case {_fmt(s['tokens_per_case_mean'])}",
    ]
    problems: list[str] = []
    for c in doc["cases"]:
        if c.get("error"):
            problems.append(f"- `{c['id']}` ERROR: {c['error']}")
        for t in c["turns"]:
            for chk in t["checks"]:
                if not chk["ok"]:
                    tag = "soft" if chk["soft"] else "FAIL"
                    problems.append(f"- `{c['id']}` {tag} `{chk['name']}`: {chk['detail']}")
        for chk in c["checks"]:
            if not chk["ok"]:
                tag = "soft" if chk["soft"] else "FAIL"
                problems.append(f"- `{c['id']}` {tag} `{chk['name']}`: {chk['detail']}")
    lines += ["", "## Failed checks", ""] + (problems or ["None."])
    return "\n".join(lines) + "\n"


def write_results(results_dir: Path, doc: dict[str, Any], stamp: str) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / f"{stamp}.json"
    md_path = results_dir / f"{stamp}.md"
    json_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(doc), encoding="utf-8")
    return json_path, md_path
