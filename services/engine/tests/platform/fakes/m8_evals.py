"""M8 Evals UI test helpers: a runner suite, runner-protocol calls and a clean eval state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from e1_env import PUBLIC
from keelson_engine.core.db import system_session

RUNNER_TOKEN = "runner-token-for-tests-0123456789abcdef"
RUNNER = "runner-1"
INTERNAL = "/api/internal/evals/runner"
FIXTURES = Path(__file__).resolve().parents[5] / "evals" / "tests" / "fixtures"


def suite(hash_char: str = "a") -> dict[str, Any]:
    case = {"title": "", "tags": [], "turns": 1, "turn_kinds": ["message"], "needs_live_embeddings": False,
            "injects": False}
    return {"suite_hash": hash_char * 64, "cases": [
        {**case, "id": "trim_delay_actual", "title": "Trim supplier delay (actual)", "tags": ["golden"]},
        {**case, "id": "store_shortage_question", "title": "Store shortage question", "tags": ["golden"]},
        {**case, "id": "adversarial_kb_doc", "title": "Adversarial KB document", "tags": ["adversarial"],
         "turns": 2, "turn_kinds": ["message", "action"], "needs_live_embeddings": True},
        {**case, "id": "adversarial_supplier_message", "title": "Adversarial supplier message",
         "tags": ["adversarial"], "injects": True},
    ]}


def runner_headers(token: str = RUNNER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Origin": PUBLIC}


async def runner_call(api: httpx.AsyncClient, method: str, path: str, body: Any = None,
                      token: str = RUNNER_TOKEN) -> httpx.Response:
    return await api.request(method, f"{INTERNAL}{path}", json=body, headers=runner_headers(token))


async def heartbeat(api: httpx.AsyncClient, runner_id: str = RUNNER, **suite_over: Any) -> httpx.Response:
    return await runner_call(api, "POST", "/heartbeat", {"runner_id": runner_id, "version": "m8.1",
                                                         "suite": {**suite(), **suite_over}})


async def clean_evals() -> None:
    """Every test starts without eval runs (one active run platform-wide) or runners."""
    async with system_session() as s:
        await s.execute(text("DELETE FROM eval_case_link"))
        await s.execute(text("DELETE FROM eval_run"))
        await s.execute(text("DELETE FROM eval_runner"))


def fixture_doc(name: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def case_doc(eval_case_id: str, status: str = "pass", **over: Any) -> dict[str, Any]:
    """A ``keelson-evals/2`` case as the runner reports it."""
    doc: dict[str, Any] = {
        "id": eval_case_id, "title": eval_case_id.replace("_", " "), "tags": [], "status": status, "case_id": None,
        "error": None, "error_code": None, "duration_s": 12.5, "duration_ms": 12_500,
        "turns": [{"id": "report", "kind": "message", "run_ids": ["r1"],
                   "timings_s": {"first_stage": 0.5, "exposure": 10.0, "options": 100.0, "total": 110.0},
                   "timings_ms": {"first_stage": 500, "exposure": 10_000, "options": 100_000, "total": 110_000},
                   "plan_version": 1, "skipped": None,
                   "checks": [{"name": "report.run", "ok": status == "pass", "detail": "x", "soft": False}],
                   "metrics": {"entity_recall": 0.9, "entity_recall_bp": 9000, "tool_calls": 3}, "usage": None,
                   "final_code": None}],
        "checks": [], "metrics": {"entity_recall": 0.9}, "usage": None,
    }
    doc.update(over)
    return doc
