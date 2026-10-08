"""Change-set engine: create → add items → validate (preview + H) → apply (atomic).

Shared by REST (``/v1/change-sets``) and MCP write tools. All public coroutines take a
:class:`Principal` and an idempotency key where the contract requires one, and return
JSON-ready dicts (exactly what both transports return to clients).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from merxa_api import tables as t
from merxa_api.canonical import preview_hash as compute_preview_hash
from merxa_api.config import Settings
from merxa_api.errors import Problem, not_found
from merxa_api.logs import get_logger
from merxa_api.security.principal import (
    ROLE_OPS_DIRECTOR,
    SCOPE_APPLY,
    SCOPE_APPLY_AUTO,
    Principal,
)
from merxa_api.security.receipts import ApplyContext as ReceiptContext
from merxa_api.security.receipts import ReceiptVerifier, VerifiedReceipt
from merxa_api.services import audit, events, idempotency
from merxa_api.services.audit import Attribution
from merxa_api.services.catalog import sim_today
from merxa_api.services.common import decode_cursor, encode_cursor, jsonable, row_dict
from merxa_api.services.ops import (
    HANDLERS,
    PUBLIC_OPS,
    ApplyContext,
    Conflict,
    ConflictInfo,
    ItemPreview,
    ItemSpec,
    NotReversible,
    Simulation,
)

log = get_logger(__name__)

MAX_ITEMS = 500
AUTO_ALLOWED_OPS = frozenset({"transfer_order.create", "allocation.set_qty"})
AUTO_MAX_ITEMS = 50
AUTO_MAX_UNITS = 5000
EDITABLE_STATUSES = ("draft", "validated")


@dataclass(frozen=True, slots=True)
class Outcome:
    """A service result + the HTTP status REST should use (MCP ignores the status)."""

    status_code: int
    body: dict[str, Any]
    replayed: bool = False


# Every quantity/version column is a Postgres ``int``; anything outside that range must be
# rejected up front (an out-of-range value would otherwise surface as a driver error -> 500
# at apply time, after the change set had been validated and approved).
PG_INT_MAX = 2_147_483_647
_MAX_DEPTH = 8


def _json_value_problem(value: Any, depth: int = 0) -> str | None:
    """Why ``value`` is not acceptable in a change-set payload/origin (``None`` if fine).

    Contract (00-overview): integers + strings + ISO dates only - no floats. Integers must
    fit a Postgres ``int``; strings must not contain NUL (not storable in ``jsonb``).
    """
    if depth > _MAX_DEPTH:
        return "is nested too deeply"
    if isinstance(value, float):
        return "must contain integers, strings and ISO dates only (no floats)"
    if isinstance(value, int) and not isinstance(value, bool) and not -PG_INT_MAX <= value <= PG_INT_MAX:
        return f"contains an integer outside ±{PG_INT_MAX}"
    if isinstance(value, str) and "\x00" in value:
        return "contains a NUL character"
    if isinstance(value, dict):
        for k, v in value.items():
            if "\x00" in str(k):
                return "contains a NUL character"
            if (why := _json_value_problem(v, depth + 1)) is not None:
                return why
    if isinstance(value, list):
        for v in value:
            if (why := _json_value_problem(v, depth + 1)) is not None:
                return why
    return None


def normalize_items(items: Any) -> list[dict[str, Any]]:
    """Structural validation at add time (semantic checks happen in validate)."""
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
        op = it.get("op")
        if not isinstance(op, str) or not op or len(op) > 64:
            raise Problem(400, "invalid_items", f"items[{i}].op must be a non-empty string")
        target_ref = it.get("target_ref")
        if target_ref is not None and (
            not isinstance(target_ref, str) or len(target_ref) > 200 or "\x00" in target_ref
        ):
            raise Problem(400, "invalid_items", f"items[{i}].target_ref must be a string or null")
        ev = it.get("expected_version")
        if ev is not None and (isinstance(ev, bool) or not isinstance(ev, int) or not 1 <= ev <= PG_INT_MAX):
            raise Problem(400, "invalid_items", f"items[{i}].expected_version must be a positive integer or null")
        payload = it.get("payload", {})
        if not isinstance(payload, dict):
            raise Problem(400, "invalid_items", f"items[{i}].payload must be an object")
        if (why := _json_value_problem(payload)) is not None:
            raise Problem(400, "invalid_items", f"items[{i}].payload {why}")
        out.append({"op": op, "target_ref": target_ref, "expected_version": ev, "payload": payload})
    return out


def _names(people: list[str]) -> str:
    return people[0] if len(people) == 1 else ", ".join(people[:-1]) + " and " + people[-1]


def co_approver_names(value: Any) -> list[str]:
    """Display names from a stored ``co_approvers`` value (``[{ref, name}]``)."""
    if not isinstance(value, list):
        return []
    out = []
    for entry in value:
        if isinstance(entry, dict):
            name = entry.get("name") or str(entry.get("ref") or "").removeprefix("user:")
            if name:
                out.append(str(name))
    return out


def attribution_text(cs: dict[str, Any]) -> str | None:
    """ "Changed by Marco Rossi via Keelson (APR-0001), co-approved by Lena Vogel",
    "Rolled back by … via Keelson (APR-…)" (compensating sets) or
    "Applied via Keelson auto-policy <rule>@<version>, initiated by Priya Shah"."""
    if cs.get("status") not in ("applied", "rolled_back"):
        return None
    approved_by = cs.get("approved_by") or ""
    via = cs.get("via") or "API"
    if approved_by.startswith("policy:"):
        rule = approved_by.removeprefix("policy:")
        return f"Applied via {via} auto-policy {rule}, initiated by {cs.get('initiated_by') or 'unknown'}"
    actor = cs.get("applied_by_display") or cs.get("applied_by_username") or "unknown"
    ref = cs.get("approval_ref")
    verb = "Rolled back by" if cs.get("rollback_of") else "Changed by"
    text = f"{verb} {actor} via {via}" + (f" ({ref})" if ref else "")
    co = co_approver_names(cs.get("co_approvers"))
    return text + (f", co-approved by {_names(co)}" if co else "")


class ChangeSetService:
    def __init__(self, engine: AsyncEngine, settings: Settings, receipts: ReceiptVerifier) -> None:
        self.engine = engine
        self.settings = settings
        self.receipts = receipts

    # ───────────── lookups ─────────────
    @staticmethod
    async def _resolve(conn: AsyncConnection, ref: str, *, lock: bool = False) -> Any:
        cs = t.change_set
        try:
            cond = cs.c.id == uuid.UUID(str(ref))
        except ValueError:
            cond = cs.c.code == str(ref)
        q = select(cs).where(cond)
        if lock:
            q = q.with_for_update()
        row = (await conn.execute(q)).first()
        if row is None:
            raise not_found("change set", str(ref))
        return row

    @staticmethod
    async def _items(conn: AsyncConnection, cs_id: Any) -> list[Any]:
        ci = t.change_set_item
        return list(await conn.execute(select(ci).where(ci.c.change_set_id == cs_id).order_by(ci.c.seq)))

    async def get(self, ref: str) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            row = await self._resolve(conn, ref)
            items = await self._items(conn, row.id)
        return self._render(row, items)

    def _render(self, row: Any, items: list[Any]) -> dict[str, Any]:
        cs = row_dict(row, exclude=("updated_at", "idempotency_key"))
        cs["change_set_id"] = cs.pop("id")
        cs["items"] = [
            {
                "seq": it.seq,
                "op": it.op,
                "target_ref": it.target_ref,
                "expected_version": it.expected_version,
                "payload": jsonable(it.payload),
                "before": jsonable(it.before),
                "after": jsonable(it.after),
                "status": it.status,
                "error": it.error,
            }
            for it in items
        ]
        cs["applied_by"] = {
            "sub": cs.get("applied_by_sub"),
            "username": cs.get("applied_by_username"),
            "display": cs.get("applied_by_display"),
            "client_id": cs.get("applied_via_client"),
        }
        cs["attribution_text"] = attribution_text(cs)
        cs["links"] = {"ui": f"{self.settings.public_url}/change-batches/{cs['code']}"}
        return cs

    async def list(self, *, limit: int, cursor: str | None, status: str | None = None) -> dict[str, Any]:
        cs = t.change_set
        q = select(
            cs.c.id.label("change_set_id"),
            cs.c.code,
            cs.c.title,
            cs.c.status,
            cs.c.items_count,
            cs.c.preview_hash,
            cs.c.approval_ref,
            cs.c.approved_by,
            cs.c.initiated_by,
            cs.c.applied_by_display,
            cs.c.applied_by_username,
            cs.c.via,
            cs.c.co_approvers,
            cs.c.rollback_of,
            cs.c.created_at,
            cs.c.validated_at,
            cs.c.applied_at,
            cs.c.version,
            cs.c.id,
        )
        if status:
            q = q.where(cs.c.status == status)
        rows, nxt = await self._paginate_desc(q, limit, cursor)
        items = []
        for r in rows:
            d = row_dict(r, exclude=("id",))
            d["attribution_text"] = attribution_text(d)
            items.append(d)
        return {"items": items, "next_cursor": nxt}

    async def _paginate_desc(self, q: Any, limit: int, cursor: str | None) -> tuple[list[Any], str | None]:
        """Newest first (uuidv7 ids are time ordered), keyset on id DESC."""
        cs = t.change_set
        if cursor:
            (last,) = decode_cursor(cursor, 1)
            try:
                q = q.where(cs.c.id < uuid.UUID(str(last)))
            except ValueError as exc:
                raise Problem(400, "invalid_cursor", "cursor is malformed") from exc
        async with self.engine.connect() as conn:
            rows = list(await conn.execute(q.order_by(cs.c.id.desc()).limit(limit + 1)))
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            nxt = encode_cursor([rows[-1].id])
        return rows, nxt

    # ───────────── idempotent wrapper ─────────────
    async def _idempotent(self, principal: Principal, key: str | None, operation: str, body: Any, run: Any) -> Outcome:
        fingerprint = idempotency.request_fingerprint(operation, body)
        claim = await idempotency.begin(
            self.engine,
            principal=principal.sub,
            key=idempotency.validate_key(key),
            request_sha256=fingerprint,
            stale_after_s=self.settings.idempotency_stale_in_flight_s,
        )
        if isinstance(claim, idempotency.Replay):
            return Outcome(claim.status_code, claim.response, replayed=True)
        try:
            return await run(claim)
        except BaseException:
            await idempotency.abandon(self.engine, claim)
            raise

    # ───────────── create ─────────────
    async def create(
        self, principal: Principal, *, title: str, origin: dict[str, Any] | None, idempotency_key: str | None
    ) -> Outcome:
        title = (title or "").strip()
        if not title or len(title) > 200 or "\x00" in title:
            raise Problem(400, "invalid_title", "title must be 1..200 characters")
        origin = origin or {}
        if not isinstance(origin, dict):
            raise Problem(400, "invalid_origin", "origin must be an object")
        if (why := _json_value_problem(origin)) is not None:
            raise Problem(400, "invalid_origin", f"origin {why}")
        body = {"title": title, "origin": origin}

        async def run(claim: idempotency.Claim) -> Outcome:
            async with self.engine.begin() as conn:
                n = (await conn.execute(text("SELECT nextval('merxa.change_set_code_seq')"))).scalar_one()
                code = f"CS-{n:04d}"
                row = (
                    await conn.execute(
                        t.change_set.insert()
                        .values(
                            code=code,
                            title=title,
                            origin=jsonable(origin),
                            status="draft",
                            idempotency_key=claim.key,
                            created_by_client=principal.client_id,
                            created_by_sub=principal.sub,
                        )
                        .returning(t.change_set.c.id, t.change_set.c.version)
                    )
                ).one()
                response = {"change_set_id": str(row.id), "code": code, "status": "draft", "version": row.version}
                await audit.write(
                    conn,
                    Attribution.of(principal, change_set_code=code),
                    entity_type="change_set",
                    entity_ref=code,
                    action="create",
                    after={"title": title, "origin": origin},
                )
                await idempotency.complete(conn, claim, 201, response)
            return Outcome(201, response)

        return await self._idempotent(principal, idempotency_key, "change_set.create", body, run)

    # ───────────── add items ─────────────
    async def add_items(self, principal: Principal, ref: str, *, items: Any, idempotency_key: str | None) -> Outcome:
        normalized = normalize_items(items)
        body = {"change_set": str(ref), "items": normalized}

        async def run(claim: idempotency.Claim) -> Outcome:
            async with self.engine.begin() as conn:
                cs = await self._resolve(conn, ref, lock=True)
                if cs.status not in EDITABLE_STATUSES:
                    raise Problem(
                        409, "invalid_state", f"items can only be added while draft/validated (status {cs.status})"
                    )
                if cs.items_count + len(normalized) > MAX_ITEMS:
                    raise Problem(400, "invalid_items", f"a change set holds at most {MAX_ITEMS} items")
                start = cs.items_count
                await conn.execute(
                    t.change_set_item.insert(),
                    [
                        {"change_set_id": cs.id, "seq": start + i, **it, "status": "pending"}
                        for i, it in enumerate(normalized, start=1)
                    ],
                )
                # Any edit invalidates a previous validation.
                await conn.execute(
                    update(t.change_set_item)
                    .where(t.change_set_item.c.change_set_id == cs.id)
                    .values(status="pending", before=None, after=None, error=None)
                )
                total = start + len(normalized)
                await conn.execute(
                    update(t.change_set)
                    .where(t.change_set.c.id == cs.id)
                    .values(items_count=total, status="draft", preview=None, preview_hash=None, validated_at=None)
                )
                response = {"change_set_id": str(cs.id), "items_count": total}
                await audit.write(
                    conn,
                    Attribution.of(principal, change_set_code=cs.code),
                    entity_type="change_set",
                    entity_ref=cs.code,
                    action="add_items",
                    after={"added": normalized, "items_count": total},
                )
                await idempotency.complete(conn, claim, 200, response)
            return Outcome(200, response)

        return await self._idempotent(principal, idempotency_key, "change_set.add_items", body, run)

    # ───────────── validate ─────────────
    async def validate(self, principal: Principal, ref: str, *, idempotency_key: str | None) -> Outcome:
        body = {"change_set": str(ref)}

        async def run(claim: idempotency.Claim) -> Outcome:
            async with self.engine.begin() as conn:
                cs = await self._resolve(conn, ref, lock=True)
                if cs.status not in EDITABLE_STATUSES:
                    raise Problem(
                        409, "invalid_state", f"only draft/validated change sets can be validated (status {cs.status})"
                    )
                items = await self._items(conn, cs.id)
                if not items:
                    raise Problem(409, "empty_change_set", "change set has no items")
                today = await sim_today(conn)
                sim = Simulation(today=today)
                out_items: list[dict[str, Any]] = []
                units = value = 0
                all_ok = True
                for it in items:
                    spec = ItemSpec(it.seq, it.op, it.target_ref, it.expected_version, dict(it.payload or {}))
                    handler = HANDLERS.get(it.op) if it.op in PUBLIC_OPS or cs.rollback_of else None
                    if handler is None:
                        prev = ItemPreview(errors=["unsupported_op"])
                    else:
                        prev = await handler.preview(conn, spec, sim)
                    all_ok = all_ok and prev.ok
                    units += prev.units_moved
                    value += prev.value_cents
                    out_items.append(
                        {
                            "seq": it.seq,
                            "op": it.op,
                            "target_ref": it.target_ref,
                            "before": jsonable(prev.before),
                            "after": jsonable(prev.after),
                            "ok": prev.ok,
                            "errors": prev.errors,
                        }
                    )
                    await conn.execute(
                        update(t.change_set_item)
                        .where(t.change_set_item.c.id == it.id)
                        .values(
                            before=jsonable(prev.before),
                            after=jsonable(prev.after),
                            status="ok" if prev.ok else "error",
                            error="; ".join(prev.errors) or None,
                        )
                    )
                h = compute_preview_hash(
                    {
                        "seq": it.seq,
                        "op": it.op,
                        "target_ref": it.target_ref,
                        "expected_version": it.expected_version,
                        "payload": it.payload or {},
                    }
                    for it in items
                )
                status = "validated" if all_ok else "invalid"
                response = {
                    "change_set_id": str(cs.id),
                    "code": cs.code,
                    "status": status,
                    "preview_hash": h,
                    "items": out_items,
                    "totals": {"lines": len(items), "units_moved": units, "value_cents": value},
                }
                await conn.execute(
                    update(t.change_set)
                    .where(t.change_set.c.id == cs.id)
                    .values(
                        status="validated" if all_ok else "draft",
                        preview=response,
                        preview_hash=h if all_ok else None,
                        validated_at=func.now() if all_ok else None,
                    )
                )
                await audit.write(
                    conn,
                    Attribution.of(principal, change_set_code=cs.code),
                    entity_type="change_set",
                    entity_ref=cs.code,
                    action="validate",
                    after={"status": status, "preview_hash": h, "totals": response["totals"]},
                )
                await idempotency.complete(conn, claim, 200, response)
            return Outcome(200, response)

        return await self._idempotent(principal, idempotency_key, "change_set.validate", body, run)

    # ───────────── apply ─────────────
    async def apply(
        self,
        principal: Principal,
        ref: str,
        *,
        preview_hash: str,
        approval_ref: str,
        approval_receipt: str,
        idempotency_key: str | None,
    ) -> Outcome:
        if not principal.has_any_scope((SCOPE_APPLY, SCOPE_APPLY_AUTO)):
            raise Problem(403, "insufficient_scope", f"requires scope {SCOPE_APPLY} or {SCOPE_APPLY_AUTO}")
        if not isinstance(preview_hash, str) or not preview_hash:
            raise Problem(400, "invalid_parameter", "preview_hash is required")
        if not isinstance(approval_ref, str) or not approval_ref:
            raise Problem(400, "invalid_parameter", "approval_ref is required")
        # The receipt is a credential, not part of the request identity: a re-minted
        # receipt for the same approval must replay, not trip the 422 reuse check.
        body = {"change_set": str(ref), "preview_hash": preview_hash, "approval_ref": approval_ref}

        async def run(claim: idempotency.Claim) -> Outcome:
            async with self.engine.connect() as conn:
                cs = await self._resolve(conn, ref)
                items = await self._items(conn, cs.id)
            self._check_ready(cs, items, preview_hash)
            receipt = await self.receipts.verify(
                approval_receipt,
                ReceiptContext(
                    change_set_id=str(cs.id),
                    change_set_code=cs.code,
                    preview_hash=preview_hash,
                    approval_ref=approval_ref,
                    principal=principal,
                ),
            )
            if receipt.is_policy:
                self._check_auto_caps(cs, items)
            elif not principal.has_role(ROLE_OPS_DIRECTOR):  # defence in depth (also in receipt check)
                raise Problem(403, "missing_role", f"requires role {ROLE_OPS_DIRECTOR}")
            return await self._apply_locked(principal, cs.id, preview_hash, approval_ref, receipt, claim)

        return await self._idempotent(principal, idempotency_key, "change_set.apply", body, run)

    @staticmethod
    def _check_ready(cs: Any, items: list[Any], preview_hash: str) -> None:
        if cs.status == "applied":
            raise Problem(409, "already_applied", f"change set {cs.code} is already applied")
        if cs.status != "validated":
            raise Problem(409, "invalid_state", f"change set must be validated before apply (status {cs.status})")
        current = compute_preview_hash(
            {
                "seq": it.seq,
                "op": it.op,
                "target_ref": it.target_ref,
                "expected_version": it.expected_version,
                "payload": it.payload or {},
            }
            for it in items
        )
        if preview_hash != cs.preview_hash or preview_hash != current:
            raise Problem(409, "preview_hash_mismatch", "preview_hash does not match the validated change set")

    @staticmethod
    def _check_auto_caps(cs: Any, items: list[Any]) -> None:
        ops = {it.op for it in items}
        units = int(((cs.preview or {}).get("totals") or {}).get("units_moved", 0))
        reasons = []
        if not ops <= AUTO_ALLOWED_OPS:
            reasons.append(f"ops {sorted(ops - AUTO_ALLOWED_OPS)} are not allowed for auto-commit")
        if len(items) > AUTO_MAX_ITEMS:
            reasons.append(f"{len(items)} items exceeds the cap of {AUTO_MAX_ITEMS}")
        if units > AUTO_MAX_UNITS:
            reasons.append(f"{units} units exceeds the cap of {AUTO_MAX_UNITS}")
        if reasons:
            raise Problem(
                403,
                "auto_cap_exceeded",
                "; ".join(reasons),
                extra={
                    "caps": {"ops": sorted(AUTO_ALLOWED_OPS), "max_items": AUTO_MAX_ITEMS, "max_units": AUTO_MAX_UNITS}
                },
            )

    async def _apply_locked(
        self,
        principal: Principal,
        cs_id: Any,
        preview_hash: str,
        approval_ref: str,
        receipt: VerifiedReceipt,
        claim: idempotency.Claim,
    ) -> Outcome:
        attribution = Attribution.of(
            principal,
            approval_ref=approval_ref,
            approved_by=receipt.approved_by,
            initiated_by=receipt.initiated_by,
            co_approved_by=receipt.co_approved_by or None,
        )
        co_approvers = receipt.co_approvers or None
        conflicts: list[ConflictInfo] = []
        cs_code = ""
        try:
            async with self.engine.begin() as conn:
                cs = await self._resolve(conn, str(cs_id), lock=True)
                cs_code = cs.code
                attribution = replace(attribution, change_set_code=cs.code)
                items = await self._items(conn, cs.id)
                self._check_ready(cs, items, preview_hash)  # re-check under lock
                used = (
                    await conn.execute(
                        select(t.change_set.c.code).where(
                            t.change_set.c.receipt_jti == receipt.jti, t.change_set.c.id != cs.id
                        )
                    )
                ).first()
                if used is not None:
                    raise Problem(
                        403, "invalid_receipt", "approval receipt was already used", extra={"reason": "replayed_jti"}
                    )
                specs = [
                    ItemSpec(it.seq, it.op, it.target_ref, it.expected_version, dict(it.payload or {})) for it in items
                ]

                # Phase 1: lock every target and collect *all* conflicts.
                sim = Simulation(today=await sim_today(conn))
                for spec in specs:
                    handler = HANDLERS.get(spec.op) if spec.op in PUBLIC_OPS else None
                    if handler is None:
                        conflicts.append(
                            ConflictInfo(spec.seq, spec.target_ref, spec.expected_version, None, "unsupported_op")
                        )
                        continue
                    info = await handler.check(conn, spec, sim)
                    if info is not None:
                        conflicts.append(info)
                if conflicts:
                    raise _AbortApply()

                # Phase 2: mutate, all-or-nothing.
                ctx = ApplyContext(change_set_code=cs.code, today=sim.today)
                audit_rows: list[tuple[str, str, str, Any, Any]] = []
                for spec in specs:
                    entries = await HANDLERS[spec.op].apply(conn, spec, ctx)
                    audit_rows.extend(entries)
                    await conn.execute(
                        update(t.change_set_item)
                        .where(t.change_set_item.c.change_set_id == cs.id, t.change_set_item.c.seq == spec.seq)
                        .values(status="applied", error=None, result=jsonable(ctx.results.get(spec.seq)))
                    )
                applied_after: dict[str, Any] = {"status": "applied", "items": len(specs), "preview_hash": preview_hash}
                if co_approvers:
                    applied_after["co_approvers"] = co_approvers
                audit_rows.append(("change_set", cs.code, "apply", {"status": "validated"}, applied_after))
                await audit.write_many(conn, attribution, audit_rows)
                await conn.execute(
                    update(t.change_set)
                    .where(t.change_set.c.id == cs.id)
                    .values(
                        status="applied",
                        applied_at=func.now(),
                        approval_ref=approval_ref,
                        receipt_jti=receipt.jti,
                        approved_by=receipt.approved_by,
                        initiated_by=receipt.initiated_by,
                        applied_by_sub=principal.sub,
                        applied_by_username=principal.username,
                        applied_by_display=principal.actor_display,
                        applied_via_client=principal.client_id,
                        via=principal.via,
                        co_approvers=co_approvers,
                        result={"status": "applied", "applied": len(specs)},
                    )
                )
                response = {
                    "status": "applied",
                    "applied": len(specs),
                    "audit_ref": f"{self.settings.public_url}/v1/audit-log?change_set_code={cs.code}",
                    "change_set_code": cs.code,
                    "change_set_id": str(cs.id),
                }
                await emit_entity_events(conn, audit_rows, cs.code)
                await events.emit(
                    conn,
                    "change_set.applied",
                    {
                        "change_set_id": str(cs.id),
                        "code": cs.code,
                        "applied": len(specs),
                        "ops": sorted({sp.op for sp in specs}),
                        "approval_ref": approval_ref,
                        "approved_by": receipt.approved_by,
                        "co_approved_by": list(receipt.co_approved_by),
                        "via": principal.via,
                    },
                    await events.changes_for(conn, audit_rows),
                )
                await idempotency.complete(conn, claim, 200, response)
            log.info("change set applied", extra={"change_set": cs_code, "items": len(specs), "via": principal.via})
            return Outcome(200, response)
        except Conflict as exc:  # raised by a handler during phase 2 (race after phase 1)
            conflicts = [exc.info]
        except _AbortApply:
            pass
        return await self._record_conflict(principal, cs_id, conflicts, attribution, claim)

    async def _record_conflict(
        self,
        principal: Principal,
        cs_id: Any,
        conflicts: list[ConflictInfo],
        attribution: Attribution,
        claim: idempotency.Claim,
    ) -> Outcome:
        payload = [c.to_dict() for c in conflicts]
        async with self.engine.begin() as conn:
            cs = await self._resolve(conn, str(cs_id), lock=True)
            response = {
                "status": "conflict",
                "conflicts": payload,
                "change_set_code": cs.code,
                "change_set_id": str(cs.id),
            }
            for c in conflicts:
                await conn.execute(
                    update(t.change_set_item)
                    .where(t.change_set_item.c.change_set_id == cs.id, t.change_set_item.c.seq == c.seq)
                    .values(status="error", error=c.reason)
                )
            await conn.execute(
                update(t.change_set).where(t.change_set.c.id == cs.id).values(status="conflict", result=response)
            )
            await audit.write(
                conn,
                replace(attribution, change_set_code=cs.code),
                entity_type="change_set",
                entity_ref=cs.code,
                action="apply_conflict",
                before={"status": "validated"},
                after={"status": "conflict", "conflicts": payload},
            )
            await idempotency.complete(conn, claim, 409, response)
        log.info("change set conflict", extra={"change_set": response["change_set_code"], "conflicts": len(payload)})
        return Outcome(409, response)

    # ───────────── rollback ─────────────
    async def rollback(
        self,
        principal: Principal,
        ref: str,
        *,
        approval_ref: str,
        approval_receipt: str,
        idempotency_key: str | None,
    ) -> Outcome:
        """Undo an applied change set with a compensating change set, applied atomically.

        The approval receipt binds to the **original** change set (``change_set_ref`` = its
        id, ``change_set_hash`` = its ``preview_hash``) and must be a user approval
        (``approved_by: user:…``; scope ``changes:apply`` + role ``ops-director``). Each
        compensating item expects the version the original apply produced, so anything
        changed since makes the rollback a conflict (nothing is applied).
        """
        if not principal.has_scope(SCOPE_APPLY):
            raise Problem(403, "insufficient_scope", f"requires scope {SCOPE_APPLY}")
        if not isinstance(approval_ref, str) or not approval_ref:
            raise Problem(400, "invalid_parameter", "approval_ref is required")
        body = {"change_set": str(ref), "approval_ref": approval_ref, "op": "rollback"}

        async def run(claim: idempotency.Claim) -> Outcome:
            async with self.engine.connect() as conn:
                cs = await self._resolve(conn, ref)
            if cs.status == "rolled_back":
                raise Problem(409, "already_rolled_back", f"change set {cs.code} is already rolled back")
            if cs.status != "applied" or cs.rollback_of is not None:
                raise Problem(409, "invalid_state", f"only applied change sets can be rolled back (status {cs.status})")
            # The receipt claims are those of an apply approval (same change_set_ref/hash), so
            # the approval that applied the set (or a re-minted receipt of it) must not undo it.
            if approval_ref == cs.approval_ref:
                raise Problem(
                    403,
                    "invalid_receipt",
                    "a rollback needs its own approval, not the one that applied the change set",
                    extra={"reason": "approval_reused"},
                )
            receipt = await self.receipts.verify(
                approval_receipt,
                ReceiptContext(
                    change_set_id=str(cs.id),
                    change_set_code=cs.code,
                    preview_hash=str(cs.preview_hash),
                    approval_ref=approval_ref,
                    principal=principal,
                ),
            )
            if receipt.is_policy:
                raise Problem(403, "rollback_requires_user_approval", "a rollback needs a user approval, not a policy")
            # docs/contracts/95 §3: "four-eyes if the original needed it". A change set applied
            # with co-approvers must not be undone on one person's approval alone.
            if co_approver_names(cs.co_approvers) and not receipt.co_approved_by:
                raise Problem(
                    403,
                    "invalid_receipt",
                    f"change set {cs.code} was co-approved (four-eyes); its rollback needs co-approvers too",
                    extra={"reason": "co_approval_required"},
                )
            if not principal.has_role(ROLE_OPS_DIRECTOR):
                raise Problem(403, "missing_role", f"requires role {ROLE_OPS_DIRECTOR}")
            return await self._rollback_locked(principal, cs.id, approval_ref, receipt, claim)

        return await self._idempotent(principal, idempotency_key, "change_set.rollback", body, run)

    async def _rollback_locked(
        self, principal: Principal, cs_id: Any, approval_ref: str, receipt: VerifiedReceipt, claim: idempotency.Claim
    ) -> Outcome:
        attribution = Attribution.of(
            principal,
            approval_ref=approval_ref,
            approved_by=receipt.approved_by,
            initiated_by=receipt.initiated_by,
            co_approved_by=receipt.co_approved_by or None,
        )
        co_approvers = receipt.co_approvers or None
        conflicts: list[ConflictInfo] = []
        orig_code = ""
        try:
            async with self.engine.begin() as conn:
                orig = await self._resolve(conn, str(cs_id), lock=True)
                orig_code = orig.code
                if orig.status != "applied":
                    raise Problem(409, "invalid_state", f"change set {orig.code} is {orig.status}")
                used = (
                    await conn.execute(select(t.change_set.c.code).where(t.change_set.c.receipt_jti == receipt.jti))
                ).first()
                if used is not None:
                    raise Problem(
                        403, "invalid_receipt", "approval receipt was already used", extra={"reason": "replayed_jti"}
                    )
                items = await self._items(conn, orig.id)
                comp: list[dict[str, Any]] = []
                try:
                    for it in reversed(items):
                        spec = ItemSpec(it.seq, it.op, it.target_ref, it.expected_version, dict(it.payload or {}))
                        comp.extend(HANDLERS[it.op].inverse(spec, it.result, it.before))
                except (NotReversible, NotImplementedError, KeyError) as exc:
                    raise Problem(
                        409, "not_reversible", f"change set {orig.code} cannot be rolled back: {exc}"
                    ) from exc
                n = (await conn.execute(text("SELECT nextval('merxa.change_set_code_seq')"))).scalar_one()
                code = f"CS-{n:04d}"
                specs = [
                    ItemSpec(i, c["op"], c["target_ref"], c["expected_version"], c["payload"])
                    for i, c in enumerate(comp, start=1)
                ]
                h = compute_preview_hash(
                    {
                        "seq": sp.seq,
                        "op": sp.op,
                        "target_ref": sp.target_ref,
                        "expected_version": sp.expected_version,
                        "payload": sp.payload,
                    }
                    for sp in specs
                )
                attribution = replace(attribution, change_set_code=code)
                today = await sim_today(conn)
                # Preview (before/after) and phase 1 (lock + collect all conflicts).
                sim = Simulation(today=today)
                previews = [await HANDLERS[sp.op].preview(conn, sp, sim) for sp in specs]
                check_sim = Simulation(today=today)
                for sp in specs:
                    info = await HANDLERS[sp.op].check(conn, sp, check_sim)
                    if info is not None:
                        conflicts.append(info)
                if conflicts:
                    raise _AbortApply()
                ctx = ApplyContext(change_set_code=code, today=today)
                audit_rows: list[tuple[str, str, str, Any, Any]] = []
                for sp in specs:
                    audit_rows.extend(await HANDLERS[sp.op].apply(conn, sp, ctx))
                totals = {
                    "lines": len(specs),
                    "units_moved": sum(p.units_moved for p in previews),
                    "value_cents": sum(p.value_cents for p in previews),
                }
                new_id = (
                    await conn.execute(
                        t.change_set.insert()
                        .values(
                            code=code,
                            title=f"Rollback of {orig.code}",
                            status="applied",
                            origin={"system": "Merxa", "rollback_of": orig.code},
                            items_count=len(specs),
                            preview={"status": "validated", "preview_hash": h, "totals": totals},
                            preview_hash=h,
                            result={"status": "applied", "applied": len(specs)},
                            approval_ref=approval_ref,
                            receipt_jti=receipt.jti,
                            approved_by=receipt.approved_by,
                            initiated_by=receipt.initiated_by,
                            applied_by_sub=principal.sub,
                            applied_by_username=principal.username,
                            applied_by_display=principal.actor_display,
                            applied_via_client=principal.client_id,
                            via=principal.via,
                            idempotency_key=claim.key,
                            created_by_client=principal.client_id,
                            created_by_sub=principal.sub,
                            validated_at=func.now(),
                            applied_at=func.now(),
                            rollback_of=orig.id,
                            co_approvers=co_approvers,
                        )
                        .returning(t.change_set.c.id)
                    )
                ).scalar_one()
                await conn.execute(
                    t.change_set_item.insert(),
                    [
                        {
                            "change_set_id": new_id,
                            "seq": sp.seq,
                            "op": sp.op,
                            "target_ref": sp.target_ref,
                            "expected_version": sp.expected_version,
                            "payload": jsonable(sp.payload),
                            "before": jsonable(pv.before),
                            "after": jsonable(pv.after),
                            "status": "applied",
                            "result": jsonable(ctx.results.get(sp.seq)),
                        }
                        for sp, pv in zip(specs, previews, strict=True)
                    ],
                )
                await conn.execute(
                    update(t.change_set).where(t.change_set.c.id == orig.id).values(status="rolled_back")
                )
                audit_rows.append(
                    ("change_set", orig.code, "rollback", {"status": "applied"}, {"status": "rolled_back", "by": code})
                )
                audit_rows.append(
                    (
                        "change_set",
                        code,
                        "apply",
                        None,
                        {"status": "applied", "rollback_of": orig.code, "items": len(specs)},
                    )
                )
                await audit.write_many(conn, attribution, audit_rows)
                await emit_entity_events(conn, audit_rows, code)
                await events.emit(
                    conn,
                    "change_set.rolled_back",
                    {
                        "change_set_id": str(orig.id),
                        "code": orig.code,
                        "compensating_change_set_id": str(new_id),
                        "compensating_code": code,
                        "approval_ref": approval_ref,
                        "approved_by": receipt.approved_by,
                        "co_approved_by": list(receipt.co_approved_by),
                        "via": principal.via,
                    },
                    await events.changes_for(conn, audit_rows),
                )
                response = {
                    "status": "rolled_back",
                    "change_set_id": str(orig.id),
                    "change_set_code": orig.code,
                    "compensating_change_set_id": str(new_id),
                    "compensating_code": code,
                    "applied": len(specs),
                    "audit_ref": f"{self.settings.public_url}/v1/audit-log?change_set_code={code}",
                }
                await idempotency.complete(conn, claim, 200, response)
            log.info("change set rolled back", extra={"change_set": orig_code, "compensating": code})
            return Outcome(200, response)
        except Conflict as exc:
            conflicts = [exc.info]
        except _AbortApply:
            pass
        payload = [c.to_dict() for c in conflicts]
        response = {
            "status": "conflict",
            "conflicts": payload,
            "change_set_code": orig_code,
            "change_set_id": str(cs_id),
        }
        async with self.engine.begin() as conn:
            await audit.write(
                conn,
                replace(attribution, change_set_code=orig_code),
                entity_type="change_set",
                entity_ref=orig_code,
                action="rollback_conflict",
                after={"conflicts": payload},
            )
            await idempotency.complete(conn, claim, 409, response)
        return Outcome(409, response)

    # ───────────── operator guide ─────────────
    async def operator_guide(self, ref: str, locale: str = "en") -> dict[str, Any]:
        """Manual steps for a change set (Guide mode, docs/contracts/95 §2).

        Each step carries a Merxa UI ``deep_link`` to a form pre-filled with the item's values
        (saved through REST with an ``Idempotency-Key``; every save emits a webhook event) and
        ``expected`` = what a read-back must find once the step is done
        (``{entity_type, entity_ref|null, match}``, the shape of a checklist step's ``expected``).
        """
        cs = await self.get(ref)
        base = self.settings.public_url
        steps: list[dict[str, Any]] = []
        for it in cs["items"]:
            p = it["payload"] if isinstance(it["payload"], dict) else {}
            builder = _GUIDE.get(it["op"])
            if builder is None:
                title = f"Manual step for unsupported operation {it['op']}"
                instructions = "This operation is not supported by Merxa; contact your Merxa administrator."
                link = f"{base}/change-batches/{_seg(cs['code'])}"
                expected = None
            else:
                title, instructions, link, expected = builder(base, it, p)
            steps.append(
                {
                    "seq": len(steps) + 1,
                    "item_seq": it["seq"],
                    "op": it["op"],
                    "target_ref": it["target_ref"],
                    "title": title,
                    "instructions": instructions,
                    "deep_link": link,
                    "expected": expected,
                }
            )
        return {"change_set_id": cs["change_set_id"], "code": cs["code"], "locale": "en", "steps": steps}


class _AbortApply(Exception):
    """Internal: unwind the apply transaction after collecting conflicts."""


# ───────────── helpers ─────────────
async def emit_entity_events(
    conn: AsyncConnection, audit_rows: list[tuple[str, str, str, Any, Any]], change_set_code: str
) -> None:
    """Business events derived from applied entity changes (``po_line.eta_changed``)."""
    eta = [
        {"ref": ref, "eta_dc_before": (before or {}).get("eta_dc"), "eta_dc": (after or {}).get("eta_dc")}
        for entity, ref, action, before, after in audit_rows
        if entity == "po_line" and action in ("reschedule",) and isinstance(after, dict)
    ]
    if eta:
        await events.emit(
            conn,
            "po_line.eta_changed",
            {"change_set_code": change_set_code, "lines": eta},
            await events.changes_for(conn, [("po_line", e["ref"], "reschedule", None, None) for e in eta]),
        )


GuideStep = tuple[str, str, str, dict[str, Any] | None]


def _seg(value: Any) -> str:
    """A path segment built from integration data: every reserved character percent-encoded."""
    return quote(str(value if value is not None else ""), safe="")


def _expected(entity_type: str, entity_ref: str | None, match: dict[str, Any]) -> dict[str, Any]:
    return {"entity_type": entity_type, "entity_ref": entity_ref, "match": match}


def _lines(p: dict[str, Any]) -> list[dict[str, Any]]:
    return [ln for ln in p.get("lines") or [] if isinstance(ln, dict)]


def _lines_param(lines: list[dict[str, Any]]) -> str:
    return ",".join(f"{ln.get('sku')}:{ln.get('qty')}" for ln in lines)


def _version_param(it: dict[str, Any]) -> dict[str, Any]:
    return {"version": it["expected_version"]} if it.get("expected_version") is not None else {}


def _guide_transfer(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    lines = _lines(p)
    query = urlencode(
        {
            "from": p.get("from_dc", ""),
            "to": p.get("to_dc", ""),
            "ship_date": p.get("ship_date", ""),
            "lines": _lines_param(lines),
        }
    )
    total = sum(ln["qty"] for ln in lines if isinstance(ln.get("qty"), int) and not isinstance(ln.get("qty"), bool))
    return (
        f"Create transfer order {p.get('from_dc')} → {p.get('to_dc')}",
        f"Open Transfer Orders › New. Set From DC {p.get('from_dc')}, To DC {p.get('to_dc')}, ship date "
        f"{p.get('ship_date')}. Add {len(lines)} line(s) totalling {total} units: "
        + ", ".join(f"{ln.get('sku')} × {ln.get('qty')}" for ln in lines)
        + ". Choose Create transfer order.",
        f"{base}/transfer-orders/new?{query}",
        _expected(
            "transfer_order",
            None,
            {
                "from_dc": p.get("from_dc"),
                "to_dc": p.get("to_dc"),
                "ship_date": p.get("ship_date"),
                "lines": [{"sku": ln.get("sku"), "qty": ln.get("qty")} for ln in lines],
            },
        ),
    )


def _guide_allocation(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    ref = it["target_ref"]
    params = {"action": "set-qty", "qty": p.get("qty", ""), **_version_param(it)}
    return (
        f"Set allocation {ref} quantity to {p.get('qty')}",
        f"Open Allocations › {ref}, choose Edit quantity, enter {p.get('qty')} and save. "
        f"Expected record version: {it['expected_version']}.",
        f"{base}/allocations/{_seg(ref)}?{urlencode(params)}",
        _expected("allocation", ref, {"qty": p.get("qty")}),
    )


def _po_link(base: str, ref: str | None, params: dict[str, Any]) -> tuple[str, str, str]:
    po_code, _, line_no = str(ref or "").rpartition("#")
    return po_code, line_no, f"{base}/purchase-orders/{_seg(po_code)}?{urlencode({'line': line_no, **params})}"


def _guide_reschedule(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    po, line, link = _po_link(
        base, it["target_ref"], {"action": "reschedule", "eta": p.get("eta_dc", ""), **_version_param(it)}
    )
    return (
        f"Reschedule {po} line {line} to ETA {p.get('eta_dc')}",
        f"Open Purchase Orders › {po} › Lines, select line {line}, set ETA DC to {p.get('eta_dc')} and save.",
        link,
        _expected("po_line", it["target_ref"], {"eta_dc": p.get("eta_dc")}),
    )


def _guide_change_qty(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    po, line, link = _po_link(
        base, it["target_ref"], {"action": "change-qty", "qty": p.get("qty", ""), **_version_param(it)}
    )
    return (
        f"Change {po} line {line} quantity to {p.get('qty')}",
        f"Open Purchase Orders › {po} › Lines, select line {line}, set ordered quantity to {p.get('qty')} and save.",
        link,
        _expected("po_line", it["target_ref"], {"qty_ordered": p.get("qty")}),
    )


def _guide_cancel(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    po, line, link = _po_link(base, it["target_ref"], {"action": "cancel"})
    return (
        f"Cancel {po} line {line}",
        f"Open Purchase Orders › {po} › Lines, select line {line} and choose Cancel line.",
        link,
        _expected("po_line", it["target_ref"], {"status": "cancelled"}),
    )


def _guide_split(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    po, line, link = _po_link(
        base, it["target_ref"], {"action": "split", "qty": p.get("qty", ""), "lane": p.get("lane_code", "")}
    )
    return (
        f"Move {p.get('qty')} units of {po} line {line} to sea-air ({p.get('lane_code')})",
        f"Open Purchase Orders › {po} › Lines, select line {line}, choose Split shipment, mode sea-air, lane "
        f"{p.get('lane_code')}, quantity {p.get('qty')}. A shipment and a capacity booking are created.",
        link,
        _expected("shipment", None, {"po_code": po, "mode": "sea_air", "lane_code": p.get("lane_code")}),
    )


def _guide_po_create(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    lines = _lines(p)
    q = urlencode(
        {
            "supplier": p.get("supplier_code", ""),
            "factory": p.get("factory_code", ""),
            "dc": p.get("dest_dc", ""),
            "eta": p.get("eta_dc", ""),
            "lines": _lines_param(lines),
        }
    )
    return (
        f"Raise a purchase order with {p.get('factory_code')} for {p.get('dest_dc')}",
        f"Open Purchase Orders › New. Supplier {p.get('supplier_code')}, factory {p.get('factory_code')}, destination "
        f"{p.get('dest_dc')}, ETA {p.get('eta_dc')}; add {len(lines)} line(s).",
        f"{base}/purchase-orders/new?{q}",
        _expected(
            "purchase_order",
            None,
            {
                "supplier_code": p.get("supplier_code"),
                "factory_code": p.get("factory_code"),
                "dest_dc": p.get("dest_dc"),
                "eta_dc": p.get("eta_dc"),
            },
        ),
    )


def _guide_booking(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    q = urlencode({"lane": p.get("lane_code", ""), "week": p.get("iso_week", ""), "qty": p.get("qty_units", "")})
    return (
        f"Book {p.get('qty_units')} units on {p.get('lane_code')} for {p.get('iso_week')}",
        f"Open Sailings & Lanes › Capacity › New booking: lane {p.get('lane_code')}, week {p.get('iso_week')}, "
        f"{p.get('qty_units')} units. Choose Book capacity.",
        f"{base}/capacity-bookings/new?{q}",
        _expected(
            "capacity_booking",
            None,
            {
                "lane_code": p.get("lane_code"),
                "iso_week": p.get("iso_week"),
                "qty_units": p.get("qty_units"),
                "status": "confirmed",
            },
        ),
    )


def _guide_booking_cancel(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    ref = it["target_ref"]
    params = {"action": "cancel", **_version_param(it)}
    return (
        f"Cancel capacity booking {ref}",
        f"Open Sailings & Lanes › Capacity › {ref} and choose Cancel booking. "
        f"Expected record version: {it['expected_version']}.",
        f"{base}/capacity-bookings/{_seg(ref)}?{urlencode(params)}",
        _expected("capacity_booking", ref, {"status": "released"}),
    )


def _guide_resequence(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    codes = [str(c) for c in p.get("order_codes") or []]
    return (
        f"Resequence production line {p.get('line_code')}",
        f"Open Production schedule › {p.get('line_code')}, drag the planned orders into this order: "
        f"{', '.join(codes)}.",
        f"{base}/production-schedule?{urlencode({'line': p.get('line_code', '')})}",
        _expected("factory_line", p.get("line_code"), {"order_codes": codes}),
    )


def _guide_promo(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    return (
        f"Move promotion {it['target_ref']} to {p.get('period_start')} – {p.get('period_end')}",
        f"Open Promotions › {it['target_ref']}, set the period to {p.get('period_start')} – {p.get('period_end')}.",
        f"{base}/promotions?{urlencode({'code': it['target_ref'] or ''})}",
        _expected(
            "promotion", it["target_ref"], {"period_start": p.get("period_start"), "period_end": p.get("period_end")}
        ),
    )


def _guide_repromise(base: str, it: dict[str, Any], p: dict[str, Any]) -> GuideStep:
    return (
        f"Re-promise online order {it['target_ref']} for {p.get('promise_date')}",
        f"Open Online orders › {it['target_ref']}, set the promise date to {p.get('promise_date')} "
        "and notify the customer.",
        f"{base}/online-orders?{urlencode({'code': it['target_ref'] or ''})}",
        _expected("online_order", it["target_ref"], {"promise_date": p.get("promise_date")}),
    )


_GUIDE = {
    "transfer_order.create": _guide_transfer,
    "allocation.set_qty": _guide_allocation,
    "po_line.reschedule": _guide_reschedule,
    "po_line.change_qty": _guide_change_qty,
    "po_line.cancel": _guide_cancel,
    "po_line.split": _guide_split,
    "po.create": _guide_po_create,
    "capacity_booking.create": _guide_booking,
    "capacity_booking.cancel": _guide_booking_cancel,
    "production_order.resequence": _guide_resequence,
    "promotion.reschedule": _guide_promo,
    "online_order.repromise": _guide_repromise,
}
