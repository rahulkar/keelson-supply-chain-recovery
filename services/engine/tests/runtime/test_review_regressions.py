"""Regression tests for defects found in the E2 review:

* a crash between a paused node's ``RequestInput`` and its interrupt row left the case without
  an open interrupt after the crash-retry (it finished "succeeded" instead of "paused");
* applying the same plan option twice compiled a second change set and applied it again;
* a duplicate apply left two live approval requests for the same changes;
* a lost response (transport error) on ``apply_change_set`` was not retried with the same key and
  a definitive-looking failure marked an in-doubt change set ``failed``;
* approvals were not re-checked against the approver role / plan version;
* the numbers rule let non-ASCII digits and unknown placeholder kinds through.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fakes.core import SimulatedCrash
from fakes.harness import MARCO, PRIYA, Harness
from fakes.scripts import chat_flow_script
from keelson_contracts.changeset import idempotency_key

from keelson_engine.runtime import commit, mcp_client
from keelson_engine.runtime.compose import ComposeContext, check_text, numeric_lint
from keelson_engine.runtime.errors import McpTransportError
from keelson_engine.runtime.mcp_client import McpGateway

DECIDER_MARCO = {"sub": MARCO["sub"], "username": MARCO["username"], "name": MARCO["name"], "role": "approver"}


@pytest.fixture(autouse=True)
def _fast_apply_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commit, "APPLY_BACKOFF_S", 0.0)


@pytest.fixture(autouse=True)
def _m1_auto_rule(request: pytest.FixtureRequest) -> None:
    """The apply mechanics below run under the M1 auto rule (only an approver's small transfer is
    auto); the M5 default (a planner's) is covered by ``test_m5_policy.py``."""
    if "harness" in request.fixturenames:
        from keelson_engine.runtime.spec import PolicyConfig

        request.getfixturevalue("harness").spec_overrides["policy"] = PolicyConfig(auto_initiator_role="approver")


async def plan_ready(h: Harness) -> None:
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    assert await h.run(h.row("chat", {"text": "Trims from Meghna are 7 days late"})) == "succeeded"


def apply_payload(plan_version: int = 1) -> dict[str, Any]:
    return {"type": "apply", "option_id": "opt-A", "plan_version": plan_version}


def approval_resume(h: Harness, interrupt: dict[str, Any], deciders: list[dict[str, Any]], decision: str = "approve"):  # type: ignore[no-untyped-def]
    payload: dict[str, Any] = {"interrupt_id": interrupt["interrupt_id"], "decision": decision, "decided_by": deciders}
    if decision == "approve":
        payload |= {"plan_version": interrupt["plan_version"], "change_set_hash": interrupt["change_set_hash"]}
    actor = {"sub": deciders[-1]["sub"], "username": deciders[-1]["username"], "name": deciders[-1]["name"],
             "roles": ["planner", "approver"]}
    return h.row("resume", payload, actor, session_id=interrupt["session_id"], invocation_id=interrupt["invocation_id"])


# ───────────────────────────── interrupts after a crash ─────────────────────────────


async def test_crash_before_interrupt_row_still_pauses_after_crash_retry(harness: Harness) -> None:
    h = harness
    h.script = chat_flow_script(h.core.repo, delay_days=None)
    chat = h.row("chat", {"text": "Meghna Trims says the trims for our puffers are delayed"})
    real_create = h.core.interrupts.create_interrupt
    calls = {"n": 0}

    async def dies_once(**kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SimulatedCrash("worker died before the interrupt row was written")
        await real_create(**kwargs)

    h.core.interrupts.create_interrupt = dies_once  # type: ignore[method-assign]
    with pytest.raises(SimulatedCrash):
        await h.run(chat)
    assert h.core.interrupts.created == []

    retry = h.row("crash_retry", chat.payload, chat.actor, run_id=chat.id, session_id=chat.id,
                  invocation_id=f"e-{chat.id}")
    retry.origin_kind = "chat"
    fresh = h.new_cache()
    try:
        assert await h.run(retry, cache=fresh) == "paused"
    finally:
        await fresh.close()
    [interrupt] = h.core.interrupts.created
    assert interrupt["interrupt_id"] == f"approach-{h.case_id}-1" and interrupt["kind"] == "approach"
    assert interrupt["invocation_id"] == f"e-{chat.id}" and interrupt["session_id"] == chat.id
    assert interrupt["quorum_spec"]["roles"] == ["planner", "approver", "tenant-admin"]
    last = h.events(chat.id)[-1]
    assert last["type"] == "RUN_FINISHED" and last["outcome"]["type"] == "interrupt"
    assert last["outcome"]["interrupts"][0]["id"] == interrupt["interrupt_id"]

    # and the recovered interrupt resumes normally
    resume = h.resume_row(chat, "confirm", [{"sub": PRIYA["sub"], "username": "priya", "name": "Priya Shah"}])
    assert await h.run(resume) == "succeeded"
    assert len(h.core.interrupts.created) == 1


# ───────────────────────────── one apply per plan option ─────────────────────────────


async def test_applying_an_applied_option_again_is_refused(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    assert await h.run(h.row("action", apply_payload(), MARCO)) == "succeeded"  # auto policy
    assert h.merxa.apply_effects == 1
    [first] = h.core.repo.change_sets.values()
    assert first["status"] == "verified"

    again = h.row("action", apply_payload(), MARCO)
    assert await h.run(again) == "succeeded"
    assert h.merxa.apply_calls == 1 and h.merxa.apply_effects == 1
    assert len(h.core.repo.change_sets) == 1, "no second change set may be compiled"
    assert "already applied" in h.core.repo.messages[-1]["text"] and "CS-0001" in h.core.repo.messages[-1]["text"]


async def test_reapply_supersedes_the_pending_approval(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    assert await h.run(h.row("action", apply_payload(), PRIYA)) == "paused"
    first_interrupt = h.core.interrupts.created[-1]
    superseded_before = h.core.interrupts.superseded

    assert await h.run(h.row("action", apply_payload(), PRIYA)) == "paused"
    second_interrupt = h.core.interrupts.created[-1]
    assert second_interrupt["interrupt_id"] != first_interrupt["interrupt_id"]
    assert h.core.interrupts.superseded > superseded_before
    cs1, cs2 = list(h.core.repo.change_sets.values())
    assert cs1["status"] == "superseded" and cs2["status"] == "validated"

    # a decision on the superseded card (resolved before the API saw it superseded) applies nothing
    assert await h.run(approval_resume(h, first_interrupt, [DECIDER_MARCO])) == "succeeded"
    assert h.merxa.apply_calls == 0
    assert "no longer waiting for approval" in h.core.repo.messages[-1]["text"]

    # the live card applies exactly once
    assert await h.run(approval_resume(h, second_interrupt, [DECIDER_MARCO])) == "succeeded"
    assert h.merxa.apply_effects == 1
    assert h.core.repo.change_sets[cs2["id"]]["status"] == "verified"


async def test_reject_marks_the_change_set_and_allows_a_new_apply(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    assert await h.run(h.row("action", apply_payload(), PRIYA)) == "paused"
    interrupt = h.core.interrupts.created[-1]
    assert await h.run(approval_resume(h, interrupt, [DECIDER_MARCO], decision="reject")) == "succeeded"
    [cs] = h.core.repo.change_sets.values()
    assert cs["status"] == "rejected"
    assert await h.run(h.row("action", apply_payload(), MARCO)) == "succeeded"  # a fresh, auto-approved set
    assert h.merxa.apply_effects == 1


async def test_approval_from_a_non_approver_role_applies_nothing(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    assert await h.run(h.row("action", apply_payload(), PRIYA)) == "paused"
    interrupt = h.core.interrupts.created[-1]
    planner = {"sub": PRIYA["sub"], "username": "priya", "name": "Priya Shah", "role": "planner"}
    assert await h.run(approval_resume(h, interrupt, [planner])) == "succeeded"
    assert h.merxa.apply_calls == 0
    assert "approver role" in h.core.repo.messages[-1]["text"]


# ───────────────────────────── lost responses on apply ─────────────────────────────


class _LossyGateway(McpGateway):
    """Delivers apply calls to Merxa but loses the first ``lose`` responses (transport errors)."""

    def __init__(self, base: McpGateway, lose: int) -> None:
        super().__init__(base._deps, url_for=base._url_for, transport_factory=base._transport_factory,
                         observer=base._observer)
        self.lose = lose

    @asynccontextmanager
    async def session(self, **kwargs: Any) -> AsyncIterator[Any]:
        gateway = self

        async with super().session(**kwargs) as inner:
            class Lossy:
                async def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
                    result = await inner.call(tool, args)
                    if tool == "apply_change_set" and gateway.lose > 0:
                        gateway.lose -= 1
                        raise McpTransportError(tool, "ReadTimeout: response lost")
                    return result

                async def list_tools(self) -> Any:
                    return await inner.list_tools()

            yield Lossy()


async def test_lost_apply_response_is_retried_with_the_same_key(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    mcp_client.set_gateway(_LossyGateway(mcp_client.get_gateway(), lose=2))
    assert await h.run(h.row("action", apply_payload(), MARCO)) == "succeeded"
    keys = [args["idempotency_key"] for tool, args in h.merxa.calls if tool == "apply_change_set"]
    [cs] = h.core.repo.change_sets.values()
    assert keys == [idempotency_key("demo-retail", cs["id"], 1)] * 3
    assert h.merxa.apply_effects == 1 and cs["status"] == "verified"


async def test_in_doubt_apply_stays_applying_and_is_resolved_by_read_back(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    mcp_client.set_gateway(_LossyGateway(mcp_client.get_gateway(), lose=100))
    action = h.row("action", apply_payload(), MARCO)
    assert await h.run(action) == "failed"
    [cs] = h.core.repo.change_sets.values()
    assert cs["status"] == "applying", "an unanswered apply is in doubt, never 'failed'"
    assert h.merxa.apply_effects == 1  # Merxa did apply it

    # the user tries again: the engine reads Merxa back instead of applying a second set
    again = h.row("action", apply_payload(), MARCO)
    assert await h.run(again) == "succeeded"
    assert h.merxa.apply_effects == 1 and len(h.core.repo.change_sets) == 1
    assert h.core.repo.change_sets[cs["id"]]["status"] == "applied"
    assert "already applied" in h.core.repo.messages[-1]["text"]


# ───────────────────────────── numbers rule ─────────────────────────────


def test_numeric_lint_catches_non_ascii_digits_and_unknown_placeholders() -> None:
    assert numeric_lint("We move １２００ units.", set())
    assert numeric_lint("We move ١٢ units.", set())
    assert numeric_lint("Recovers {{margin}} overall.", set()) == ["unknown placeholder {{margin}}"]
    assert numeric_lint("Ships from DE1 in 2026-W43.", {"DE1", "2026-W43"}) == []
    filled, violations = check_text("Total {{units:opt-A}} moved.", ComposeContext(), set())
    assert violations and "{{units:opt-A}}" in filled


# ───────────────────────────── case status, run boundary, worker ─────────────────────────────


async def test_case_status_follows_the_approval_lifecycle(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    assert await h.run(h.row("action", apply_payload(), PRIYA)) == "paused"
    assert h.core.repo.cases[h.case_id]["status"] == "awaiting_approval"
    interrupt = h.core.interrupts.created[-1]
    resume = approval_resume(h, interrupt, [DECIDER_MARCO])
    assert await h.run(resume) == "succeeded"
    assert h.core.repo.cases[h.case_id]["status"] == "committed"
    updates = [e["value"]["status"] for e in h.events(resume.id) if e["type"] == "CUSTOM"]
    assert updates == ["open", "committed"], "the awaiting-approval badge must be cleared live"


async def test_cancel_at_the_approach_card_closes_the_case(harness: Harness) -> None:
    h = harness
    h.script = chat_flow_script(h.core.repo, delay_days=None)
    chat = h.row("chat", {"text": "Meghna Trims says the trims for our puffers are delayed"})
    assert await h.run(chat) == "paused"
    resume = h.resume_row(chat, "cancel", [{"sub": PRIYA["sub"], "username": "priya", "name": "Priya Shah"}])
    assert await h.run(resume) == "succeeded"
    assert h.core.repo.cases[h.case_id]["status"] == "closed"


async def test_signal_runs_are_rejected_not_crashed(harness: Harness) -> None:
    # M5: signal runs go to the deterministic re-plan; an unknown signal ends the run quietly
    h = harness
    run = h.row("signal", {"signal_id": "sig-1"})
    assert await h.run(run) == "succeeded"
    assert not h.core.repo.plans and not h.core.repo.scenarios
    assert not any(e["type"] == "RUN_ERROR" for e in h.events(run.id))


async def test_runner_unavailable_is_reported_on_the_case_stream(harness: Harness) -> None:
    from keelson_engine.runtime import runner_cache
    from keelson_engine.runtime.execute import run_case
    from keelson_engine.runtime.runner_cache import RunnerCache

    h = harness

    async def no_key(tenant: str, spec: Any) -> Any:
        raise LookupError("no LLM virtual key for tenant demo-retail")

    broken = RunnerCache(session_service=h.cache.session_service, spec_loader=lambda t, v: _spec(h, t), bindings=no_key)
    runner_cache.set_runner_cache(broken)
    try:
        row = h.row("chat", {"text": "hello"})
        assert await run_case(row, deps=h.core.deps()) == "failed"
    finally:
        runner_cache.set_runner_cache(None)
    [event] = h.events(row.id)
    assert event["type"] == "RUN_ERROR" and event["code"] == "RUNTIME_UNAVAILABLE"
    assert h.core.runs.status_of(row.id) == "failed"


async def _spec(h: Harness, tenant: str) -> Any:
    return h.spec(tenant)


async def test_heartbeat_renews_with_the_claimed_lease_length() -> None:
    import asyncio

    from fakes.core import FakeCore, RunRow

    from keelson_engine.runtime.settings import WorkerSettings
    from keelson_engine.worker import Worker

    core = FakeCore()
    core.runs.queue = [RunRow(id="r1", tenant="demo-retail", case_id="case-1", kind="chat",
                              payload={"text": "hi"}, actor=dict(PRIYA))]

    async def slow_run(run: Any, *, controller: Any, deps: Any) -> str:
        await asyncio.sleep(0.08)
        return "succeeded"

    settings = WorkerSettings(heartbeat_interval_s=0.01, cancel_poll_interval_s=0.01, reaper_interval_s=0.05,
                              idle_poll_interval_s=0.01, shutdown_grace_s=0.5)
    worker = Worker(deps=core.deps(), settings=settings, run_case_fn=slow_run, lease_seconds=90,
                    refresh_snapshot=False)
    task = asyncio.create_task(worker.run_forever())
    for _ in range(300):
        if worker.completed:
            break
        await asyncio.sleep(0.01)
    worker.request_stop()
    await asyncio.wait_for(task, 5)
    assert core.runs.lease_seconds and set(core.runs.lease_seconds) == {90}
