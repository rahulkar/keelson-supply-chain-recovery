"""Ops hardening regressions: user-visible errors never carry exception text (generic message +
error id, full exception logged under that id) and every event names its run
(``metadata.sc.runId``)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog
from fakes.core import FakeCore, RunRow
from fakes.harness import PRIYA, Harness
from fakes.scripts import intake_disruption
from google.adk.events.event import Event, NodeInfo
from keelson_contracts.agui import ScMetadata

from keelson_engine.public_errors import GENERIC_RUN_FAILURE, safe_code, user_message
from keelson_engine.runtime.translator import Translator

from test_action_flow import apply_payload, approve, m1_auto_rule, plan_ready  # noqa: F401 (autouse fixture)

SECRET = "postgresql://keelson:hunter2@pg-core:5432/keelson"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _assert_generic(event: dict[str, Any], *, code: str) -> str:
    assert event["type"] == "RUN_ERROR" and event["code"] == code
    message = str(event["message"])
    for leaked in ("hunter2", "pg-core", "RuntimeError", "Traceback", "postgresql://"):
        assert leaked not in message
    error_id = event["metadata"]["errorId"]
    assert UUID_RE.fullmatch(error_id) and error_id in message
    return str(error_id)


async def test_node_exception_is_logged_with_error_id_not_shown(harness: Harness) -> None:
    h = harness
    await plan_ready(h)

    async def broken(**_: Any) -> str:
        raise RuntimeError(f"could not connect to {SECRET}")

    h.core.repo.create_change_set = broken  # type: ignore[method-assign]
    action = h.row("action", apply_payload(), PRIYA)
    with structlog.testing.capture_logs() as logs:
        assert await h.run(action) == "failed"
    last = h.events(action.id)[-1]
    error_id = _assert_generic(last, code="INTERNAL_ERROR")
    assert last["message"] == user_message(error_id, GENERIC_RUN_FAILURE)
    assert last["metadata"]["sc"]["runId"] == action.id
    # the server side keeps the cause, under the same id
    [finished] = [f for f in h.core.runs.finished if f["run_id"] == action.id]
    assert finished["status"] == "failed" and finished["error"].startswith(f"[{error_id}] RuntimeError")
    logged = [entry for entry in logs if entry.get("error_id") == error_id]
    assert logged and logged[0]["log_level"] == "error" and isinstance(logged[0].get("exc_info"), RuntimeError)


async def test_connect_required_at_apply_has_its_own_code(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    action = h.row("action", apply_payload(), PRIYA)
    assert await h.run(action) == "paused"
    h.core.tokens.unlinked.add("sub-marco")
    resume = await approve(h, action)
    assert await h.run(resume) == "failed"
    last = h.events(resume.id)[-1]
    _assert_generic(last, code="CONNECT_REQUIRED")
    assert "Connect it and approve again" in last["message"]
    assert "/api/connectors" not in last["message"]


async def test_runner_unavailable_hides_the_cause(harness: Harness) -> None:
    from keelson_engine.runtime import runner_cache
    from keelson_engine.runtime.execute import run_case
    from keelson_engine.runtime.runner_cache import RunnerCache

    h = harness

    async def no_key(tenant: str, spec: Any) -> Any:
        raise LookupError(f"no LLM virtual key for tenant demo-retail ({SECRET})")

    async def spec_loader(tenant: str, version: Any) -> Any:
        return h.spec(tenant)

    runner_cache.set_runner_cache(RunnerCache(session_service=h.cache.session_service, spec_loader=spec_loader,
                                              bindings=no_key))
    try:
        row = h.row("chat", {"text": "hello"})
        assert await run_case(row, deps=h.core.deps()) == "failed"
    finally:
        runner_cache.set_runner_cache(None)
    [event] = h.events(row.id)
    error_id = _assert_generic(event, code="RUNTIME_UNAVAILABLE")
    assert event["metadata"]["sc"]["runId"] == row.id
    assert h.core.runs.finished[-1]["error"].startswith(f"[{error_id}]")


async def test_every_event_of_a_run_carries_its_run_id(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    action = h.row("action", apply_payload(), PRIYA)
    assert await h.run(action) == "paused"
    for run_id in {e["run_id"] for e in h.core.bus.events}:
        events = h.events(run_id)
        assert events
        assert {ScMetadata.model_validate(e["metadata"]["sc"]).run_id for e in events} == {run_id}


async def test_subagent_error_is_generic_with_error_id() -> None:
    from fakes.core import FakeBus

    bus = FakeBus()
    t = Translator(case_id="case-1", run_id="run-1", tenant="demo-retail", bus=bus)
    await t.start()
    spec = "keelson@1/investigate@1/inventory_demand@task-inventory_demand-r1"
    await t.handle(Event(invocation_id="e-run-1", author="keelson", node_info=NodeInfo(path=spec),
                         custom_metadata={"lifecycle": "agent_started", "agent": "inventory_demand"}, partial=True))
    with structlog.testing.capture_logs() as logs:
        await t.handle(Event(invocation_id="e-run-1", author="keelson", node_info=NodeInfo(path=spec),
                             error_code="ProgrammingError", error_message=f"relation missing at {SECRET}"))
    [err] = bus.of_type("SUBAGENT_ERROR")
    assert "hunter2" not in err["message"] and "relation" not in err["message"]
    assert err["code"] == "SUBAGENT_FAILED"  # an exception class name is not a code
    error_id = err["metadata"]["errorId"]
    assert error_id in err["message"] and err["metadata"]["sc"]["runId"] == "run-1"
    assert any(e.get("error_id") == error_id and SECRET in str(e.get("error_message")) for e in logs)
    await t.finish_error("The run was cancelled.", code="CANCELLED")
    last = bus.events[-1]["event"]
    assert last["type"] == "RUN_ERROR" and "errorId" not in last["metadata"]  # cancellations carry no id


def test_safe_code() -> None:
    assert safe_code("CANCELLED") == "CANCELLED" and safe_code("MAX_TOKENS") == "MAX_TOKENS"
    for bad in ("ProgrammingError", "error: boom", "", None, "A" * 80):
        assert safe_code(bad) == "INTERNAL_ERROR"


async def test_worker_crash_reports_a_generic_run_error_once() -> None:
    from keelson_engine.runtime.settings import WorkerSettings
    from keelson_engine.worker import Worker

    core = FakeCore()
    core.runs.queue = [RunRow(id="r-crash", tenant="demo-retail", case_id="case-1", kind="chat",
                              payload={"text": "hi"}, actor=dict(PRIYA))]

    async def crashing(run: Any, *, controller: Any, deps: Any) -> str:
        raise RuntimeError(f"boom {SECRET}")

    settings = WorkerSettings(heartbeat_interval_s=0.01, cancel_poll_interval_s=0.01, reaper_interval_s=0.05,
                              idle_poll_interval_s=0.01, shutdown_grace_s=0.5)
    worker = Worker(deps=core.deps(), settings=settings, run_case_fn=crashing, refresh_snapshot=False)
    task = asyncio.create_task(worker.run_forever())
    for _ in range(300):
        if worker.completed:
            break
        await asyncio.sleep(0.01)
    worker.request_stop()
    await asyncio.wait_for(task, 5)
    assert worker.completed == [("r-crash", "failed")]
    [event] = core.bus.of_type("RUN_ERROR", "r-crash")
    error_id = _assert_generic(event, code="INTERNAL_ERROR")
    assert event["metadata"]["sc"]["runId"] == "r-crash"
    assert core.runs.finished[-1]["error"].startswith(f"[{error_id}] run crashed")


async def test_budget_message_is_user_facing(tmp_path: Any) -> None:
    from keelson_engine.runtime.spec import Budgets as SpecBudgets

    h = await Harness(tmp=tmp_path, spec_overrides={"budgets": SpecBudgets(max_llm_calls=1, max_total_tokens=1000)}).setup()
    try:
        h.script.add("intake", intake_disruption(delay_days=7))
        row = h.row("chat", {"text": "Meghna trims 7 days late"})
        assert await h.run(row) == "failed"
        last = h.events(row.id)[-1]
        assert last["code"] == "BUDGET_EXCEEDED" and "caps" not in last["message"] and "tokens" not in last["message"]
    finally:
        await h.close()
