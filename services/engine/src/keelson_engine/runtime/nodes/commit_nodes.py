"""Commit path: ``compile_changeset`` → ``dry_run`` → ``approval_gate`` → ``approval_check`` →
``execute`` → ``reconcile`` (+ M5 ``prepare_rollback``). Writes happen only here (deterministic
function nodes), with separate write principals (preparer for the dry-run; the approver's
delegated token or the autocommit service account for the apply).

Exactly-once apply: ``execute`` re-runs on resume/crash-retry, so before calling
``apply_change_set`` it consults the engine change-set row; an *in-doubt* row (``applying``)
is read back from the target system first and only re-applied if it never applied it (the same
idempotency key is used either way).

One apply per plan option: every ``apply`` action compiles *new* engine change sets (new
idempotency keys), so ``compile_changeset`` first looks at the case's earlier change sets for
the same ``(plan_version, option_id)``: an applied one refuses the action, an in-doubt one is
read back, and one still waiting for approval is superseded (its approval card can no longer be
used). ``approval_check`` and ``execute`` re-check this before anything is applied.

M5 (``docs/contracts/95-m5-remediation.md``):

* **Guide mode / mixed plans** (§2): ``compile_changeset`` partitions the option's actions per
  connector into apply lines and guide lines (:func:`~keelson_engine.runtime.remediation.partition_actions`
  over the capability table); guide lines become ``remediation_checklist@1`` blocks
  (:mod:`keelson_engine.runtime.guide`); a pure Guide (or an option without any apply line) ends
  there (``GUIDED``).
* **Bundles** (§5): one change set per connector, in capability-table order; each member is
  dry-run with its own hash and idempotency key; if any dry-run fails nothing is applied; one
  approval binds the bundle hash; ``execute`` applies the members in order.
* **Policy** (§1): ``approval_gate`` evaluates the tenant's CEL rules (``runtime/policy.py``)
  over the bundle — refuse, auto (autocommit principal, receipt ``approved_by =
  policy:<rule>@<version>``) or a human quorum (``interrupt.quorum_spec``). ``approval_check``
  re-counts the deciders with :func:`keelson_contracts.policy.quorum_assign`; the executor is the
  member counted as the approver, the other members go to the receipt's ``co_approved_by``.
* **Rollback** (§3): case action ``rollback`` → ``prepare_rollback`` (always human; the original
  quorum when it needed more) → ``approval_gate`` → ``approval_check`` → ``execute``
  (``rollback_change_set`` with a fresh receipt) → ``reconcile`` (read-back; plan ``rolled_back``,
  recovery plan v+1 recommends the baseline).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import structlog
from google.adk.agents.context import Context
from google.adk.events.request_input import RequestInput
from keelson_contracts.agui import approval_response_schema
from keelson_contracts.blocks import (
    ApprovalBundleMember,
    ApprovalCardProps,
    ChangeTotals,
    CommitTrackerProps,
    ConnectState,
    RollbackConflict,
    TrackerAction,
)
from keelson_contracts.changeset import ChangeSet, bundle_hash
from keelson_contracts.common import Provenance
from keelson_contracts.policy import QuorumCandidate, QuorumView, Requirement, quorum_assign
from keelson_contracts.receipts import approved_by_user
from keelson_contracts.runs import Actor, Decider

from ...public_errors import report_exception, user_message
from ..candidates import CapabilityTable, capability_table
from ..commit import (
    AllocationExpander,
    GroupTooLargeError,
    apply_with_retry,
    approval_id_for,
    expand_actions,
    mint_receipt,
    partition_groups,
    stored_items,
    token_subject,
)
from ..commit import (
    compile_changeset as compile_items,
)
from ..commit import (
    dry_run as run_dry_run,
)
from ..commit import (
    reconcile as run_reconcile,
)
from ..context import RunContext, current_run
from ..deps import PLATFORM_AUTH_KIND
from ..errors import HashMismatchError, McpToolError, McpTransportError, StaleSessionStateError
from ..guide import build_checklist, checklist_id_for, checklist_props
from ..mcp_client import UserPrincipal, get_gateway
from ..policy import PolicyEvaluationError, decide_approval, fallback_requirement, requirement_text
from ..problem_scn import GROUP_ALLOC_PREFIX, expand_allocation_group
from ..quarantine import is_untrusted_text_row
from ..remediation import partition_actions, plan_dependencies
from .per_op import (
    PER_OP,
    PerOpRefused,
    apply_per_op,
    dry_run_per_op,
    guided_lines,
    is_per_op,
    prior_ledger_summary,
    verify_per_op,
)
from .common import (
    ENGINE_COMMIT,
    block_event,
    case_status,
    input_dict,
    progress_event,
    routed,
    say,
)
from .framing import PROBLEM_STATE_KEY, load_problem
from .planning import DO_NOTHING_ID, plan_diff_props

_log = structlog.get_logger(__name__)

__all__ = [
    "APPROVAL_RESUME_SCHEMA",
    "ROUTE_GUIDED",
    "approval_check",
    "approval_gate",
    "case_origin",
    "compile_changeset",
    "dry_run",
    "execute",
    "prepare_rollback",
    "reconcile",
    "rollback_interrupt_id",
]

CHANGE_SET_KEY = "change_set_id"
BUNDLE_KEY = "bundle"
APPROVAL_KEY = "approval"
INITIATOR_KEY = "initiator"
ORIGIN_KEY = "run_origin"
COMMIT_MODE_KEY = "commit_mode"
ROLLBACK_KEY = "rollback"
CHECKLISTS_KEY = "checklists"

ROUTE_GUIDED = "GUIDED"
ROUTE_ROLLBACK = "ROLLBACK"

COMMITTED_STATES = frozenset({"applying", "applied", "verified", "partial"})
"""Engine change-set states in which the target system may hold (or holds) the changes."""
OPEN_STATES = frozenset({"draft", "validated"})
"""Compiled or validated, not applied: superseded when the same option is applied again."""
UNDOABLE = frozenset({"applied", "verified"})

APPROVAL_RESUME_SCHEMA = {
    "type": "object",
    "properties": {"decision": {"type": "string"}, "interrupt_id": {"type": "string"}},
    "required": ["decision", "interrupt_id"],
}


def _refuse(message: str) -> Any:
    return routed({"reason": "refused", "message": message}, "REFUSED")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _load_change_set(run: RunContext, change_set_id: str) -> tuple[dict[str, Any], ChangeSet]:
    row = await run.deps.repo.get_change_set(tenant=run.tenant, change_set_id=change_set_id)
    if row is None:
        raise LookupError(f"change set {change_set_id} not found")
    change_set = compile_items(
        tenant=run.tenant,
        change_set_id=str(row["id"]),
        connector=str(row.get("connector") or run.spec.connector),
        actions=list(row["items"] or []),
        revision=int(row.get("revision") or 1),
    )
    if change_set.hash != row.get("hash"):
        raise HashMismatchError(f"change set {change_set_id}: stored hash does not match its items")
    return row, change_set


def _bundle(ctx: Context, fallback_id: str | None = None) -> dict[str, Any]:
    bundle = dict(ctx.state.get(BUNDLE_KEY) or {})
    if not bundle.get("change_set_ids"):
        cs_id = str(ctx.state.get(CHANGE_SET_KEY) or fallback_id or "")
        bundle = {"id": cs_id, "change_set_ids": [cs_id] if cs_id else [], "systems": {}}
    return bundle


def _system(run: RunContext, bundle: Mapping[str, Any], connector: str) -> str:
    systems = bundle.get("systems") or {}
    return str(systems.get(connector) or (run.spec.vendor_name if connector == run.spec.connector else connector))


def _ext_base_url(run: RunContext) -> str:
    from ..settings import engine_settings  # noqa: PLC0415

    url = str(engine_settings().merxa_mcp_url)
    return url[: -len("/mcp")] if url.endswith("/mcp") else url.rstrip("/")


async def _read_back(run: RunContext, change_set: ChangeSet, ext_id: str) -> dict[str, Any]:
    async with get_gateway().session(tenant=run.tenant, connector=change_set.connector, principal="reader") as s:
        return await s.call("get_change_set", {"change_set_id": ext_id})


async def _record_applied(run: RunContext, change_set: ChangeSet, ext: Mapping[str, Any],
                          extra: Mapping[str, Any] | None = None) -> int:
    """The target system reports the change set applied: record it on the engine row and the ledger."""
    applied = sum(1 for i in ext.get("items") or [] if i.get("status") in ("applied", "ok"))
    await run.deps.repo.update_change_set(
        tenant=run.tenant, change_set_id=change_set.id, status="applied", ext_code=ext.get("code"),
        result={**dict(extra or {}), "status": "applied", "applied": applied, "recovered": True},
    )
    for item in change_set.items:
        await run.deps.repo.ledger_mark(tenant=run.tenant, change_set_id=change_set.id, line_seq=item.seq,
                                        state="succeeded", ext_ref=ext.get("code"))
    await _mark_stale(run, change_set.connector, "change_set.applied")
    return applied


async def _siblings(run: RunContext, row: Mapping[str, Any], exclude: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Other engine change sets of the same plan option (bundle members excluded)."""
    rows = await run.deps.repo.list_change_sets(tenant=run.tenant, case_id=run.case_id)
    skip = {str(row["id"]), *map(str, exclude)}
    return [
        r for r in rows
        if str(r["id"]) not in skip
        and int(r.get("plan_version") or 0) == int(row.get("plan_version") or 0)
        and str(r.get("option_id")) == str(row.get("option_id"))
    ]


async def _settle_previous(run: RunContext, plan_version: int, option_id: str) -> str | None:
    """Resolve earlier change sets of this plan option before compiling a new one.

    Returns a refusal message when the option is (or may be) applied already."""
    repo = run.deps.repo
    vendor = run.spec.vendor_name
    superseded = False
    for prev in await repo.list_change_sets(tenant=run.tenant, case_id=run.case_id):
        if int(prev.get("plan_version") or 0) != plan_version or str(prev.get("option_id")) != option_id:
            continue
        status = str(prev.get("status") or "")
        code = prev.get("ext_code") or "a change batch"
        if status in ("applied", "verified"):
            return f"These changes were already applied in {vendor} as {code}; nothing new was applied."
        if status == "partial":
            return (f"Some of these changes were (or may have been) applied in {vendor} one by one; the rest are guided "
                    "steps. Nothing new was applied.")
        if status == "applying" and is_per_op(prev):
            # M7 per-op: the ledger of that apply decides (its run resumes or a person checks the
            # in-doubt lines); never a second apply on top of it
            return (f"An earlier apply of these changes in {vendor} has not finished (some calls may be in doubt); "
                    "nothing new was applied.")
        if status == "applying":
            row, change_set = await _load_change_set(run, str(prev["id"]))
            ext_id = row.get("ext_change_set_id")
            ext = await _read_back(run, change_set, str(ext_id)) if ext_id else {}
            ext_status = str(ext.get("status") or "")
            if ext_status == "applied":
                await _record_applied(run, change_set, ext)
                return (
                    f"These changes were already applied in {vendor} as {ext.get('code') or code}; "
                    "nothing new was applied."
                )
            if ext_status == "applying":
                return f"{vendor} is still applying {code}; wait for it to finish before applying again."
            await repo.update_change_set(
                tenant=run.tenant, change_set_id=change_set.id, status="failed",
                result={"status": "not_applied", "ext_status": ext_status or None,
                        "reason": "read back after an interrupted apply"},
            )
            continue
        if status in OPEN_STATES:
            await repo.update_change_set(tenant=run.tenant, change_set_id=str(prev["id"]), status="superseded")
            superseded = superseded or status == "validated"
    if superseded:
        await run.deps.interrupts.supersede_open(
            tenant=run.tenant, case_id=run.case_id, reason=f"plan v{plan_version} {option_id} applied again"
        )
    return None


def _allocation_expander(run: RunContext, snapshot_id: Any) -> AllocationExpander | None:
    """Reads the store allocations behind a cluster allocation from the plan's pinned snapshot."""
    if not snapshot_id or run.deps.scn is None:
        return None

    async def expand(group_code: str) -> list[Any]:
        async with run.deps.scn.reader(run.tenant) as reader:
            return await expand_allocation_group(
                reader, tenant=run.tenant, snapshot_id=str(snapshot_id), group_code=group_code
            )

    return expand


async def _mark_stale(run: RunContext, connector: str, reason: str) -> None:
    """The engine just changed the source system: its snapshot is stale (best effort; the
    webhook / poll path catches up anyway)."""
    if run.deps.scn is None:
        return
    from .framing import SNAPSHOT_ERRORS  # noqa: PLC0415

    try:
        await run.deps.scn.mark_stale(tenant=run.tenant, connector=connector, reason=reason)
    except SNAPSHOT_ERRORS as exc:
        _log.warning("could not mark the snapshot stale", connector=connector, error=type(exc).__name__)


def run_origin(run: RunContext) -> str:
    """``chat`` for a person's run (chat / case action / resume), ``webhook``/``signal`` for signal runs."""
    if run.kind == "signal" or str(run.payload.get("signal_id") or ""):
        return "webhook"
    return "chat"


def effective_origin(requested: Any, run: RunContext) -> str:
    """The policy origin of a commit: the run's own origin (:func:`run_origin`), never relaxed by
    a node input. A node input may only make it stricter (``prepare_replan`` passes ``webhook``);
    ``chat`` — the only origin that can be auto-approved — comes from the run envelope alone."""
    base = run_origin(run)
    if base != "chat":
        return base
    return str(requested) if requested in ("signal", "webhook") else "chat"


def case_origin(origin: str, scenario: Any) -> str:
    """M6 (``100`` §5, §6): a plan for a scenario framed from a Signal Hub event (an alert or an
    alert rule's drafted case) is a ``signal``-origin plan even when a person clicks Apply later:
    an external signal never leads to an auto-approval (policy rule 2 / ``human-untrusted``)."""
    from .framing import is_signal_scenario  # noqa: PLC0415 - import cycle

    if origin == "chat" and is_signal_scenario(scenario):
        return "signal"
    return origin


async def _tainted(run: RunContext, scenario: Any) -> bool:
    """The plan rests on values taken from untrusted text (quarantined extractions or raw rows),
    even when a person confirmed them: never auto-approved (plan §3)."""
    ids = sorted({str(e) for a in getattr(scenario, "assumptions", []) for e in getattr(a, "source_evidence_ids", [])})
    if not ids:
        return False
    rows = await run.deps.repo.get_evidence(tenant=run.tenant, ids=ids)
    return any(is_untrusted_text_row(r) for r in rows)


# ─────────────────────────────── compile ───────────────────────────────


def _guide_fingerprint(actions: Sequence[Mapping[str, Any]]) -> str:
    """Identity of a set of guided lines (op, target, payload, group; order-free)."""
    canon = sorted(
        json.dumps({k: a.get(k) for k in ("op", "target_ref", "payload", "group")}, sort_keys=True, default=str)
        for a in actions
    )
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()


async def _open_checklist_like(run: RunContext, *, connector: str, plan_version: int, option_id: str,
                               fingerprint: str) -> dict[str, Any] | None:
    """The case's open checklist for the same plan version, option, connector and guided lines."""
    try:
        rows = await run.deps.checklists.list_open_checklists(tenant=run.tenant)  # type: ignore[union-attr]
    except Exception as exc:  # reuse is an optimisation; a new checklist is still correct
        _log.debug("open checklists unavailable", error=type(exc).__name__)
        return None
    for row in rows:
        if (str(row.get("case_id")) == run.case_id and str(row.get("connector")) == connector
                and int(row.get("plan_version") or 0) == plan_version and str(row.get("option_id")) == option_id
                and (row.get("meta") or {}).get("lines_fingerprint") == fingerprint):
            return dict(row)
    return None


async def _create_guides(
    ctx: Context, run: RunContext, table: CapabilityTable, guide: Mapping[str, list[dict[str, Any]]],
    *, plan_version: int, option_id: str,
) -> list[Any]:
    """Build, persist and announce one checklist per connector; returns the events."""
    events: list[Any] = []
    created: list[str] = list(ctx.state.get(CHECKLISTS_KEY) or [])
    for connector, actions in guide.items():
        caps = table.get(connector)
        vendor = caps.vendor if caps is not None else connector
        tools = set(caps.tools) if caps is not None else set()
        try:
            info = await run.deps.connectors.get_connector(tenant=run.tenant, slug=connector)
            config = dict(getattr(info, "config", {}) or {})
        except Exception:  # a guide never fails over cosmetic config
            config = {}
        checklist_id = checklist_id_for(tenant=run.tenant, case_id=run.case_id, plan_version=plan_version,
                                        option_id=option_id, connector=connector, nonce=run.invocation_id)
        existing = await run.deps.checklists.get_checklist(tenant=run.tenant, checklist_id=checklist_id)  # type: ignore[union-attr]
        fingerprint = _guide_fingerprint(actions)
        if existing is None:
            # an apply retried after a rejection / failed quorum (or Guide clicked twice) guides the
            # same lines again: keep the open checklist instead of a duplicate the same entry in
            # the target system would verify twice
            existing = await _open_checklist_like(run, connector=connector, plan_version=plan_version,
                                                  option_id=option_id, fingerprint=fingerprint)
            if existing is not None:
                checklist_id = str(existing["id"])
        if existing is None:
            built = await build_checklist(
                run.deps, tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector=connector,
                vendor=vendor, actions=actions, checklist_id=checklist_id, plan_version=plan_version,
                option_id=option_id, tools=tools, config=config, sop=run.spec.remediation.guide_sop_snippets,
                kb_ids=run.spec.investigation.kb_ids,
            )
            meta = {**built.meta, "lines_fingerprint": fingerprint}
            await run.deps.checklists.create_checklist(  # type: ignore[union-attr]
                tenant=run.tenant, case_id=run.case_id, checklist_id=checklist_id, connector=connector,
                plan_version=plan_version, option_id=option_id, steps=built.steps, meta=meta,
            )
            existing = {"id": checklist_id, "case_id": run.case_id, "connector": connector, "steps": built.steps,
                        "meta": meta, "status": "open"}
        if checklist_id not in created:
            created.append(checklist_id)
        meta = existing.get("meta") or {}
        events.append(await block_event(
            run, "remediation_checklist", checklist_props(existing), title=f"Guided steps in {vendor}",
            prov=str(meta.get("prov") or "inference"), engine=ENGINE_COMMIT,
            key=(checklist_id,),
        ))
    ctx.state[CHECKLISTS_KEY] = created
    return events


async def compile_changeset(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """Input ``{option_id, plan_version, mode: apply|guide, origin?}`` (``pre_route`` / ``prepare_replan``).

    ``COMPILED`` → dry_run with the bundle in ``ctx.state["bundle"]``; ``GUIDED`` → end (only guide
    lines); ``REFUSED`` (default route) → close_case."""
    run = current_run()
    data = input_dict(node_input)
    mode = "guide" if data.get("mode") == "guide" else "apply"
    repo = run.deps.repo
    plan = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
    record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if plan is None or record is None:
        yield _refuse("There is no plan to apply yet.")
        return
    if int(plan["version"]) != int(data.get("plan_version") or 0):
        yield _refuse("The plan has changed since you chose this option; review the latest plan and try again.")
        return
    if record.scenario.mode == "what_if" or run.case_mode == "what_if":
        yield _refuse("This is a what-if case; promote it to an actual case before applying changes.")
        return
    pending = record.scenario.pending_confirmation
    if pending:  # docs/contracts/90 §5: inference-backed values need a human confirmation first
        yield _refuse("Confirm before applying: " + ", ".join(a.label for a in pending) + ".")
        return
    option = next((o for o in plan["options"] or [] if str(o["id"]) == str(data.get("option_id"))), None)
    if option is None:
        yield _refuse("That option is not part of the current plan.")
        return
    if not option.get("actions") or option.get("fallback"):
        yield _refuse("This option makes no changes, so there is nothing to apply.")
        return
    if mode == "apply" and option.get("guided_only"):
        yield _refuse(str(option.get("guided_reason") or "This option can only be followed as guided steps.")
                      + " Choose “Guide me” to get the steps.")
        return
    try:
        actions = await expand_actions(
            option["actions"],
            expander=_allocation_expander(run, (plan.get("kpis") or {}).get("snapshot_id")),
            is_group=lambda code: code.startswith(GROUP_ALLOC_PREFIX),
        )
    except (LookupError, ValueError) as exc:
        _log.warning("plan option could not be compiled", option_id=option["id"], error=str(exc)[:300])
        yield _refuse(
            f"The plan's allocation changes could not be matched to {run.spec.vendor_name} records any more; "
            "re-run the plan to work from the latest data."
        )
        return
    if not actions:
        yield _refuse("This option makes no changes, so there is nothing to apply.")
        return
    table = await capability_table(run.deps, tenant=run.tenant, connectors=run.spec.write_connectors(),
                                   vendor=run.spec.vendor_name)
    part = partition_actions(actions, table, guide_all=mode == "guide")
    limit = run.spec.planning.max_change_set_items
    for connector, lines in part.apply.items():
        system = (table.get(connector).vendor if table.get(connector) else connector)  # type: ignore[union-attr]
        try:
            chunks = partition_groups(lines, limit)
        except GroupTooLargeError as exc:
            _log.warning("plan option has an oversized atomic group", option_id=option["id"], error=str(exc)[:300])
            yield _refuse(
                f"This option has changes that must be applied together but are more than the "
                f"{limit} {system} accepts in one change batch; narrow the plan before applying it."
            )
            return
        if len(chunks) > 1:
            yield _refuse(
                f"This option needs {len(lines)} change lines, more than the {limit} {system} accepts in one "
                "change batch; narrow the plan before applying it."
            )
            return
    if part.guide and run.deps.checklists is None:
        if not part.apply_lines:
            yield _refuse("Guided steps are not available in this installation yet, so nothing was prepared.")
            return
        _log.warning("guide lines dropped: no checklist store", lines=part.guide_lines)
    plan_version = int(plan["version"])
    option_id = str(option["id"])
    if part.apply_lines:
        refusal = await _settle_previous(run, plan_version, option_id)
        if refusal is not None:
            yield _refuse(refusal)
            return
    ctx.state[INITIATOR_KEY] = run.actor.model_dump(mode="json")
    ctx.state[ORIGIN_KEY] = effective_origin(data.get("origin"), run)
    ctx.state[COMMIT_MODE_KEY] = "apply"
    if part.guide and run.deps.checklists is not None:
        for event in await _create_guides(ctx, run, table, part.guide, plan_version=plan_version, option_id=option_id):
            yield event
    if not part.apply_lines:
        steps = part.guide_lines
        systems = ", ".join(sorted({(table.get(c).vendor if table.get(c) else c) for c in part.guide}))  # type: ignore[union-attr]
        yield await say(
            run, "guide",
            f"I prepared {steps} guided step{'s' if steps != 1 else ''} in {systems}. Follow the links on the "
            "checklist; I tick each step as soon as I can read the change back, and you can mark a step done "
            "or skipped yourself.",
        )
        yield routed({"guided": True, "checklists": list(ctx.state.get(CHECKLISTS_KEY) or []),
                      "plan_version": plan_version, "option_id": option_id}, ROUTE_GUIDED)
        return
    ids: list[str] = []
    members: list[tuple[str, str]] = []
    systems: dict[str, str] = {}
    for caps in table.connectors:
        lines = part.apply.get(caps.connector)
        if not lines:
            continue
        for seq, item in enumerate(lines, start=1):
            item["seq"] = seq
        draft = compile_items(tenant=run.tenant, change_set_id="draft", connector=caps.connector, actions=lines)
        cs_id = await repo.create_change_set(
            tenant=run.tenant, case_id=run.case_id, plan_version=plan_version, option_id=option_id,
            connector=caps.connector, items=stored_items(draft, lines), hash=draft.hash,
        )
        ids.append(cs_id)
        members.append((caps.connector, draft.hash))
        systems[caps.connector] = caps.vendor
    b_hash = bundle_hash(members)
    ctx.state[CHANGE_SET_KEY] = ids[0]
    modes = {c: (PER_OP if (table.get(c) is not None and table.get(c).per_op) else "change_set")  # type: ignore[union-attr]
             for c, _ in members}
    ctx.state[BUNDLE_KEY] = {"id": ids[0] if len(ids) == 1 else f"bundle-{ids[0]}", "change_set_ids": ids,
                             "hash": b_hash, "systems": systems, "modes": modes}
    total = sum(len(v) for v in part.apply.values())
    where = " and ".join(systems[c] for c, _ in members)
    note = f" ({part.guide_lines} more as guided steps)" if part.guide_lines else ""
    yield progress_event(f"Prepared {total} change lines for {where}{note}", prov="simulated")
    yield routed(
        {"change_set_id": ids[0], "change_set_ids": ids, "hash": b_hash, "plan_version": plan_version,
         "option_id": option_id},
        "COMPILED",
    )


# ─────────────────────────────── dry run ───────────────────────────────


async def dry_run(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input)
    bundle = _bundle(ctx, str(data.get("change_set_id") or ""))
    repo = run.deps.repo
    problem = await load_problem(run, str(ctx.state.get(PROBLEM_STATE_KEY))) if ctx.state.get(PROBLEM_STATE_KEY) else None
    plan_version = int(data["plan_version"])
    loaded: list[tuple[dict[str, Any], ChangeSet]] = [await _load_change_set(run, cs_id) for cs_id in bundle["change_set_ids"]]
    problems: list[str] = []
    totals_all = {"lines": 0, "units_moved": 0, "value_cents": 0}
    multi = len(loaded) > 1
    for row, change_set in loaded:
        system = _system(run, bundle, change_set.connector)
        if is_per_op(row, bundle):
            outcome: dict[str, Any] = {}
            async for item in dry_run_per_op(run, row, change_set, system=system, plan_version=plan_version,
                                             option_id=str(data["option_id"]), problem=problem, multi=multi):
                if isinstance(item, dict) and "ok" in item and "totals" in item:
                    outcome = item
                else:
                    yield item
            if not outcome.get("ok"):
                problems.append(str(outcome.get("problem") or f"{system} could not prepare its changes."))
                continue
            for key in totals_all:
                totals_all[key] += int((outcome.get("totals") or {}).get(key, 0))
            continue
        try:
            async with get_gateway().session(tenant=run.tenant, connector=change_set.connector, principal="preparer") as s:
                result = await run_dry_run(
                    s,
                    change_set,
                    title=f"{run.case_title or 'Keelson case'} — plan v{row.get('plan_version') or plan_version}",
                    origin={"system": "Keelson", "case_ref": run.case_id, "plan_version": plan_version},
                )
        except HashMismatchError as exc:
            await repo.update_change_set(tenant=run.tenant, change_set_id=change_set.id, status="invalid")
            error_id = report_exception(_log, "dry run hash mismatch", exc, change_set_id=change_set.id)
            problems.append(user_message(
                error_id, f"The preview from {system} does not match the planned changes, so nothing was sent."
            ))
            continue
        except McpToolError as exc:
            if not multi:
                raise
            await repo.update_change_set(tenant=run.tenant, change_set_id=change_set.id, status="invalid")
            problems.append(f"{system} could not prepare its changes ({exc.code}).")
            continue
        await repo.update_change_set(
            tenant=run.tenant,
            change_set_id=change_set.id,
            status="validated" if result.ok else "invalid",
            ext_change_set_id=result.ext_change_set_id,
            ext_code=result.ext_code,
            preview={"preview_hash": result.preview_hash, "items": result.items, "totals": result.totals},
        )
        for key in totals_all:
            totals_all[key] += int(result.totals.get(key, len(change_set.items) if key == "lines" else 0))
        yield await block_event(
            run,
            "plan_diff",
            plan_diff_props(
                plan_version=plan_version,
                actions=list(row.get("items") or []) or [i.model_dump(mode="json") for i in change_set.items],
                vendor=system,
                problem=problem,
                validated=result.items,
                totals=result.totals or None,
            ),
            title=f"Changes in {system} ({result.ext_code or 'preview'})",
            prov=Provenance.SIMULATED,
            engine=ENGINE_COMMIT,
            key=("plan_diff", plan_version, str(data["option_id"]), *((change_set.connector,) if multi else ())),
            placement="canvas",
        )
        if not result.ok:
            errs = sorted({e for item in result.items for e in item.get("errors") or []})
            problems.append(f"{system} rejected the preview: " + ("; ".join(errs[:5]) or "validation failed") + ".")
    if problems:
        if multi:  # one failing member refuses the whole bundle: nothing is applied anywhere
            for row, change_set in loaded:
                await repo.update_change_set(tenant=run.tenant, change_set_id=change_set.id, status="invalid")
            yield _refuse("Nothing was applied: " + " ".join(problems))
        else:
            yield _refuse(problems[0])
        return
    yield routed({**data, "change_set_ids": list(bundle["change_set_ids"]),
                  "ext_change_set_id": loaded[0][0].get("ext_change_set_id") if not multi else None,
                  "totals": totals_all}, "VALIDATED")


# ─────────────────────────────── approval ───────────────────────────────


def approval_interrupt_id(change_set_id: str, revision: int) -> str:
    return f"approval-{change_set_id}-{revision}"


def rollback_interrupt_id(change_set_id: str, attempt: int) -> str:
    return f"approval-rb-{change_set_id}-{attempt}"


async def _connect_state(run: RunContext, connector: str, sub: str) -> ConnectState:
    try:
        info = await get_gateway().usable_info(tenant=run.tenant, connector=connector)
    except Exception:  # cosmetic: fall back to the delegated-link check
        info = None
    if info is not None and info.auth_kind != PLATFORM_AUTH_KIND:
        # M7 tier A (static header / client credentials): no delegated user links to connect
        return ConnectState(status="active", connect_url=None)
    link = await run.deps.tokens.link_status(tenant=run.tenant, connector=connector, keelson_sub=sub)
    status = str(link.get("status") or "missing")
    if status not in ("active", "missing", "expired"):
        status = "missing"
    from ..settings import engine_settings  # noqa: PLC0415

    connect_url = f"{engine_settings().public_url}/api/connectors/{connector}/connect?return_to=/cases/{run.case_id}"
    return ConnectState(status=status, connect_url=None if status == "active" else connect_url)  # type: ignore[arg-type]


async def approval_gate(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input)
    if ctx.state.get(COMMIT_MODE_KEY) == "rollback":
        async for event in _rollback_gate(ctx, run, data):
            yield event
        return
    bundle = _bundle(ctx, str(data.get("change_set_id") or ""))
    loaded = [await _load_change_set(run, cs_id) for cs_id in bundle["change_set_ids"]]
    change_sets = [cs for _, cs in loaded]
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        raise LookupError("no scenario saved for the case")
    plan = await run.deps.repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
    option = next((o for o in (plan or {}).get("options") or [] if str(o.get("id")) == str(data.get("option_id"))), None)
    initiator = Actor.model_validate(ctx.state.get(INITIATOR_KEY) or run.actor.model_dump(mode="json"))
    origin = case_origin(effective_origin(ctx.state.get(ORIGIN_KEY), run), record.scenario)
    totals = data.get("totals") or {}
    try:
        decision = decide_approval(
            change_sets=change_sets, scenario=record.scenario, case_mode=run.case_mode, initiator=initiator,
            config=run.spec.policy, origin=origin, tainted=await _tainted(run, record.scenario),  # type: ignore[arg-type]
            option=option, value_cents=int(totals.get("value_cents", 0)),
        )
    except PolicyEvaluationError as exc:
        error_id = report_exception(_log, "approval policy could not be evaluated", exc, rule_id=exc.rule_id)
        for cs in change_sets:
            await run.deps.repo.update_change_set(tenant=run.tenant, change_set_id=cs.id, status="refused")
        yield {"decision": "refuse", "message": user_message(
            error_id, f"The approval policy could not be evaluated (rule {exc.rule_id or 'unknown'}), "
                      "so nothing was applied.")}
        return
    ctx.state[APPROVAL_KEY] = {
        "outcome": decision.outcome,
        "approved_by": decision.approved_by,
        "policy_version": decision.policy_version,
        "rule_id": decision.rule_id,
        "reason": decision.reason,
        "requirement": decision.quorum or None,
    }
    if decision.outcome == "refuse":
        for cs in change_sets:
            await run.deps.repo.update_change_set(tenant=run.tenant, change_set_id=cs.id, status="refused")
        yield {"decision": "refuse", "message": decision.reason}
        return
    if decision.outcome == "auto":
        yield progress_event(f"Approved by policy {decision.approved_by}: {decision.reason}", prov="decision")
        yield {"decision": "approve", "auto": True, "interrupt_id": None}
        return
    assert decision.requirement is not None
    first = change_sets[0]
    interrupt_id = approval_interrupt_id(first.id, first.revision) if len(change_sets) == 1 \
        else approval_interrupt_id(str(bundle["id"]), first.revision)
    ctx.state[APPROVAL_KEY] = {**ctx.state[APPROVAL_KEY], "interrupt_id": interrupt_id}
    lines = int(totals.get("lines", sum(len(cs.items) for cs in change_sets)))
    units = int(totals.get("units_moved", 0))
    plan_version = int(data["plan_version"])
    members = [
        ApprovalBundleMember(connector=cs.connector, system=_system(run, bundle, cs.connector), change_set_id=cs.id,
                             change_set_hash=cs.hash, lines=len(cs.items))
        for cs in change_sets
    ] if len(change_sets) > 1 else []
    if members:
        lines = sum(m.lines for m in members)
    b_hash = bundle_hash([(cs.connector, cs.hash) for cs in change_sets])
    props = ApprovalCardProps(
        interrupt_id=interrupt_id,
        change_set_id=first.id,
        change_set_hash=b_hash,
        plan_version=plan_version,
        summary=ChangeTotals(lines=lines, units=units, value_cents=int(totals.get("value_cents", 0))),
        reason=decision.reason,
        required_roles=list(decision.required_roles),
        connect=await _connect_state(run, first.connector, initiator.sub),
        buttons=["approve", "reject", "revise"],
        quorum=QuorumView.build(decision.requirement, []) if decision.requirement.count > 1 else None,
        bundle=members,
    )
    yield await block_event(
        run, "approval_card", props, title="Approval needed", prov=Provenance.DECISION, engine=ENGINE_COMMIT,
        key=("approval", first.id),
    )
    yield await case_status(run, "awaiting_approval")
    where = " and ".join(dict.fromkeys(_system(run, bundle, cs.connector) for cs in change_sets))
    payload: dict[str, Any] = {
        "kind": "approval",
        "plan_version": plan_version,
        "change_set_hash": b_hash,
        "quorum_spec": decision.quorum,
        "response_schema": approval_response_schema(),
    }
    if len(change_sets) == 1:
        payload["change_set_id"] = first.id
    else:
        payload["change_set_ids"] = [cs.id for cs in change_sets]
        payload["bundle_id"] = bundle["id"]
    yield RequestInput(
        interrupt_id=interrupt_id,
        message=f"Approve {lines} changes in {where}?",
        payload=payload,
        response_schema=APPROVAL_RESUME_SCHEMA,
    )


def _deciders_view(requirement: Requirement, deciders: Sequence[Decider], initiator_sub: str | None,
                   ) -> tuple[QuorumView, list[str]]:
    at = _now()
    candidates = [QuorumCandidate(sub=d.sub, name=d.name or d.username, roles=[d.role] if d.role else [], at=at)
                  for d in deciders]
    return quorum_assign(requirement, candidates, initiator_sub=initiator_sub)


async def approval_check(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """APPROVED→execute | REVISE→close_case | REJECT→close_case | DEFAULT_ROUTE→close_case."""
    run = current_run()
    data = input_dict(node_input)
    decision = str(data.get("decision") or "").lower()
    if decision == "refuse":
        yield _refuse(str(data.get("message") or "The policy does not allow applying this plan."))
        return
    bundle = _bundle(ctx)
    ids = [str(x) for x in bundle["change_set_ids"]]
    rollback = ctx.state.get(COMMIT_MODE_KEY) == "rollback"
    approval = dict(ctx.state.get(APPROVAL_KEY) or {})
    if data.get("auto"):
        # Only the gate's own policy decision auto-approves, and never a rollback.
        if rollback or approval.get("outcome") != "auto" or not approval.get("approved_by"):
            yield _refuse("The approval policy does not allow applying these changes automatically; "
                          "nothing was applied.")
            return
        yield routed({"change_set_id": ids[0], "change_set_ids": ids, "approved_by": "policy"}, "APPROVED")
        return
    resume = run.resume
    if resume is None or resume.interrupt_id != data.get("interrupt_id"):
        yield _refuse("The approval decision does not match the open approval request.")
        return
    if approval.get("outcome") not in (None, "human") or (  # None: a pre-M5 paused approval
        approval.get("interrupt_id") and resume.interrupt_id != approval.get("interrupt_id")
    ):
        # the decision answers another approval request than the one this commit waits for
        yield _refuse("The approval decision does not match the open approval request; nothing was applied.")
        return
    yield await case_status(run, "open")
    repo = run.deps.repo
    if decision in ("reject", "revise"):
        if rollback:
            yield routed({"reason": "rejected", "message": "The rollback was not approved; the changes stay applied."},
                         "REJECT")
            return
        for cs_id in ids:
            await repo.update_change_set(
                tenant=run.tenant, change_set_id=cs_id,
                status="rejected" if decision == "reject" else "revise_requested",
            )
        yield routed({"reason": "rejected" if decision == "reject" else "revise"},
                     "REJECT" if decision == "reject" else "REVISE")
        return
    if decision != "approve":
        yield _refuse("Unknown approval decision; nothing was applied.")
        return
    loaded = [await _load_change_set(run, cs_id) for cs_id in ids]
    if rollback:
        row, change_set = loaded[0]
        if str(row.get("status") or "") not in UNDOABLE:
            yield _refuse("These changes are no longer applied, so there is nothing to undo.")
            return
        rb = dict(ctx.state.get(ROLLBACK_KEY) or {})
        if resume.change_set_hash != change_set.hash or resume.plan_version != int(rb.get("plan_version") or 0):
            yield _refuse("The approval was given for a different change batch; nothing was rolled back.")
            return
    else:
        if any(str(row.get("status") or "") != "validated" for row, _ in loaded):
            yield _refuse(
                "These changes are no longer waiting for approval (they were replaced or closed); nothing was applied."
            )
            return
        b_hash = bundle_hash([(cs.connector, cs.hash) for _, cs in loaded])
        if resume.change_set_hash != b_hash or resume.plan_version != int(loaded[0][0].get("plan_version") or 0):
            yield _refuse("The approval was given for a different version of the changes; nothing was applied.")
            return
    requirement = Requirement.model_validate(approval.get("requirement") or
                                             fallback_requirement(run.spec.policy).model_dump(mode="json"))
    initiator = Actor.model_validate(ctx.state.get(INITIATOR_KEY) or run.actor.model_dump(mode="json"))
    initiator_sub = initiator.sub if str(ctx.state.get(ORIGIN_KEY) or "chat") == "chat" else None
    view, not_counted = _deciders_view(requirement, resume.decided_by, initiator_sub)
    if not resume.decided_by or not view.met or not_counted:
        yield _refuse(
            "The approval does not come from enough people with the required roles; nothing was applied. "
            + requirement_text(requirement)
        )
        return
    if not rollback:
        for row, cs in loaded:
            if any(str(s.get("status")) in COMMITTED_STATES for s in await _siblings(run, row, exclude=ids)):
                for cs_id in ids:
                    await repo.update_change_set(tenant=run.tenant, change_set_id=cs_id, status="superseded")
                yield _refuse("This plan option was already applied through another change batch; nothing new was applied.")
                return
    by_sub = {d.sub: d for d in resume.decided_by}
    executor = next((by_sub[a.sub] for a in view.approvals if a.role == run.spec.policy.approver_role), None) \
        or by_sub[view.approvals[0].sub]  # same rule as core.interrupts.quorum_executor
    approval["deciders"] = [d.model_dump(mode="json") for d in resume.decided_by]
    approval["executor"] = executor.model_dump(mode="json")
    approval["co_approved_by"] = [approved_by_user(by_sub[a.sub].username) for a in view.approvals
                                  if a.sub != executor.sub]
    approval["quorum"] = view.model_dump(mode="json")
    approval["interrupt_id"] = resume.interrupt_id
    ctx.state[APPROVAL_KEY] = approval
    yield routed({"change_set_id": ids[0], "change_set_ids": ids, "approved_by": "user"}, "APPROVED")


# ─────────────────────────────── execute ───────────────────────────────


async def _undo_action(run: RunContext, connector: str, status: str) -> list[TrackerAction]:
    if status not in UNDOABLE:
        return []
    try:
        tools = {t.name for t in await run.deps.connectors.list_approved_tools(tenant=run.tenant, connector=connector)}
    except Exception:  # cosmetic: an unknown tool list disables the button
        tools = set()
    if "rollback_change_set" in tools:
        return [TrackerAction(type="undo", label="Undo changes", enabled=True)]
    return [TrackerAction(type="undo", label="Undo changes", enabled=False,
                          disabled_reason="This system does not let Keelson undo change batches.")]


async def _tracker(run: RunContext, change_set_id: str, status: str, *, ext_code: str | None, applied: int,
                   verified: int, system: str | None = None, connector: str | None = None,
                   conflicts: Sequence[Mapping[str, Any]] = ()) -> Any:
    connector = connector or run.spec.connector
    ext_url = f"{_ext_base_url(run)}/change-batches/{ext_code}" if ext_code and connector == run.spec.connector else None
    system = system or run.spec.vendor_name
    return await block_event(
        run,
        "commit_tracker",
        CommitTrackerProps(
            change_set_id=change_set_id, system=system, status=status,  # type: ignore[arg-type]
            ext_code=ext_code, ext_url=ext_url, applied=applied, verified=min(verified, applied),
            actions=await _undo_action(run, connector, status),
            conflicts=[RollbackConflict(entity_ref=str(c.get("entity_ref") or c.get("target_ref") or "?"),
                                        detail=str(c.get("detail") or c.get("reason") or "changed later"))
                       for c in conflicts] if status in UNDOABLE else [],
        ),
        title=f"Changes in {system}",
        prov=Provenance.COMMITTED,
        engine=ENGINE_COMMIT,
        key=("commit", change_set_id),
    )


async def _credentials(run: RunContext, connector: str, approval: Mapping[str, Any]) -> tuple[Any, str | None, str]:
    """``(principal, token, ext_sub)`` of the apply: the autocommit service account, or the
    executor's delegated user token (connectors without delegated users: the connector's apply
    service principal)."""
    gateway = get_gateway()
    info = await gateway.usable_info(tenant=run.tenant, connector=connector)
    platform = info.auth_kind == PLATFORM_AUTH_KIND
    if approval.get("outcome") == "auto":
        principal: Any = "autocommit"
    else:
        executor = Decider.model_validate(approval.get("executor") or (approval.get("deciders") or [None])[-1])
        principal = UserPrincipal(executor.sub) if platform else "autocommit"
    if not platform:
        return principal, None, f"connector:{connector}"
    token = await gateway.token(tenant=run.tenant, connector=connector, principal=principal)
    return principal, token, token_subject(token)


@dataclass(frozen=True)
class ReceiptTarget:
    """Who an approval receipt is for (M8, ``122-m8-adapter-sidecar.md`` §8): ``aud`` / ``ext_iss``.
    ``None`` keeps Merxa's (the platform connector: receipts byte-identical to M1–M7)."""

    audience: str | None = None
    ext_iss: str | None = None

    def claims(self) -> dict[str, str]:
        """``ApprovalReceiptClaims.issue`` overrides (empty for the platform connector)."""
        out: dict[str, str] = {}
        if self.audience is not None:
            out["aud"] = self.audience
        if self.ext_iss is not None:
            out["ext_iss"] = self.ext_iss
        return out


async def _receipt_target(run: RunContext, connector: str) -> ReceiptTarget:
    """A non-platform connector (static header / client credentials, e.g. an adapter sidecar) gets
    receipts bound to **its own** MCP URL (``aud``) and issuer (``ext_iss``: the connector's issuer,
    else the URL's origin), so a receipt minted for one system is refused by every other one."""
    info = await get_gateway().usable_info(tenant=run.tenant, connector=connector)
    if info.auth_kind == PLATFORM_AUTH_KIND or not info.url:
        return ReceiptTarget()
    url = info.url.rstrip("/")
    parts = urlsplit(url)
    return ReceiptTarget(audience=url, ext_iss=(info.issuer or f"{parts.scheme}://{parts.netloc}").rstrip("/"))


def _approver_claims(approval: Mapping[str, Any], initiator: Actor) -> tuple[str, str, str | None, list[str]]:
    """``(keelson_sub, approved_by, approver_name, co_approved_by)`` for the receipt."""
    if approval.get("outcome") == "auto":
        return initiator.sub, str(approval["approved_by"]), None, []
    raw = approval.get("executor") or (approval.get("deciders") or [None])[-1]
    if raw is None:
        raise LookupError("approved change set has no recorded approver")
    executor = Decider.model_validate(raw)
    co = [str(x) for x in approval.get("co_approved_by") or [] if str(x) != approved_by_user(executor.username)]
    return executor.sub, approved_by_user(executor.username), executor.name, co


_RESULT = "__apply_result__"


async def _apply_one(run: RunContext, ctx: Context, change_set_id: str, *, bundle: Mapping[str, Any],
                     approval: Mapping[str, Any], initiator: Actor) -> AsyncGenerator[Any]:
    """Apply one bundle member exactly once: yields its ``applying`` tracker, then
    ``{_RESULT: {change_set_id, status, ext_code, …}}``."""
    repo = run.deps.repo
    row, change_set = await _load_change_set(run, change_set_id)
    preview = row.get("preview") or {}
    ext_id = row.get("ext_change_set_id")
    system = _system(run, bundle, change_set.connector)
    out: dict[str, Any] = {"change_set_id": change_set_id, "connector": change_set.connector, "system": system}
    if is_per_op(row, bundle):
        async for item in _apply_per_op_member(run, row, change_set, out=out, bundle=bundle, approval=approval):
            yield item
        return
    if not ext_id or preview.get("preview_hash") != change_set.hash:
        raise HashMismatchError("change set was not validated with the current hash")
    status = str(row.get("status") or "")
    if status in ("applied", "verified"):
        yield {_RESULT: {**out, "status": status, "ext_code": row.get("ext_code"), "replayed": True,
                "applied": int((row.get("result") or {}).get("applied") or len(change_set.items))}}
        return
    if status == "conflict":  # replay of a finished attempt: the target system already answered
        conflicts = list((row.get("result") or {}).get("conflicts") or [])
        yield {_RESULT: {**out, "status": "conflict", "conflicts": conflicts, "replayed": True, "ext_code": row.get("ext_code")}}
        return
    if status not in ("validated", "applying"):
        raise StaleSessionStateError(f"change set {change_set_id} is {status or 'unknown'}; it cannot be applied")
    if any(str(s.get("status")) in COMMITTED_STATES
           for s in await _siblings(run, row, exclude=bundle.get("change_set_ids") or ())):
        raise StaleSessionStateError(f"the plan option of change set {change_set_id} was already applied")
    keep = {"requirement": approval.get("requirement"), "approved_by": approval.get("approved_by")}
    if status == "applying":
        # In doubt: a previous attempt may have reached the target system. Read back before re-applying.
        ext = await _read_back(run, change_set, str(ext_id))
        if ext.get("status") == "applied":
            applied = await _record_applied(run, change_set, ext, keep)
            yield {_RESULT: {**out, "status": "applied", "ext_code": ext.get("code"), "replayed": True, "recovered": True,
                             "applied": applied}}
            return
    principal, token, ext_sub = await _credentials(run, change_set.connector, approval)
    keelson_sub, approved_by, approver_name, co_approved = _approver_claims(approval, initiator)
    target = await _receipt_target(run, change_set.connector)
    receipt, claims = mint_receipt(
        run.deps.crypto,
        change_set=change_set,
        ext_change_set_id=str(ext_id),
        keelson_sub=keelson_sub,
        ext_sub=ext_sub,
        approved_by=approved_by,
        approver_name=approver_name,
        initiated_by=initiator.name or initiator.username,
        policy_version=str(approval.get("policy_version") or run.spec.policy.version),
        co_approved_by=co_approved,
        audience=target.audience,
        ext_iss=target.ext_iss,
    )
    await repo.update_change_set(
        tenant=run.tenant, change_set_id=change_set_id, status="applying",
        approval_id=claims.approval_id, receipt_jti=claims.jti,
    )
    for item in change_set.items:
        await repo.ledger_mark(tenant=run.tenant, change_set_id=change_set_id, line_seq=item.seq, state="attempted")
    yield await _tracker(run, change_set_id, "applying", ext_code=row.get("ext_code"), applied=0, verified=0,
                         system=system, connector=change_set.connector)
    try:
        async with get_gateway().session(tenant=run.tenant, connector=change_set.connector, principal=principal,
                                         token=token) as s:
            outcome = await apply_with_retry(
                s,
                ext_change_set_id=str(ext_id),
                preview_hash=change_set.hash,
                approval_ref=approval_id_for(change_set),
                receipt=receipt,
                idempotency_key=change_set.idempotency_key,
            )
    except McpToolError as exc:
        if exc.transient:
            raise  # still in doubt after the retries: the row stays "applying" and is read back later
        # A definitive rejection: nothing was applied.
        await repo.update_change_set(
            tenant=run.tenant, change_set_id=change_set_id, status="failed",
            result={"status": "failed", "error": f"{exc.code}: {exc.detail}"},
        )
        for item in change_set.items:
            await repo.ledger_mark(tenant=run.tenant, change_set_id=change_set_id, line_seq=item.seq, state="failed")
        raise
    if outcome.status != "applied":
        await repo.update_change_set(
            tenant=run.tenant, change_set_id=change_set_id, status="conflict",
            result={"status": outcome.status, "conflicts": outcome.conflicts},
        )
        for item in change_set.items:
            await repo.ledger_mark(tenant=run.tenant, change_set_id=change_set_id, line_seq=item.seq, state="conflict")
        yield {_RESULT: {**out, "status": "conflict", "conflicts": outcome.conflicts, "ext_code": row.get("ext_code")}}
        return
    await repo.update_change_set(
        tenant=run.tenant, change_set_id=change_set_id, status="applied", ext_code=outcome.ext_code,
        result={**keep, "status": "applied", "applied": outcome.applied, "audit_ref": outcome.audit_ref},
    )
    for item in change_set.items:
        await repo.ledger_mark(tenant=run.tenant, change_set_id=change_set_id, line_seq=item.seq,
                               state="succeeded", ext_ref=outcome.ext_code)
    await _mark_stale(run, change_set.connector, "change_set.applied")
    yield {_RESULT: {**out, "status": "applied", "ext_code": outcome.ext_code, "applied": outcome.applied}}


async def _apply_per_op_member(run: RunContext, row: Mapping[str, Any], change_set: ChangeSet, *,
                               out: Mapping[str, Any], bundle: Mapping[str, Any],
                               approval: Mapping[str, Any]) -> AsyncGenerator[Any]:
    """M7 tier-A member: one tool call per line through the commit ledger (``nodes/per_op.py``)."""
    status = str(row.get("status") or "")
    res = dict(row.get("result") or {}) if isinstance(row.get("result"), Mapping) else {}
    if status in ("applied", "verified", "partial", "failed") and res.get("mode") == PER_OP:
        yield {_RESULT: {**out, "status": "applied" if status in ("applied", "verified") else status, "per_op": True,
                         "replayed": True, "applied": int(res.get("applied") or 0), "lines": int(res.get("lines") or 0),
                         "guided_seqs": list(res.get("guided_seqs") or []), "ext_code": row.get("ext_code")}}
        return
    if status not in ("validated", "applying"):
        raise StaleSessionStateError(f"change set {change_set.id} is {status or 'unknown'}; it cannot be applied")
    if any(str(s.get("status")) in COMMITTED_STATES
           for s in await _siblings(run, row, exclude=bundle.get("change_set_ids") or ())):
        raise StaleSessionStateError(f"the plan option of change set {change_set.id} was already applied")
    keep = {"requirement": approval.get("requirement"), "approved_by": approval.get("approved_by")}
    principal, token, _ext_sub = await _credentials(run, change_set.connector, approval)
    if status == "validated":
        await run.deps.repo.update_change_set(tenant=run.tenant, change_set_id=change_set.id, status="applying",
                                              approval_id=approval_id_for(change_set))
    yield await _tracker(run, change_set.id, "applying", ext_code=None, applied=0, verified=0,
                         system=str(out["system"]), connector=change_set.connector)
    try:
        summary = await apply_per_op(run, row, change_set, principal=principal, token=token, keep=keep)
    except PerOpRefused as exc:
        prior = prior_ledger_summary(change_set, row) if status == "applying" else None
        if prior is not None and prior["status"] != "failed":
            # a resumed apply whose earlier calls reached (or may have reached) the target system:
            # keep their ledger and keep the row committed — never a second apply on top of them
            message = (f"{exc.message} Some changes were already sent to {out['system']} before; "
                       "check them there.")
            await run.deps.repo.update_change_set(
                tenant=run.tenant, change_set_id=change_set.id, status=prior["status"],
                ext_code=", ".join(prior["ext_refs"][:3]) or None,
                result={**res, **keep, **prior, "error": exc.message, "mode": PER_OP})
            yield {_RESULT: {**out, "status": prior["status"], "per_op": True, "error": "mapping_changed",
                             "message": message, "applied": prior["applied"], "lines": prior["lines"],
                             "guided_seqs": prior["guided_seqs"], "in_doubt_seqs": prior["in_doubt_seqs"],
                             "ext_code": ", ".join(prior["ext_refs"][:3]) or None}}
            return
        await run.deps.repo.update_change_set(tenant=run.tenant, change_set_id=change_set.id, status="failed",
                                              result={**keep, "status": "refused", "error": exc.message,
                                                      "mode": PER_OP})
        yield {_RESULT: {**out, "status": "failed", "per_op": True, "error": "mapping_changed",
                         "message": exc.message, "applied": 0, "lines": len(change_set.items), "guided_seqs": []}}
        return
    if summary["applied"]:
        await _mark_stale(run, change_set.connector, "per_op.applied")
    yield {_RESULT: {**out, "status": summary["status"], "per_op": True, "applied": summary["applied"],
                     "lines": summary["lines"], "guided_seqs": summary["guided_seqs"],
                     "in_doubt_seqs": summary["in_doubt_seqs"],
                     "ext_code": ", ".join(summary["ext_refs"][:3]) or None}}


async def execute(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """Mint the receipt(s) and apply exactly once (``rerun_on_resume=True``); bundle members in order."""
    run = current_run()
    if ctx.state.get(COMMIT_MODE_KEY) == "rollback":
        async for event in _execute_rollback(ctx, run):
            yield event
        return
    bundle = _bundle(ctx, str(input_dict(node_input).get("change_set_id") or ""))
    approval = dict(ctx.state.get(APPROVAL_KEY) or {})
    initiator = Actor.model_validate(ctx.state.get(INITIATOR_KEY) or run.actor.model_dump(mode="json"))
    results: list[dict[str, Any]] = []
    for cs_id in bundle["change_set_ids"]:
        result: dict[str, Any] = {}
        try:
            async for item in _apply_one(run, ctx, str(cs_id), bundle=bundle, approval=approval, initiator=initiator):
                if isinstance(item, dict) and _RESULT in item:
                    result = item[_RESULT]
                else:
                    yield item
        except McpToolError as exc:
            if exc.transient or not results:
                raise
            # a later member was rejected outright: the earlier members stay applied (reported)
            results.append({"change_set_id": str(cs_id), "status": "failed", "error": exc.code})
            break
        results.append(result)
        status = str(result["status"])
        if result.get("per_op"):
            # M7: lines that did not apply become guided steps in reconcile; a member of which
            # nothing applied (refused, every line failed) stops the bundle like a rejection
            if status == "failed" and len(bundle["change_set_ids"]) > 1:
                break
            continue
        if status == "applied" and (not result.get("replayed") or result.get("recovered")):
            yield await _tracker(run, str(cs_id), "applied", ext_code=result.get("ext_code"),
                                 applied=int(result.get("applied") or 0), verified=0, system=result.get("system"),
                                 connector=result.get("connector"))
        elif status == "conflict":
            if not result.get("replayed"):
                yield await _tracker(run, str(cs_id), "conflict", ext_code=result.get("ext_code"), applied=0,
                                     verified=0, system=result.get("system"), connector=result.get("connector"))
            break
    overall = "applied" if results and all(r["status"] in ("applied", "verified") for r in results) and \
        len(results) == len(bundle["change_set_ids"]) else ("conflict" if any(r["status"] == "conflict" for r in results)
                                                            else "partial")
    first = results[0] if results else {"change_set_id": bundle["change_set_ids"][0], "status": "failed"}
    yield {"change_set_id": first["change_set_id"], "status": overall, "ext_code": first.get("ext_code"),
           "conflicts": next((r.get("conflicts") for r in results if r.get("conflicts")), []),
           "members": results, **({"replayed": True} if results and all(r.get("replayed") for r in results) else {})}


# ─────────────────────────────── reconcile ───────────────────────────────


def _created_refs(ext: Mapping[str, Any]) -> dict[str, str]:
    """Entity codes the target system created per item (``after.code`` / ``result_ref``)."""
    out: dict[str, str] = {}
    for item in ext.get("items") or []:
        after = item.get("after") if isinstance(item.get("after"), Mapping) else {}
        ref = item.get("result_ref") or item.get("created_ref") or (after or {}).get("code")
        if ref and item.get("seq") is not None:
            out[str(item["seq"])] = str(ref)
    return out


async def reconcile(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input)
    if ctx.state.get(COMMIT_MODE_KEY) == "rollback":
        async for event in _reconcile_rollback(ctx, run, data):
            yield event
        return
    bundle = _bundle(ctx, str(data.get("change_set_id") or ""))
    repo = run.deps.repo
    conflict_note: str | None = None
    if data.get("status") == "conflict":
        vendor = next((m.get("system") for m in data.get("members") or [] if m.get("status") == "conflict"),
                      run.spec.vendor_name)
        conflict_note = (f"{vendor} reported that some records changed since the preview, so nothing was applied "
                         "there. Re-run the plan to work from the latest data.")
        if not any(m.get("status") in ("applied", "verified") for m in data.get("members") or []):
            yield await say(run, "conflict", conflict_note)
            yield {"change_set_id": data.get("change_set_id"), "status": "conflict"}
            return
        # a bundle whose earlier members were applied: read those back (they stay applied) and report both
    members = {str(m["change_set_id"]): m for m in data.get("members") or []}
    messages: list[str] = []
    all_verified = True
    plan_version: int | None = None
    committed_items: list[dict[str, Any]] = []
    first_status = "applied"
    verified_first = 0
    guides: dict[str, list[dict[str, Any]]] = {}
    guide_origin: tuple[int, str] | None = None
    for cs_id in bundle["change_set_ids"]:
        member = members.get(str(cs_id), {})
        if member.get("per_op") or (not member and bundle.get("modes")):
            row, change_set = await _load_change_set(run, str(cs_id))
            if is_per_op(row, bundle):
                if not member:
                    all_verified = False  # never executed (an earlier member stopped the bundle)
                    continue
                outcome = await _reconcile_per_op(run, row, change_set, member=member, bundle=bundle)
                for event in outcome["events"]:
                    yield event
                all_verified = all_verified and outcome["verified_all"]
                messages.append(outcome["message"])
                committed_items += outcome["committed"]
                if outcome["guide"]:
                    guides.setdefault(change_set.connector, []).extend(outcome["guide"])
                    guide_origin = (int(row.get("plan_version") or 0), str(row.get("option_id") or ""))
                plan_version = int(row["plan_version"]) if row.get("plan_version") is not None else plan_version
                if len(messages) == 1:
                    first_status, verified_first = outcome["status"], outcome["verified"]
                continue
        if member and member.get("status") not in ("applied", "verified"):
            all_verified = False
            continue
        row, change_set = await _load_change_set(run, str(cs_id))
        system = _system(run, bundle, change_set.connector)
        if str(row.get("status") or "") not in ("applied", "verified"):
            all_verified = False
            continue
        async with get_gateway().session(tenant=run.tenant, connector=change_set.connector, principal="reader") as s:
            result = await run_reconcile(s, ext_change_set_id=str(row["ext_change_set_id"]), change_set=change_set)
            try:
                ext = await s.call("get_change_set", {"change_set_id": str(row["ext_change_set_id"])})
            except (McpToolError, McpTransportError):
                ext = {}
        verified_all = result.status == "applied" and result.verified == len(change_set.items)
        all_verified = all_verified and verified_all
        for item in change_set.items[: result.verified]:
            await repo.ledger_mark(tenant=run.tenant, change_set_id=str(cs_id), line_seq=item.seq,
                                   state="verified", ext_ref=result.ext_code)
        previous = row.get("result") if isinstance(row.get("result"), Mapping) else {}
        await repo.update_change_set(
            tenant=run.tenant, change_set_id=str(cs_id), status="verified" if verified_all else "applied",
            result={
                **{k: v for k, v in dict(previous or {}).items() if k in ("requirement", "approved_by", "audit_ref")},
                "status": result.status, "applied": result.applied, "verified": result.verified,
                "attribution_text": result.attribution_text, "transfer_orders": result.transfer_orders,
                "audit_rows": result.audit_rows, "created_refs": _created_refs(ext),
            },
        )
        plan_version = int(row["plan_version"]) if row.get("plan_version") is not None else plan_version
        committed_items += [i.model_dump(mode="json") for i in change_set.items]
        status = "verified" if verified_all else "applied"
        if not messages:
            first_status, verified_first = status, result.verified
        yield await _tracker(run, str(cs_id), status, ext_code=result.ext_code, applied=result.applied,
                             verified=result.verified, system=system, connector=change_set.connector)
        attribution = f" {result.attribution_text}." if result.attribution_text else ""
        if verified_all:
            extra = f" ({len(result.transfer_orders)} transfer orders)" if result.transfer_orders else \
                (" (0 transfer orders)" if len(bundle["change_set_ids"]) == 1 else "")
            messages.append(f"Applied {result.applied} changes in {system} as {result.ext_code} and verified them by "
                            f"reading them back{extra}.{attribution}")
        else:
            messages.append(f"{system} applied {result.ext_code}, but only {result.verified} of "
                            f"{len(change_set.items)} changes could be verified by reading them back.{attribution}")
    if guides and guide_origin is not None:
        all_verified = False
        if run.deps.checklists is None:
            messages.append("Guided steps are not available in this installation, so the remaining changes "
                            "must be made by hand.")
        else:
            table = await capability_table(run.deps, tenant=run.tenant, connectors=run.spec.write_connectors(),
                                           vendor=run.spec.vendor_name)
            for event in await _create_guides(ctx, run, table, guides, plan_version=guide_origin[0],
                                              option_id=guide_origin[1]):
                yield event
    if conflict_note is not None:
        messages.append(conflict_note)
    failed = [m for m in data.get("members") or [] if m.get("status") == "failed"]
    if failed:
        messages.append("Some systems did not apply their part: " + ", ".join(
            f"{m.get('system') or m.get('change_set_id')} ({m.get('error')})" for m in failed) + ".")
    if all_verified and plan_version is not None and not failed:
        await repo.mark_plan_status(tenant=run.tenant, case_id=run.case_id, version=plan_version, status="committed")
        yield await case_status(run, "committed")
    if plan_version is not None and run.deps.plans is not None and committed_items:
        plan = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
        if plan is not None and int(plan["version"]) == plan_version:
            problem = await load_problem(run, str(ctx.state.get(PROBLEM_STATE_KEY))) \
                if ctx.state.get(PROBLEM_STATE_KEY) else None
            deps = plan_dependencies(plan.get("options") or [], problem, run.spec.write_connectors(), committed_items)
            try:
                await run.deps.plans.save_plan_dependencies(tenant=run.tenant, case_id=run.case_id,
                                                            plan_version=plan_version, dependencies=deps)
            except Exception as exc:  # the index is best effort; the plan row keeps the fingerprint
                _log.warning("plan dependency index not updated", error=type(exc).__name__)
    yield await say(run, "reconcile", " ".join(messages) or "Nothing was applied.")
    yield {"change_set_id": bundle["change_set_ids"][0], "status": "verified" if all_verified else "applied",
           "verified": verified_first, "first_status": first_status}


async def _reconcile_per_op(run: RunContext, row: Mapping[str, Any], change_set: ChangeSet, *,
                            member: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
    """M7 per-op member: read back the applied lines, report, and hand the rest to the guide."""
    system = _system(run, bundle, change_set.connector)
    out: dict[str, Any] = {"events": [], "verified_all": False, "message": "", "committed": [], "guide": [],
                           "status": "applied", "verified": 0}
    if member.get("status") == "failed" and member.get("error") == "mapping_changed":
        out["message"] = str(member.get("message") or f"Nothing was applied in {system}.")
        out["status"] = "conflict"
        return out
    summary = await verify_per_op(run, row, change_set)
    lines, applied, verified = int(summary["lines"]), int(summary["applied"]), int(summary["verified"])
    status = "verified" if summary["row_status"] == "verified" else ("applied" if applied else "conflict")
    out.update(status=status, verified=verified, verified_all=summary["row_status"] == "verified")
    out["events"].append(await _tracker(run, change_set.id, status, ext_code=row.get("ext_code"), applied=applied,
                                        verified=verified, system=system, connector=change_set.connector))
    done = {int(s) for s in range(1, lines + 1)} - {int(s) for s in summary["guided_seqs"]}
    out["committed"] = [dict(i) for i in row.get("items") or [] if int(i.get("seq") or 0) in done]
    out["guide"] = guided_lines(change_set, row, summary["guided_seqs"])
    parts: list[str] = []
    if applied:
        parts.append(f"Applied {applied} of {lines} change{'s' if lines != 1 else ''} in {system} one call at a time "
                     f"and verified {verified} by reading them back.")
    else:
        parts.append(f"None of the {lines} change{'s' if lines != 1 else ''} could be applied in {system}.")
    doubt = len(summary["in_doubt_seqs"])
    refused = len(summary["guided_seqs"]) - doubt
    if refused:
        parts.append(f"{system} refused {refused} of them (for example, the record changed since the plan); "
                     "they are listed as guided steps.")
    if doubt:
        parts.append(f"The outcome of {doubt} could not be confirmed, so they were not repeated; check them in "
                     f"{system} with the guided steps.")
    out["message"] = " ".join(parts)
    return out


# ─────────────────────────────── rollback (§3) ───────────────────────────────


async def prepare_rollback(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """Case action ``rollback {change_set_id}`` → ``ROLLBACK`` (approval_gate) | ``REFUSED``.

    Always a human approval: the quorum the original apply needed (stored on the change set),
    else the policy's requirement for that change set (auto rules never apply), else one approver."""
    run = current_run()
    data = input_dict(node_input)
    cs_id = str(data.get("change_set_id") or "")
    repo = run.deps.repo
    row = await repo.get_change_set(tenant=run.tenant, change_set_id=cs_id) if cs_id else None
    if row is None or str(row.get("case_id") or "") != run.case_id:
        yield _refuse("That change batch is not part of this case.")
        return
    status = str(row.get("status") or "")
    if status == "rolled_back":
        yield _refuse(f"{row.get('ext_code') or 'This change batch'} was already rolled back.")
        return
    if status not in UNDOABLE:
        yield _refuse("Only applied changes can be undone; nothing was changed.")
        return
    row, change_set = await _load_change_set(run, cs_id)
    table = await capability_table(run.deps, tenant=run.tenant, connectors=[change_set.connector],
                                   vendor=run.spec.vendor_name if change_set.connector == run.spec.connector
                                   else change_set.connector)
    caps = table.primary
    if not caps.has_tool("rollback_change_set"):
        yield _refuse(f"{caps.vendor} does not let Keelson undo change batches; undo the changes there by hand.")
        return
    plan = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
    record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    stored = (row.get("result") or {}).get("requirement") if isinstance(row.get("result"), Mapping) else None
    requirement: Requirement | None = Requirement.model_validate(stored) if stored else None
    rule_id: str | None = None
    if requirement is None:
        option = next((o for o in (plan or {}).get("options") or [] if str(o.get("id")) == str(row.get("option_id"))), None)
        try:
            decision = decide_approval(change_set=change_set, scenario=None if record is None else
                                       record.scenario.model_copy(update={"mode": "actual"}),
                                       case_mode="actual", initiator=run.actor, config=run.spec.policy,
                                       option=option, allow_auto=False)
            if decision.requirement is not None:
                requirement, rule_id = decision.requirement, decision.rule_id
        except PolicyEvaluationError as exc:
            _log.warning("rollback policy evaluation failed; one approver applies", rule_id=exc.rule_id)
    requirement = requirement or fallback_requirement(run.spec.policy)
    stored_result = dict(row.get("result") or {}) if isinstance(row.get("result"), Mapping) else {}
    # every rollback request gets its own attempt (interrupt id, approval ref, idempotency key)
    attempt = max(int(stored_result.get("rollback_attempts") or 0),
                  int(stored_result.get("rollback_requested") or 0)) + 1
    await repo.update_change_set(tenant=run.tenant, change_set_id=cs_id,
                                 result={**stored_result, "rollback_requested": attempt})
    ctx.state[COMMIT_MODE_KEY] = "rollback"
    ctx.state[CHANGE_SET_KEY] = cs_id
    ctx.state[BUNDLE_KEY] = {"id": cs_id, "change_set_ids": [cs_id], "hash": change_set.hash,
                             "systems": {change_set.connector: caps.vendor}}
    ctx.state[INITIATOR_KEY] = run.actor.model_dump(mode="json")
    ctx.state[ORIGIN_KEY] = "chat"
    ctx.state[ROLLBACK_KEY] = {"plan_version": int((plan or {}).get("version") or row.get("plan_version") or 1),
                               "attempt": attempt}
    reason = (f"Undo {row.get('ext_code') or 'the change batch'} in {caps.vendor}: every change it made is reversed. "
              + requirement_text(requirement))
    ctx.state[APPROVAL_KEY] = {"outcome": "human", "approved_by": None, "policy_version": run.spec.policy.version,
                               "rule_id": rule_id, "reason": reason, "requirement": requirement.model_dump(mode="json")}
    yield routed({"change_set_id": cs_id, "rollback": True}, ROUTE_ROLLBACK)


async def _rollback_gate(ctx: Context, run: RunContext, data: Mapping[str, Any]) -> AsyncGenerator[Any]:
    cs_id = str(ctx.state.get(CHANGE_SET_KEY) or data.get("change_set_id"))
    row, change_set = await _load_change_set(run, cs_id)
    approval = dict(ctx.state.get(APPROVAL_KEY) or {})
    rb = dict(ctx.state.get(ROLLBACK_KEY) or {})
    requirement = Requirement.model_validate(approval["requirement"])
    bundle = _bundle(ctx)
    system = _system(run, bundle, change_set.connector)
    interrupt_id = rollback_interrupt_id(cs_id, int(rb.get("attempt") or 1))
    ctx.state[APPROVAL_KEY] = {**approval, "interrupt_id": interrupt_id}
    initiator = Actor.model_validate(ctx.state.get(INITIATOR_KEY) or run.actor.model_dump(mode="json"))
    props = ApprovalCardProps(
        interrupt_id=interrupt_id,
        change_set_id=cs_id,
        change_set_hash=change_set.hash,
        plan_version=int(rb["plan_version"]),
        summary=ChangeTotals(lines=len(change_set.items), units=0, value_cents=0),
        reason=str(approval.get("reason") or "Undo these changes."),
        required_roles=list(requirement.roles),
        connect=await _connect_state(run, change_set.connector, initiator.sub),
        buttons=["approve", "reject"],
        quorum=QuorumView.build(requirement, []) if requirement.count > 1 else None,
    )
    yield await block_event(run, "approval_card", props, title="Approve the rollback", prov=Provenance.DECISION,
                            engine=ENGINE_COMMIT, key=("approval", cs_id, "rollback", int(rb.get("attempt") or 1)))
    yield await case_status(run, "awaiting_approval")
    yield RequestInput(
        interrupt_id=interrupt_id,
        message=f"Undo {row.get('ext_code') or 'these changes'} in {system}?",
        payload={
            "kind": "approval",
            "change_set_id": cs_id,
            "rollback": True,
            "plan_version": int(rb["plan_version"]),
            "change_set_hash": change_set.hash,
            "quorum_spec": requirement.model_dump(mode="json"),
            "response_schema": approval_response_schema(),
        },
        response_schema=APPROVAL_RESUME_SCHEMA,
    )


def rollback_key(change_set: ChangeSet, attempt: int) -> str:
    """Idempotency key of the ``attempt``-th rollback (never the apply key)."""
    return hashlib.sha256(f"{change_set.tenant}|{change_set.id}|rollback|{attempt}".encode()).hexdigest()


def rollback_approval_id(change_set: ChangeSet, attempt: int) -> str:
    return f"APR-RB-{rollback_key(change_set, attempt)[:10].upper()}"


async def _execute_rollback(ctx: Context, run: RunContext) -> AsyncGenerator[Any]:
    repo = run.deps.repo
    cs_id = str(ctx.state.get(CHANGE_SET_KEY))
    row, change_set = await _load_change_set(run, cs_id)
    bundle = _bundle(ctx)
    system = _system(run, bundle, change_set.connector)
    rb = dict(ctx.state.get(ROLLBACK_KEY) or {})
    attempt = int(rb.get("attempt") or 1)
    approval = dict(ctx.state.get(APPROVAL_KEY) or {})
    previous = dict(row.get("result") or {}) if isinstance(row.get("result"), Mapping) else {}
    status = str(row.get("status") or "")
    ext_id = str(row.get("ext_change_set_id") or "")
    if status == "rolled_back":
        yield {"change_set_id": cs_id, "status": "rolled_back", "replayed": True}
        return
    if status not in UNDOABLE and status != "rolling_back":
        raise StaleSessionStateError(f"change set {cs_id} is {status or 'unknown'}; it cannot be rolled back")
    if status == "rolling_back":
        ext = await _read_back(run, change_set, ext_id)
        if ext.get("status") == "rolled_back":
            await repo.update_change_set(tenant=run.tenant, change_set_id=cs_id, status="rolled_back",
                                         result={**previous, "rolled_back": True, "recovered": True})
            yield {"change_set_id": cs_id, "status": "rolled_back", "replayed": True}
            return
    initiator = Actor.model_validate(ctx.state.get(INITIATOR_KEY) or run.actor.model_dump(mode="json"))
    principal, token, ext_sub = await _credentials(run, change_set.connector, approval)
    keelson_sub, approved_by, approver_name, co_approved = _approver_claims(approval, initiator)
    approval_ref = rollback_approval_id(change_set, attempt)
    from keelson_contracts.receipts import ApprovalReceiptClaims  # noqa: PLC0415

    target = await _receipt_target(run, change_set.connector)
    claims = ApprovalReceiptClaims.issue(
        approval_id=approval_ref, change_set_ref=ext_id, change_set_hash=change_set.hash, tenant=change_set.tenant,
        keelson_sub=keelson_sub, ext_sub=ext_sub, approved_by=approved_by, approver_name=approver_name,
        initiated_by=initiator.name or initiator.username,
        policy_version=str(approval.get("policy_version") or run.spec.policy.version), co_approved_by=co_approved,
        **target.claims(),
    )
    receipt = run.deps.crypto.sign_approval_receipt(claims)
    if status in UNDOABLE:
        previous["rollback_from"] = status  # restored when the target system refuses the rollback
    restore = str(previous.get("rollback_from") or "applied") if status == "rolling_back" else status
    await repo.update_change_set(tenant=run.tenant, change_set_id=cs_id, status="rolling_back",
                                 result={**previous, "rollback_attempts": attempt, "rollback_ref": approval_ref})
    try:
        async with get_gateway().session(tenant=run.tenant, connector=change_set.connector, principal=principal,
                                         token=token) as s:
            data = await s.call("rollback_change_set", {
                "change_set_id": ext_id, "approval_ref": approval_ref, "approval_receipt": receipt,
                "idempotency_key": rollback_key(change_set, attempt),
            })
    except McpToolError as exc:
        if exc.transient:
            raise  # in doubt: read back on the retry
        await repo.update_change_set(tenant=run.tenant, change_set_id=cs_id, status=restore,
                                     result={**previous, "rollback_attempts": attempt,
                                             "rollback_error": f"{exc.code}: {exc.detail}"})
        yield {"change_set_id": cs_id, "status": "rollback_failed", "error": exc.code}
        return
    outcome = str(data.get("status") or "")
    if outcome != "rolled_back":
        conflicts = [dict(c) for c in data.get("conflicts") or []]
        await repo.update_change_set(tenant=run.tenant, change_set_id=cs_id, status=restore,
                                     result={**previous, "rollback_attempts": attempt, "rollback_conflicts": conflicts})
        yield await _tracker(run, cs_id, restore, ext_code=row.get("ext_code"),
                             applied=int(previous.get("applied") or len(change_set.items)),
                             verified=int(previous.get("verified") or 0), system=system,
                             connector=change_set.connector, conflicts=conflicts)
        yield {"change_set_id": cs_id, "status": "rollback_conflict", "conflicts": conflicts}
        return
    await repo.update_change_set(tenant=run.tenant, change_set_id=cs_id, status="rolled_back",
                                 result={**previous, "rollback_attempts": attempt, "rolled_back": True,
                                         "rollback_ref": approval_ref,
                                         "rollback_change_set": data.get("compensating_change_set_code")
                                         or data.get("change_set_code")})
    for item in change_set.items:
        await repo.ledger_mark(tenant=run.tenant, change_set_id=cs_id, line_seq=item.seq, state="rolled_back",
                               ext_ref=row.get("ext_code"))
    await _mark_stale(run, change_set.connector, "change_set.rolled_back")
    yield {"change_set_id": cs_id, "status": "rolled_back"}


async def _reconcile_rollback(ctx: Context, run: RunContext, data: Mapping[str, Any]) -> AsyncGenerator[Any]:
    repo = run.deps.repo
    cs_id = str(ctx.state.get(CHANGE_SET_KEY))
    row, change_set = await _load_change_set(run, cs_id)
    bundle = _bundle(ctx)
    system = _system(run, bundle, change_set.connector)
    status = str(data.get("status") or "")
    code = row.get("ext_code") or "the change batch"
    if status == "rollback_conflict":
        refs = ", ".join(str(c.get("entity_ref") or c.get("target_ref") or "?") for c in data.get("conflicts") or [])
        yield await say(run, "rollback_conflict",
                        f"{system} refused to undo {code}: later changes touched the same records ({refs or 'see the tracker'}). "
                        "Nothing was rolled back.")
        yield {"change_set_id": cs_id, "status": "conflict"}
        return
    if status == "rollback_failed":
        yield await say(run, "rollback_failed", f"{system} did not undo {code}; nothing was rolled back.")
        yield {"change_set_id": cs_id, "status": "failed"}
        return
    ext = await _read_back(run, change_set, str(row.get("ext_change_set_id") or ""))
    if str(ext.get("status") or "") != "rolled_back":
        yield await say(run, "rollback_unverified",
                        f"{system} accepted the rollback of {code}, but reading it back still shows it as "
                        f"{ext.get('status') or 'unknown'}.")
        yield {"change_set_id": cs_id, "status": "unverified"}
        return
    plan_version = int(row.get("plan_version") or 0)
    if plan_version:
        await repo.mark_plan_status(tenant=run.tenant, case_id=run.case_id, version=plan_version, status="rolled_back")
    plan = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
    already = ((plan or {}).get("kpis") or {}).get("recovery") or {}
    if plan is not None and str(already.get("rolled_back_change_set") or "") != cs_id:  # once per rollback
        version = int(plan["version"]) + 1
        await repo.save_plan(
            tenant=run.tenant, case_id=run.case_id, version=version, options=list(plan.get("options") or []),
            recommended_option_id=DO_NOTHING_ID,
            kpis={**dict(plan.get("kpis") or {}), "recovery": {"rolled_back_change_set": cs_id,
                                                               "rolled_back_plan_version": plan_version,
                                                               "baseline": True}},
        )
        await run.deps.interrupts.supersede_open(tenant=run.tenant, case_id=run.case_id, reason=f"rollback v{version}")
    yield await _tracker(run, cs_id, "rolled_back", ext_code=row.get("ext_code"),
                         applied=int(((row.get("result") or {}) if isinstance(row.get("result"), Mapping) else {})
                                     .get("applied") or len(change_set.items)), verified=0,
                         system=system, connector=change_set.connector)
    yield await case_status(run, "open")
    attribution = f" {ext['attribution_text']}." if ext.get("attribution_text") else ""
    yield await say(run, "rolled_back",
                    f"Rolled back {code} in {system} and verified it by reading it back.{attribution} "
                    "The plan is back to its baseline.")
    yield {"change_set_id": cs_id, "status": "rolled_back"}
