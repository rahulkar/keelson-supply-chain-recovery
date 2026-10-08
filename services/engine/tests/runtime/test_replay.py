"""Engine-level LLM record/replay (runtime/replay.py; plan §16 "Demo determinism", spike S9).

Offline only: FakeLlm (scripted, request-counting) behind :class:`ReplayLlm`, KeelsonLiteLlm with a
capturing LiteLLM client, and the in-memory store. The Postgres table (RLS) is covered by
``test_replay_store_integration.py``.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fakes.harness import PRIYA, Harness
from fakes.scripts import chat_flow_script
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from keelson_contracts.runs import Actor

from keelson_engine.runtime import replay as rp
from keelson_engine.runtime.context import RunContext, TaskBinding, bind_run, bind_task
from keelson_engine.runtime.errors import RuntimeConfigError
from keelson_engine.runtime.llm import FakeLlm, FakeModelFactory, FakeScript, FakeTurn, KeelsonLiteLlm
from keelson_engine.runtime.replay import (
    InMemoryReplayStore,
    Normaliser,
    ReplayLlm,
    ReplayMiss,
    ReplayModelFactory,
    ReplayRecord,
    prepare_call,
    replay_stats,
)
from keelson_engine.runtime.runner_cache import RunnerCache
from keelson_engine.runtime.spec import AgentSpec
from keelson_engine.runtime.workflow import WorkflowBindings

TENANT, OTHER = "demo-retail", "northwind-test"


# ─────────────────────────────── helpers ───────────────────────────────


class _NoDeps:
    """RunContext.deps placeholder (the replay layer never touches deps)."""


def _run(tenant: str = TENANT, *, case_id: str | None = None, spec_version: str = "default") -> RunContext:
    run_id = str(uuid.uuid4())
    return RunContext(
        tenant=tenant, case_id=case_id or str(uuid.uuid4()), run_id=run_id, kind="chat",
        actor=Actor.model_validate(PRIYA), payload={}, spec=AgentSpec(tenant=tenant, version=spec_version),
        deps=_NoDeps(),  # type: ignore[arg-type]
        invocation_id=f"e-{run_id}", trace_id=f"trace-{uuid.uuid4().hex[:12]}",
    )


def _ev() -> str:
    return f"ev_{uuid.uuid4().hex}"


def _request(run: RunContext, evidence: list[str], *, agent: str = "inventory_demand_investigator",
             stamp: str = "2026-10-07T09:15:00Z", call_id: str | None = None, extra: str = "") -> LlmRequest:
    """A realistic request: system instruction with the case id, tool results carrying evidence
    ids and a timestamp, a previous model function call with a thought signature."""
    call_id = call_id or f"call_{uuid.uuid4().hex[:20]}"
    signature = base64.b64encode(uuid.uuid4().bytes * 3)
    config = types.GenerateContentConfig(
        system_instruction=f"You investigate case {run.case_id} (run {run.run_id}). Cite evidence ids.",
        temperature=0.2,
        labels={"adk_agent_name": agent},
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
            name="get_po", description="Read a purchase order",
            parameters_json_schema={"type": "object", "properties": {"po": {"type": "string"}}},
        )])],
        http_options=types.HttpOptions(headers=dict(run.llm_headers)),
    )
    contents = [
        types.Content(role="user", parts=[types.Part(text=f"Trims for case {run.case_id} are late.{extra}")]),
        types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(id=call_id, name="get_po", args={"po": "PO-BD-0001"}),
            thought_signature=base64.b64decode(signature),
        )]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            id=call_id, name="get_po",
            response={"_evidence_id": evidence[0], "eta": stamp, "lines": [{"ev": evidence[1]}],
                      "ordered": "2026-09-01"},
        ))]),
    ]
    return LlmRequest(model="litellm_proxy/fast-default", contents=contents, config=config)


def _citing_turn(request: LlmRequest) -> FakeTurn:
    """The model answers by citing the evidence ids, the case id and a timestamp it was given."""
    blob = json.dumps(request.model_dump(mode="json", exclude_none=True))
    evidence = list(dict.fromkeys(tok.strip('"\\,') for tok in blob.replace('"', " ").split() if tok.startswith("ev_")))
    case = str(request.config.system_instruction).split("case ", 1)[1].split(" ", 1)[0]
    return FakeTurn(
        thought="checking the purchase order evidence first",
        text=f"Delay confirmed for case {case} per {evidence[1]} and {evidence[0]} (eta 2026-10-07T09:15:00Z).",
        function_calls=[("get_po", {"po": "PO-BD-0002", "cite": evidence[0]})],
        total_tokens=1234,
    )


def _model(script: FakeScript, store: InMemoryReplayStore, mode: str, agent: str = "inv") -> ReplayLlm:
    return ReplayLlm(inner=FakeLlm(agent=agent, script=script), alias="fast-default", mode=mode, store=store)


async def _call(model: Any, request: LlmRequest, *, stream: bool = False) -> list[LlmResponse]:
    return [r async for r in model.generate_content_async(request, stream=stream)]


def _final(responses: list[LlmResponse]) -> LlmResponse:
    [final] = [r for r in responses if not r.partial]
    return final


# ─────────────────────────────── normalisation ───────────────────────────────


def test_same_request_shape_with_new_run_ids_has_the_same_key() -> None:
    a, b = _run(), _run()
    with bind_run(a):
        key_a = prepare_call(_request(a, [_ev(), _ev()]), alias="fast-default", run=a, occurrence=0).key
    with bind_run(b):
        call_b = prepare_call(_request(b, [_ev(), _ev()]), alias="fast-default", run=b, occurrence=0)
    assert key_a == call_b.key
    placeholders = set(call_b.normaliser.forward.values())
    assert {"⟦case⟧", "⟦run⟧", "⟦ev#1⟧", "⟦ev#2⟧", "⟦call#1⟧", "⟦sig#1⟧"} <= placeholders
    # dates and date-times are data, not volatile tokens
    assert "2026-09-01" not in call_b.normaliser.forward
    assert "2026-10-07T09:15:00Z" not in call_b.normaliser.forward
    assert not any(ph.startswith("⟦ts") for ph in placeholders)


@pytest.mark.parametrize("stamp", ["2026-10-08T11:00:00+02:00", "2026-10-07T09:16:00Z", "2026-10-07T09:15:00.5Z"])
def test_a_changed_date_time_is_a_different_request(stamp: str) -> None:
    """Review fix: an ETA / receipt time is data the model reasons about (late or not, by how much).
    Normalising it let a request with another ETA replay an answer about the old one, with the new
    value pasted in. Correctness over hit rate: any changed date-time changes the key."""
    run = _run()
    ev = [_ev(), _ev()]
    with bind_run(run):
        base = prepare_call(_request(run, ev), alias="fast-default", run=run, occurrence=0).key
        moved = prepare_call(_request(run, ev, stamp=stamp), alias="fast-default", run=run, occurrence=0).key
    assert moved != base


async def test_replay_never_serves_an_answer_recorded_for_another_eta() -> None:
    store = InMemoryReplayStore()
    run1 = _run()
    with bind_run(run1):
        await _call(_model(FakeScript({"inv": [_citing_turn]}), store, "record"), _request(run1, [_ev(), _ev()]))
    run2 = _run()
    script = FakeScript()
    with bind_run(run2), pytest.raises(ReplayMiss):
        await _call(_model(script, store, "replay"),
                    _request(run2, [_ev(), _ev()], stamp="2026-10-14T09:15:00Z"))  # the PO slipped a week
    assert script.requests == {}


def test_key_includes_the_models_lock_entry_of_the_alias(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review fix: the proxy resolves the alias, so a recording made with another provider model or
    reasoning effort must not answer for this one (models.lock says: re-record when changing)."""
    lock = tmp_path / "models.lock"
    bump = iter(range(1, 100))

    def write(effort: str, model: str = "gemini/gemini-3.8-flash") -> None:
        lock.write_text(f"version: 1\naliases:\n  fast-default:\n    model: {model}\n    mode: chat\n"
                        f"    params: {{ reasoning_effort: {effort} }}\n  critic-default:\n"
                        f"    model: gemini/gemini-3.8-flash\n    params: {{ reasoning_effort: low }}\n",
                        encoding="utf-8")
        stamp = 1_700_000_000_000_000_000 + next(bump) * 1_000_000_000  # a new mtime for the parse cache
        os.utime(lock, ns=(stamp, stamp))

    run = _run()
    ev = [_ev(), _ev()]

    def key() -> str:
        with bind_run(run):
            return prepare_call(_request(run, ev), alias="fast-default", run=run, occurrence=0).key

    monkeypatch.setenv("MODELS_LOCK", str(tmp_path / "missing.lock"))
    assert rp.alias_fingerprint("fast-default") == ""
    no_lock = key()
    monkeypatch.setenv("MODELS_LOCK", str(lock))
    write("low")
    low = key()
    fp_critic = rp.alias_fingerprint("critic-default")
    write("medium")
    medium = key()
    assert len({no_lock, low, medium}) == 3
    assert rp.alias_fingerprint("critic-default") == fp_critic  # other aliases are unaffected
    write("medium", model="gemini/gemini-3.5-flash-lite")
    assert key() != medium
    write("medium")
    assert key() == medium  # same entry → same key
    assert rp.alias_fingerprint("no-such-alias") == ""


def test_unknown_request_objects_are_keyed_by_value() -> None:
    @dataclass
    class Opaque:
        value: int

    assert rp._plain(Opaque(1)) != rp._plain(Opaque(2))


@pytest.mark.parametrize(
    ("change", "kwargs"),
    [
        ("alias", {"alias": "reasoning-default"}),
        ("agent", {"agent": "critic"}),
        ("content", {"extra": " Also the zips."}),
        ("spec", {"spec_version": "v2"}),
        ("occurrence", {"occurrence": 1}),
    ],
)
def test_key_changes_with_alias_agent_content_spec_and_occurrence(change: str, kwargs: dict[str, Any]) -> None:
    run = _run()
    ev = [_ev(), _ev()]
    with bind_run(run):
        base = prepare_call(_request(run, ev), alias="fast-default", run=run, occurrence=0).key
        other_run = _run(spec_version=kwargs.get("spec_version", "default"))
        other_run.case_id, other_run.run_id, other_run.invocation_id = run.case_id, run.run_id, run.invocation_id
        request = _request(other_run, ev, agent=kwargs.get("agent", "inventory_demand_investigator"),
                           extra=kwargs.get("extra", ""))
        changed = prepare_call(request, alias=kwargs.get("alias", "fast-default"), run=other_run,
                               occurrence=kwargs.get("occurrence", 0)).key
    assert changed != base, change


def test_key_ignores_transport_headers_and_includes_the_investigation_task() -> None:
    run = _run()
    ev = [_ev(), _ev()]
    with bind_run(run):
        plain = prepare_call(_request(run, ev), alias="fast-default", run=run, occurrence=0).key
        run.trace_id = "trace-other"  # → different x-litellm-trace-id header
        request = _request(run, ev)
        assert prepare_call(request, alias="fast-default", run=run, occurrence=0).key == plain
        with bind_task(TaskBinding(task_id="t2", specialist="inventory_demand", tools=())):
            in_task = prepare_call(_request(run, ev), alias="fast-default", run=run, occurrence=0).key
    assert in_task != plain


def test_identical_requests_in_one_run_take_successive_occurrences() -> None:
    run = _run()
    ev = [_ev(), _ev()]
    with bind_run(run):
        first = prepare_call(_request(run, ev, call_id="call_x"), alias="fast-default", run=run)
        second = prepare_call(_request(run, ev, call_id="call_x"), alias="fast-default", run=run)
    assert first.base_key == second.base_key
    assert (first.occurrence, second.occurrence) == (0, 1) and first.key != second.key


def test_response_placeholders_round_trip_to_the_current_runs_ids() -> None:
    rec_run, new_run = _run(), _run()
    rec_ev, new_ev = [_ev(), _ev()], [_ev(), _ev()]
    with bind_run(rec_run):
        rec = prepare_call(_request(rec_run, rec_ev), alias="fast-default", run=rec_run, occurrence=0)
    sig = base64.b64encode(b"opaque-signature").decode()
    response = {
        "content": {"role": "model", "parts": [
            {"text": f"Per {rec_ev[1]} and {rec_ev[0]}: case {rec_run.case_id} slips; unknown {_ev()}."},
            {"function_call": {"id": "call_minted123", "name": "get_po", "args": {"cite": rec_ev[0]}},
             "thought_signature": sig},
        ]},
    }
    stored = rec.normaliser.normalise_response(response)
    text = stored["content"]["parts"][0]["text"]
    assert text.startswith("Per ⟦ev#2⟧ and ⟦ev#1⟧: case ⟦case⟧ slips; unknown ev_")  # unknown ids stay literal
    assert stored["content"]["parts"][1]["function_call"]["id"] == "⟦newcall#1:call_⟧"
    assert stored["content"]["parts"][1]["thought_signature"] == sig

    with bind_run(new_run):
        new = prepare_call(_request(new_run, new_ev), alias="fast-default", run=new_run, occurrence=0)
    assert new.key == rec.key
    fresh: dict[str, str] = {}
    restored = new.normaliser.denormalise(stored, fresh)
    text = restored["content"]["parts"][0]["text"]
    assert f"Per {new_ev[1]} and {new_ev[0]}: case {new_run.case_id} slips" in text
    call = restored["content"]["parts"][1]["function_call"]
    assert call["args"]["cite"] == new_ev[0]
    assert call["id"].startswith("call_") and call["id"] != "call_minted123"
    # a second replay mints another fresh id (unique within the session)
    again = new.normaliser.denormalise(stored)
    assert again["content"]["parts"][1]["function_call"]["id"] != call["id"]


def test_normaliser_ignores_short_run_literals_and_handles_uuid_v7_and_blocks() -> None:
    norm = Normaliser([("c", "case"), ("run-12345", "run")])
    v7 = "0199b3c2-7e4a-7c3d-9f00-0123456789ab"
    out = norm.normalise_request({"t": f"c run-12345 run-123456 {v7} blk_0199b3c27e4a7c3d9f000123456789ab {v7}"})
    assert out["t"] == "c ⟦run⟧ run-123456 ⟦uuid#1⟧ ⟦blk#1⟧ ⟦uuid#1⟧"


def test_invalid_mode_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REPLAY_MODE", "sometimes")
    with pytest.raises(RuntimeConfigError):
        rp.replay_mode()
    monkeypatch.setenv("LLM_REPLAY_MODE", "")
    assert rp.replay_mode() == "off"
    with pytest.raises(RuntimeConfigError):
        KeelsonLiteLlm(alias="fast-default", api_base="http://x", api_key="k", replay_mode="always")  # type: ignore[arg-type]


# ─────────────────────────────── modes ───────────────────────────────


async def test_record_or_replay_second_identical_run_makes_zero_live_calls() -> None:
    store = InMemoryReplayStore()
    first_script = FakeScript({"inv": [_citing_turn]})
    run1 = _run()
    ev1 = [_ev(), _ev()]
    with bind_run(run1):
        live = await _call(_model(first_script, store, "record_or_replay"), _request(run1, ev1))
    assert len(first_script.requests["inv"]) == 1 and len(store.records) == 1
    stats1 = replay_stats(run1.run_id)
    assert (stats1.live, stats1.recorded, stats1.misses, stats1.replayed) == (1, 1, 1, 0)

    second_script = FakeScript()  # no scripted turn: any live call would fail the test
    run2 = _run()
    ev2 = [_ev(), _ev()]
    with bind_run(run2):
        replayed = await _call(_model(second_script, store, "record_or_replay"), _request(run2, ev2))
    assert second_script.requests == {}
    stats = replay_stats(run2.run_id)
    assert (stats.live, stats.replayed, stats.replayed_tokens) == (0, 1, 1234)

    final, original = _final(replayed), _final(live)
    text = next(p.text for p in final.content.parts if p.text and not p.thought)
    assert text == f"Delay confirmed for case {run2.case_id} per {ev2[1]} and {ev2[0]} (eta 2026-10-07T09:15:00Z)."
    [call] = [p.function_call for p in final.content.parts if p.function_call]
    assert call.args == {"po": "PO-BD-0002", "cite": ev2[0]}
    assert call.id and call.id != next(p.function_call.id for p in original.content.parts if p.function_call)
    assert [p.thought for p in final.content.parts] == [p.thought for p in original.content.parts]
    # zero cost in the trace; the recorded usage rides along for reporting
    assert final.usage_metadata.total_token_count == 0
    meta = final.custom_metadata["keelson_replay"]
    assert meta["replayed"] and meta["cost_usd"] == 0.0 and meta["recorded_usage"]["total_token_count"] == 1234
    assert original.usage_metadata.total_token_count == 1234 and not (original.custom_metadata or {})


async def test_replay_mode_fails_closed_without_a_live_call() -> None:
    store = InMemoryReplayStore()
    script = FakeScript({"inv": [_citing_turn]})
    run = _run()
    with bind_run(run), pytest.raises(ReplayMiss) as err:
        await _call(_model(script, store, "replay"), _request(run, [_ev(), _ev()]))
    assert script.requests == {} and store.puts == 0 and "fast-default" in str(err.value)
    assert replay_stats(run.run_id).misses == 1 and replay_stats(run.run_id).live == 0


async def test_replay_mode_serves_a_recording_made_in_record_mode() -> None:
    store = InMemoryReplayStore()
    rec_script = FakeScript({"inv": [_citing_turn, _citing_turn]})
    run1 = _run()
    with bind_run(run1):
        ev = [_ev(), _ev()]
        await _call(_model(rec_script, store, "record"), _request(run1, ev))
    run1b = _run()
    with bind_run(run1b):
        # record mode never replays: it calls live again and re-records the same key
        await _call(_model(rec_script, store, "record"), _request(run1b, [_ev(), _ev()]))
    assert len(rec_script.requests["inv"]) == 2 and store.puts == 2 and len(store.records) == 1
    assert replay_stats(run1b.run_id).replayed == 0 and store.gets == 0

    run2 = _run()
    empty = FakeScript()
    with bind_run(run2):
        out = await _call(_model(empty, store, "replay"), _request(run2, [_ev(), _ev()]))
    assert empty.requests == {} and _final(out).custom_metadata["keelson_replay"]["replayed"]


async def test_off_mode_and_unbound_calls_are_pure_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryReplayStore()
    script = FakeScript({"inv": [_citing_turn, _citing_turn, _citing_turn]})
    run = _run()
    monkeypatch.delenv("LLM_REPLAY_MODE", raising=False)
    with bind_run(run):
        await _call(_model(script, store, None), _request(run, [_ev(), _ev()]))  # type: ignore[arg-type]
        await _call(_model(script, store, "off"), _request(run, [_ev(), _ev()]))
    # no run bound (no tenant): always live even in replay mode
    await _call(_model(script, store, "replay"), _request(_run(), [_ev(), _ev()]))
    assert len(script.requests["inv"]) == 3 and store.gets == 0 and store.puts == 0


async def test_identical_request_twice_in_a_run_replays_both_answers_in_order() -> None:
    store = InMemoryReplayStore()
    turns = [FakeTurn(text="first answer"), FakeTurn(text="second answer (retry)")]
    run1 = _run()
    with bind_run(run1):
        ev = [_ev(), _ev()]
        model = _model(FakeScript({"inv": list(turns)}), store, "record_or_replay")
        texts1 = [_final(await _call(model, _request(run1, ev, call_id="call_same"))).content.parts[0].text
                  for _ in range(2)]
    run2 = _run()
    empty = FakeScript()
    with bind_run(run2):
        ev = [_ev(), _ev()]
        model = _model(empty, store, "replay")
        texts2 = [_final(await _call(model, _request(run2, ev, call_id="call_other"))).content.parts[0].text
                  for _ in range(2)]
    assert texts1 == texts2 == ["first answer", "second answer (retry)"] and empty.requests == {}


async def test_tenant_isolation_in_the_store() -> None:
    store = InMemoryReplayStore()
    run_a = _run(TENANT)
    with bind_run(run_a):
        await _call(_model(FakeScript({"inv": [_citing_turn]}), store, "record_or_replay"),
                    _request(run_a, [_ev(), _ev()]))
    run_b = _run(OTHER)
    script_b = FakeScript({"inv": [_citing_turn]})
    with bind_run(run_b), pytest.raises(ReplayMiss):
        await _call(_model(script_b, store, "replay"), _request(run_b, [_ev(), _ev()]))
    # record_or_replay in the other tenant calls live and records its own row
    run_b2 = _run(OTHER)
    with bind_run(run_b2):
        await _call(_model(script_b, store, "record_or_replay"), _request(run_b2, [_ev(), _ev()]))
    assert len(script_b.requests["inv"]) == 1
    assert {tenant for tenant, _key in store.records} == {TENANT, OTHER}


# ─────────────────────────────── streaming, errors, limits ───────────────────────────────


async def test_streaming_replay_emits_the_recorded_partial_sequence() -> None:
    store = InMemoryReplayStore()
    turn = FakeTurn(thought="a long chain of thought " * 4, text="the visible streamed answer " * 3, total_tokens=50)
    run1 = _run()
    with bind_run(run1):
        recorded = await _call(_model(FakeScript({"inv": [turn]}), store, "record_or_replay"),
                               _request(run1, [_ev(), _ev()]), stream=True)
    run2 = _run()
    with bind_run(run2):
        streamed = await _call(_model(FakeScript(), store, "replay"), _request(run2, [_ev(), _ev()]), stream=True)
    run3 = _run()
    with bind_run(run3):
        unary = await _call(_model(FakeScript(), store, "replay"), _request(run3, [_ev(), _ev()]))

    def shape(rs: list[LlmResponse]) -> list[tuple[bool, bool, str]]:
        return [(bool(r.partial), bool(r.content.parts[0].thought), r.content.parts[0].text or "") for r in rs]

    assert len(recorded) > 3 and shape(streamed) == shape(recorded)
    assert len(unary) == 1 and not unary[0].partial


async def test_errors_and_abandoned_calls_are_not_recorded() -> None:
    store = InMemoryReplayStore()
    run = _run()
    with bind_run(run):
        ev = [_ev(), _ev()]
        failing = FakeScript({"inv": [FakeTurn(error_code="429")]})
        await _call(_model(failing, store, "record_or_replay"), _request(run, ev))
        assert store.puts == 0
        # the consumer stops after the first partial: nothing is recorded
        model = _model(FakeScript({"inv": [FakeTurn(thought="x" * 100, text="y")]}), store, "record_or_replay")
        agen = model.generate_content_async(_request(run, ev, extra="!"), stream=True)
        first = await agen.__anext__()
        assert first.partial
        await agen.aclose()
    assert store.puts == 0 and replay_stats(run.run_id).recorded == 0


async def test_size_limit_compacts_partials_then_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryReplayStore()
    monkeypatch.setenv("LLM_REPLAY_MAX_BYTES", "6000")
    big_thought = "t" * 2500  # streamed as ~100 partial chunks (~20 kB with envelopes) + the final (~3 kB)
    run = _run()
    with bind_run(run):
        await _call(_model(FakeScript({"inv": [FakeTurn(thought=big_thought, text="ok")]}), store, "record_or_replay"),
                    _request(run, [_ev(), _ev()]), stream=True)
    [record] = store.records.values()
    assert record.bytes <= 6000 and all(not r.get("partial") for r in record.responses)

    huge = "z" * 7000  # the final alone exceeds the limit → not recorded
    run2 = _run()
    with bind_run(run2):
        await _call(_model(FakeScript({"inv": [FakeTurn(text=huge)]}), store, "record_or_replay"),
                    _request(run2, [_ev(), _ev()], extra="?"))
    assert len(store.records) == 1 and replay_stats(run2.run_id).too_large == 1
    # the limit is clamped to the DB hard cap
    monkeypatch.setenv("LLM_REPLAY_MAX_BYTES", str(10**9))
    assert rp.replay_max_bytes() == rp.HARD_MAX_BYTES
    with pytest.raises(ValueError):
        await store.put(tenant=TENANT, record=ReplayRecord(key="k", alias="a", agent="", spec_version="", model="m",
                                                           responses=[], bytes=rp.HARD_MAX_BYTES + 1))


class _BrokenStore:
    def __init__(self) -> None:
        self.puts = 0

    async def get(self, *, tenant: str, key: str) -> ReplayRecord | None:
        raise ConnectionError("db down")

    async def put(self, *, tenant: str, record: ReplayRecord) -> None:
        self.puts += 1
        raise ConnectionError("db down")


async def test_store_error_text_never_reaches_the_run_error() -> None:
    """A driver message can carry the SQL parameters (the recorded payload): only the class is shown."""

    class _LeakyStore(_BrokenStore):
        async def get(self, *, tenant: str, key: str) -> ReplayRecord | None:
            raise ConnectionError("INSERT ... params=('sk-secret-value', ...)")

    run = _run()
    with bind_run(run), pytest.raises(ReplayMiss) as err:
        await _call(ReplayLlm(inner=FakeLlm(agent="inv", script=FakeScript()), alias="fast-default", mode="replay",
                              store=_LeakyStore()), _request(run, [_ev(), _ev()]))
    assert "ConnectionError" in str(err.value) and "sk-secret" not in str(err.value)


class _Annotating(FakeLlm):
    """A live model whose responses carry metadata another layer attached."""

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):  # type: ignore[override]
        async for r in super().generate_content_async(llm_request, stream=stream):
            yield r.model_copy(update={"custom_metadata": {"x-litellm-key": "sk-live-123", "cost": 0.01}})


async def test_metadata_attached_to_a_live_response_is_not_recorded() -> None:
    """Only the model's answer is stored: ``custom_metadata`` (headers, cost, other layers' notes)
    never reaches the long-lived table."""
    store = InMemoryReplayStore()
    run = _run()
    with bind_run(run):
        model = ReplayLlm(inner=_Annotating(agent="inv", script=FakeScript({"inv": [_citing_turn]})),
                          alias="fast-default", mode="record_or_replay", store=store)
        live = await _call(model, _request(run, [_ev(), _ev()]))
    assert _final(live).custom_metadata["x-litellm-key"] == "sk-live-123"  # the live caller still sees it
    [record] = store.records.values()
    assert "sk-live" not in json.dumps(record.responses)
    assert all("custom_metadata" not in r for r in record.responses)


def _replay_problems(name: str, text: str, *, yaml_file: bool) -> list[str]:
    """What a manifest does wrong with ``LLM_REPLAY_MODE``: every setting must be the literal
    ``off`` — env entries ``{name: LLM_REPLAY_MODE, value: …}`` (``valueFrom`` is not a literal) and
    ``LLM_REPLAY_MODE=<value>`` in strings (``--set-env-vars``, scripts) — and a mention neither
    form explains (it could set the mode some other way) is a problem too."""
    import re  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    found: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("name") == "LLM_REPLAY_MODE":
                found.append({"valueFrom": node["valueFrom"]} if "valueFrom" in node else node.get("value"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node != "LLM_REPLAY_MODE":
            found.extend(m.group(1) for m in re.finditer(r"LLM_REPLAY_MODE=([^\s\"',;]*)", node))

    if yaml_file:
        for doc in yaml.safe_load_all(text):
            walk(doc)
    else:
        walk(text)
    mentions = text.count("LLM_REPLAY_MODE")
    problems = [f"{name}: LLM_REPLAY_MODE={v!r}" for v in found if v != "off"]
    if len(found) != mentions:
        problems.append(f"{name}: {mentions} mention(s), {len(found)} recognised setting(s)")
    return problems


def test_production_deployments_never_enable_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code default is ``off``; the Cloud Run manifests (production) may only pin it ``off``
    explicitly (M6 sets ``LLM_REPLAY_MODE=off`` on the engine services), never enable replay.
    Only the dev/demo compose stack defaults to ``record_or_replay``."""
    monkeypatch.delenv("LLM_REPLAY_MODE", raising=False)
    assert rp.replay_mode() == "off"
    cloudrun = Path(__file__).resolve().parents[4] / "infra" / "cloudrun"
    manifests = [p for p in cloudrun.rglob("*") if p.is_file()]
    if not manifests:
        pytest.skip("infra/cloudrun is not in this checkout")
    problems: list[str] = []
    for path in manifests:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "LLM_REPLAY_MODE" in text:
            problems += _replay_problems(path.name, text, yaml_file=path.suffix in (".yaml", ".yml"))
    assert not problems


@pytest.mark.parametrize(("text", "ok"), [
    ('env:\n  - name: LLM_REPLAY_MODE\n    value: "off"\n', True),
    ("env:\n  - name: LLM_REPLAY_MODE\n    value: record_or_replay\n", False),
    ("env:\n  - name: LLM_REPLAY_MODE\n    value: replay\n  - name: LLM_REPLAY_MODE\n    value: 'off'\n", False),
    ("env:\n  - name: LLM_REPLAY_MODE\n    valueFrom: {secretKeyRef: {name: x, key: y}}\n", False),
    ("env:\n  - name: LLM_REPLAY_MODE\n", False),  # no value at all: not a literal off
    ('args: ["--set-env-vars=LLM_REPLAY_MODE=replay"]\n', False),
    ('args: ["--set-env-vars=A=1,LLM_REPLAY_MODE=off"]\n', True),
    ("# LLM_REPLAY_MODE: record_or_replay\nx: 1\n", False),  # an unexplained mention is flagged
])
def test_replay_manifest_check(text: str, ok: bool) -> None:
    assert (not _replay_problems("m.yaml", text, yaml_file=True)) is ok


async def test_store_errors_fail_open_only_in_record_or_replay() -> None:
    store = _BrokenStore()
    script = FakeScript({"inv": [_citing_turn]})
    run = _run()
    with bind_run(run):
        model = ReplayLlm(inner=FakeLlm(agent="inv", script=script), alias="fast-default", mode="record_or_replay",
                          store=store)
        out = await _call(model, _request(run, [_ev(), _ev()]))
        assert _final(out).usage_metadata.total_token_count == 1234  # the live answer stands
        strict = ReplayLlm(inner=FakeLlm(agent="inv", script=script), alias="fast-default", mode="replay", store=store)
        with pytest.raises(ReplayMiss, match="store error"):
            await _call(strict, _request(run, [_ev(), _ev()]))
    assert replay_stats(run.run_id).store_errors == 3 and len(script.requests["inv"]) == 1


# ─────────────────────────────── KeelsonLiteLlm ───────────────────────────────


class CapturingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def acompletion(self, model: str, messages: Any, tools: Any, **kwargs: Any) -> Any:
        import litellm

        self.calls.append({"model": model, **kwargs})
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "ok from gemini"}, "finish_reason": "stop",
                      "index": 0}],
            usage={"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
        )


async def test_keelson_litellm_records_then_replays_without_calling_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    store = InMemoryReplayStore()
    rp.set_default_store(store)
    try:
        client = CapturingClient()
        model = KeelsonLiteLlm(alias="fast-default", api_base="http://litellm:4000", api_key="sk-tenant")
        model.llm_client = client  # type: ignore[assignment]
        run1 = _run()
        with bind_run(run1):
            [first] = await _call(model, _request(run1, [_ev(), _ev()]))
        assert len(client.calls) == 1 and client.calls[0]["extra_headers"]["x-litellm-customer-id"] == TENANT
        [record] = store.records.values()
        assert record.alias == "fast-default" and record.agent == "inventory_demand_investigator"
        assert record.usage["total_token_count"] == 42

        run2 = _run()  # new run, case, trace id → new headers, same normalised request
        with bind_run(run2):
            [again] = await _call(model, _request(run2, [_ev(), _ev()]))
        assert len(client.calls) == 1
        assert again.content.parts[0].text == first.content.parts[0].text == "ok from gemini"
        assert again.usage_metadata.total_token_count == 0

        # an explicit replay mode on the model wins over the env; a miss never reaches the proxy
        strict = KeelsonLiteLlm(alias="fast-default", api_base="http://litellm:4000", api_key="sk-tenant",
                                replay_mode="replay", replay_store=store)
        strict.llm_client = client  # type: ignore[assignment]
        run3 = _run()
        with bind_run(run3), pytest.raises(ReplayMiss):
            await _call(strict, _request(run3, [_ev(), _ev()], extra=" new question"))
        assert len(client.calls) == 1
    finally:
        rp.set_default_store(None)


# ─────────────────────────────── whole workflow ───────────────────────────────


def _cache(h: Harness, models: Any) -> RunnerCache:
    async def spec_loader(tenant: str, version: str) -> AgentSpec:
        return h.spec(tenant)

    async def bindings(tenant: str, spec: AgentSpec) -> WorkflowBindings:
        return WorkflowBindings(models=models)

    return RunnerCache(session_service=DatabaseSessionService(db_url=h.db_url), spec_loader=spec_loader,
                       bindings=bindings)


async def _chat_run(h: Harness, models: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Chat (pauses at the approach card) → confirm → resume on a fresh runner (investigation with
    evidence ids, structurer, critic, recommender)."""
    statuses: list[str] = []
    events: list[dict[str, Any]] = []
    cache = _cache(h, models)
    try:
        row = h.row("chat", {"text": "Meghna Trims says the trims for our puffers are delayed"})
        statuses.append(await h.run(row, cache=cache))
        events += h.events(row.id)
    finally:
        await cache.close()
    fresh = _cache(h, models)
    try:
        decider = {"sub": PRIYA["sub"], "username": PRIYA["username"], "name": PRIYA["name"]}
        resume = h.resume_row(row, "confirm", [decider])
        statuses.append(await h.run(resume, cache=fresh))
        events += h.events(resume.id)
    finally:
        await fresh.close()
    return statuses, events


async def test_identical_second_workflow_run_makes_zero_live_calls(tmp_path: Any) -> None:
    """A whole chat case (intake → planner → approach pause → resume → investigation → critic →
    recommender) recorded once, then replayed in a fresh harness with a different case id, new run
    ids and new evidence ids and an *empty* script: zero model calls, the same outcome and blocks."""
    store = InMemoryReplayStore()
    for sub in ("rec", "replay"):
        (tmp_path / sub).mkdir()
    first = await Harness(tmp=tmp_path / "rec").setup()
    try:
        first.script = chat_flow_script(first.core.repo)
        status1, events1 = await _chat_run(first, ReplayModelFactory(FakeModelFactory(first.script),
                                                                     mode="record_or_replay", store=store))
        live_calls = sum(len(v) for v in first.script.requests.values())
    finally:
        await first.close()
    assert status1 == ["paused", "succeeded"] and live_calls >= 5 and len(store.records) == live_calls

    second = await Harness(tmp=tmp_path / "replay", case_id="case-replayed-7").setup()
    try:
        second.script = FakeScript()
        status2, events2 = await _chat_run(second, ReplayModelFactory(FakeModelFactory(second.script),
                                                                      mode="replay", store=store))
        assert second.script.requests == {}
        [interrupt] = second.core.interrupts.created
        assert interrupt["interrupt_id"] == "approach-case-replayed-7-1"
        # the replayed structurer cited *this* run's evidence ids (re-mapped by position)
        evidence = set(second.core.repo.evidence)
        cited = {ref for e in events2 if e["type"] == "TOOL_CALL_RESULT" for ref in e["metadata"]["sc"]["evidenceRefs"]}
        assert cited and cited <= evidence
    finally:
        await second.close()
    assert status2 == ["paused", "succeeded"]

    def kinds(events: list[dict[str, Any]]) -> list[str]:
        return [e["content"]["kind"] for e in events if e["type"] == "ACTIVITY_SNAPSHOT"]

    assert kinds(events2) == kinds(events1)
    assert [e["type"] for e in events2 if e["type"].startswith("SUBAGENT")] == \
           [e["type"] for e in events1 if e["type"].startswith("SUBAGENT")]
