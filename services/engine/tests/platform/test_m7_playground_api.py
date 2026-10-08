"""M7 agent playground API + custom specialist checks on the spec PUT
(docs/contracts/110-m7-backoffice-generic.md §3). No model is ever called: the turn itself
(``runtime.playground.run_playground``) is replaced by a fake that streams two AG-UI events and
returns a ``PlaygroundResult``; the runtime side has its own FakeLlm tests (tests/runtime)."""

from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any

import pytest
import pytest_asyncio
from keelson_contracts.agents import PlaygroundError, PlaygroundResult, PlaygroundUsage

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import TENANT_A
from e1_litellm import FakeLiteLLM
from keelson_engine.core import connectors
from test_api_connectors import TOOLS
from test_tokens import MERXA_SPEC

pytestmark = pytest.mark.asyncio(loop_scope="session")

SPEC = "/api/admin/agents/spec"
PG = "/api/admin/agents/playground"


def specialist(**over: Any) -> dict[str, Any]:
    base = {
        "slug": "supplier_watch", "display_name": "Supplier watch", "description": "Checks inventory for a style.",
        "role_prompt": "Look at inventory positions only.", "model_alias": "fast-default",
        "tools": [{"connector": "merxa", "tool": "get_inventory"}], "kb_ids": [], "max_tool_calls": 4,
        "max_turns": 3, "enabled": True,
    }
    base.update(over)
    return base


@pytest_asyncio.fixture(loop_scope="session")
async def sam(browser_factory: Any, platform: None) -> Browser:
    await reset_specs(TENANT_A)
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    await connectors.upsert_tool_snapshot(TENANT_A, "merxa", copy.deepcopy(TOOLS), auto_approve=True,
                                          untrusted=("list_supplier_messages",))
    return await browser_factory("sam")


@pytest.fixture
def live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    monkeypatch.delenv("PLAYGROUND_ENABLED", raising=False)


def _frames(text: str) -> list[dict[str, Any]]:
    out = []
    for block in text.split("\n\n"):
        data = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


async def test_availability_follows_replay_mode(sam: Browser, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    monkeypatch.delenv("PLAYGROUND_ENABLED", raising=False)
    assert (await sam.get(PG)).json() == {"available": True, "mode": "record_or_replay", "reason": None,
                                          "message": None}
    monkeypatch.setenv("LLM_REPLAY_MODE", "replay")
    body = (await sam.get(PG)).json()
    assert body["available"] is False and body["reason"] == "replay_only"
    r = await sam.post(PG, json={"specialist": specialist(), "question": "Which styles are short?"})
    assert r.status_code == 409 and r.json()["code"] == "playground_unavailable"
    monkeypatch.setenv("LLM_REPLAY_MODE", "off")
    monkeypatch.setenv("PLAYGROUND_ENABLED", "0")
    assert (await sam.get(PG)).json()["reason"] == "disabled"


async def test_playground_refusals_before_any_model_call(sam: Browser, gateway_ready: FakeLiteLLM,
                                                         live_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []

    async def never(*args: Any, **kwargs: Any) -> Any:
        called.append(args)
        raise AssertionError("the turn must not start")

    monkeypatch.setattr("keelson_engine.runtime.playground.run_playground", never)
    q = "Which styles are short?"
    r = await sam.post(PG, json={"specialist": "nobody_here", "question": q})
    assert r.status_code == 404, r.text
    r = await sam.post(PG, json={"specialist": specialist(slug="inventory_demand"), "question": q})
    assert r.status_code in (422,), r.text
    r = await sam.post(PG, json={"specialist": specialist(tools=[{"connector": "merxa", "tool": "apply_change_set"}]),
                                 "question": q})
    assert r.status_code == 422 and r.json()["code"] == "custom_specialist_invalid", r.text
    r = await sam.post(PG, json={"specialist": specialist(model_alias="no-such-alias"), "question": q})
    assert r.status_code == 422 and "not available" in r.json()["message"], r.text
    r = await sam.post(PG, json={"specialist": specialist(model_alias="gemini-2.5-pro"), "question": q})
    assert r.status_code == 422, r.text  # a Pro alias never passes the contract model
    r = await sam.post(PG, json={"specialist": specialist(), "question": ""})
    assert r.status_code == 422
    assert not called


async def test_playground_streams_the_turn_and_its_result(sam: Browser, gateway_ready: FakeLiteLLM,
                                                          live_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_turn(request: Any, *, tenant: str, spec: Any, models: Any, actor: Any, emit: Any = None,
                        playground_id: str | None = None, controller: Any = None, deps: Any = None) -> PlaygroundResult:
        seen.update(tenant=tenant, slug=getattr(request.specialist, "slug", request.specialist), actor=actor)
        await emit({"type": "RUN_STARTED", "threadId": "t", "runId": "r"})
        await emit({"type": "SUBAGENT_STARTED", "name": "supplier_watch", "description": "Supplier watch"})
        return PlaygroundResult(
            playground_id=playground_id or "x", specialist="supplier_watch", display_name="Supplier watch",
            status="failed", findings=None, tool_calls=[],
            usage=PlaygroundUsage(llm_calls=0, tool_calls=0, tokens_in=0, tokens_out=0),
            error=PlaygroundError(code="no_findings", message="The specialist returned no findings."),
            started_at=dt.datetime.now(dt.UTC), duration_ms=5,
        )

    monkeypatch.setattr("keelson_engine.runtime.playground.run_playground", fake_turn)
    r = await sam.post(PG, json={"specialist": specialist(), "question": "Which styles are short?"},
                       headers={"Accept": "text/event-stream"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _frames(r.text)
    assert [f["type"] for f in frames][:2] == ["RUN_STARTED", "SUBAGENT_STARTED"]
    last = frames[-1]
    assert last["type"] == "CUSTOM" and last["name"] == "sc.playground.result"
    assert last["value"]["specialist"] == "supplier_watch"
    assert seen["tenant"] == TENANT_A and seen["slug"] == "supplier_watch" and seen["actor"]["username"] == "sam"
    audit = (await sam.get("/api/admin/audit?action=agents.playground&limit=5")).json()["items"]
    assert audit and audit[0]["target"] == "specialist:supplier_watch"


async def test_spec_put_checks_custom_specialists_against_tenant_state(sam: Browser,
                                                                       gateway_ready: FakeLiteLLM) -> None:
    base = (await sam.get(SPEC)).json()
    bad = await sam.put(SPEC, json={"base_version": base["version"], "spec_patch": {"custom_specialists": [
        specialist(tools=[{"connector": "merxa", "tool": "apply_change_set"}])]}})
    assert bad.status_code == 422 and bad.json()["code"] == "custom_specialist_invalid", bad.text
    bad = await sam.put(SPEC, json={"base_version": base["version"], "spec_patch": {"custom_specialists": [
        specialist(model_alias="no-such-alias")]}})
    assert bad.status_code == 422 and "not available" in bad.json()["message"], bad.text
    bad = await sam.put(SPEC, json={"base_version": base["version"], "spec_patch": {"custom_specialists": [
        specialist(tools=[], kb_ids=["018f2b1e-0000-7000-8000-000000000001"])]}})
    assert bad.status_code == 422 and "unknown knowledge bases" in bad.json()["message"], bad.text
    ok = await sam.put(SPEC, json={"base_version": base["version"], "spec_patch": {"custom_specialists": [
        specialist()]}})
    assert ok.status_code == 201, ok.text
    saved = ok.json()
    assert [s["slug"] for s in saved["spec"]["custom_specialists"]] == ["supplier_watch"]
    # unchanged entries are not re-checked: a later unrelated edit passes even if state moved
    again = await sam.put(SPEC, json={"base_version": saved["version"], "spec_patch": {"prompts": {
        "planner": "Prefer fewer lines."}}})
    assert again.status_code == 201, again.text
    await reset_specs(TENANT_A)
