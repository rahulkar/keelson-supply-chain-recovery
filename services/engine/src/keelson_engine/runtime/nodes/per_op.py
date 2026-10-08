"""M7 tier-A **per-op** commit inside the commit path (``docs/contracts/110-m7-backoffice-generic.md``
§4): the ``dry_run`` / ``execute`` / ``reconcile`` steps of a bundle member whose connector has
no change-set tools but an action mapping (:class:`~keelson_engine.runtime.candidates.ConnectorCapabilities`
``commit_mode == "per_op"``). The engine change-set row, its hash, the approval (policy as usual,
human by default) and the bundle rules are the same as for change sets; what differs:

* **dry run** renders every line's tool call from the action mapping (never executed) and stores
  them in the row's ``preview`` (``mode: per_op``, ``calls_hash``). A line that cannot be rendered
  refuses the apply.
* **execute** re-renders the calls from the *current* mapping and refuses when they differ from
  the approved preview (an admin changed the mapping in between), then drives each line through
  the ledger state machine (:class:`~keelson_engine.runtime.tier_a.PerOpCommitter`) with the apply
  principal; reads use the reader principal. The ledger lives in ``change_set.result.ledger`` (full
  :class:`~keelson_contracts.commit.CommitLedgerEntry` per line) and ``commit_ledger`` (state).
* **reconcile** reads back every ``succeeded`` line (``verified`` when found) and turns every line
  that did not apply (``failed``, unresolved ``in_doubt``, ``none`` not found, unmapped) into
  guided steps with generated instructions (``remediation_checklist@1``).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any, Final

import httpx2
import structlog
from keelson_contracts.changeset import ChangeSet
from keelson_contracts.common import Provenance
from keelson_contracts.connectors import Idempotency
from keelson_contracts.mapping import ActionMapping, ExpressionError, TemplateError
from mcp.shared.exceptions import MCPError

from ..context import RunContext
from ..errors import HashMismatchError, McpToolError, McpTransportError
from ..mcp_client import get_gateway
from ..tier_a import (
    LineRecord,
    PerOpCommitter,
    PerOpLine,
    RenderError,
    canonical_json,
    load_action_mapping,
    per_op_lines,
    render_call,
    request_id_for,
)
from .common import ENGINE_COMMIT, block_event

__all__ = [
    "PER_OP",
    "PerOpRefused",
    "apply_per_op",
    "connector_call",
    "dry_run_per_op",
    "guided_lines",
    "is_per_op",
    "ledger_of",
    "prior_ledger_summary",
    "verify_per_op",
]

_log = structlog.get_logger(__name__)

PER_OP: Final = "per_op"
"""``change_set.preview.mode`` / bundle ``modes`` value of a per-op member."""
DONE_STATES: Final = frozenset({"succeeded", "verified"})


class PerOpRefused(Exception):
    """The per-op member cannot be applied (user-facing ``message``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def is_per_op(row: Mapping[str, Any] | None, bundle: Mapping[str, Any] | None = None) -> bool:
    if isinstance(row, Mapping) and (row.get("preview") or {}).get("mode") == PER_OP:
        return True
    modes = (bundle or {}).get("modes") or {}
    return isinstance(row, Mapping) and modes.get(str(row.get("connector"))) == PER_OP


def ledger_of(row: Mapping[str, Any]) -> dict[str, LineRecord]:
    raw = (row.get("result") or {}).get("ledger") if isinstance(row.get("result"), Mapping) else None
    return {str(k): LineRecord.from_json(v) for k, v in (raw or {}).items()}


def _calls(lines: Sequence[PerOpLine], key: str) -> list[dict[str, Any]]:
    out = []
    for line in lines:
        rid = request_id_for(key, line.seq) if (line.mapping.request_id_arg or line.mapping.idempotency.value
                                                 == "enforced") else None
        call = render_call(line.mapping, line.item, request_id=rid)
        out.append({"seq": line.seq, "op": line.op, "tool": call.tool, "args": call.args,
                    "idempotency": line.mapping.idempotency.value})
    return out


def _calls_hash(calls: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(list(calls)).encode()).hexdigest()


async def _mapping(run: RunContext, connector: str) -> ActionMapping:
    """The connector's action mapping reduced to the ops its approved tools can still apply, each
    with its **effective** idempotency: the mapping's claim capped by the tool's admin-confirmed
    one (:func:`~keelson_engine.runtime.candidates.per_op_capabilities`, the same rule the
    capability table plans with). The ledger state machine retries by this value, so a mapping
    that claims ``enforced`` for a tool confirmed ``none`` is never auto-retried."""
    from ..candidates import per_op_capabilities  # noqa: PLC0415 - import cycle

    mapping = await load_action_mapping(run.deps, tenant=run.tenant, connector=connector)
    if mapping is None:
        raise PerOpRefused("The connector's action mapping is not available any more, so nothing was applied.")
    tools = {t.name: t for t in await run.deps.connectors.list_approved_tools(tenant=run.tenant, connector=connector)}
    effective = per_op_capabilities(mapping, tools)
    ops = [op.model_copy(update={"idempotency": Idempotency(effective[op.op])})
           for op in mapping.ops if op.op in effective]
    return mapping.model_copy(update={"ops": ops})


def _lines(change_set: ChangeSet, mapping: ActionMapping) -> list[PerOpLine]:
    lines, unmapped = per_op_lines([i.model_dump(mode="json") for i in change_set.items], mapping)
    if unmapped:  # compile only puts mapped ops into a per-op member
        raise PerOpRefused("The connector's action mapping no longer covers every planned change; "
                           "re-run the plan to work from the current mapping.")
    return lines


async def dry_run_per_op(run: RunContext, row: Mapping[str, Any], change_set: ChangeSet, *, system: str,
                         plan_version: int, option_id: str, problem: Any, multi: bool) -> AsyncGenerator[Any]:
    """Render (never execute) every line's call; validate the member. Yields its ``plan_diff``
    block, then ``{"ok": bool, "problem": str | None, "totals": {...}}``."""
    from .planning import plan_diff_props  # noqa: PLC0415 - import cycle

    repo = run.deps.repo
    try:
        mapping = await _mapping(run, change_set.connector)
        lines = _lines(change_set, mapping)
        calls = _calls(lines, change_set.idempotency_key)
    except PerOpRefused as exc:
        await repo.update_change_set(tenant=run.tenant, change_set_id=change_set.id, status="invalid")
        yield {"ok": False, "problem": exc.message, "totals": {}}
        return
    except (RenderError, TemplateError, ExpressionError, ValueError) as exc:
        _log.warning("per-op call could not be rendered", change_set_id=change_set.id, error=str(exc)[:300])
        await repo.update_change_set(tenant=run.tenant, change_set_id=change_set.id, status="invalid")
        yield {"ok": False, "problem": f"The {system} action mapping could not prepare these changes "
                                       "(a planned value does not fit the mapping), so nothing was sent.", "totals": {}}
        return
    props = plan_diff_props(plan_version=plan_version, actions=list(row.get("items") or []), vendor=system,
                            problem=problem)
    totals = {"lines": len(lines), "units_moved": props.totals.units, "value_cents": props.totals.value_cents}
    await repo.update_change_set(
        tenant=run.tenant, change_set_id=change_set.id, status="validated",
        preview={"preview_hash": change_set.hash, "mode": PER_OP, "calls": calls, "calls_hash": _calls_hash(calls),
                 "totals": totals},
    )
    yield await block_event(
        run, "plan_diff", props, title=f"Changes in {system} (one call per change)", prov=Provenance.SIMULATED,
        engine=ENGINE_COMMIT, key=("plan_diff", plan_version, option_id, *((change_set.connector,) if multi else ())),
        placement="canvas",
    )
    yield {"ok": True, "problem": None, "totals": totals}


async def connector_call(*, tenant: str, connector: str, principal: Any, tool: str, args: dict[str, Any],
                         token: str | None = None) -> dict[str, Any]:
    """One tool call in its own session. A lost response can also surface when the MCP client
    tears its session down (the HTTP stream task re-raises it): any transport-level failure is a
    :class:`~keelson_engine.runtime.errors.McpTransportError` (unknown outcome), never a crash."""
    try:
        async with get_gateway().session(tenant=tenant, connector=connector, principal=principal, token=token) as s:
            return await s.call(tool, args)
    except (McpToolError, McpTransportError):
        raise
    except (MCPError, httpx2.HTTPError, OSError, TimeoutError) as exc:
        raise McpTransportError(tool, f"{type(exc).__name__}: {exc}") from exc
    except BaseExceptionGroup as group:
        if group.subgroup((McpToolError,)) is not None:
            raise
        raise McpTransportError(tool, f"{type(group).__name__}: {group}") from group


def _committer(run: RunContext, change_set: ChangeSet, *, principal: Any, token: str | None,
               ledger: dict[str, LineRecord], row: Mapping[str, Any]) -> PerOpCommitter:
    repo = run.deps.repo
    result: dict[str, Any] = dict(row.get("result") or {}) if isinstance(row.get("result"), Mapping) else {}

    async def write(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await connector_call(tenant=run.tenant, connector=change_set.connector, principal=principal,
                                    tool=tool, args=args, token=token)

    async def read(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await connector_call(tenant=run.tenant, connector=change_set.connector, principal="reader",
                                    tool=tool, args=args)

    async def persist(seq: int, record: LineRecord) -> None:
        ledger[str(seq)] = record
        result["ledger"] = {k: v.to_json() for k, v in sorted(ledger.items(), key=lambda kv: int(kv[0]))}
        await repo.update_change_set(tenant=run.tenant, change_set_id=change_set.id, result=dict(result))
        entry = record.entry
        if entry is None:
            return
        recorder = getattr(repo, "ledger_record", None)
        if callable(recorder):
            await recorder(tenant=run.tenant, entry=entry)
        else:
            await repo.ledger_mark(tenant=run.tenant, change_set_id=change_set.id, line_seq=seq, state=entry.state,
                                   ext_ref=entry.ext_ref)

    return PerOpCommitter(change_set_id=change_set.id, connector=change_set.connector,
                          idempotency_key=change_set.idempotency_key, write=write, read=read, persist=persist)


def _summary(lines: Sequence[PerOpLine], ledger: Mapping[str, LineRecord]) -> dict[str, Any]:
    done = [line.seq for line in lines if (r := ledger.get(str(line.seq))) is not None and r.settled]
    verified = [line.seq for line in lines
                if (r := ledger.get(str(line.seq))) is not None and r.entry is not None and r.entry.state == "verified"]
    open_ = [line.seq for line in lines if line.seq not in done]
    # unknown outcome: no read-back could tell (a read-back that found nothing is a clear "not applied")
    in_doubt = [s for s in open_ if (r := ledger.get(str(s))) is not None and r.entry is not None
                and r.entry.state in ("attempted", "in_doubt")
                and (r.entry.readback is None or not r.entry.readback.ok)]
    # ``partial`` whenever the target system holds (or may hold) some of the changes: an in-doubt
    # line keeps the option committed, so a later Apply can never send it a second time
    status = "applied" if not open_ else ("partial" if done or in_doubt else "failed")
    ext = [r.entry.ext_ref for line in lines if (r := ledger.get(str(line.seq))) is not None and r.entry is not None
           and r.entry.ext_ref]
    return {"status": status, "applied": len(done), "verified": len(verified), "lines": len(lines),
            "guided_seqs": open_, "in_doubt_seqs": in_doubt, "ext_refs": ext[:50]}


async def apply_per_op(run: RunContext, row: Mapping[str, Any], change_set: ChangeSet, *, principal: Any,
                       token: str | None, keep: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the member's lines exactly once each (re-run safe: settled lines are skipped,
    unsettled ones resume from their ledger entry). Returns the summary written to the row."""
    preview = row.get("preview") or {}
    if preview.get("mode") != PER_OP or preview.get("preview_hash") != change_set.hash:
        raise HashMismatchError("per-op change set was not validated with the current hash")
    mapping = await _mapping(run, change_set.connector)
    lines = _lines(change_set, mapping)
    try:
        calls = _calls(lines, change_set.idempotency_key)
    except (RenderError, TemplateError, ExpressionError, ValueError) as exc:
        raise PerOpRefused("The action mapping can no longer prepare the approved changes; nothing new was "
                           "applied.") from exc
    if _calls_hash(calls) != preview.get("calls_hash"):
        raise PerOpRefused("The connector's action mapping changed after the preview was approved, so nothing new "
                           "was applied. Apply the plan again to approve the new calls.")
    ledger = ledger_of(row)
    committer = _committer(run, change_set, principal=principal, token=token, ledger=ledger, row=row)
    for line in lines:
        record = ledger.get(str(line.seq))
        if record is not None and record.settled:
            continue
        await committer.apply(line, record)
    summary = _summary(lines, ledger)
    result = {**dict(keep), "ledger": {k: v.to_json() for k, v in sorted(ledger.items(), key=lambda kv: int(kv[0]))},
              **summary, "mode": PER_OP}
    await run.deps.repo.update_change_set(
        tenant=run.tenant, change_set_id=change_set.id, status=summary["status"],
        ext_code=", ".join(summary["ext_refs"][:3]) or None, result=result,
    )
    return summary


async def verify_per_op(run: RunContext, row: Mapping[str, Any], change_set: ChangeSet) -> dict[str, Any]:
    """Reconcile: read back the ``succeeded`` lines; returns the updated summary. When the
    mapping can no longer read the lines back (changed or removed since the apply), the stored
    ledger is reported as it is (nothing is read back, nothing is lost)."""
    try:
        mapping = await _mapping(run, change_set.connector)
        lines = _lines(change_set, mapping)
    except PerOpRefused:
        prior = prior_ledger_summary(change_set, row)
        if prior is None:
            seqs = sorted(int(i.seq) for i in change_set.items)
            prior = {"status": "failed", "applied": 0, "verified": 0, "lines": len(seqs), "guided_seqs": seqs,
                     "in_doubt_seqs": [], "ext_refs": []}
        summary = {k: v for k, v in prior.items() if k != "ledger"}
        return {**summary, "row_status": str(row.get("status") or summary["status"])}
    ledger = ledger_of(row)
    committer = _committer(run, change_set, principal="reader", token=None, ledger=ledger, row=row)
    for line in lines:
        record = ledger.get(str(line.seq))
        if record is not None and record.entry is not None and record.entry.state == "succeeded":
            await committer.verify(line, record)
    summary = _summary(lines, ledger)
    status = "verified" if summary["status"] == "applied" and summary["verified"] == summary["lines"] \
        else summary["status"]
    previous = dict(row.get("result") or {}) if isinstance(row.get("result"), Mapping) else {}
    await run.deps.repo.update_change_set(
        tenant=run.tenant, change_set_id=change_set.id, status=status,
        result={**previous, "ledger": {k: v.to_json() for k, v in sorted(ledger.items(), key=lambda kv: int(kv[0]))},
                **summary, "mode": PER_OP},
    )
    return {**summary, "row_status": status}


def prior_ledger_summary(change_set: ChangeSet, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The summary of an earlier, interrupted apply of this member from its stored ledger alone
    (``None`` when no call was ever attempted). Used when a resumed apply is refused (the mapping
    changed or vanished meanwhile): lines that were sent stay on record — the row stays committed
    (``partial``) so the option can never be applied a second time on top of them."""
    ledger = ledger_of(row)
    records = {seq: r for seq, r in ledger.items() if r.entry is not None}
    if not records:
        return None
    seqs = sorted(int(i.seq) for i in change_set.items)
    done = [s for s in seqs if (r := records.get(str(s))) is not None and r.settled]
    open_ = [s for s in seqs if s not in done]
    in_doubt = [s for s in open_ if (r := records.get(str(s))) is not None and r.entry is not None
                and r.entry.state in ("attempted", "in_doubt")
                and (r.entry.readback is None or not r.entry.readback.ok)]
    status = "applied" if not open_ else ("partial" if done or in_doubt else "failed")
    ext = [r.entry.ext_ref for s in seqs if (r := records.get(str(s))) is not None and r.entry is not None
           and r.entry.ext_ref]
    verified = [s for s in done if (r := records.get(str(s))) is not None and r.entry is not None
                and r.entry.state == "verified"]
    return {"status": status, "applied": len(done), "verified": len(verified), "lines": len(seqs),
            "guided_seqs": open_, "in_doubt_seqs": in_doubt, "ext_refs": ext[:50],
            "ledger": {k: v.to_json() for k, v in sorted(ledger.items(), key=lambda kv: int(kv[0]))}}


def guided_lines(change_set: ChangeSet, row: Mapping[str, Any], seqs: Sequence[int]) -> list[dict[str, Any]]:
    """The stored actions (with their atomic group) of the lines that did not apply."""
    wanted = {int(s) for s in seqs}
    return [dict(i) for i in row.get("items") or [] if int(i.get("seq") or 0) in wanted]

