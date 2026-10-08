"""Stand-ins for the framing-owned M4 nodes, used by ``build_workflow`` only while the framing
modules do not provide them (``nodes/framing.py``, ``nodes/refine.py``, ``nodes/followup.py``;
see ``nodes/M4_NODES.md``). Each is deliberately conservative:

* ``refine_scenario`` — applies **nothing** (no scenario update without the real refine rules);
  passes the verified findings on to ``impact``.
* ``confirm_assumptions`` — confirms the named assumptions (``DisruptionScenario.confirm_assumptions``)
  and re-runs impact + simulate (route ``RESIMULATE``); unknown keys only → ``DEFAULT_ROUTE``.
* ``followup_delta`` — no classifier: the follow-up is answered on the question path
  (``DEFAULT_ROUTE`` → answer investigator), as in M1–M3.
* ``explain`` / ``prepare_resimulate`` / ``prepare_reinvestigate`` — minimal pass-throughs.
* ``end_turn`` — quiet terminal node for routers whose default needs no reply.

Route contract (``workflow.py``, ``nodes/M4_NODES.md``): ``promote_case`` / ``confirm_assumptions`` /
``explain`` / ``prepare_resimulate`` → ``RESIMULATE`` (→ impact) | DEFAULT (``DONE`` → end_turn);
``prepare_reinvestigate`` → ``REINVESTIGATE`` (→ investigate) | DEFAULT (→ end_turn);
``followup_delta`` → ``EXPLAIN`` | ``RESIMULATE`` | ``REINVESTIGATE`` | DEFAULT (→ answer investigator).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from google.adk.agents.context import Context

from ..context import current_run
from .case_state import ensure_planning_state
from .common import input_dict, routed, say
from .routing import CHAT_TEXT_KEY, UNROUTED

log = structlog.get_logger(__name__)

__all__ = [
    "ROUTE_RESIMULATE",
    "confirm_assumptions",
    "end_turn",
    "explain",
    "followup_delta",
    "prepare_reinvestigate",
    "prepare_resimulate",
    "refine_scenario",
]

ROUTE_RESIMULATE = "RESIMULATE"
ROUTE_REINVESTIGATE = "REINVESTIGATE"


async def end_turn(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    yield {"done": True}


async def refine_scenario(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    data = input_dict(node_input)
    log.info("refine_scenario stand-in: no scenario updates applied",
             updates=sum(len(f.get("scenario_updates") or []) for f in data.get("findings") or []))
    yield {"scenario_version": data.get("scenario_version"), "mode": data.get("mode"),
           "findings": list(data.get("findings") or []), "applied_updates": []}


async def confirm_assumptions(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input)
    keys = [str(k) for k in data.get("assumption_keys") or []]
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    known = {a.key for a in record.scenario.assumptions} if record is not None else set()
    wanted = [k for k in keys if k in known]
    if record is None or not wanted:
        yield await say(run, "confirm_none", "There was nothing to confirm on this case; nothing was changed.")
        yield routed({"confirmed": []}, UNROUTED)
        return
    version = await run.deps.repo.save_scenario(
        tenant=run.tenant, case_id=run.case_id, scenario=record.scenario.confirm_assumptions(wanted)
    )
    await ensure_planning_state(ctx, run)
    yield routed({"scenario_version": version, "confirmed": wanted}, ROUTE_RESIMULATE)


async def followup_delta(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    question = ctx.state.get(CHAT_TEXT_KEY) or str(node_input or "")
    yield routed(str(question), UNROUTED)


async def explain(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    yield {"explained": False}


async def prepare_resimulate(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    await ensure_planning_state(ctx, run)
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    yield routed({"scenario_version": record.version if record else None}, ROUTE_RESIMULATE)


async def prepare_reinvestigate(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input) if node_input is not None else {}
    await ensure_planning_state(ctx, run)
    yield routed({"task_ids": list(data.get("task_ids") or data.get("dirty_task_ids") or []),
                  "notes": data.get("notes") or {}}, ROUTE_REINVESTIGATE)
