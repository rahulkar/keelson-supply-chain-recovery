"""S1 (opt-in, ``pytest -m live``): the real LiteLLM aliases with the real output schemas.

Runs each LLM role as a workflow node exactly as the runtime builds it (single_turn,
include_contents='none', instruction provider, strict output schema through
``litellm_proxy/<alias>``) and checks that the output is schema-valid and that reasoning
(thought) parts come back. Needs ``LITELLM_BASE_URL`` (default ``http://litellm:4000``) to be
reachable and ``LITELLM_TEST_KEY`` (a LiteLLM key: the tenant virtual key or the master key).
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from fakes.core import FakeCore
from fakes.harness import PRIYA
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.apps.app import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, Workflow
from google.genai import types
from keelson_contracts.llm import Findings, IntakeResult, Recommendation
from keelson_contracts.runs import Actor
from pydantic import BaseModel

from keelson_engine.runtime.agents import fill_nullable, make_agent
from keelson_engine.runtime.context import RunContext, bind_run
from keelson_engine.runtime.llm import LiteLlmModelFactory
from keelson_engine.runtime.nodes.prompts import (
    intake_instruction,
    recommender_instruction,
    structurer_instruction,
)
from keelson_engine.runtime.spec import AgentSpec

pytestmark = pytest.mark.live

BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
KEY = os.environ.get("LITELLM_TEST_KEY", "")


def _reachable() -> bool:
    try:
        return httpx.get(f"{BASE}/health/readiness", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


skip_unless_live = pytest.mark.skipif(not (KEY and _reachable()), reason="LiteLLM proxy not reachable / no key")


async def _run_role(name: str, alias: str, instruction: Any, schema: type[BaseModel], text: str, run: RunContext) -> tuple[dict[str, Any], list[str]]:
    agent = make_agent(name=name, alias=alias, models=LiteLlmModelFactory(api_base=BASE, api_key=KEY),
                       instruction=instruction, output_schema=schema)
    wf = Workflow(name="s1", edges=[(START, agent)])
    sessions = InMemorySessionService()
    runner = Runner(app=App(name="t_demo_retail", root_agent=wf), session_service=sessions)
    await sessions.create_session(app_name="t_demo_retail", user_id="u", session_id=name)
    output: dict[str, Any] | None = None
    thoughts: list[str] = []
    with bind_run(run):
        async for event in runner.run_async(
            user_id="u", session_id=name, new_message=types.Content(role="user", parts=[types.Part(text=text)]),
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            assert not event.error_code, f"{name}: {event.error_code} {event.error_message}"
            for part in (event.content.parts if event.content and event.content.parts else []):
                if part.thought and part.text:
                    thoughts.append(part.text)
            if event.output is not None and isinstance(event.output, dict):
                output = event.output
    await runner.close()
    assert output is not None, f"{name} produced no structured output"
    schema.model_validate(fill_nullable(schema, output))
    return output, thoughts


def _run_ctx() -> RunContext:
    core = FakeCore()
    return RunContext(
        tenant="demo-retail", case_id="case-live", run_id="run-live", kind="chat", actor=Actor.model_validate(PRIYA),
        payload={}, spec=AgentSpec(tenant="demo-retail"), deps=core.deps(), invocation_id="e-run-live",
        trace_id="s1-live", case_title="Trim delay", history=[],
    )


@skip_unless_live
async def test_s1_intake_fast_default() -> None:
    run = _run_ctx()
    out, thoughts = await _run_role(
        "intake", "fast-default", intake_instruction, IntakeResult,
        "Meghna Trims just emailed: the zips and wadding for PO-BD-0001 and PO-BD-0002 will ship 7 days late.", run,
    )
    result = IntakeResult.model_validate(fill_nullable(IntakeResult, out))
    print("S1 intake:", json.dumps(out)[:400], "| thought parts:", len(thoughts))
    assert result.kind == "NEW_DISRUPTION" and result.disruption is not None
    assert result.disruption.delay_days == 7 and not result.disruption.is_hypothetical
    assert any("PO-BD-0001" in m.text or "Meghna" in m.text for m in result.disruption.entity_mentions)


@skip_unless_live
async def test_s1_structurer_findings() -> None:
    run = _run_ctx()
    run.evidence_log = [
        {"id": "ev_inv001", "agent": "inventory_demand_investigator", "tool": "get_inventory", "args": {"location_type": "dc"}},
        {"id": "ev_fc002", "agent": "inventory_demand_investigator", "tool": "get_demand_forecast", "args": {"weeks": ["2026-W45"]}},
    ]
    notes = (
        "TASK task-inventory_demand-r1\n\nUK1 holds limited ATP for PUF-W-BLK-M [ev_inv001 #/items/0/atp]. "
        "DE1 holds surplus of the same SKU [ev_inv001 #/items/4/atp]. Peak week demand at UK1-COLD-A is high "
        "[ev_fc002 #/items/0/units]. Inference: UK1 will run short in 2026-W45 if the BD POs slip."
    )
    out, thoughts = await _run_role("inventory_demand_structurer", "fast-default", structurer_instruction("inventory_demand"),
                                    Findings, notes, run)
    findings = Findings.model_validate(fill_nullable(Findings, out))
    print("S1 structurer:", json.dumps(out)[:400], "| thought parts:", len(thoughts))
    assert findings.status == "ok" and findings.claims
    assert {c.evidence_id for c in findings.claims if c.kind == "fact"} <= {"ev_inv001", "ev_fc002"}


@skip_unless_live
async def test_s1_recommender_reasoning_default() -> None:
    run = _run_ctx()
    brief = {
        "scenario": {"event_type": "trim_delay", "description": "Trims for BD POs delayed by a week", "mode": "actual"},
        "options": [
            {"id": "opt-A", "title": "Rebalance between DCs",
             "assessment": "improves the outcome versus doing nothing (recovers more margin than the transfers cost)",
             "kpi_placeholders": ["{{kpi:opt-A.units_recovered}}", "{{kpi:opt-A.net_benefit_cents}}",
                                  "{{kpi:opt-A.transfer_cost_cents}}", "{{kpi:opt-A.units_at_risk}}"]},
            {"id": "do-nothing", "title": "Do nothing", "assessment": "baseline: keeps today's plan",
             "kpi_placeholders": ["{{kpi:do-nothing.units_at_risk}}", "{{kpi:do-nothing.margin_at_risk_cents}}"]},
        ],
        "findings": ["UK1 will run short in the peak weeks.", "DE1 holds surplus stock of the affected styles."],
    }
    out, thoughts = await _run_role("recommend", "reasoning-default", recommender_instruction, Recommendation,
                                    json.dumps(brief), run)
    rec = Recommendation.model_validate(fill_nullable(Recommendation, out))
    print("S1 recommender:", json.dumps(out)[:500], "| thought parts:", len(thoughts))
    assert rec.recommended_option_id in {"opt-A", "do-nothing"}
    assert thoughts, "reasoning-default should stream thought (reasoning) parts"
