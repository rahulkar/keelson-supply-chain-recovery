"""Deterministic routing nodes and the small terminal nodes.

Routing is decided on typed envelopes only (the run envelope built by the server, the
``IntakeResult`` enum) — never on free text.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from google.adk.agents.context import Context
from google.adk.events.event import Event
from keelson_contracts.llm import DisruptionExtract, IntakeResult
from keelson_contracts.signals import is_uuid

from ..agents import fill_nullable
from ..context import RunContext, current_run
from .common import case_event, case_status, input_dict, routed, say
from .prompts import CASE_BRIEF_KEY

__all__ = [
    "CASE_BRIEF_KEY",
    "ROUTE_APPLY",
    "ROUTE_CHAT",
    "ROUTE_CHECKLIST_STEP",
    "ROUTE_GUIDE",
    "ROUTE_ROLLBACK",
    "ROUTE_SIGNAL",
    "ROUTE_SIGNAL_EVENT",
    "ROUTE_CONFIRM_ASSUMPTIONS",
    "ROUTE_FOLLOWUP",
    "ROUTE_NEW_DISRUPTION",
    "ROUTE_PROMOTE",
    "ROUTE_QUESTION",
    "close_case",
    "compose_smalltalk",
    "pre_route",
    "case_brief",
    "modifies_current_case",
    "promote_case",
    "reject_run",
    "route_intake",
]

log = structlog.get_logger(__name__)

ROUTE_CHAT = "CHAT"
ROUTE_APPLY = "APPLY"
ROUTE_GUIDE = "GUIDE"
ROUTE_ROLLBACK = "ROLLBACK"
ROUTE_CHECKLIST_STEP = "CHECKLIST_STEP"
ROUTE_SIGNAL = "SIGNAL"
ROUTE_SIGNAL_EVENT = "SIGNAL_EVENT"
"""M6: a ``signal`` run ``{event_id, alert_id}`` (Signal Hub event) → ``frame_case`` (deterministic)."""
ROUTE_PROMOTE = "PROMOTE"
ROUTE_CONFIRM_ASSUMPTIONS = "CONFIRM_ASSUMPTIONS"
ROUTE_FOLLOWUP = "FOLLOWUP"
ROUTE_RESIMULATE = "RESIMULATE"
ROUTE_QUESTION = "QUESTION"
ROUTE_NEW_DISRUPTION = "NEW_DISRUPTION"
UNROUTED = "UNSUPPORTED"
"""A route value with no explicit edge: the router's DEFAULT_ROUTE edge is taken."""

CHAT_TEXT_KEY = "chat_text"


async def pre_route(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """START → chat→intake | action:apply / action:guide→compile_changeset | action:promote→promote_case |
    action:confirm_assumptions→confirm_assumptions | action:rollback→prepare_rollback |
    action:checklist_step→checklist_step | signal {signal_id}→prepare_replan (deterministic, M5) |
    signal {event_id, alert_id}→frame_case (deterministic frame from the Signal Hub event, M6) |
    DEFAULT_ROUTE→reject_run. Both signal shapes are told apart by the typed payload only."""
    envelope = input_dict(node_input)
    kind = envelope.get("kind")
    payload = envelope.get("payload") or {}
    if kind == "chat" and isinstance(payload.get("text"), str) and payload["text"].strip():
        ctx.state[CHAT_TEXT_KEY] = payload["text"]
        ctx.state[CASE_BRIEF_KEY] = await case_brief(current_run())
        yield routed(payload["text"], ROUTE_CHAT)
        return
    if kind == "action" and payload.get("type") in ("apply", "guide"):
        yield routed(
            {"option_id": str(payload.get("option_id")), "plan_version": int(payload.get("plan_version") or 0),
             "mode": str(payload["type"]), "origin": "chat"},
            ROUTE_APPLY if payload.get("type") == "apply" else ROUTE_GUIDE,
        )
        return
    if kind == "action" and payload.get("type") == "rollback" and payload.get("change_set_id"):
        yield routed({"change_set_id": str(payload["change_set_id"])}, ROUTE_ROLLBACK)
        return
    if kind == "action" and payload.get("type") == "checklist_step":
        yield routed({"checklist_id": str(payload.get("checklist_id") or ""), "seq": payload.get("seq"),
                      "status": str(payload.get("status") or "")}, ROUTE_CHECKLIST_STEP)
        return
    if kind == "signal" and isinstance(payload.get("signal_id"), str) and payload["signal_id"]:
        yield routed({"signal_id": payload["signal_id"]}, ROUTE_SIGNAL)
        return
    alert_ok = payload.get("alert_id") is None or is_uuid(payload.get("alert_id"))
    if kind == "signal" and is_uuid(payload.get("event_id")) and alert_ok:
        yield routed({"signal": {"event_id": payload["event_id"], "alert_id": payload.get("alert_id")}},
                     ROUTE_SIGNAL_EVENT)
        return
    if kind == "action" and payload.get("type") == "promote":
        yield routed({"plan_version": payload.get("plan_version")}, ROUTE_PROMOTE)
        return
    if kind == "action" and payload.get("type") == "confirm_assumptions":
        keys = [str(k) for k in payload.get("assumption_keys") or [] if isinstance(k, str) and k][:50]
        yield routed(
            {"option_id": str(payload.get("option_id")), "plan_version": int(payload.get("plan_version") or 0),
             "assumption_keys": keys},
            ROUTE_CONFIRM_ASSUMPTIONS,
        )
        return
    yield routed({"kind": kind, "type": payload.get("type")}, UNROUTED)


async def case_brief(run: RunContext) -> dict[str, Any]:
    """What the intake must know to tell a follow-up from a new disruption: whether the case
    already has a plan and, if so, the disruption it was planned for (typed values only)."""
    has_plan = await run.deps.repo.latest_plan(tenant=run.tenant, case_id=run.case_id) is not None
    brief: dict[str, Any] = {"has_plan": has_plan}
    if has_plan:
        record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
        if record is not None:
            brief["current_event_type"] = record.scenario.event_type
            brief["current_event"] = record.scenario.description[:300]
    return brief


async def modifies_current_case(run: RunContext, disruption: DisruptionExtract) -> bool:
    """A "new disruption" that names nothing (no entity mention) on a case that already has a plan,
    of the same event family as the case's scenario (or unclassified ``OTHER``), can only be read
    against the current case ("what if the delay doubles?"): it is a follow-up, not a new case
    framing that would drop the current scope. Anything naming a supplier, port, chokepoint,
    region, material, … or another kind of event stays a new disruption."""
    if disruption.entity_mentions:
        return False
    if await run.deps.repo.latest_plan(tenant=run.tenant, case_id=run.case_id) is None:
        return False
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        return False
    family = disruption.taxonomy_family
    return family == "OTHER" or family == record.scenario.event_family


async def route_intake(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """QUESTION → answer path · FOLLOWUP on a case with a plan → followup_delta (else the answer
    path) · NEW_DISRUPTION → frame_case (unless it only modifies the current case's disruption:
    :func:`modifies_current_case` → followup_delta) · DEFAULT_ROUTE → smalltalk."""
    intake = IntakeResult.model_validate(fill_nullable(IntakeResult, input_dict(node_input)))
    run = current_run()
    if intake.kind == "NEW_DISRUPTION" and intake.disruption is not None \
            and await modifies_current_case(run, intake.disruption):
        log.info("new-disruption intake names nothing new; routed as a follow-up of the current plan",
                 case_id=run.case_id, event_type=intake.disruption.event_type)
        question = ctx.state.get(CHAT_TEXT_KEY) or intake.summary
        yield routed({"question": str(question), "summary": intake.summary}, ROUTE_FOLLOWUP)
        return
    if intake.kind == "FOLLOWUP":
        if await run.deps.repo.latest_plan(tenant=run.tenant, case_id=run.case_id) is not None:
            question = ctx.state.get(CHAT_TEXT_KEY) or intake.summary
            yield routed({"question": str(question), "summary": intake.summary}, ROUTE_FOLLOWUP)
            return
    if intake.kind in ("QUESTION", "FOLLOWUP"):
        question = ctx.state.get(CHAT_TEXT_KEY) or intake.summary
        yield routed(str(question), ROUTE_QUESTION)
        return
    if intake.kind == "NEW_DISRUPTION" and intake.disruption is not None:
        yield routed({"intake": intake.model_dump(mode="json"), "edits": None}, ROUTE_NEW_DISRUPTION)
        return
    yield routed(intake.model_dump(mode="json"), UNROUTED)


SMALLTALK_REPLY = (
    "I help you plan around supply-chain disruptions. You can tell me about one, for example "
    "*\"Meghna Trims says trims for our puffer jackets are delayed by a week\"*, ask a what-if such as "
    "*\"What if the Bangladesh POs slip by two weeks?\"*, or ask about your data, e.g. "
    "*\"Which open purchase orders land in UK1 next month?\"*"
)


async def compose_smalltalk(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    yield await say(run, "smalltalk", SMALLTALK_REPLY)
    yield {"reply": "smalltalk"}


async def reject_run(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input)
    what = data.get("type") or data.get("kind") or "request"
    yield await say(run, "rejected", f"Sorry, the {what} action is not available in this version of Keelson.")
    yield {"rejected": str(what)}


async def promote_case(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """What-if → actual (stand-in until ``framing.promote_case`` lands). With a plan on the case
    the options are re-simulated as an actual case (route ``RESIMULATE`` → impact → simulate →
    recommend → compose, so Apply is offered on a fresh plan version); otherwise the default
    route ends the turn."""
    run = current_run()
    repo = run.deps.repo
    await repo.set_case_mode(tenant=run.tenant, case_id=run.case_id, mode="actual")
    record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is not None and record.scenario.mode != "actual":
        await repo.save_scenario(
            tenant=run.tenant, case_id=run.case_id, scenario=record.scenario.model_copy(update={"mode": "actual"})
        )
    await run.deps.interrupts.supersede_open(tenant=run.tenant, case_id=run.case_id, reason="case promoted")
    run.case_mode = "actual"
    yield case_event(mode="actual", status="open", title=run.case_title)
    if record is not None and await run.deps.repo.latest_plan(tenant=run.tenant, case_id=run.case_id) is not None:
        from .case_state import ensure_planning_state  # noqa: PLC0415 - import cycle

        try:
            await ensure_planning_state(ctx, run)
        except LookupError:
            pass
        else:
            yield await say(run, "promoted", "This case now plans for real. I am re-checking the options as an "
                            "actual case before anything can be applied.")
            yield routed({"promoted": True, "scenario_version": None}, ROUTE_RESIMULATE)
            return
    yield await say(
        run,
        "promoted",
        "This case now plans for real. Ask me to re-run the plan so the options are confirmed against the "
        "actual scenario before applying anything.",
    )
    yield {"promoted": True}


CLOSE_MESSAGES = {
    "cancelled": "Understood — I have closed this case without changing anything.",
    "rejected": "The change set was rejected; nothing was applied.",
    "revise": "The change set was sent back for revision; nothing was applied. Tell me what to change.",
}


async def close_case(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input) if node_input is not None else {}
    reason = str(data.get("reason") or "cancelled")
    message = str(data.get("message") or CLOSE_MESSAGES.get(reason, "Nothing was changed."))
    if reason == "cancelled" and not data.get("message"):
        yield await case_status(run, "closed")  # the user cancelled the case at the approach card
    yield await say(run, f"close_{reason}", message)
    yield Event(output={"closed": reason})
