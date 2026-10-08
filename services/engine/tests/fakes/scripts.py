"""Canned FakeLlm turns for the workflow tests."""

from __future__ import annotations

from typing import Any

from google.adk.models.llm_request import LlmRequest

from keelson_engine.runtime.llm import FakeScript, FakeTurn

from .core import FakeRepo


def intake_disruption(*, delay_days: int | None = None, hypothetical: bool = False, mentions: list[dict[str, str]] | None = None) -> FakeTurn:
    return FakeTurn(
        thought="The user reports a supplier trim delay.",
        json={
            "kind": "NEW_DISRUPTION",
            "summary": "Trims from Meghna are delayed",
            "disruption": {
                "event_family": "SUP",
                "event_type": "SUP.delay",
                "description": "Meghna Trims reports delayed trims for puffer jackets",
                "entity_mentions": mentions if mentions is not None else [{"type": "supplier", "text": "Meghna Trims"}],
                "delay_days": delay_days,
                "demand_change_pct": None,
                "start_date": None,
                "duration_days": None,
                "is_hypothetical": hypothetical,
            },
        },
    )


def intake_kind(kind: str) -> FakeTurn:
    return FakeTurn(json={"kind": kind, "summary": "user message", "disruption": None})


def _evidence_ids(request: LlmRequest) -> list[str]:
    ids: list[str] = []
    for content in request.contents or []:
        for part in content.parts or []:
            if part.function_response is not None:
                ev = (part.function_response.response or {}).get("_evidence_id")
                if ev:
                    ids.append(ev)
    return ids


def investigator_turns() -> list[Any]:
    def notes(request: LlmRequest) -> FakeTurn:
        ids = _evidence_ids(request)
        assert ids, "investigator should see tool results with _evidence_id"
        return FakeTurn(
            thought="Checking stock against forecast.",
            text=f"UK1 holds stock for PUF-W-BLK-M [{ids[0]} #/items/0/atp]. Peak weeks need more [{ids[-1]} #/items/0/units].",
        )

    return [
        FakeTurn(function_calls=[("get_inventory", {"location_type": "dc"}), ("get_demand_forecast", {"weeks": ["2026-W45"]})]),
        notes,
    ]


def structurer_turn(repo: FakeRepo, task_slug: str = "inventory_demand") -> Any:
    def findings(request: LlmRequest) -> FakeTurn:
        inv = repo.evidence_by_tool("get_inventory")[-1]["id"]
        return FakeTurn(
            json={
                "task_id": f"task-{task_slug}-r1",
                "status": "ok",
                "claims": [
                    {"text": f"UK1 has {{{{ev:{inv}#/items/0/atp}}}} units available of the first SKU.", "kind": "fact",
                     "evidence_id": inv, "json_pointer": "/items/0/atp", "calc_id": None, "confidence": "high"},
                    {"text": "Peak weeks will run short in UK1 and NL1.", "kind": "inference", "evidence_id": None,
                     "json_pointer": None, "calc_id": None, "confidence": "medium"},
                ],
                "affected": [{"type": "dc", "code": "UK1", "label": "Thurrock"}],
                "gaps": [],
            }
        )

    return findings


def recommendation(*, free_number: bool = False, option: str = "opt-A") -> FakeTurn:
    bullet = "Moves {{kpi:opt-A.units_moved}} from DE1 to NL1 and recovers {{kpi:opt-A.units_recovered}}."
    if free_number:
        bullet = "Moves 300 units from DE1 to NL1."
    return FakeTurn(
        thought="Rebalancing beats doing nothing.",
        json={
            "headline": "Rebalance stock from DE1 to NL1 to protect the peak weeks.",
            "bullets": [bullet, "Net benefit is {{kpi:opt-A.net_benefit_cents}} after transfer costs."],
            "recommended_option_id": option,
            "why_not_others": [{"option_id": "do-nothing", "reason": "Leaves {{kpi:do-nothing.units_at_risk}} at risk."}],
            "risks": ["Transit could slip if the lane is congested."],
            "follow_ups": ["Confirm the new ETA with Meghna Trims."],
        },
    )


def planner_turn(*tasks: tuple[str, str, list[str], list[str]] | tuple[str, str], max_parallel: int = 4,
                 objective: str | None = None) -> FakeTurn:
    """An ``InvestigationPlan``: ``(id, specialist[, tools, depends_on])`` per task."""
    items = []
    for task in tasks or (("t1", "inventory_demand"),):
        tid, specialist, *rest = task
        tools = list(rest[0]) if rest else []
        deps = list(rest[1]) if len(rest) > 1 else []
        items.append({"id": tid, "specialist": specialist,
                      "objective": objective or f"Check the {specialist.replace('_', ' ')} exposure",
                      "tools": tools, "depends_on": deps})
    return FakeTurn(json={"rationale": "Check the systems this event touches.", "tasks": items,
                          "budget": {"max_parallel": max_parallel, "max_tasks": max(1, len(items))}})


def critic_turn(verdict: str = "PASS", revise: list[str] | None = None, issues: list[dict[str, Any]] | None = None) -> FakeTurn:
    return FakeTurn(json={"verdict": verdict, "issues": issues or [], "revise_task_ids": revise or []})


def m4_review_turns(script: FakeScript, *, planner: FakeTurn | None = None) -> FakeScript:
    """The M4 planner (one inventory & demand task) and a PASS critique for M1–M3 style flows."""
    script.add("planner", planner or planner_turn())
    script.add("critic", critic_turn())
    return script


def chat_flow_script(repo: FakeRepo, *, delay_days: int | None = None, free_number: bool = False) -> FakeScript:
    script = FakeScript()
    script.add("intake", intake_disruption(delay_days=delay_days))
    m4_review_turns(script)
    script.add("inventory_demand_investigator", *investigator_turns())
    script.add("inventory_demand_structurer", structurer_turn(repo))
    script.add("recommend", recommendation(free_number=free_number))
    if free_number:
        script.add("recommender_repair", recommendation())
    return script


# ─────────────────────────────── M4 helpers ───────────────────────────────


def user_text(request: LlmRequest) -> str:
    """The text of the request's user turn(s) (single_turn agents: the node input)."""
    return "\n".join(
        p.text or "" for c in request.contents or [] if c.role == "user" for p in c.parts or [] if p.text
    )


def request_dump(request: LlmRequest) -> str:
    """Everything a model call carries: system instruction, texts, function calls and responses."""
    import json  # noqa: PLC0415

    chunks = [str(request.config.system_instruction or "")]
    for content in request.contents or []:
        for part in content.parts or []:
            if part.text:
                chunks.append(part.text)
            if part.function_call is not None:
                chunks.append(json.dumps(dict(part.function_call.args or {}), default=str))
            if part.function_response is not None:
                chunks.append(json.dumps(dict(part.function_response.response or {}), default=str))
    return "\n".join(chunks)


def function_responses(request: LlmRequest, name: str) -> list[dict[str, Any]]:
    return [
        dict(p.function_response.response or {})
        for c in request.contents or [] for p in c.parts or []
        if p.function_response is not None and p.function_response.name == name
    ]


def guard_by_keyword(*flag_words: str) -> Any:
    """Guard turn: flags a text containing any of ``flag_words`` (order-independent: guard
    calls run in parallel)."""

    def turn(request: LlmRequest) -> FakeTurn:
        text = user_text(request).lower()
        flagged = any(w.lower() in text for w in flag_words)
        return FakeTurn(json={"injection_suspected": flagged, "reason": "instructions to an assistant" if flagged else "none"})

    return turn


def extractor_items(make: Any, *, injection: bool = False) -> Any:
    """Extractor turn: ``make(items)`` → item dicts, where ``items`` are the
    ``{source_evidence_id, text}`` the quarantine wrapper sent."""
    import json  # noqa: PLC0415
    import re  # noqa: PLC0415

    def turn(request: LlmRequest) -> FakeTurn:
        text = user_text(request)
        match = re.search(r"\{.*\}", text, re.S)
        items = json.loads(match.group(0))["items"] if match else []
        return FakeTurn(json={"items": [] if injection else make(items), "injection_suspected": injection})

    return turn


# ─────────────────────────────── M7 helpers ───────────────────────────────


def declared_tools(request: LlmRequest) -> set[str]:
    """Every tool name a model call was offered (ADK's ``tools_dict`` + the function declarations)."""
    names = set(getattr(request, "tools_dict", {}) or {})
    for tool in request.config.tools or []:
        for decl in getattr(tool, "function_declarations", None) or []:
            names.add(decl.name)
    return names


def write_tools(core: Any) -> set[str]:
    """The write / destructive tools of every connector of a :class:`~fakes.core.FakeCore`."""
    out = {t.name for t in core.connectors.tools if t.access != "read"}
    for tools in core.connectors.registry_tools.values():
        out |= {t.name for t in tools if t.access != "read"}
    return out


def assert_no_write_tool_reached_an_llm(script: FakeScript, writes: set[str]) -> None:
    """No model call of any agent was offered one of ``writes`` (write tools run only from
    deterministic nodes after approval)."""
    assert writes, "the fixture should offer write tools"
    for agent, requests in script.requests.items():
        for request in requests:
            offered = declared_tools(request) & writes
            assert not offered, f"{agent} was offered write tools {sorted(offered)}"
