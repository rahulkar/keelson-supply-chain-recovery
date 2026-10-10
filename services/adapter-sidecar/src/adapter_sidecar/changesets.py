"""Tier W change-set facade: create → add items → validate (preview + ``H``) → apply (atomic, with a
Keelson approval receipt) → rollback (compensating change set). Merxa-compatible wire shapes and
error codes (contracts 20 / 22 / 95), so Keelson's commit path works unchanged (contract 122 §5).

Every public coroutine takes the caller's :class:`~adapter_sidecar.auth.Principal` (and an
idempotency key where the contract requires one) and returns the JSON the MCP tool returns.
All change-set state mutations and every backend write run under the sidecar's write lock.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from adapter_sidecar import idempotency, journal
from adapter_sidecar.auth import Principal
from adapter_sidecar.backends import Backend, SourceChanged, Stmt, run_statements
from adapter_sidecar.config import (
    SCOPE_APPLY,
    SCOPE_APPLY_AUTO,
    VIEW_HEADER_COLUMNS,
    VIEW_LINE_COLUMNS,
    SidecarConfig,
)
from adapter_sidecar.db import begin_write, iso, jdump, jload, jsonable, utcnow
from adapter_sidecar.errors import Problem, not_found
from adapter_sidecar.logs import get_logger
from adapter_sidecar.primitives import Effect, Executor, ItemResult, ItemSpec, StepFailure, key_text
from adapter_sidecar.receipts import ReceiptContext, ReceiptVerifier, VerifiedReceipt, check_autocommit_caps
from adapter_sidecar.settings import Settings
from adapter_sidecar.templates import parse_ref, render, resolve
from keelson_contracts.changeset import CHANGE_SET_ITEM_ADAPTER, change_set_hash

log = get_logger(__name__)

MAX_ITEMS = 500
PG_INT_MAX = 2_147_483_647
EDITABLE = ("draft", "validated")
VIA_KEELSON = "Keelson"
VIA_MCP = "MCP"
CS_SEQUENCE = "change_set"

_CS_COLUMNS = ("id, code, title, status, origin, items_count, preview, preview_hash, approval_ref, receipt_jti, "
               "approved_by, approver_name, initiated_by, co_approvers, via, created_by, applied_by, rollback_of, "
               "rolled_back_by, result, created_at, updated_at, validated_at, applied_at, version")


@dataclass(frozen=True, slots=True)
class Outcome:
    status_code: int
    body: dict[str, Any]


class _Conflict(Exception):
    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__("conflict")
        self.conflicts = conflicts


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any("\x00" in str(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


def _int_out_of_range(value: Any) -> bool:
    """An integer outside ±``PG_INT_MAX`` anywhere in ``value`` (Merxa's rule: every payload integer
    fits a Postgres ``int`` column, whatever the backend)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return not -PG_INT_MAX <= value <= PG_INT_MAX
    if isinstance(value, dict):
        return any(_int_out_of_range(v) for v in value.values())
    if isinstance(value, list):
        return any(_int_out_of_range(v) for v in value)
    return False


def normalize_items(items: Any) -> list[dict[str, Any]]:
    """Structural + typed validation at add time (``keelson_contracts.changeset``: typed ops, no
    floats, ISO dates; integers within the Postgres ``int`` range); semantic checks happen in
    validate."""
    if not isinstance(items, list) or not items:
        raise Problem(400, "invalid_items", "items must be a non-empty array")
    if len(items) > MAX_ITEMS:
        raise Problem(400, "invalid_items", f"at most {MAX_ITEMS} items per request")
    out: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise Problem(400, "invalid_items", f"items[{i}] must be an object")
        extra = set(it) - {"op", "target_ref", "expected_version", "payload"}
        if extra:
            raise Problem(400, "invalid_items", f"items[{i}] has unknown fields {sorted(extra)}")
        item = {"op": it.get("op"), "target_ref": it.get("target_ref"), "expected_version": it.get("expected_version"),
                "payload": it.get("payload") if it.get("payload") is not None else {}}
        if _has_nul(item):
            raise Problem(400, "invalid_items", f"items[{i}] contains a NUL character")
        if _int_out_of_range(item["payload"]) or _int_out_of_range(item["expected_version"]):
            raise Problem(400, "invalid_items", f"items[{i}] contains an integer outside ±{PG_INT_MAX}")
        try:
            CHANGE_SET_ITEM_ADAPTER.validate_python({"seq": 1, **item})
        except ValidationError as exc:
            errs = exc.errors()[:3]
            detail = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ())[1:])}: {e.get('msg')}" for e in errs)
            raise Problem(400, "invalid_items", f"items[{i}]: {detail}") from None
        out.append(item)
    return out


def _names(people: Sequence[str]) -> str:
    return people[0] if len(people) == 1 else ", ".join(people[:-1]) + " and " + people[-1]


def attribution_text(cs: Mapping[str, Any]) -> str | None:
    """Merxa's formats: "Changed by Marco Rossi via Keelson (APR-…), co-approved by Lena Vogel",
    "Rolled back by … via Keelson (APR-…)" or "Applied via Keelson auto-policy <rule>@<v>, initiated by …"."""
    if cs.get("status") not in ("applied", "rolled_back"):
        return None
    approved_by = str(cs.get("approved_by") or "")
    via = cs.get("via") or VIA_MCP
    if approved_by.startswith("policy:"):
        return f"Applied via {via} auto-policy {approved_by.removeprefix('policy:')}, initiated by " \
               f"{cs.get('initiated_by') or 'unknown'}"
    actor = cs.get("approver_name") or approved_by.removeprefix("user:") or "unknown"
    verb = "Rolled back by" if cs.get("rollback_of") else "Changed by"
    ref = cs.get("approval_ref")
    out = f"{verb} {actor} via {via}" + (f" ({ref})" if ref else "")
    co = [str(c.get("name") or c.get("ref")) for c in (cs.get("co_approvers") or []) if isinstance(c, dict)]
    return out + (f", co-approved by {_names(co)}" if co else "")


class ChangeSetService:
    def __init__(self, *, cfg: SidecarConfig, settings: Settings, state: AsyncEngine, backend: Backend,
                 lock: asyncio.Lock, receipts: ReceiptVerifier,
                 sync_sources: Callable[[], Awaitable[list[str]]]) -> None:
        self.cfg = cfg
        self.settings = settings
        self.state = state
        self.backend = backend
        self.lock = lock
        self.receipts = receipts
        self.sync_sources = sync_sources

    # ───────────────────────────── reads ─────────────────────────────

    @staticmethod
    async def _resolve(conn: AsyncConnection, ref: str) -> Any:
        try:
            cond, value = "id = :r", str(uuid.UUID(str(ref)))
        except ValueError:
            cond, value = "code = :r", str(ref)
        row = (await conn.execute(text(f"SELECT {_CS_COLUMNS} FROM change_set WHERE {cond}"), {"r": value})).first()
        if row is None:
            raise not_found("change set", str(ref))
        return row

    @staticmethod
    async def _items(conn: AsyncConnection, cs_id: str) -> list[Any]:
        return list(await conn.execute(
            text("SELECT seq, op, target_ref, expected_version, payload, before, after, status, error, result_ref "
                 "FROM change_set_item WHERE change_set_id = :c ORDER BY seq"), {"c": cs_id}))

    async def _load(self, ref: str) -> tuple[Any, list[Any]]:
        async with self.state.connect() as conn:
            cs = await self._resolve(conn, ref)
            return cs, await self._items(conn, cs.id)

    def _render(self, cs: Any, items: Sequence[Any], rollback_code: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "change_set_id": cs.id, "code": cs.code, "title": cs.title, "status": cs.status,
            "origin": jload(cs.origin) or {}, "items_count": cs.items_count, "preview_hash": cs.preview_hash,
            "approval_ref": cs.approval_ref, "approved_by": cs.approved_by, "approver_name": cs.approver_name,
            "initiated_by": cs.initiated_by, "co_approvers": jload(cs.co_approvers), "via": cs.via,
            "applied_by": {"client_id": cs.applied_by}, "rollback_of": rollback_code,
            "rolled_back_by": cs.rolled_back_by, "result": jload(cs.result), "created_at": cs.created_at,
            "validated_at": cs.validated_at, "applied_at": cs.applied_at, "version": cs.version,
            "items": [
                {"seq": it.seq, "op": it.op, "target_ref": it.target_ref, "expected_version": it.expected_version,
                 "payload": jload(it.payload), "before": jload(it.before), "after": jload(it.after),
                 "status": it.status, "error": it.error, "result_ref": it.result_ref}
                for it in items
            ],
            "links": {"ui": None},
        }
        out["attribution_text"] = attribution_text(out)
        return out

    async def get(self, ref: str) -> dict[str, Any]:
        async with self.state.connect() as conn:
            cs = await self._resolve(conn, ref)
            items = await self._items(conn, cs.id)
            rb = None
            if cs.rollback_of:
                rb = (await conn.execute(text("SELECT code FROM change_set WHERE id = :i"),
                                         {"i": cs.rollback_of})).scalar_one_or_none()
        return self._render(cs, items, rb)

    async def audit(self, *, change_set_code: str | None, entity_type: str | None, entity_ref: str | None,
                    limit: int) -> dict[str, Any]:
        clauses, params = [], {"lim": max(1, min(int(limit), 1000))}
        for col, value in (("change_set_code", change_set_code), ("entity_type", entity_type),
                           ("entity_ref", entity_ref)):
            if value:
                clauses.append(f"{col} = :{col}")
                params[col] = value
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.state.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT id, at, change_set_code, entity_type, entity_ref, action, before, after, client_id, via, "
                f"approval_ref, approved_by, approver_name, initiated_by, co_approved_by FROM audit_log {where} "
                "ORDER BY id DESC LIMIT :lim"), params)).all()
        return {"items": [
            {"id": r.id, "at": r.at, "change_set_code": r.change_set_code, "entity_type": r.entity_type,
             "entity_ref": r.entity_ref, "action": r.action, "before": jload(r.before), "after": jload(r.after),
             "client_id": r.client_id, "via": r.via, "approval_ref": r.approval_ref, "approved_by": r.approved_by,
             "approver_name": r.approver_name, "initiated_by": r.initiated_by,
             "co_approved_by": jload(r.co_approved_by)}
            for r in rows
        ]}

    async def transfer_orders(self, *, status: str | None, from_dc: str | None, to_dc: str | None) -> dict[str, Any]:
        view = self.cfg.views.transfer_orders
        if view is None:
            raise Problem(404, "not_supported", "this system has no transfer orders view")
        async with self.backend.read() as conn:
            sql = (f"SELECT {', '.join(VIEW_HEADER_COLUMNS)} FROM ({view.header_sql}) h "
                   "WHERE (CAST(:status AS TEXT) IS NULL OR h.status = CAST(:status AS TEXT)) "
                   "AND (CAST(:from_dc AS TEXT) IS NULL OR h.from_dc = CAST(:from_dc AS TEXT)) "
                   "AND (CAST(:to_dc AS TEXT) IS NULL OR h.to_dc = CAST(:to_dc AS TEXT)) ORDER BY h.code LIMIT 1000")
            headers = (await conn.execute(text(sql), {"status": status, "from_dc": from_dc, "to_dc": to_dc})).all()
            codes = [h.code for h in headers]
            lines: dict[str, list[dict[str, Any]]] = {c: [] for c in codes}
            if codes:
                stmt = text(f"SELECT {', '.join(VIEW_LINE_COLUMNS)} FROM ({view.lines_sql}) l WHERE l.code IN :codes "
                            "ORDER BY l.code, l.line_no").bindparams(bindparam("codes", expanding=True))
                for ln in (await conn.execute(stmt, {"codes": codes})).all():
                    lines[ln.code].append({"line_no": ln.line_no, "sku": ln.sku, "qty": ln.qty})
        return {"items": [
            {**{c: jsonable(getattr(h, c)) for c in VIEW_HEADER_COLUMNS}, "lines": lines[h.code]} for h in headers
        ]}

    # ───────────────────────────── idempotent wrapper ─────────────────────────────

    async def _idempotent(self, principal: Principal, key: str, operation: str, body: Any,
                          run: Callable[[idempotency.Claim], Awaitable[Outcome]]) -> Outcome:
        claim = await idempotency.begin(self.state, principal=principal.client_id, key=key,
                                        request_sha256=idempotency.request_fingerprint(operation, body),
                                        stale_after_s=self.settings.idempotency_stale_s)
        if isinstance(claim, idempotency.Replay):
            return Outcome(claim.status_code, claim.response)
        try:
            return await run(claim)
        except BaseException:
            await idempotency.abandon(self.state, claim)
            raise

    @staticmethod
    def _audit(*, code: str | None, entity_type: str, entity_ref: str, action: str, before: Any, after: Any,
               principal: Principal, via: str, receipt: VerifiedReceipt | None = None,
               approval_ref: str | None = None) -> Stmt:
        return (
            "INSERT INTO audit_log (at, change_set_code, entity_type, entity_ref, action, before, after, client_id, "
            "via, approval_ref, approved_by, approver_name, initiated_by, co_approved_by) VALUES (:at, :cs, :et, :er, "
            ":a, :b, :af, :cl, :via, :ar, :ab, :an, :ib, :co)",
            {"at": iso(utcnow()), "cs": code, "et": entity_type, "er": entity_ref, "a": action, "b": jdump(before),
             "af": jdump(after), "cl": principal.client_id, "via": via, "ar": approval_ref,
             "ab": receipt.approved_by if receipt else None, "an": receipt.approver_name if receipt else None,
             "ib": receipt.initiated_by if receipt else None,
             "co": jdump(list(receipt.co_approved_by)) if receipt and receipt.co_approved_by else None},
        )

    # ───────────────────────────── create / add ─────────────────────────────

    async def create(self, principal: Principal, *, title: str, origin: Mapping[str, Any] | None,
                     idempotency_key: str) -> dict[str, Any]:
        title = (title or "").strip()
        if not title or len(title) > 200 or "\x00" in title:
            raise Problem(400, "invalid_title", "title must be 1..200 characters")
        origin = dict(origin or {})
        if _has_nul(origin) or not _no_floats(origin):
            raise Problem(400, "invalid_origin", "origin holds integers, strings and booleans only (no floats, no NUL)")
        body = {"title": title, "origin": origin}

        async def run(claim: idempotency.Claim) -> Outcome:
            from adapter_sidecar.state import next_value

            async with self.lock, begin_write(self.state) as conn:
                n = await next_value(conn, CS_SEQUENCE)
                cs_id, code, now = str(uuid.uuid7()), f"CS-{n:04d}", iso(utcnow())
                response = {"change_set_id": cs_id, "code": code, "status": "draft"}
                await run_statements(conn, [
                    ("INSERT INTO change_set (id, code, title, status, origin, created_by, created_at, updated_at) "
                     "VALUES (:i, :c, :t, 'draft', :o, :by, :now, :now)",
                     {"i": cs_id, "c": code, "t": title, "o": jdump(origin), "by": principal.client_id, "now": now}),
                    self._audit(code=code, entity_type="change_set", entity_ref=code, action="create", before=None,
                                after=body, principal=principal, via=VIA_MCP),
                    idempotency.complete_statement(claim, 201, response),
                ])
            return Outcome(201, response)

        return (await self._idempotent(principal, idempotency_key, "change_set.create", body, run)).body

    async def add_items(self, principal: Principal, ref: str, *, items: Any, idempotency_key: str) -> dict[str, Any]:
        normalized = normalize_items(items)
        body = {"change_set": str(ref), "items": normalized}

        async def run(claim: idempotency.Claim) -> Outcome:
            async with self.lock, begin_write(self.state) as conn:
                cs = await self._resolve(conn, ref)
                if cs.status not in EDITABLE:
                    raise Problem(409, "invalid_state",
                                  f"items can only be added while draft/validated (status {cs.status})")
                if cs.items_count + len(normalized) > MAX_ITEMS:
                    raise Problem(400, "invalid_items", f"a change set holds at most {MAX_ITEMS} items")
                start, total, now = cs.items_count, cs.items_count + len(normalized), iso(utcnow())
                response = {"change_set_id": cs.id, "items_count": total}
                stmts: list[Stmt] = [
                    ("INSERT INTO change_set_item (change_set_id, seq, op, target_ref, expected_version, payload, "
                     "status) VALUES (:c, :s, :op, :t, :v, :p, 'pending')",
                     {"c": cs.id, "s": start + i, "op": it["op"], "t": it["target_ref"], "v": it["expected_version"],
                      "p": jdump(it["payload"])})
                    for i, it in enumerate(normalized, start=1)
                ]
                stmts += [
                    # any edit invalidates a previous validation
                    ("UPDATE change_set_item SET status = 'pending', before = NULL, after = NULL, error = NULL "
                     "WHERE change_set_id = :c", {"c": cs.id}),
                    ("UPDATE change_set SET items_count = :n, status = 'draft', preview = NULL, preview_hash = NULL, "
                     "validated_at = NULL, updated_at = :now, version = version + 1 WHERE id = :c",
                     {"c": cs.id, "n": total, "now": now}),
                    self._audit(code=cs.code, entity_type="change_set", entity_ref=cs.code, action="add_items",
                                before=None, after={"added": normalized, "items_count": total}, principal=principal,
                                via=VIA_MCP),
                    idempotency.complete_statement(claim, 200, response),
                ]
                await run_statements(conn, stmts)
            return Outcome(200, response)

        return (await self._idempotent(principal, idempotency_key, "change_set.add_items", body, run)).body

    # ───────────────────────────── validate ─────────────────────────────

    async def _code_floors(self, conn: AsyncConnection | None = None) -> dict[str, int]:
        sql = text("SELECT name, value FROM sequence WHERE name LIKE 'code:%'")
        if conn is not None:
            rows = (await conn.execute(sql)).all()
        else:
            async with self.state.connect() as c:
                rows = (await c.execute(sql)).all()
        return {str(r[0]): int(r[1]) for r in rows}

    @staticmethod
    def _specs(items: Sequence[Any], *, with_before: bool) -> list[ItemSpec]:
        return [ItemSpec(int(it.seq), str(it.op), it.target_ref, it.expected_version, dict(jload(it.payload) or {}),
                         jload(it.before) if with_before else None) for it in items]

    async def _sync(self) -> None:
        from adapter_sidecar.errors import SourceDataError

        try:
            await self.sync_sources()
        except SourceDataError as exc:
            raise Problem(503, "source_invalid", str(exc)) from None

    async def validate(self, principal: Principal, ref: str, *, idempotency_key: str) -> dict[str, Any]:
        body = {"change_set": str(ref)}

        async def run(claim: idempotency.Claim) -> Outcome:
            async with self.lock:
                await self._sync()
                cs, items = await self._load(ref)
                if cs.status not in EDITABLE:
                    raise Problem(409, "invalid_state",
                                  f"only draft/validated change sets can be validated (status {cs.status})")
                if not items:
                    raise Problem(409, "empty_change_set", "change set has no items")
                if not self.cfg.writable:
                    raise Problem(409, "read_only", "this system accepts no changes")
                async with self.backend.transaction() as w:
                    floors = await self._code_floors(w.conn if self.backend.shared_state else None)
                    ex = Executor(self.cfg, w, mode="validate", change_set={"id": cs.id, "code": cs.code,
                                                                             "title": cs.title}, code_floors=floors)
                    results = [await ex.run_item(spec) for spec in self._specs(items, with_before=False)]
                    await w.rollback()
                h = change_set_hash([{"seq": it.seq, "op": it.op, "target_ref": it.target_ref,
                                      "expected_version": it.expected_version, "payload": jload(it.payload)}
                                     for it in items])
                ok = all(r.ok for r in results)
                status = "validated" if ok else "invalid"
                totals = {"lines": len(items), "units_moved": sum(r.units for r in results),
                          "value_cents": sum(r.value_cents for r in results)}
                response = {
                    "change_set_id": cs.id, "code": cs.code, "status": status, "preview_hash": h,
                    "items": [{"seq": r.seq, "op": r.op, "target_ref": r.target_ref, "before": r.before,
                               "after": r.after, "ok": r.ok, "errors": r.errors} for r in results],
                    "totals": totals, "atomic": True,
                }
                now = iso(utcnow())
                stmts: list[Stmt] = [
                    ("UPDATE change_set_item SET before = :b, after = :a, status = :s, error = :e "
                     "WHERE change_set_id = :c AND seq = :q",
                     {"c": cs.id, "q": r.seq, "b": jdump(r.before), "a": jdump(r.after), "s": "ok" if r.ok else "error",
                      "e": "; ".join(r.errors) or None})
                    for r in results
                ]
                stmts += [
                    ("UPDATE change_set SET status = :s, preview = :p, preview_hash = :h, validated_at = :va, "
                     "updated_at = :now, version = version + 1 WHERE id = :c",
                     {"c": cs.id, "s": "validated" if ok else "draft", "p": jdump(response), "h": h if ok else None,
                      "va": now if ok else None, "now": now}),
                    self._audit(code=cs.code, entity_type="change_set", entity_ref=cs.code, action="validate",
                                before=None, after={"status": status, "preview_hash": h, "totals": totals},
                                principal=principal, via=VIA_MCP),
                    idempotency.complete_statement(claim, 200, response),
                ]
                async with begin_write(self.state) as conn:
                    await run_statements(conn, stmts)
            return Outcome(200, response)

        return (await self._idempotent(principal, idempotency_key, "change_set.validate", body, run)).body

    # ───────────────────────────── apply ─────────────────────────────

    @staticmethod
    def _check_ready(cs: Any, items: Sequence[Any], preview_hash: str) -> None:
        if cs.status == "applied":
            raise Problem(409, "already_applied", f"change set {cs.code} is already applied")
        if cs.status != "validated":
            raise Problem(409, "invalid_state", f"change set must be validated before apply (status {cs.status})")
        current = change_set_hash([{"seq": it.seq, "op": it.op, "target_ref": it.target_ref,
                                    "expected_version": it.expected_version, "payload": jload(it.payload)}
                                   for it in items])
        if preview_hash != cs.preview_hash or preview_hash != current:
            raise Problem(409, "preview_hash_mismatch", "preview_hash does not match the validated change set")

    async def apply(self, principal: Principal, ref: str, *, preview_hash: str, approval_ref: str,
                    approval_receipt: str, idempotency_key: str) -> dict[str, Any]:
        if not principal.has_any(SCOPE_APPLY, SCOPE_APPLY_AUTO):
            raise Problem(403, "insufficient_scope", f"requires scope {SCOPE_APPLY} or {SCOPE_APPLY_AUTO}")
        if not approval_ref:
            raise Problem(400, "invalid_parameter", "approval_ref is required")
        # The receipt is a credential, not part of the request identity: a re-minted receipt for
        # the same approval replays instead of tripping the reuse check.
        body = {"change_set": str(ref), "preview_hash": preview_hash, "approval_ref": approval_ref}

        async def run(claim: idempotency.Claim) -> Outcome:
            cs, items = await self._load(ref)
            self._check_ready(cs, items, preview_hash)
            receipt = await self.receipts.verify(approval_receipt, ReceiptContext(
                change_set_id=cs.id, change_set_code=cs.code, preview_hash=preview_hash, approval_ref=approval_ref,
                scopes=principal.scopes))
            if receipt.is_policy:
                totals = (jload(cs.preview) or {}).get("totals") or {}
                check_autocommit_caps(self.cfg.receipts.autocommit, {it.op for it in items}, len(items),
                                      int(totals.get("units_moved") or 0))
            return await self._apply_locked(principal, cs.id, preview_hash, approval_ref, receipt, claim)

        return (await self._idempotent(principal, idempotency_key, "change_set.apply", body, run)).body

    async def _apply_locked(self, principal: Principal, cs_id: str, preview_hash: str, approval_ref: str,
                            receipt: VerifiedReceipt, claim: idempotency.Claim) -> Outcome:
        async with self.lock:
            await self._sync()
            cs, items = await self._load(cs_id)
            self._check_ready(cs, items, preview_hash)  # re-check under the lock
            await self._check_jti(receipt.jti)
            results: list[ItemResult] = []
            try:
                async with self.backend.transaction() as w:
                    floors = await self._code_floors(w.conn if self.backend.shared_state else None)
                    ex = Executor(self.cfg, w, mode="apply",
                                  change_set={"id": cs.id, "code": cs.code, "title": cs.title}, code_floors=floors)
                    results = [await ex.run_item(spec) for spec in self._specs(items, with_before=True)]
                    failed = [r.conflict() for r in results if not r.ok]
                    if failed:
                        raise _Conflict(failed)
                    response = {"status": "applied", "applied": len(results), "audit_ref": f"audit:{cs.code}",
                                "change_set_code": cs.code, "change_set_id": cs.id}
                    stmts = self._apply_statements(principal, cs, results, receipt, approval_ref, ex.codes, response)
                    stmts.append(idempotency.complete_statement(claim, 200, response))
                    await w.commit(change_set_id=cs.id, purpose="apply", statements=stmts,
                                   marker={"id": cs.id, "preview_hash": preview_hash})
            except _Conflict as exc:
                return await self._record_conflict(principal, cs, exc.conflicts, receipt, approval_ref, claim,
                                                   action="apply_conflict")
            except SourceChanged as exc:
                touched = set(exc.tables)
                conflicts = [{"seq": r.seq, "target_ref": r.target_ref, "expected_version": r.expected_version,
                              "actual_version": None, "reason": "source_changed"}
                             for r in results if any(e.table in touched for e in r.effects)]
                return await self._record_conflict(principal, cs, conflicts, receipt, approval_ref, claim,
                                                   action="apply_conflict")
        log.info("change set applied", extra={"change_set": cs.code, "items": len(results),
                                              "approved_by": receipt.approved_by})
        return Outcome(200, response)

    async def _check_jti(self, jti: str) -> None:
        async with self.state.connect() as conn:
            used = (await conn.execute(text("SELECT change_set_id FROM used_receipt WHERE jti = :j"),
                                       {"j": jti})).first()
        if used is not None:
            raise Problem(403, "invalid_receipt", "approval receipt was already used", extra={"reason": "replayed_jti"})

    def _effect_statements(self, cs_id: str, code: str, effects: Sequence[Effect], principal: Principal,
                           receipt: VerifiedReceipt, approval_ref: str, now: str) -> list[Stmt]:
        stmts: list[Stmt] = []
        for n, eff in enumerate(effects, start=1):
            table = self.cfg.tables[eff.table]
            stmts.append((
                "INSERT INTO change_set_effect (change_set_id, n, item_seq, kind, tbl, key, before, after) "
                "VALUES (:c, :n, :s, :k, :t, :key, :b, :a)",
                {"c": cs_id, "n": n, "s": eff.item_seq, "k": eff.kind, "t": eff.table, "key": jdump(eff.key),
                 "b": jdump(eff.before), "a": jdump(eff.after)},
            ))
            stmts.append(self._audit(code=code, entity_type=eff.table, entity_ref=key_text(table, eff.key),
                                     action=eff.kind, before=eff.before, after=eff.after, principal=principal,
                                     via=VIA_KEELSON, receipt=receipt, approval_ref=approval_ref))
        stmts += journal.change_statements(self.cfg, [e.to_dict() for e in effects], change_set_code=code, at=now)
        return stmts

    @staticmethod
    def _sequence_statements(codes: Mapping[str, int]) -> list[Stmt]:
        return [("INSERT INTO sequence (name, value) VALUES (:n, :v) ON CONFLICT (name) DO UPDATE SET "
                 "value = max(value, :v)", {"n": name, "v": value}) for name, value in codes.items()]

    def _apply_statements(self, principal: Principal, cs: Any, results: Sequence[ItemResult],
                          receipt: VerifiedReceipt, approval_ref: str, codes: Mapping[str, int],
                          response: dict[str, Any]) -> list[Stmt]:
        now = iso(utcnow())
        effects = [e for r in results for e in r.effects]
        co = receipt.co_approvers or None
        stmts: list[Stmt] = [
            ("UPDATE change_set_item SET status = 'applied', error = NULL, result_ref = :r, after = :a "
             "WHERE change_set_id = :c AND seq = :s",
             {"c": cs.id, "s": r.seq, "r": r.result_ref, "a": jdump(r.after)})
            for r in results
        ]
        stmts += self._effect_statements(cs.id, cs.code, effects, principal, receipt, approval_ref, now)
        after = {"status": "applied", "items": len(results), "preview_hash": cs.preview_hash}
        if co:
            after["co_approvers"] = co
        stmts += [
            self._audit(code=cs.code, entity_type="change_set", entity_ref=cs.code, action="apply",
                        before={"status": "validated"}, after=after, principal=principal, via=VIA_KEELSON,
                        receipt=receipt, approval_ref=approval_ref),
            ("UPDATE change_set SET status = 'applied', applied_at = :now, approval_ref = :ar, receipt_jti = :j, "
             "approved_by = :ab, approver_name = :an, initiated_by = :ib, co_approvers = :co, via = :via, "
             "applied_by = :cl, result = :res, updated_at = :now, version = version + 1 WHERE id = :c",
             {"c": cs.id, "now": now, "ar": approval_ref, "j": receipt.jti, "ab": receipt.approved_by,
              "an": receipt.approver_name, "ib": receipt.initiated_by, "co": jdump(co), "via": VIA_KEELSON,
              "cl": principal.client_id, "res": jdump({"status": "applied", "applied": len(results)})}),
            ("INSERT INTO used_receipt (jti, purpose, change_set_id, used_at) VALUES (:j, 'apply', :c, :now)",
             {"j": receipt.jti, "c": cs.id, "now": now}),
        ]
        stmts += self._sequence_statements(codes)
        return stmts

    async def _record_conflict(self, principal: Principal, cs: Any, conflicts: list[dict[str, Any]],
                               receipt: VerifiedReceipt, approval_ref: str, claim: idempotency.Claim, *,
                               action: str) -> Outcome:
        response = {"status": "conflict", "conflicts": conflicts, "change_set_code": cs.code, "change_set_id": cs.id}
        now = iso(utcnow())
        stmts: list[Stmt] = []
        if action == "apply_conflict":
            stmts += [("UPDATE change_set_item SET status = 'error', error = :e WHERE change_set_id = :c AND seq = :s",
                       {"c": cs.id, "s": c["seq"], "e": c["reason"]}) for c in conflicts]
            stmts.append(("UPDATE change_set SET status = 'conflict', result = :r, updated_at = :now, "
                          "version = version + 1 WHERE id = :c", {"c": cs.id, "r": jdump(response), "now": now}))
        stmts += [
            self._audit(code=cs.code, entity_type="change_set", entity_ref=cs.code, action=action,
                        before={"status": cs.status}, after={"conflicts": conflicts}, principal=principal,
                        via=VIA_KEELSON, receipt=receipt, approval_ref=approval_ref),
            idempotency.complete_statement(claim, 409, response),
        ]
        async with begin_write(self.state) as conn:
            await run_statements(conn, stmts)
        log.info("change set conflict", extra={"change_set": cs.code, "action": action, "conflicts": len(conflicts)})
        return Outcome(409, response)

    # ───────────────────────────── rollback ─────────────────────────────

    async def rollback(self, principal: Principal, ref: str, *, approval_ref: str, approval_receipt: str,
                       idempotency_key: str) -> dict[str, Any]:
        """Undo an applied change set with a compensating change set, applied atomically. The receipt
        binds to the **original** change set (its id/code and ``preview_hash``), must be a user
        approval with its own ``approval_ref``, and four-eyes when the original was co-approved."""
        if not principal.has_any(SCOPE_APPLY):
            raise Problem(403, "insufficient_scope", f"requires scope {SCOPE_APPLY}")
        if not approval_ref:
            raise Problem(400, "invalid_parameter", "approval_ref is required")
        body = {"change_set": str(ref), "approval_ref": approval_ref, "op": "rollback"}

        async def run(claim: idempotency.Claim) -> Outcome:
            cs, _items = await self._load(ref)
            if cs.status == "rolled_back":
                raise Problem(409, "already_rolled_back", f"change set {cs.code} is already rolled back")
            if cs.status != "applied" or cs.rollback_of is not None:
                raise Problem(409, "invalid_state", f"only applied change sets can be rolled back (status {cs.status})")
            if approval_ref == cs.approval_ref:
                raise Problem(403, "invalid_receipt", "a rollback needs its own approval, not the one that applied "
                              "the change set", extra={"reason": "approval_reused"})
            receipt = await self.receipts.verify(approval_receipt, ReceiptContext(
                change_set_id=cs.id, change_set_code=cs.code, preview_hash=str(cs.preview_hash),
                approval_ref=approval_ref, scopes=principal.scopes))
            if receipt.is_policy:
                raise Problem(403, "rollback_requires_user_approval", "a rollback needs a user approval, not a policy")
            if jload(cs.co_approvers) and not receipt.co_approved_by:
                raise Problem(403, "invalid_receipt", f"change set {cs.code} was co-approved (four-eyes); its "
                              "rollback needs co-approvers too", extra={"reason": "co_approval_required"})
            return await self._rollback_locked(principal, cs.id, approval_ref, receipt, claim)

        return (await self._idempotent(principal, idempotency_key, "change_set.rollback", body, run)).body

    async def _rollback_locked(self, principal: Principal, cs_id: str, approval_ref: str,
                               receipt: VerifiedReceipt, claim: idempotency.Claim) -> Outcome:
        from adapter_sidecar.state import next_value

        async with self.lock:
            await self._sync()
            cs, _items = await self._load(cs_id)
            if cs.status != "applied":
                raise Problem(409, "invalid_state", f"change set {cs.code} is {cs.status}")
            await self._check_jti(receipt.jti)
            async with self.state.connect() as conn:
                effects = [Effect.from_dict({"item_seq": r.item_seq, "kind": r.kind, "table": r.tbl,
                                             "key": jload(r.key), "before": jload(r.before),
                                             "after": jload(r.after)})
                           for r in (await conn.execute(text(
                               "SELECT item_seq, kind, tbl, key, before, after FROM change_set_effect "
                               "WHERE change_set_id = :c ORDER BY n"), {"c": cs.id})).all()]
            if not effects:
                raise Problem(409, "not_reversible", f"change set {cs.code} recorded no effects to undo")
            async with begin_write(self.state) as conn:
                n = await next_value(conn, CS_SEQUENCE)
            new_id, new_code = str(uuid.uuid7()), f"CS-{n:04d}"
            done: list[Effect] = []
            try:
                async with self.backend.transaction() as w:
                    ex = Executor(self.cfg, w, mode="apply", change_set={"id": new_id, "code": new_code,
                                                                          "title": f"Rollback of {cs.code}"})
                    try:
                        done, conflicts = await ex.revert(effects)
                    except StepFailure as exc:
                        if exc.code == "not_reversible":
                            raise Problem(409, "not_reversible", f"change set {cs.code} cannot be rolled back: "
                                          f"{exc.detail}") from None
                        conflicts = [{"seq": 0, "target_ref": None, "expected_version": None, "actual_version": None,
                                      "reason": exc.code}]
                    if conflicts:
                        raise _Conflict(conflicts)
                    response = {"status": "rolled_back", "change_set_id": cs.id, "change_set_code": cs.code,
                                "compensating_change_set_id": new_id, "compensating_code": new_code,
                                "applied": len(done), "audit_ref": f"audit:{new_code}"}
                    stmts = self._rollback_statements(principal, cs, new_id, new_code, effects, done, receipt,
                                                      approval_ref)
                    stmts.append(idempotency.complete_statement(claim, 200, response))
                    await w.commit(change_set_id=new_id, purpose="rollback", statements=stmts,
                                   marker={"id": new_id, "preview_hash": cs.preview_hash})
            except _Conflict as exc:
                return await self._record_conflict(principal, cs, exc.conflicts, receipt, approval_ref, claim,
                                                   action="rollback_conflict")
            except SourceChanged as exc:
                conflicts = [{"seq": e.item_seq, "target_ref": e.table, "expected_version": None,
                              "actual_version": None, "reason": "source_changed"}
                             for e in effects if e.table in set(exc.tables)]
                return await self._record_conflict(principal, cs, conflicts, receipt, approval_ref, claim,
                                                   action="rollback_conflict")
        log.info("change set rolled back", extra={"change_set": cs.code, "compensating": new_code})
        return Outcome(200, response)

    def _rollback_statements(self, principal: Principal, cs: Any, new_id: str, new_code: str,
                             original: Sequence[Effect], done: Sequence[Effect], receipt: VerifiedReceipt,
                             approval_ref: str) -> list[Stmt]:
        now = iso(utcnow())
        items: list[dict[str, Any]] = []
        for seq, eff in enumerate(reversed(original), start=1):
            table = self.cfg.tables[eff.table]
            vcol = table.version_col
            items.append({"seq": seq, "op": "revert.insert" if eff.kind == "insert" else "revert.update",
                          "target_ref": f"{eff.table}:{key_text(table, eff.key)}",
                          "expected_version": (eff.after or {}).get(vcol) if vcol else None,
                          "payload": {"table": eff.table, "key": eff.key}})
        h = change_set_hash(items)
        co = receipt.co_approvers or None
        stmts: list[Stmt] = [
            ("INSERT INTO change_set (id, code, title, status, origin, items_count, preview, preview_hash, "
             "approval_ref, receipt_jti, approved_by, approver_name, initiated_by, co_approvers, via, created_by, "
             "applied_by, rollback_of, result, created_at, updated_at, validated_at, applied_at) VALUES (:i, :code, "
             ":title, "
             "'applied', :origin, :n, :preview, :h, :ar, :j, :ab, :an, :ib, :co, :via, :cl, :cl, :orig, :res, :now, "
             ":now, :now, :now)",
             {"i": new_id, "code": new_code, "title": f"Rollback of {cs.code}",
              "origin": jdump({"system": self.cfg.system.name, "rollback_of": cs.code}), "n": len(items),
              "preview": jdump({"status": "validated", "preview_hash": h,
                                "totals": {"lines": len(items), "units_moved": 0, "value_cents": 0}}),
              "h": h, "ar": approval_ref, "j": receipt.jti, "ab": receipt.approved_by, "an": receipt.approver_name,
              "ib": receipt.initiated_by, "co": jdump(co), "via": VIA_KEELSON, "cl": principal.client_id,
              "orig": cs.id, "res": jdump({"status": "applied", "applied": len(items)}), "now": now}),
        ]
        stmts += [
            ("INSERT INTO change_set_item (change_set_id, seq, op, target_ref, expected_version, payload, before, "
             "after, status, result_ref) VALUES (:c, :s, :op, :t, :v, :p, :b, :a, 'applied', :t)",
             {"c": new_id, "s": it["seq"], "op": it["op"], "t": it["target_ref"], "v": it["expected_version"],
              "p": jdump(it["payload"]), "b": jdump(eff.after), "a": jdump(nxt.after)})
            for it, eff, nxt in zip(items, reversed(original), done, strict=True)
        ]
        stmts += self._effect_statements(new_id, new_code, done, principal, receipt, approval_ref, now)
        stmts += [
            ("UPDATE change_set SET status = 'rolled_back', rolled_back_by = :code, updated_at = :now, "
             "version = version + 1 WHERE id = :c", {"c": cs.id, "code": new_code, "now": now}),
            self._audit(code=cs.code, entity_type="change_set", entity_ref=cs.code, action="rollback",
                        before={"status": "applied"}, after={"status": "rolled_back", "by": new_code},
                        principal=principal, via=VIA_KEELSON, receipt=receipt, approval_ref=approval_ref),
            self._audit(code=new_code, entity_type="change_set", entity_ref=new_code, action="apply", before=None,
                        after={"status": "applied", "rollback_of": cs.code, "items": len(items)}, principal=principal,
                        via=VIA_KEELSON, receipt=receipt, approval_ref=approval_ref),
            ("INSERT INTO used_receipt (jti, purpose, change_set_id, used_at) VALUES (:j, 'rollback', :c, :now)",
             {"j": receipt.jti, "c": cs.id, "now": now}),
        ]
        return stmts

    # ───────────────────────────── operator guide ─────────────────────────────

    async def operator_guide(self, ref: str, locale: str = "en") -> dict[str, Any]:
        """Manual steps for a change set (Guide mode, contract 95 §2), rendered from the op's
        ``guide`` templates; ops without one get a generic step."""
        cs = await self.get(ref)
        steps: list[dict[str, Any]] = []
        for it in cs["items"]:
            payload = it["payload"] if isinstance(it["payload"], dict) else {}
            scope: dict[str, Any] = {"payload": payload, "target_ref": it["target_ref"],
                                     "expected_version": it["expected_version"], "op": it["op"],
                                     "change_set": {"id": cs["change_set_id"], "code": cs["code"],
                                                    "title": cs["title"]},
                                     "result_ref": it.get("result_ref")}
            spec = self.cfg.ops.get(it["op"])
            guide = spec.guide if spec is not None else None
            if guide is None:
                title = f"{it['op']} {it['target_ref'] or ''}".strip()
                instructions = (f"Carry out {it['op']} in {self.cfg.system.name}"
                                + (f" for {it['target_ref']}" if it["target_ref"] else "")
                                + f" with these values: {jdump(payload)}.")
                link = expected = None
            else:
                if guide.list is not None:
                    elements = resolve(parse_ref(guide.list.for_each), scope)
                    parts = [render(guide.list.item, {**scope, "item": el, "index": i})
                             for i, el in enumerate(elements if isinstance(elements, list) else [], start=1)]
                    scope["list"] = guide.list.sep.join(str(p) for p in parts if p is not None)
                title = str(render(guide.title, scope) or it["op"])
                instructions = str(render(guide.instructions, scope) or "")
                link = render(guide.deep_link, scope) if guide.deep_link else None
                expected = render(guide.expected, scope) if guide.expected else None
            steps.append({"seq": len(steps) + 1, "item_seq": it["seq"], "op": it["op"], "target_ref": it["target_ref"],
                          "title": title, "instructions": instructions,
                          "deep_link": link if isinstance(link, str) and link else None, "expected": expected})
        return {"change_set_id": cs["change_set_id"], "code": cs["code"], "locale": "en", "steps": steps}


def _no_floats(value: Any) -> bool:
    if isinstance(value, float):
        return False
    if isinstance(value, dict):
        return all(_no_floats(v) for v in value.values())
    if isinstance(value, list):
        return all(_no_floats(v) for v in value)
    return True
