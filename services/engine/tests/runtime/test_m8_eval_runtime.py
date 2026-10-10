"""M8 eval cases in the runtime (``docs/contracts/128-m8-evals-ui.md`` §3): a linked case runs in its
eval run's LLM mode (``replay`` never calls a provider, whatever the process mode), does not start
once the eval run is cancelled or its live budget is used up, stops at the remaining live budget,
and reports its usage to the eval run exactly once; a replay miss is ``LLM_REPLAY_MISS`` on the
stream. FakeLlm behind the replay layer + the in-memory store; no network, no model."""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from fakes.harness import PRIYA, Harness
from fakes.scripts import chat_flow_script
from google.adk.sessions import DatabaseSessionService
from keelson_contracts.runs import Actor

from keelson_engine.runtime import replay as rp
from keelson_engine.runtime.context import RunContext
from keelson_engine.runtime.errors import RuntimeConfigError
from keelson_engine.runtime.execute import run_case
from keelson_engine.runtime.llm import FakeModelFactory, FakeScript
from keelson_engine.runtime.plugins import BudgetPlugin
from keelson_engine.runtime.replay import InMemoryReplayStore, ReplayModelFactory, effective_mode
from keelson_engine.runtime.runner_cache import RunnerCache
from keelson_engine.runtime.spec import AgentSpec
from keelson_engine.runtime.workflow import WorkflowBindings

EVAL_RUN = "0199c1a2-7b3c-7d4e-8f50-0123456789ab"
MESSAGE = {"text": "Meghna Trims says the trims for our puffers are delayed 7 days"}


@dataclass
class FakeEvals:
    """The ``EvalsPort``: one link (or none) and the usage reports it received."""

    link: dict[str, Any] | None = None
    usage: list[dict[str, Any]] = field(default_factory=list)
    lookups: int = 0
    broken: bool = False

    async def get_link(self, *, tenant: str, case_id: str) -> dict[str, Any] | None:
        self.lookups += 1
        if self.broken:
            raise ConnectionError("eval tables unreachable")
        return self.link

    async def add_usage(self, **kw: Any) -> None:
        self.usage.append(kw)


def _link(mode: str = "replay", **over: Any) -> dict[str, Any]:
    return {"run_id": EVAL_RUN, "eval_case_id": "trim_delay_actual", "llm_mode": mode, "status": "running",
            "cancel_requested": False, "budget_max_tokens": None, "budget_max_cost_microusd": None, "tokens_live": 0,
            "cost_microusd": 0, **over}


def _cache(h: Harness, models: Any) -> RunnerCache:
    async def spec_loader(tenant: str, version: str) -> AgentSpec:
        return h.spec(tenant)

    async def bindings(tenant: str, spec: AgentSpec) -> WorkflowBindings:
        return WorkflowBindings(models=models)

    return RunnerCache(session_service=DatabaseSessionService(db_url=h.db_url), spec_loader=spec_loader,
                       bindings=bindings)


async def _run(h: Harness, evals: FakeEvals, *, store: InMemoryReplayStore | None = None) -> tuple[str, str]:
    """One chat run through the replay layer (mode = process ``LLM_REPLAY_MODE``) → (status, run id)."""
    models = ReplayModelFactory(FakeModelFactory(h.script), mode=None, store=store or InMemoryReplayStore())
    cache = _cache(h, models)
    row = h.row("chat", MESSAGE)
    deps = dataclasses.replace(h.core.deps(), evals=evals)
    lease = await cache.get(row.tenant, row.agent_spec_version)
    try:
        status = await run_case(row, deps=deps, lease=lease)
    finally:
        await lease.release()
        await cache.close()
    return status, row.id


def _final(h: Harness, run_id: str) -> dict[str, Any]:
    return next(e for e in h.events(run_id) if e["type"] in ("RUN_FINISHED", "RUN_ERROR"))


# ───────────────────────────── effective mode ─────────────────────────────


@pytest.mark.parametrize(("process", "override", "expected"), [
    ("off", None, "off"),
    ("record_or_replay", None, "record_or_replay"),
    ("off", "replay", "replay"),
    ("record_or_replay", "replay", "replay"),
    ("record", "replay", "replay"),
    ("replay", "record", "replay"),            # a replay-only installation never escalates
    ("replay", "record_or_replay", "replay"),
    ("record_or_replay", "record", "record"),
    ("off", "record_or_replay", "record_or_replay"),
])
def test_effective_mode_matrix(process: Any, override: str | None, expected: str) -> None:
    assert effective_mode(process, override) == expected


def test_unknown_override_is_a_config_error() -> None:
    with pytest.raises(RuntimeConfigError):
        effective_mode("off", "live")


# ───────────────────────────── replay eval cases ─────────────────────────────


@pytest.mark.parametrize("process", ["off", "record_or_replay", "record"])
async def test_replay_eval_case_never_calls_the_model(harness: Harness, monkeypatch: pytest.MonkeyPatch,
                                                      process: str) -> None:
    """Even a live-recording process: the linked replay case fails closed at the first model call."""
    monkeypatch.setenv("LLM_REPLAY_MODE", process)
    harness.script = chat_flow_script(harness.core.repo)
    evals = FakeEvals(link=_link("replay"))
    status, run_id = await _run(harness, evals)
    assert status == "failed"
    assert dict(harness.script.requests) == {}  # the inner model was never asked
    final = _final(harness, run_id)
    assert final["type"] == "RUN_ERROR" and final["code"] == "LLM_REPLAY_MISS"
    assert "intake" in final["message"] and "replays recorded answers" in final["message"]
    [usage] = evals.usage  # accounted exactly once
    assert usage["run_id"] == EVAL_RUN and usage["case_id"] == harness.case_id
    assert usage["calls_live"] == 0 and usage["tokens_live"] == 0 and usage["misses"] == 1


async def test_process_replay_stays_replay_with_a_record_override(harness: Harness,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REPLAY_MODE", "replay")
    harness.script = chat_flow_script(harness.core.repo)
    evals = FakeEvals(link=_link("record", budget_max_tokens=10_000, budget_max_cost_microusd=10_000))
    status, run_id = await _run(harness, evals)
    assert status == "failed" and dict(harness.script.requests) == {}
    assert _final(harness, run_id)["code"] == "LLM_REPLAY_MISS"


async def test_replay_eval_case_serves_recordings(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A case recorded live replays with zero model calls when linked to a replay eval run."""
    store = InMemoryReplayStore()
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    harness.script = chat_flow_script(harness.core.repo)
    status, _ = await _run(harness, FakeEvals(), store=store)  # recorded live (no link)
    recorded = sum(len(v) for v in harness.script.requests.values())
    assert status == "paused" and recorded >= 2 and len(store.records) == recorded

    (harness.tmp / "second").mkdir()
    second = await Harness(tmp=harness.tmp / "second", case_id="case-replayed").setup()
    try:
        second.script = FakeScript()
        evals = FakeEvals(link=_link("replay"))
        status2, run2 = await _run(second, evals, store=store)
        assert status2 == "paused" and dict(second.script.requests) == {}
        assert _final(second, run2)["type"] == "RUN_FINISHED"
        [usage] = evals.usage
        assert usage["calls_live"] == 0 and usage["calls_replayed"] == recorded and usage["misses"] == 0
    finally:
        await second.close()


async def test_unlinked_case_is_unchanged(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    harness.script = chat_flow_script(harness.core.repo)
    evals = FakeEvals(link=None)
    status, run_id = await _run(harness, evals)
    assert status == "paused" and harness.script.requests["intake"]  # a live (recorded) call as before
    assert evals.lookups == 1 and evals.usage == []


async def test_a_failing_link_lookup_fails_closed(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    harness.script = chat_flow_script(harness.core.repo)
    status, run_id = await _run(harness, FakeEvals(broken=True))
    assert status == "failed" and dict(harness.script.requests) == {}
    assert _final(harness, run_id)["code"] == "RUNTIME_UNAVAILABLE"


# ───────────────────────────── cancel / budget ─────────────────────────────


@pytest.mark.parametrize(("link", "status", "code"), [
    (_link("replay", cancel_requested=True), "cancelled", "EVAL_CANCELLED"),
    (_link("replay", status="cancelled"), "cancelled", "EVAL_CANCELLED"),
    (_link("record_or_replay", budget_max_tokens=1000, budget_max_cost_microusd=5000, tokens_live=1000),
     "failed", "EVAL_BUDGET_EXHAUSTED"),
    (_link("record", budget_max_tokens=1000, budget_max_cost_microusd=5000, cost_microusd=5001),
     "failed", "EVAL_BUDGET_EXHAUSTED"),
])
async def test_cancelled_or_exhausted_eval_runs_do_not_start(harness: Harness, monkeypatch: pytest.MonkeyPatch,
                                                             link: dict[str, Any], status: str, code: str) -> None:
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    harness.script = chat_flow_script(harness.core.repo)
    evals = FakeEvals(link=link)
    got, run_id = await _run(harness, evals)
    assert got == status and harness.core.runs.status_of(run_id) == status
    assert dict(harness.script.requests) == {}
    assert _final(harness, run_id)["code"] == code
    assert evals.usage == []  # nothing ran, nothing to account


async def test_live_budget_left_caps_the_run(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live eval run with 50 tokens left: the first response (100 tokens) aborts the run."""
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    harness.script = chat_flow_script(harness.core.repo)
    evals = FakeEvals(link=_link("record_or_replay", budget_max_tokens=1000, budget_max_cost_microusd=5000,
                                 tokens_live=950))
    status, run_id = await _run(harness, evals)
    assert status == "failed" and len(harness.script.requests["intake"]) == 1
    assert _final(harness, run_id)["code"] == "BUDGET_EXCEEDED"
    [usage] = evals.usage
    assert usage["calls_live"] == 1 and usage["tokens_live"] == 100 and usage["misses"] == 0


def _ctx(**over: Any) -> RunContext:
    run_id = str(uuid.uuid4())
    ctx = RunContext(tenant="demo-retail", case_id="c", run_id=run_id, kind="chat", actor=Actor.model_validate(PRIYA),
                     payload={}, spec=AgentSpec(tenant="demo-retail"), deps=None,  # type: ignore[arg-type]
                     invocation_id=f"e-{run_id}")
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


async def test_budget_plugin_checks_the_eval_cost_cap_before_each_call() -> None:
    from types import SimpleNamespace

    from google.adk.models.llm_request import LlmRequest

    from keelson_engine.runtime.context import bind_run

    plugin = BudgetPlugin()
    cb = SimpleNamespace(agent_name="planner")
    ctx = _ctx(eval_cost_cap_microusd=2000)
    with bind_run(ctx):
        assert await plugin.before_model_callback(callback_context=cb, llm_request=LlmRequest()) is None  # type: ignore[arg-type]
        ctx.usage.add_cost("planner", 0.002)  # the LiteLLM callback reports the cost after the response
        out = await plugin.before_model_callback(callback_context=cb, llm_request=LlmRequest())  # type: ignore[arg-type]
    assert out is not None and out.error_code == "BUDGET_EXCEEDED" and ctx.abort_reason == "budget"
    assert "cost budget" in (ctx.abort_message or "")
    plain = _ctx()
    with bind_run(plain):
        assert await plugin.before_model_callback(callback_context=cb, llm_request=LlmRequest()) is None  # type: ignore[arg-type]
    assert plain.abort_reason is None


@pytest.mark.parametrize(("process", "override", "blocked"), [
    ("record_or_replay", "replay", True),       # a replay eval case: no embedding request
    ("replay", "record_or_replay", True),       # a replay-only installation never escalates
    ("record_or_replay", "record_or_replay", False),
    ("record_or_replay", None, False),          # not an eval case: unchanged
])
async def test_replay_eval_runs_never_embed_kb_queries(monkeypatch: pytest.MonkeyPatch, process: str,
                                                       override: str | None, blocked: bool) -> None:
    """Query embeddings are not replayed: in a replay eval run ``kb.search`` must not reach the
    provider, so the KB port reports the usual 'unavailable' gap without calling ``core.kb``."""
    from keelson_engine.core import kb
    from keelson_engine.runtime.context import bind_run
    from keelson_engine.runtime.deps import _KbAdapter

    calls: list[str] = []

    async def fake_search(tenant: str, query: str, kb_ids: Any = None, top_k: int = 6) -> list[dict[str, Any]]:
        calls.append(query)
        return [{"chunk_id": "k1", "document_title": "SOP", "heading": "h", "text": "t", "score": 1.0}]

    monkeypatch.setattr(kb, "search", fake_search)
    monkeypatch.setenv("LLM_REPLAY_MODE", process)
    with bind_run(_ctx(llm_replay_override=override)):
        if blocked:
            with pytest.raises(LookupError, match="replay eval run"):
                await _KbAdapter().search(tenant="demo-retail", query="air freight SOP")
        else:
            assert len(await _KbAdapter().search(tenant="demo-retail", query="air freight SOP")) == 1
    assert calls == ([] if blocked else ["air freight SOP"])


def test_replay_miss_text_never_carries_the_exception_or_key() -> None:
    from keelson_engine.runtime.execute import _public_failure

    miss = rp.ReplayMiss(alias="fast-default", agent="inventory_demand_investigator\n<script>", key="a" * 64)
    code, message = _public_failure(RuntimeError("x"))
    assert code == "INTERNAL_ERROR"
    wrapper = RuntimeError("node failed")
    wrapper.__cause__ = miss
    code, message = _public_failure(wrapper)
    assert code == "LLM_REPLAY_MISS" and "inventory_demand_investigatorscript" in message
    assert "aaaa" not in message and "fast-default" not in message and "<" not in message
