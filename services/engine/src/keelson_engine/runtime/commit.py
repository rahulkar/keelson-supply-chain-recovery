"""Deterministic commit service: compile → dry-run (preparer) → receipt → apply → reconcile.

* ``compile_changeset``: plan option actions → canonical change-set items (identity mapping to
  Merxa ops in M1) → ``ChangeSet`` and its hash ``H`` (``keelson_contracts.changeset``).
* ``dry_run``: preparer principal: ``create_change_set`` → ``add_change_set_items`` →
  ``validate_change_set``; Merxa's ``preview_hash`` must equal ``H``.
* ``mint_receipt``: approval receipt claims (``10-auth.md``) signed by ``core.crypto``.
* ``apply_with_retry``: ``apply_change_set`` with ``idempotency_key = ChangeSet.idempotency_key``
  (retries the *same* key on transient errors such as ``receipt_jwks_unavailable``).
* ``reconcile``: read back ``get_change_set``, ``get_audit_trail`` and ``list_transfer_orders``
  (``created_via = change_set:<code>``) and count verified items.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from keelson_contracts.changeset import CHANGE_SET_ITEM_ADAPTER, ChangeSet
from keelson_contracts.receipts import ApprovalReceiptClaims

from .deps import CryptoPort
from .errors import HashMismatchError, McpToolError, McpTransportError
from .problem import ToolCaller

__all__ = [
    "APPLY_BACKOFF_S",
    "ApplyOutcome",
    "AllocationExpander",
    "GroupTooLargeError",
    "expand_actions",
    "partition_groups",
    "split_allocation",
    "stored_items",
    "DryRunResult",
    "ReconcileResult",
    "apply_with_retry",
    "approval_id_for",
    "compile_changeset",
    "dry_run",
    "mint_receipt",
    "reconcile",
    "token_subject",
]


APPLY_BACKOFF_S = 1.0
"""Base back-off between apply retries (attempt n waits n × this)."""


def compile_changeset(
    *, tenant: str, change_set_id: str, connector: str, actions: Sequence[Mapping[str, Any]], revision: int = 1
) -> ChangeSet:
    """Canonical items (seq 1..n in the option's order) → validated :class:`ChangeSet`.

    Only the five hashed keys enter the items; an action's ``group`` (atomic group) is kept by
    the caller next to the items (:func:`stored_items`), never in the hash."""
    items = []
    for seq, action in enumerate(sorted(actions, key=lambda a: int(a.get("seq") or 0)), start=1):
        items.append(
            CHANGE_SET_ITEM_ADAPTER.validate_python(
                {
                    "seq": seq,
                    "op": action["op"],
                    "target_ref": action.get("target_ref"),
                    "expected_version": action.get("expected_version"),
                    "payload": action["payload"],
                }
            )
        )
    return ChangeSet(id=change_set_id, tenant=tenant, connector=connector, revision=revision, items=items)


# ─────────────────────── M3: groups and allocation groups ───────────────────────

AllocationExpander = Callable[[str], Awaitable[Sequence[Any]]]
"""``group_code -> [AllocationMember]`` (store allocations of a cluster allocation, pinned snapshot)."""


class GroupTooLargeError(ValueError):
    """An atomic group alone exceeds the connector's change-set size."""


def split_allocation(new_qty: int, members: Sequence[Any]) -> list[tuple[Any, int]]:
    """Distribute a cluster allocation's new quantity over its store allocations in proportion
    to their current quantities (largest remainder, ties by allocation code; equal shares when
    they are all zero)."""
    if not members:
        return []
    ordered = sorted(members, key=lambda m: str(m.code))
    weights = [int(m.qty) for m in ordered]
    total = sum(weights)
    if total <= 0:
        weights = [1] * len(ordered)
        total = len(ordered)
    base = [new_qty * w // total for w in weights]
    rema = [new_qty * w % total for w in weights]
    left = new_qty - sum(base)
    for i in sorted(range(len(ordered)), key=lambda i: (-rema[i], str(ordered[i].code)))[:left]:
        base[i] += 1
    return list(zip(ordered, base, strict=True))


async def expand_actions(actions: Sequence[Mapping[str, Any]], *, expander: AllocationExpander | None,
                         is_group: Callable[[str], bool]) -> list[dict[str, Any]]:
    """Plan actions → change-set actions in plan order with ``seq`` renumbered 1..n.

    ``allocation.set_qty`` on a cluster allocation group (``is_group(target_ref)``) becomes one
    item per store allocation whose quantity changes (``expected_version`` from the snapshot),
    all in the action's atomic group (or a new ``alloc:<code>`` group)."""
    out: list[dict[str, Any]] = []
    for action in sorted(actions, key=lambda a: int(a.get("seq") or 0)):
        ref = action.get("target_ref")
        if action["op"] == "allocation.set_qty" and isinstance(ref, str) and is_group(ref):
            if expander is None:
                raise LookupError(f"allocation group {ref} needs the plan's snapshot to be expanded")
            members = list(await expander(ref))
            if not members:
                raise LookupError(f"allocation group {ref} has no store allocations in the snapshot")
            group = action.get("group") or f"alloc:{ref}"
            for member, qty in split_allocation(int(action["payload"]["qty"]), members):
                if qty == int(member.qty):
                    continue
                out.append({"op": "allocation.set_qty", "target_ref": member.code,
                            "expected_version": int(member.version), "payload": {"qty": qty}, "group": group})
            continue
        item = {k: action.get(k) for k in ("op", "target_ref", "expected_version", "payload")}
        if action.get("group"):
            item["group"] = action["group"]
        out.append(item)
    touched = [a["target_ref"] for a in out if a["op"] == "allocation.set_qty"]
    if len(set(touched)) != len(touched):
        raise ValueError("an allocation is changed by more than one item")
    for seq, item in enumerate(out, start=1):
        item["seq"] = seq
    return out


def partition_groups(items: Sequence[Mapping[str, Any]], max_items: int) -> list[list[Mapping[str, Any]]]:
    """Split items (in order) into change sets of at most ``max_items`` without ever separating
    an atomic group; ungrouped items fill the gaps."""
    blocks: list[list[Mapping[str, Any]]] = []
    for item in items:
        group = item.get("group")
        if group and blocks and blocks[-1][0].get("group") == group:
            blocks[-1].append(item)
        else:
            blocks.append([item])
    chunks: list[list[Mapping[str, Any]]] = []
    for block in blocks:
        if len(block) > max_items:
            raise GroupTooLargeError(
                f"atomic group {block[0].get('group')!r} has {len(block)} items; a change set holds {max_items}"
            )
        if not chunks or len(chunks[-1]) + len(block) > max_items:
            chunks.append([])
        chunks[-1].extend(block)
    return chunks


def stored_items(change_set: ChangeSet, actions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Items as stored on the engine change-set row: the canonical item plus its ``group``."""
    groups = {int(a["seq"]): a.get("group") for a in actions}
    out = []
    for item in change_set.items:
        row = item.model_dump(mode="json")
        if groups.get(item.seq):
            row["group"] = groups[item.seq]
        out.append(row)
    return out


def approval_id_for(change_set: ChangeSet) -> str:
    """Deterministic approval id (= ``approval_ref``) per change set + revision, so a retried
    apply carries the same reference and replays instead of tripping idempotency reuse."""
    return f"APR-{change_set.idempotency_key[:12].upper()}"


@dataclass(frozen=True)
class DryRunResult:
    ext_change_set_id: str
    ext_code: str
    status: str
    preview_hash: str
    items: list[dict[str, Any]]
    totals: dict[str, int]

    @property
    def ok(self) -> bool:
        return self.status == "validated"


async def dry_run(
    caller: ToolCaller, change_set: ChangeSet, *, title: str, origin: Mapping[str, Any]
) -> DryRunResult:
    key = change_set.idempotency_key
    created = await caller.call(
        "create_change_set", {"title": title, "origin": dict(origin), "idempotency_key": f"{key}:create"}
    )
    ext_id = str(created["change_set_id"])
    await caller.call(
        "add_change_set_items",
        {"change_set_id": ext_id, "items": change_set.tool_items(), "idempotency_key": f"{key}:add"},
    )
    validated = await caller.call("validate_change_set", {"change_set_id": ext_id, "idempotency_key": f"{key}:validate"})
    result = DryRunResult(
        ext_change_set_id=ext_id,
        ext_code=str(created.get("code") or ""),
        status=str(validated["status"]),
        preview_hash=str(validated["preview_hash"]),
        items=list(validated.get("items") or []),
        totals={k: int(v) for k, v in (validated.get("totals") or {}).items()},
    )
    if result.ok and result.preview_hash != change_set.hash:
        raise HashMismatchError(
            f"Merxa preview_hash {result.preview_hash} != engine change-set hash {change_set.hash}"
        )
    return result


def token_subject(token: str) -> str:
    """``sub`` of a JWT we just obtained from the IdP (payload read without verification —
    the token is only *presented* by us; Merxa verifies it)."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError) as exc:
        raise ValueError("token is not a JWT") from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ValueError("token has no sub claim")
    return sub


def mint_receipt(
    crypto: CryptoPort,
    *,
    change_set: ChangeSet,
    ext_change_set_id: str,
    keelson_sub: str,
    ext_sub: str,
    approved_by: str,
    approver_name: str | None,
    initiated_by: str,
    policy_version: str,
    co_approved_by: Sequence[str] = (),
) -> tuple[str, ApprovalReceiptClaims]:
    """Sign the approval receipt (``co_approved_by``: the other quorum members, M5 four-eyes)."""
    claims = ApprovalReceiptClaims.issue(
        approval_id=approval_id_for(change_set),
        change_set_ref=ext_change_set_id,
        change_set_hash=change_set.hash,
        tenant=change_set.tenant,
        keelson_sub=keelson_sub,
        ext_sub=ext_sub,
        approved_by=approved_by,
        approver_name=approver_name,
        initiated_by=initiated_by,
        policy_version=policy_version,
        co_approved_by=list(co_approved_by),
    )
    return crypto.sign_approval_receipt(claims), claims


@dataclass(frozen=True)
class ApplyOutcome:
    status: str
    applied: int
    ext_code: str | None
    audit_ref: str | None
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


async def apply_with_retry(
    caller: ToolCaller,
    *,
    ext_change_set_id: str,
    preview_hash: str,
    approval_ref: str,
    receipt: str,
    idempotency_key: str,
    attempts: int = 4,
    backoff_s: float | None = None,
) -> ApplyOutcome:
    """Apply with the same idempotency key.

    Transient tool errors (``receipt_jwks_unavailable``, ``idempotency_in_flight``) and
    transport failures (timeouts, proxy/connection errors — the request may or may not have
    reached Merxa) are retried with the *same* key, so Merxa replays instead of applying
    twice. Definitive tool errors propagate immediately."""
    backoff = APPLY_BACKOFF_S if backoff_s is None else backoff_s
    for attempt in range(1, attempts + 1):
        try:
            data = await caller.call(
                "apply_change_set",
                {
                    "change_set_id": ext_change_set_id,
                    "preview_hash": preview_hash,
                    "approval_ref": approval_ref,
                    "approval_receipt": receipt,
                    "idempotency_key": idempotency_key,
                },
            )
        except McpToolError as exc:
            if exc.transient and attempt < attempts:
                await asyncio.sleep(backoff * attempt)
                continue
            raise
        except McpTransportError:
            if attempt < attempts:
                await asyncio.sleep(backoff * attempt)
                continue
            raise
        return ApplyOutcome(
            status=str(data.get("status")),
            applied=int(data.get("applied") or 0),
            ext_code=data.get("change_set_code"),
            audit_ref=data.get("audit_ref"),
            conflicts=list(data.get("conflicts") or []),
            raw=dict(data),
        )
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    applied: int
    verified: int
    attribution_text: str | None
    audit_rows: int
    transfer_orders: list[str]
    ext_code: str


def _to_matches(item: Mapping[str, Any], to: Mapping[str, Any]) -> bool:
    payload = item.get("payload") or {}
    if to.get("from_dc") != payload.get("from_dc") or to.get("to_dc") != payload.get("to_dc"):
        return False
    want = {(line["sku"], int(line["qty"])) for line in payload.get("lines") or []}
    have = {(line["sku"], int(line["qty"])) for line in to.get("lines") or []}
    return want == have


async def reconcile(caller: ToolCaller, *, ext_change_set_id: str, change_set: ChangeSet) -> ReconcileResult:
    cs = await caller.call("get_change_set", {"change_set_id": ext_change_set_id})
    ext_code = str(cs.get("code") or "")
    items = list(cs.get("items") or [])
    trail = await caller.call("get_audit_trail", {"change_set_code": ext_code, "limit": 500})
    expected_items = [item.model_dump(mode="json") for item in change_set.items]
    transfer_orders: list[dict[str, Any]] = []
    pairs = sorted(
        {(i["payload"]["from_dc"], i["payload"]["to_dc"]) for i in expected_items if i["op"] == "transfer_order.create"}
    )
    for from_dc, to_dc in pairs:
        listing = await caller.call("list_transfer_orders", {"from_dc": from_dc, "to_dc": to_dc})
        transfer_orders.extend(
            to for to in listing.get("items") or [] if to.get("created_via") == f"change_set:{ext_code}"
        )
    status_by_seq = {int(i["seq"]): str(i.get("status")) for i in items}
    unmatched = list(transfer_orders)
    verified = 0
    for item in expected_items:
        if status_by_seq.get(int(item["seq"])) not in ("applied", "ok"):
            continue
        if item["op"] == "transfer_order.create":
            match = next((to for to in unmatched if _to_matches(item, to)), None)
            if match is None:
                continue
            unmatched.remove(match)
        verified += 1
    applied = sum(1 for s in status_by_seq.values() if s in ("applied", "ok")) if cs.get("status") == "applied" else 0
    return ReconcileResult(
        status=str(cs.get("status")),
        applied=applied,
        verified=min(verified, applied),
        attribution_text=cs.get("attribution_text"),
        audit_rows=len(trail.get("items") or []),
        transfer_orders=sorted(str(to.get("code")) for to in transfer_orders),
        ext_code=ext_code,
    )
