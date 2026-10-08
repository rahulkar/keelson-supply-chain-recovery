"""Tier-A per-op commit (``docs/contracts/110-m7-backoffice-generic.md`` §4, plan §10 "Commit
service"): a connector without change-set tools applies each mapped plan line with **one** tool
call, rendered from the connector's action mapping
(:class:`~keelson_contracts.mapping.ActionMapping`), and records it in the commit ledger
(:class:`~keelson_contracts.commit.CommitLedgerEntry`).

Rendering (:func:`render_template`) and read-back matching use the engine's safe mapping
evaluator (``keelson_engine.core.mapping_eval``, the same one the crawl and the admin dry-run
use) after the contract's :func:`~keelson_contracts.mapping.template_refs` validation: a string
that is exactly one ``{{ref}}`` takes the referenced value with its JSON type, other strings get
each placeholder's text, ``{"$json": …}`` becomes canonical JSON text. A ref only indexes dict keys
and list positions of the values (``payload``, ``target_ref``, ``expected_version``,
``request_id``, ``op``): no ``eval``, no attribute access, no Python object in reach.

The ledger state machine (:class:`PerOpCommitter`) is the contract's
:func:`~keelson_contracts.commit.ledger_next_action`, data-driven by the tool's confirmed
idempotency:

* ``enforced`` — the per-line request id (``<change-set idempotency key>:<seq>``, never the run id)
  is sent under ``request_id_arg``; an unknown outcome (timeout, transport error, transient tool
  error) is retried with the **same** id, at most :data:`~keelson_contracts.commit.MAX_AUTO_ATTEMPTS`
  attempts in all;
* ``best_effort`` — an unknown outcome is **read back** before any retry: a found effect settles
  the line (``verified``), else it is retried (with the same request id when the mapping sends
  one). A read-back is only valid for the attempt it followed (:attr:`LineRecord.readback_attempt`):
  every further retry needs a fresh one;
* ``none`` — never auto-retried: read back, and a line whose effect is not found becomes a
  guided step.

Before the first call of a ``best_effort`` / ``none`` line the read-back runs once as a
**baseline**: when it already matches (an existing entity looks like the line's effect), a later
match proves nothing, so it never settles the line (``in_doubt`` → a person checks) nor verifies it.
A baseline that could not be taken (the read-back failed) is treated the same way: without it, a
match cannot be told apart from an entity that existed before the call.

A definitive tool error (``version_conflict``, ``invalid…``) marks the line ``failed``: nothing
changed in the target system and the line becomes a guided step. ``attempted`` is persisted
**before** each call, so a crash between the call and its result is recovered from the ledger
like an ``in_doubt`` line (crash-retry re-runs ``execute``).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

import structlog
from keelson_contracts.commit import (
    MAX_AUTO_ATTEMPTS,
    CommitLedgerEntry,
    LedgerAction,
    LedgerReadback,
    ledger_next_action,
)
from keelson_contracts.connectors import Idempotency
from keelson_contracts.mapping import (
    ACTION_TEMPLATE_ROOTS,
    ActionMapping,
    ActionOpMapping,
    ExpressionError,
    Readback,
    RenderedCall,
    TemplateError,
    template_refs,
)

from .errors import McpToolError, McpTransportError

__all__ = [
    "LineRecord",
    "PerOpCommitter",
    "PerOpLine",
    "RenderError",
    "action_values",
    "ext_ref_of",
    "load_action_mapping",
    "per_op_lines",
    "readback_match",
    "render_call",
    "render_readback",
    "render_template",
    "request_id_for",
]

log = structlog.get_logger(__name__)

MAX_RENDERED_CHARS: Final = 200_000
"""Upper bound of one rendered call's JSON (a mapping can never blow up a payload)."""


class RenderError(ValueError):
    """A template could not be rendered for this line (missing value, wrong type, bad mapping)."""


# ─────────────────────────────── rendering ───────────────────────────────


def _eval() -> Any:
    """The engine's safe mapping evaluator (``keelson_engine.core.mapping_eval``: a hand-written
    interpreter over plain JSON). Imported lazily, like every platform module the runtime uses."""
    from keelson_engine.core import mapping_eval  # noqa: PLC0415

    return mapping_eval


def canonical_json(value: Any) -> str:
    return str(_eval().canonical_json(value))


def _context(values: Mapping[str, Any]) -> Any:
    ev = _eval()
    return ev.ActionContext(op=str(values.get("op") or ""), payload=dict(values.get("payload") or {}),
                            target_ref=values.get("target_ref"), expected_version=values.get("expected_version"),
                            request_id=values.get("request_id"))


def render_template(template: Any, values: Mapping[str, Any]) -> Any:
    """Render an argument template (see the module docstring) with the core evaluator after the
    contract's :func:`~keelson_contracts.mapping.template_refs` validation; :class:`RenderError`
    when a value is missing or has the wrong type, :class:`~keelson_contracts.mapping.TemplateError`
    for a malformed template."""
    template_refs(template, roots=ACTION_TEMPLATE_ROOTS)
    ev = _eval()
    try:
        out = ev.render_template(template, _context(values))
    except ev.MappingEvalError as exc:
        raise RenderError(str(exc)) from exc
    try:
        size = len(canonical_json(out))
    except (TypeError, ValueError) as exc:
        raise RenderError(f"the rendered arguments are not JSON: {exc}") from exc
    if size > MAX_RENDERED_CHARS:
        raise RenderError("the rendered arguments are too large")
    return out


def action_values(item: Mapping[str, Any], *, request_id: str | None) -> dict[str, Any]:
    """The placeholder roots of one change-set item (plain JSON: the payload is a deep copy)."""
    return {
        "payload": json.loads(json.dumps(dict(item.get("payload") or {}))),
        "target_ref": item.get("target_ref"),
        "expected_version": item.get("expected_version"),
        "request_id": request_id,
        "op": item.get("op"),
    }


def render_call(mapping: ActionOpMapping, item: Mapping[str, Any], *, request_id: str | None) -> RenderedCall:
    """The exact tool call for one line (``request_id`` goes under ``request_id_arg`` when the
    mapping names one)."""
    args = render_template(mapping.args, action_values(item, request_id=request_id))
    if not isinstance(args, dict):
        raise RenderError("the action arguments must render to an object")
    if mapping.request_id_arg is not None:
        if request_id is None:
            raise RenderError("this mapping sends a request id, but the line has none")
        args[mapping.request_id_arg] = request_id
    return RenderedCall(tool=mapping.tool, args=args)


def render_readback(mapping: ActionOpMapping, item: Mapping[str, Any], *, request_id: str | None) -> RenderedCall | None:
    if mapping.readback is None:
        return None
    args = render_template(mapping.readback.args, action_values(item, request_id=request_id))
    if not isinstance(args, dict):
        raise RenderError("the read-back arguments must render to an object")
    return RenderedCall(tool=mapping.readback.tool, args=args)


def readback_items(readback: Readback, result: Any) -> list[Any]:
    return list(_eval().readback_items(result, readback.items_path))


def readback_match(readback: Readback, result: Any, values: Mapping[str, Any]) -> tuple[int, bool]:
    """``(items found, some item matches every match path)`` (core evaluator)."""
    ev = _eval()
    items = readback_items(readback, result)
    template_refs(list(readback.match.values()), roots=ACTION_TEMPLATE_ROOTS)
    try:
        matched = bool(ev.readback_match(items, readback.match, _context(values)))
    except ev.MappingEvalError as exc:
        raise RenderError(str(exc)) from exc
    return len(items), matched


def request_id_for(idempotency_key: str, seq: int) -> str:
    """Per-line idempotency key: the change set's key + the line suffix (never the run id)."""
    return f"{idempotency_key}:{int(seq)}"


def ext_ref_of(result: Any) -> str | None:
    """What the target system returned for the line (created code, key or id)."""
    if not isinstance(result, Mapping):
        return None
    for key in ("code", "key", "id", "ref"):
        value = result.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)[:200]
    nested = result.get("item")
    if isinstance(nested, Mapping):
        return ext_ref_of(nested)
    return None


# ─────────────────────────────── lines + ledger ───────────────────────────────


@dataclass(frozen=True)
class PerOpLine:
    seq: int
    item: dict[str, Any]
    mapping: ActionOpMapping

    @property
    def op(self) -> str:
        return str(self.item["op"])


def per_op_lines(items: Sequence[Mapping[str, Any]], mapping: ActionMapping) -> tuple[list[PerOpLine], list[dict[str, Any]]]:
    """``(mapped lines, unmapped items)`` of a change set in ``seq`` order."""
    lines: list[PerOpLine] = []
    unmapped: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda i: int(i.get("seq") or 0)):
        m = mapping.for_op(str(item.get("op")))
        if m is None:
            unmapped.append(dict(item))
        else:
            lines.append(PerOpLine(seq=int(item["seq"]), item=dict(item), mapping=m))
    return lines, unmapped


@dataclass
class LineRecord:
    """The ledger entry of one line plus the attempt its last read-back followed."""

    entry: CommitLedgerEntry | None = None
    readback_attempt: int | None = None
    call: dict[str, Any] | None = None
    """The last rendered call (tool + arguments) — what was sent, for the audit and the tracker."""
    baseline_found: bool | None = None
    """The read-back already matched **before** the first call (``None``: not taken / could not
    tell). Such a read-back cannot tell this line's effect from an existing entity, so it never
    settles the line (no false ``verified``; an unknown outcome goes to a person)."""
    baseline_unknown: bool = False
    """The baseline read-back was attempted but could not tell (failed): a later match is just as
    ambiguous as one after a matching baseline."""

    def to_json(self) -> dict[str, Any]:
        return {"entry": self.entry.model_dump(mode="json") if self.entry is not None else None,
                "readback_attempt": self.readback_attempt, "call": self.call,
                "baseline_found": self.baseline_found, "baseline_unknown": self.baseline_unknown}

    @classmethod
    def from_json(cls, data: Any) -> LineRecord:
        if not isinstance(data, Mapping):
            return cls()
        raw = data.get("entry")
        entry = CommitLedgerEntry.model_validate(raw) if isinstance(raw, Mapping) else None
        attempt = data.get("readback_attempt")
        call = data.get("call")
        baseline = data.get("baseline_found")
        return cls(entry=entry, readback_attempt=int(attempt) if isinstance(attempt, int) else None,
                   call=dict(call) if isinstance(call, Mapping) else None,
                   baseline_found=baseline if isinstance(baseline, bool) else None,
                   baseline_unknown=data.get("baseline_unknown") is True)

    @property
    def settled(self) -> bool:
        return self.entry is not None and self.entry.state in ("succeeded", "verified")


Caller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
Persist = Callable[[int, LineRecord], Awaitable[None]]


def next_action(record: LineRecord) -> LedgerAction:
    """:func:`ledger_next_action` plus the stale read-back rule (see the module docstring): a
    ``best_effort`` retry needs a read-back taken after the latest attempt."""
    entry = record.entry
    assert entry is not None
    action = ledger_next_action(entry)
    if (action == "retry" and entry.idempotency is not Idempotency.ENFORCED
            and record.readback_attempt != entry.attempts):
        return "readback"
    return action


@dataclass
class PerOpCommitter:
    """Applies the lines of one per-op change set (see the module docstring). ``write`` calls the
    target system with the apply principal, ``read`` with the reader principal; ``persist`` stores
    a line's record (before every call and after every outcome)."""

    change_set_id: str
    connector: str
    idempotency_key: str
    write: Caller
    read: Caller
    persist: Persist
    clock: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(dt.UTC))

    def _entry(self, line: PerOpLine, **fields: Any) -> CommitLedgerEntry:
        mapping = line.mapping
        base = {
            "change_set_id": self.change_set_id, "line_seq": line.seq, "op": line.op, "connector": self.connector,
            "tool": mapping.tool, "idempotency": mapping.idempotency,
            "request_id": self.request_id(line), "updated_at": self.clock(),
        }
        return CommitLedgerEntry.model_validate({**base, **fields})

    def request_id(self, line: PerOpLine) -> str | None:
        """Sent when the mapping names a ``request_id_arg`` (required for ``enforced``)."""
        if line.mapping.request_id_arg is None and line.mapping.idempotency is not Idempotency.ENFORCED:
            return None
        return request_id_for(self.idempotency_key, line.seq)

    async def _attempt(self, line: PerOpLine, record: LineRecord, attempts: int) -> LineRecord:
        prior = record.entry
        call = render_call(line.mapping, line.item, request_id=self.request_id(line))
        attempted = self._entry(line, state="attempted", attempts=attempts,
                                readback=prior.readback if prior is not None else None,
                                ext_ref=prior.ext_ref if prior is not None else None)
        record = replace(record, entry=attempted, call={"tool": call.tool, "args": call.args})
        await self.persist(line.seq, record)  # before the call: a crash leaves it in doubt, never lost
        try:
            result = await self.write(call.tool, dict(call.args))
        except McpToolError as exc:
            if exc.transient:
                return await self._settle(line, record, state="in_doubt", error=f"{exc.code}: {exc.detail}")
            # a definitive refusal: nothing changed in the target system
            return await self._settle(line, record, state="failed", error=f"{exc.code}: {exc.detail}")
        except McpTransportError as exc:
            return await self._settle(line, record, state="in_doubt", error=f"transport: {type(exc).__name__}")
        return await self._settle(line, record, state="succeeded", ext_ref=ext_ref_of(result))

    async def _settle(self, line: PerOpLine, record: LineRecord, *, state: str, error: str | None = None,
                      ext_ref: str | None = None) -> LineRecord:
        assert record.entry is not None
        entry = record.entry
        update: dict[str, Any] = {"state": state, "updated_at": self.clock(),
                                  "error": (error or None) and error[:500]}
        if ext_ref is not None:
            update["ext_ref"] = ext_ref
        if state == "in_doubt" and entry.readback is not None and entry.readback.found:
            update["readback"] = None
        settled = CommitLedgerEntry.model_validate({**entry.model_dump(mode="python"), **update})
        out = replace(record, entry=settled)
        await self.persist(line.seq, out)
        return out

    async def read_back(self, line: PerOpLine, record: LineRecord) -> LedgerReadback:
        """Run the line's read-back query (read principal); ``ok=False`` when it cannot say."""
        try:
            call = render_readback(line.mapping, line.item, request_id=self.request_id(line))
        except (RenderError, TemplateError, ExpressionError) as exc:
            return LedgerReadback(at=self.clock(), ok=False, found=False, detail=f"render: {str(exc)[:200]}")
        if call is None:
            return LedgerReadback(at=self.clock(), ok=False, found=False, detail="no read-back configured")
        try:
            result = await self.read(call.tool, dict(call.args))
        except (McpToolError, McpTransportError) as exc:
            return LedgerReadback(at=self.clock(), ok=False, found=False, detail=f"read-back failed: {type(exc).__name__}")
        assert line.mapping.readback is not None
        values = action_values(line.item, request_id=self.request_id(line))
        try:
            count, matched = readback_match(line.mapping.readback, result, values)
        except (RenderError, TemplateError, ExpressionError) as exc:
            return LedgerReadback(at=self.clock(), ok=False, found=False, detail=f"match: {str(exc)[:200]}")
        return LedgerReadback(at=self.clock(), ok=True, found=matched,
                              detail=f"{count} item(s) read back; {'match' if matched else 'no match'}")

    def _ambiguous(self, rb: LedgerReadback, record: LineRecord) -> LedgerReadback:
        """A match the read-back also found before the first call says nothing about this call."""
        if rb.ok and rb.found and record.baseline_found:
            return LedgerReadback(at=rb.at, ok=False, found=False,
                                  detail="the read-back matched before the call too, so it cannot tell "
                                         "whether this call applied")
        if rb.ok and rb.found and record.baseline_unknown:
            return LedgerReadback(at=rb.at, ok=False, found=False,
                                  detail="the read-back before the call failed, so a match cannot tell "
                                         "whether this call applied")
        return rb

    async def _readback(self, line: PerOpLine, record: LineRecord) -> LineRecord:
        assert record.entry is not None
        rb = self._ambiguous(await self.read_back(line, record), record)
        entry = record.entry
        data = {**entry.model_dump(mode="python"), "readback": rb, "updated_at": self.clock()}
        if rb.found:
            data.update(state="verified", error=None)
        out = replace(record, entry=CommitLedgerEntry.model_validate(data), readback_attempt=entry.attempts)
        await self.persist(line.seq, out)
        return out

    async def apply(self, line: PerOpLine, record: LineRecord | None = None) -> LineRecord:
        """Drive one line to a resting state: ``succeeded`` / ``verified`` (done), ``failed`` or a
        line that may not be retried (guide), or ``in_doubt`` whose read-back cannot tell (manual
        check, also guided)."""
        record = record or LineRecord()
        if record.entry is None:
            if line.mapping.idempotency is not Idempotency.ENFORCED and line.mapping.readback is not None:
                # baseline: does the read-back already match before anything is sent?
                base = await self.read_back(line, record)
                record = replace(record, baseline_found=base.found if base.ok else None,
                                 baseline_unknown=not base.ok)
            record = await self._attempt(line, record, attempts=1)
        for _ in range(2 * MAX_AUTO_ATTEMPTS + 2):
            action = next_action(record)
            if action in ("none", "guide"):
                return record
            assert record.entry is not None
            if action == "retry":
                log.info("per-op line retried", change_set_id=self.change_set_id, seq=line.seq,
                         attempt=record.entry.attempts + 1, idempotency=record.entry.idempotency.value)
                record = await self._attempt(line, record, attempts=record.entry.attempts + 1)
                continue
            before = record.readback_attempt
            record = await self._readback(line, record)
            assert record.entry is not None
            if record.entry.readback is not None and not record.entry.readback.ok:
                # the read-back cannot tell: stop here (a person checks; never a blind retry)
                return record
            if before == record.readback_attempt and not record.settled:
                return record
        return record

    async def verify(self, line: PerOpLine, record: LineRecord) -> LineRecord:
        """Reconcile: read back a ``succeeded`` line (``verified`` when its effect is visible)."""
        if record.entry is None or record.entry.state != "succeeded" or line.mapping.readback is None:
            return record
        rb = self._ambiguous(await self.read_back(line, record), record)
        if not rb.found:
            return record  # stays succeeded (the target system confirmed the call itself)
        data = {**record.entry.model_dump(mode="python"), "readback": rb, "state": "verified",
                "updated_at": self.clock()}
        out = replace(record, entry=CommitLedgerEntry.model_validate(data), readback_attempt=record.entry.attempts)
        await self.persist(line.seq, out)
        return out


async def load_action_mapping(deps: Any, *, tenant: str, connector: str) -> ActionMapping | None:
    """The connector's current action mapping (``ConnectorsPort.get_action_mapping``; ``None``
    when the port or the mapping is missing, or the stored mapping does not validate)."""
    getter = getattr(deps.connectors, "get_action_mapping", None)
    if not callable(getter):
        return None
    try:
        raw = await getter(tenant=tenant, connector=connector)
    except LookupError:
        return None
    if raw is None or isinstance(raw, ActionMapping):
        return raw
    try:
        return ActionMapping.model_validate(raw)
    except ValueError as exc:
        log.warning("stored action mapping is invalid; its ops are guided", connector=connector,
                    error=str(exc)[:300])
        return None
