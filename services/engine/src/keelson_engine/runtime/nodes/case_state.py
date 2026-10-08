"""Planning state of a case across runs (M4).

One ADK session per run: ``ctx.state`` starts empty in a later run of the case (a case action
``promote`` / ``confirm_assumptions`` or a follow-up). :func:`ensure_planning_state` restores the
keys ``impact`` → ``simulate`` → ``compose`` read (problem data, snapshot, data source, findings)
from the case's evidence rows, so every re-entry path into ``impact`` works without re-framing.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents.context import Context

from ..context import RunContext
from .framing import (
    DATA_SOURCE_STATE_KEY,
    EVIDENCE_STATE_KEY,
    PROBLEM_STATE_KEY,
    SNAPSHOT_LABEL_STATE_KEY,
    SNAPSHOT_STATE_KEY,
)
from .investigate import FINDINGS_TOOL, PLAN_STATE_KEY

__all__ = ["FINDINGS_STATE_KEY", "PROBLEM_TOOL", "ensure_planning_state"]

PROBLEM_TOOL = "problem_data"
"""Evidence ``tool`` of the ProblemData row written by ``frame_case``."""
FINDINGS_STATE_KEY = "findings"
"""``ctx.state``: findings handed to the Strategist and the evidence pack (``planning``)."""


async def ensure_planning_state(ctx: Context, run: RunContext) -> None:
    """Fill the planning keys of ``ctx.state`` from the case's latest rows when they are missing.

    Raises ``LookupError`` when the case has no problem data (it was never framed)."""
    if not ctx.state.get(PROBLEM_STATE_KEY):
        row = await run.deps.repo.latest_evidence(tenant=run.tenant, case_id=run.case_id, tool=PROBLEM_TOOL)
        if row is None:
            raise LookupError("the case has no problem data yet")
        args: dict[str, Any] = dict(row.get("args") or {})
        ctx.state[PROBLEM_STATE_KEY] = str(row["id"])
        ctx.state[SNAPSHOT_STATE_KEY] = args.get("snapshot_id")
        ctx.state[DATA_SOURCE_STATE_KEY] = args.get("source") or "mcp"
        ctx.state[SNAPSHOT_LABEL_STATE_KEY] = args.get("snapshot_label")
    if ctx.state.get(EVIDENCE_STATE_KEY) is None:
        ctx.state[EVIDENCE_STATE_KEY] = []
    if ctx.state.get(FINDINGS_STATE_KEY) is None:
        row = await run.deps.repo.latest_evidence(tenant=run.tenant, case_id=run.case_id, tool=FINDINGS_TOOL)
        result = (row or {}).get("result") or {}
        by_task = dict(result.get("findings_by_task") or {})
        plan = result.get("plan") or {}
        order = [t.get("id") for t in plan.get("tasks") or []] if isinstance(plan, dict) else []
        ctx.state[FINDINGS_STATE_KEY] = [by_task[t] for t in order if t in by_task] or list(by_task.values())
        if plan and not ctx.state.get(PLAN_STATE_KEY):
            ctx.state[PLAN_STATE_KEY] = plan
