"""Eval results: the JSON metrics file and its Markdown summary (``evals/results/``).

Schema ``keelson-evals/2`` (M8, ``docs/contracts/128-m8-evals-ui.md`` §5) is an additive superset of
``keelson-evals/1``: every v1 field keeps its meaning (ratios 0–1, seconds, ``metrics.tokens`` =
the LiteLLM usage delta around the case), and v2 adds the run's ``mode`` / ``runner`` /
``suite_hash``, exact integer fields (``timings_ms``, ``duration_ms``, ``*_bp`` ratios), per-turn
``trajectory`` / ``entities`` / ``targets`` / ``usage`` / ``final_code`` and per-case ``usage`` /
``error_code``. The engine imports both versions (``keelson_contracts.evals.case_result_from_doc``)
and the M7 listing reads both.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .assertions import Check
from .evaluate import to_bp

__all__ = ["CaseResult", "SCHEMA", "TurnResult", "case_status", "render_markdown", "summarise", "write_results"]

SCHEMA = "keelson-evals/2"
LATENCY_KEYS = ("first_stage", "exposure", "options", "total")
STRUCTURED_METRICS = ("trajectory", "entities", "targets")
"""Turn metrics that are objects: written next to ``metrics`` (v2), not inside it."""
RATIO_METRICS = ("entity_recall", "numeric_grounding", "trajectory_recall")


def _with_bp(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """v1 metrics plus the exact ``<ratio>_bp`` integers (v2)."""
    out = dict(metrics)
    for name in RATIO_METRICS:
        value = out.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            out[f"{name}_bp"] = to_bp(value)
    return out


def _ms(seconds: float | None) -> int | None:
    return None if seconds is None else round(seconds * 1000)


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
    usage: dict[str, Any] | None = None
    """(v2) Sum of the turn's runs' ``metadata.usage`` (``assertions.usage_total``)."""
    final_code: str | None = None
    """(v2) ``RUN_ERROR`` code of the turn's last run."""

    def to_json(self) -> dict[str, Any]:
        metrics = {k: v for k, v in self.metrics.items() if k not in STRUCTURED_METRICS}
        out: dict[str, Any] = {
            "id": self.id, "kind": self.kind, "run_ids": self.run_ids,
            "timings_s": {k: (None if v is None else round(v, 2)) for k, v in self.timings.items()},
            "timings_ms": {k: _ms(v) for k, v in self.timings.items()},
            "plan_version": self.plan_version, "skipped": self.skipped,
            "checks": [c.to_json() for c in self.checks], "metrics": _with_bp(metrics),
            "usage": self.usage, "final_code": self.final_code,
        }
        for key in STRUCTURED_METRICS:
            if isinstance(self.metrics.get(key), Mapping):
                out[key] = dict(self.metrics[key])
        return out


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
    error_code: str | None = None
    """(v2) Why the case stopped early: ``replay_miss`` | ``budget_exhausted`` | ``cancelled`` |
    ``needs_live_embeddings`` (with ``skipped``)."""
    skipped: str | None = None
    """(v2) The case was not run (e.g. the in-app runner's replay mode cannot replay embeddings)."""
    usage: dict[str, Any] | None = None
    """(v2) Exact LLM usage of every run of the case (``assertions.usage_total``)."""

    def all_checks(self) -> list[Check]:
        return [c for t in self.turns for c in t.checks] + list(self.checks)

    @property
    def status(self) -> str:
        return case_status(self.error, self.all_checks(), error_code=self.error_code, skipped=self.skipped)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "tags": self.tags, "status": self.status, "case_id": self.case_id,
            "error": self.error, "error_code": self.error_code, "duration_s": round(self.duration_s, 1),
            "duration_ms": round(self.duration_s * 1000),
            "turns": [t.to_json() for t in self.turns], "checks": [c.to_json() for c in self.checks],
            "metrics": _with_bp(self.metrics), "usage": self.usage,
        }


def case_status(error: str | None, checks: Sequence[Check], *, error_code: str | None = None,
                skipped: str | None = None) -> str:
    """``skipped`` (not run) / ``cancelled`` > ``error`` (the run could not complete, e.g. a
    replay miss) > ``fail`` (a hard check failed) > ``pass``."""
    if skipped:
        return "skipped"
    if error_code == "cancelled":
        return "cancelled"
    if error or error_code:
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
    trajectory = [float(r.metrics["trajectory_recall"]) for r in results
                  if r.metrics.get("trajectory_recall") is not None]
    latency: dict[str, dict[str, float | None]] = {}
    for key in LATENCY_KEYS:
        vals = [float(v) for r in results for t in r.turns if (v := t.timings.get(key)) is not None]
        latency[f"{key}_s"] = {
            "n": len(vals),
            "p50": round(statistics.median(vals), 2) if vals else None,
            "max": round(max(vals), 2) if vals else None,
        }
    tokens = [int(r.metrics["tokens"]) for r in results if isinstance(r.metrics.get("tokens"), int)]
    engine_tokens = [int(r.usage["total"]["total_tokens"]) for r in results if r.usage is not None]
    costs = [r.usage["total"]["cost_microusd"] for r in results
             if r.usage is not None and r.usage["total"].get("cost_microusd") is not None]
    return {
        "cases": len(results),
        "passed": statuses.count("pass"),
        "failed": statuses.count("fail"),
        "errors": statuses.count("error"),
        "skipped": statuses.count("skipped"),
        "cancelled": statuses.count("cancelled"),
        "checks": len(checks),
        "checks_failed": sum(1 for c in checks if not c.ok and not c.soft),
        "soft_failed": sum(1 for c in checks if not c.ok and c.soft),
        "entity_recall_mean": _mean(recall),
        "numeric_grounding_mean": _mean(grounding),
        "trajectory_recall_mean": _mean(trajectory),
        "latency": latency,
        "tokens_per_case_mean": _mean([float(t) for t in tokens]),
        "tokens_total": sum(tokens) if tokens else None,
        "tokens_engine": sum(engine_tokens) if engine_tokens else None,
        "cost_microusd_total": sum(costs) if costs else None,
        "replay_misses": sum(1 for r in results if r.error_code == "replay_miss"),
    }


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.2f}{unit}"
    return f"{value}{unit}"


def render_markdown(doc: Mapping[str, Any]) -> str:
    s = doc["summary"]
    mode = f" · mode `{doc['mode']}`" if doc.get("mode") else ""
    lines = [
        f"# Keelson live evals — {doc['started_at']}",
        "",
        f"Target `{doc['keelson_url']}`{mode} · {s['cases']} case(s): **{s['passed']} passed**, {s['failed']} failed, "
        f"{s['errors']} error(s) · checks {s['checks'] - s['checks_failed']}/{s['checks']} "
        f"(soft failures {s['soft_failed']})",
        "",
        "| case | status | entity recall | numeric grounding | first stage | exposure | options | tokens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in doc["cases"]:
        first: dict[str, Any] = next((t["timings_s"] for t in c["turns"] if t["timings_s"]), {})
        m = c["metrics"]
        tokens = m.get("tokens")
        if tokens is None and isinstance(c.get("usage"), Mapping):
            tokens = c["usage"]["total"]["total_tokens"]
        lines.append(
            f"| `{c['id']}` | {c['status'].upper()} | {_fmt(m.get('entity_recall'))} | "
            f"{_fmt(m.get('numeric_grounding'))} | {_fmt(first.get('first_stage'), 's')} | "
            f"{_fmt(first.get('exposure'), 's')} | {_fmt(first.get('options'), 's')} | {_fmt(tokens)} |"
        )
    lat = s["latency"]
    lines += [
        "",
        "Latency (s, p50 / max over turns): "
        + ", ".join(f"{k} {_fmt(v['p50'])} / {_fmt(v['max'])}" for k, v in lat.items()),
        f"Mean entity recall {_fmt(s['entity_recall_mean'])} · mean numeric grounding "
        f"{_fmt(s['numeric_grounding_mean'])} · tokens/case {_fmt(s['tokens_per_case_mean'])}",
    ]
    if s.get("tokens_engine") is not None:
        cost = s.get("cost_microusd_total")
        lines.append(f"Engine-metered tokens {s['tokens_engine']} · cost "
                     f"{'–' if cost is None else f'${cost / 1_000_000:.4f}'} · replay misses {s.get('replay_misses', 0)}")
    problems: list[str] = []
    for c in doc["cases"]:
        if c.get("error"):
            code = f" [{c['error_code']}]" if c.get("error_code") else ""
            problems.append(f"- `{c['id']}` ERROR{code}: {c['error']}")
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
