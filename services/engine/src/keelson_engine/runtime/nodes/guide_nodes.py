"""Guide mode nodes (``docs/contracts/95-m5-remediation.md`` §2).

``checklist_step`` — case action ``checklist_step {checklist_id, seq, status: done|skipped}``: a
person ticks a guide step by hand. The step is updated under the checklist row lock (audited as
``checklist.step.<status>`` by ``user:<username>``), the ``remediation_checklist@1`` block is
re-emitted, and a completed checklist (every step verified or skipped) says "Guide completed". A
step already ``verified`` by read-back is never downgraded.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents.context import Context
from keelson_contracts.runs import CHECKLIST_STEP_STATUSES

from ..context import current_run
from ..guide import checklist_props, completion_text, finish_if_completed, is_completed
from .common import ENGINE_COMMIT, block_event, input_dict, say

__all__ = ["checklist_step"]


async def checklist_step(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input)
    checklist_id = str(data.get("checklist_id") or "")
    status = str(data.get("status") or "")
    try:
        seq = int(data.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    store = run.deps.checklists
    if store is None:
        yield await say(run, "checklist_unavailable", "Guided checklists are not available in this installation.")
        yield {"updated": False}
        return
    checklist = await store.get_checklist(tenant=run.tenant, checklist_id=checklist_id) if checklist_id else None
    if checklist is None or str(checklist.get("case_id")) != run.case_id:
        yield await say(run, "checklist_unknown", "That checklist is not part of this case.")
        yield {"updated": False}
        return
    step = next((s for s in checklist.get("steps") or [] if int(s.get("seq") or 0) == seq), None)
    if step is None or status not in CHECKLIST_STEP_STATUSES:
        yield await say(run, "checklist_step_unknown", "That step is not on the checklist.")
        yield {"updated": False}
        return
    if str(step.get("status")) == "verified":
        yield await say(run, "checklist_step_verified",
                        f"Step {seq} is already verified by reading it back, so it stays verified.")
        yield {"updated": False}
        return
    if str(checklist.get("status") or "open") != "open":
        yield await say(run, "checklist_closed", "This checklist is already closed.")
        yield {"updated": False}
        return
    actor = f"user:{run.actor.username}"
    updated = checklist
    if str(step.get("status")) != status:
        updated = await store.set_step(tenant=run.tenant, checklist_id=checklist_id, seq=seq, status=status,
                                       actor=actor) or checklist
        await run.deps.repo.admin_audit(
            tenant=run.tenant, actor=actor, action=f"checklist.step.{status}", target=f"{checklist_id}#{seq}",
            before={"status": step.get("status")}, after={"status": status},
        )
    meta = updated.get("meta") or {}
    vendor = str(meta.get("vendor") or updated.get("connector"))
    yield await block_event(
        run, "remediation_checklist", checklist_props(updated), title=f"Guided steps in {vendor}",
        prov=str(meta.get("prov") or "inference"), engine=ENGINE_COMMIT, key=(checklist_id,),
    )
    if is_completed(updated):
        # the platform may already have closed it in the same update (set_checklist_step completes it)
        await finish_if_completed(run.deps, tenant=run.tenant, checklist=updated, announce=False)
        yield await say(run, "guide_completed", completion_text(updated))
    yield {"updated": True, "checklist_id": checklist_id, "seq": seq, "status": status}
