"""Canonical change-set ops, items, hashing and idempotency keys.

The engine compiles a plan option into a :class:`ChangeSet`; Merxa receives the
items through ``add_change_set_items`` and computes::

    preview_hash = sha256(canonical([{seq, op, target_ref, expected_version, payload}
                                     for items ordered by seq]))

:func:`change_set_hash` computes the same ``H`` independently (``20-merxa.md``),
so the engine can require ``preview_hash == H`` after ``validate_change_set`` and
bind the approval receipt to it.

M1 typed ops: ``transfer_order.create``, ``allocation.set_qty``,
``po_line.reschedule``, ``po_line.change_qty``. M3 adds typed models for the v1 lever ops
(``80-m3-deterministic-core.md`` §3, Merxa ``22-merxa-m2.md`` §3): ``po_line.split``,
``capacity_booking.create``, ``po.create``, ``po_line.cancel`` and
``production_order.resequence``. Any other op is carried by :class:`GenericOpItem`
(payload = JSON object without floats).

Typing an op never changes its hash: every typed payload field is required (no
defaults are filled in), so a typed item dumps exactly the five keys and the payload it
was built from, and :func:`change_set_hash` of the typed item equals the hash of the
wire dict. The only default is ``po_line.cancel``'s empty payload, which Merxa also
hashes as ``{}`` when it is missing or ``null``.

M5 (``95-m5-remediation.md`` §5): a :class:`ChangeSetBundle` groups one :class:`ChangeSet`
per target connector, in apply order, with its atomic groups (never split across
connectors). Approval covers the whole bundle: :func:`bundle_hash` binds every member's
``H`` in order and equals ``H`` itself for a single-connector bundle, so the M1 approval
binding (``interrupt.change_set_hash`` = receipt ``change_set_hash`` = Merxa ``preview_hash``)
is unchanged. Each member keeps its own ``H``, receipt and idempotency key.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final, Literal, TypeAlias, TypeAliasType

from pydantic import (
    Discriminator,
    Field,
    Strict,
    StrictBool,
    StrictInt,
    StrictStr,
    Tag,
    TypeAdapter,
    model_validator,
)

from .common import (
    MAX_SAFE_INTEGER,
    ContractModel,
    IsoDate,
    IsoWeek,
    PositiveInt,
    TenantSlug,
    sha256_hex,
)

__all__ = [
    "AtomicGroup",
    "BUNDLE_HASH_VERSION",
    "ChangeSetBundle",
    "MAX_BUNDLE_CONNECTORS",
    "bundle_hash",
    "AllocationSetQtyItem",
    "AllocationSetQtyPayload",
    "CHANGE_SET_ITEM_ADAPTER",
    "ChangeSet",
    "ChangeSetItem",
    "CapacityBookingCreateItem",
    "CapacityBookingCreatePayload",
    "GenericOpItem",
    "M1_OPS",
    "M3_OPS",
    "MAX_PO_CREATE_LINES",
    "NoFloatJson",
    "OP_PAYLOAD_MODELS",
    "PO_LINE_REF_PATTERN",
    "PoCreateItem",
    "PoCreateLine",
    "PoCreatePayload",
    "PoLineCancelItem",
    "PoLineCancelPayload",
    "PoLineChangeQtyItem",
    "PoLineChangeQtyPayload",
    "PoLineRescheduleItem",
    "PoLineReschedulePayload",
    "PoLineSplitItem",
    "PoLineSplitPayload",
    "ProductionOrderResequenceItem",
    "ProductionOrderResequencePayload",
    "SplitMode",
    "TYPED_OPS",
    "TransferOrderCreateItem",
    "TransferOrderCreatePayload",
    "TransferOrderLine",
    "change_set_hash",
    "hash_items",
    "idempotency_key",
    "parse_po_line_ref",
    "po_line_ref",
]

M1_OPS: Final = (
    "transfer_order.create",
    "allocation.set_qty",
    "po_line.reschedule",
    "po_line.change_qty",
)

M3_OPS: Final = (
    "po_line.split",
    "capacity_booking.create",
    "po.create",
    "po_line.cancel",
    "production_order.resequence",
)
"""Ops typed in M3 (v1 lever set); with :data:`M1_OPS` they form :data:`TYPED_OPS`."""

TYPED_OPS: Final = M1_OPS + M3_OPS
"""Every op with a typed item model; any other op is a :class:`GenericOpItem`."""

MAX_PO_CREATE_LINES: Final = 500
"""Merxa's limit on ``po.create`` lines."""

PO_LINE_REF_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_-]*#[1-9][0-9]*$"
_PO_LINE_REF_RE: Final = re.compile(PO_LINE_REF_PATTERN)
OP_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"

Code = Annotated[str, Field(min_length=1)]
PoLineRef = Annotated[str, Field(pattern=PO_LINE_REF_PATTERN)]
SafeInt = Annotated[StrictInt, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]

NoFloatJson = TypeAliasType(
    "NoFloatJson",
    "StrictStr | SafeInt | StrictBool | None"
    " | Annotated[list[NoFloatJson], Strict()] | Annotated[dict[str, NoFloatJson], Strict()]",
)
"""Any JSON value except floats (money is cents, ratios are basis points)."""


def po_line_ref(po_code: str, line_no: int) -> str:
    """``("PO-26-0101", 1) -> "PO-26-0101#1"``."""
    ref = f"{po_code}#{line_no}"
    if not _PO_LINE_REF_RE.match(ref):
        raise ValueError(f"invalid PO line reference: {ref!r}")
    return ref


def parse_po_line_ref(ref: str) -> tuple[str, int]:
    """``"PO-26-0101#1" -> ("PO-26-0101", 1)``."""
    if not _PO_LINE_REF_RE.match(ref):
        raise ValueError(f"invalid PO line reference: {ref!r}")
    po_code, line_no = ref.rsplit("#", 1)
    return po_code, int(line_no)


# ─────────────────────────────────── payloads ───────────────────────────────


class TransferOrderLine(ContractModel):
    sku: Code
    qty: PositiveInt


class TransferOrderCreatePayload(ContractModel):
    from_dc: Code
    to_dc: Code
    ship_date: IsoDate
    lines: Annotated[list[TransferOrderLine], Field(min_length=1)]

    @model_validator(mode="after")
    def _check(self) -> TransferOrderCreatePayload:
        if self.from_dc == self.to_dc:
            raise ValueError("from_dc and to_dc must differ")
        skus = [line.sku for line in self.lines]
        if len(set(skus)) != len(skus):
            raise ValueError("transfer order lines must have unique skus")
        return self

    @property
    def total_qty(self) -> int:
        return sum(line.qty for line in self.lines)


class AllocationSetQtyPayload(ContractModel):
    qty: Annotated[StrictInt, Field(ge=0)]


class PoLineReschedulePayload(ContractModel):
    eta_dc: IsoDate


class PoLineChangeQtyPayload(ContractModel):
    qty: Annotated[StrictInt, Field(ge=0)]


SplitMode = Literal["sea_air", "air"]
"""Faster inbound modes a PO line can be split onto (Merxa accepts ``sea_air`` only)."""


class PoLineSplitPayload(ContractModel):
    """``po_line.split``: move ``qty`` of an open PO line onto a faster lane (new shipment).

    ``qty`` must be below the line's open quantity (checked by the target system).
    """

    mode: SplitMode
    qty: PositiveInt
    lane_code: Code


class CapacityBookingCreatePayload(ContractModel):
    """``capacity_booking.create``: hold ``qty_units`` of a per-unit lane in ``iso_week``."""

    lane_code: Code
    iso_week: IsoWeek
    qty_units: PositiveInt


class PoCreateLine(ContractModel):
    sku: Code
    qty: PositiveInt


class PoCreatePayload(ContractModel):
    """``po.create``: a new purchase order from an alternate (e.g. near-shore) source."""

    supplier_code: Code
    factory_code: Code
    dest_dc: Code
    eta_dc: IsoDate
    lines: Annotated[list[PoCreateLine], Field(min_length=1, max_length=MAX_PO_CREATE_LINES)]

    @model_validator(mode="after")
    def _unique_skus(self) -> PoCreatePayload:
        skus = [line.sku for line in self.lines]
        if len(set(skus)) != len(skus):
            raise ValueError("po.create lines must have unique skus")
        return self

    @property
    def total_qty(self) -> int:
        return sum(line.qty for line in self.lines)


class PoLineCancelPayload(ContractModel):
    """``po_line.cancel`` takes no payload fields (``{}``)."""


class ProductionOrderResequencePayload(ContractModel):
    """``production_order.resequence``: the new order of every planned/held order on a line."""

    line_code: Code
    order_codes: Annotated[list[Code], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_codes(self) -> ProductionOrderResequencePayload:
        if len(set(self.order_codes)) != len(self.order_codes):
            raise ValueError("order_codes must be unique")
        return self


# ───────────────────────────────────── items ────────────────────────────────


class _ItemBase(ContractModel):
    seq: PositiveInt

    def hash_view(self) -> dict[str, Any]:
        """The exact five-key dict that enters the change-set hash."""
        return self.model_dump(
            mode="json", include={"seq", "op", "target_ref", "expected_version", "payload"}
        )

    def tool_item(self) -> dict[str, Any]:
        """Item as sent to ``add_change_set_items`` (``{op, target_ref, expected_version, payload}``)."""
        return self.model_dump(
            mode="json", include={"op", "target_ref", "expected_version", "payload"}
        )


class TransferOrderCreateItem(_ItemBase):
    op: Literal["transfer_order.create"]
    target_ref: None = None
    expected_version: None = None
    payload: TransferOrderCreatePayload


class AllocationSetQtyItem(_ItemBase):
    op: Literal["allocation.set_qty"]
    target_ref: Code
    expected_version: PositiveInt
    payload: AllocationSetQtyPayload


class PoLineRescheduleItem(_ItemBase):
    op: Literal["po_line.reschedule"]
    target_ref: Annotated[str, Field(pattern=PO_LINE_REF_PATTERN)]
    expected_version: PositiveInt
    payload: PoLineReschedulePayload


class PoLineChangeQtyItem(_ItemBase):
    op: Literal["po_line.change_qty"]
    target_ref: Annotated[str, Field(pattern=PO_LINE_REF_PATTERN)]
    expected_version: PositiveInt
    payload: PoLineChangeQtyPayload


class PoLineSplitItem(_ItemBase):
    op: Literal["po_line.split"]
    target_ref: PoLineRef
    expected_version: PositiveInt
    payload: PoLineSplitPayload


class CapacityBookingCreateItem(_ItemBase):
    op: Literal["capacity_booking.create"]
    target_ref: None = None
    expected_version: None = None
    payload: CapacityBookingCreatePayload


class PoCreateItem(_ItemBase):
    op: Literal["po.create"]
    target_ref: None = None
    expected_version: None = None
    payload: PoCreatePayload


class PoLineCancelItem(_ItemBase):
    op: Literal["po_line.cancel"]
    target_ref: PoLineRef
    expected_version: PositiveInt
    payload: PoLineCancelPayload = Field(default_factory=PoLineCancelPayload)

    @model_validator(mode="before")
    @classmethod
    def _null_payload_is_empty(cls, data: Any) -> Any:
        # Merxa hashes a missing or null payload as {}; accept null the same way.
        if isinstance(data, Mapping) and "payload" in data and data["payload"] is None:
            return {**data, "payload": {}}
        return data


class ProductionOrderResequenceItem(_ItemBase):
    """``target_ref`` is ``null`` or the factory line code; the optional
    ``expected_version`` is the factory line's version."""

    op: Literal["production_order.resequence"]
    target_ref: Code | None = None
    expected_version: PositiveInt | None = None
    payload: ProductionOrderResequencePayload

    @model_validator(mode="after")
    def _target_is_line(self) -> ProductionOrderResequenceItem:
        if self.target_ref is not None and self.target_ref != self.payload.line_code:
            raise ValueError("target_ref must be null or equal payload.line_code")
        return self


class GenericOpItem(_ItemBase):
    """Fallback for ops without a typed model (M5 ops, vendor-specific ops)."""

    op: Annotated[str, Field(pattern=OP_NAME_PATTERN)]
    target_ref: str | None = None
    expected_version: PositiveInt | None = None
    payload: Annotated[dict[str, NoFloatJson], Strict()]

    @model_validator(mode="after")
    def _not_a_typed_op(self) -> GenericOpItem:
        if self.op in TYPED_OPS:
            raise ValueError(f"op {self.op!r} has a typed model; do not use GenericOpItem")
        return self


def _op_tag(value: Any) -> str:
    op = value.get("op") if isinstance(value, Mapping) else getattr(value, "op", None)
    return op if op in TYPED_OPS else "generic"


ChangeSetItem: TypeAlias = Annotated[
    Annotated[TransferOrderCreateItem, Tag("transfer_order.create")]
    | Annotated[AllocationSetQtyItem, Tag("allocation.set_qty")]
    | Annotated[PoLineRescheduleItem, Tag("po_line.reschedule")]
    | Annotated[PoLineChangeQtyItem, Tag("po_line.change_qty")]
    | Annotated[PoLineSplitItem, Tag("po_line.split")]
    | Annotated[CapacityBookingCreateItem, Tag("capacity_booking.create")]
    | Annotated[PoCreateItem, Tag("po.create")]
    | Annotated[PoLineCancelItem, Tag("po_line.cancel")]
    | Annotated[ProductionOrderResequenceItem, Tag("production_order.resequence")]
    | Annotated[GenericOpItem, Tag("generic")],
    Discriminator(_op_tag),
]
"""A change-set item, discriminated on ``op`` with a generic fallback."""

CHANGE_SET_ITEM_ADAPTER: Final[TypeAdapter[Any]] = TypeAdapter(ChangeSetItem)

OP_PAYLOAD_MODELS: Final[Mapping[str, type[ContractModel]]] = {
    "transfer_order.create": TransferOrderCreatePayload,
    "allocation.set_qty": AllocationSetQtyPayload,
    "po_line.reschedule": PoLineReschedulePayload,
    "po_line.change_qty": PoLineChangeQtyPayload,
    "po_line.split": PoLineSplitPayload,
    "capacity_booking.create": CapacityBookingCreatePayload,
    "po.create": PoCreatePayload,
    "po_line.cancel": PoLineCancelPayload,
    "production_order.resequence": ProductionOrderResequencePayload,
}
"""Payload model per typed op (keys = :data:`TYPED_OPS`)."""


def hash_items(items: Sequence[_ItemBase | Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The list that is hashed: five-key dicts ordered by ``seq`` (duplicates rejected).

    Accepts typed items or plain mappings (e.g. items read back from Merxa).
    Mirrors Merxa's ``preview_hash`` exactly: missing ``target_ref`` /
    ``expected_version`` count as ``null`` and a missing or ``null`` ``payload``
    counts as ``{}``. A payload that is not a JSON object is rejected.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, _ItemBase):
            rows.append(item.hash_view())
        elif isinstance(item, Mapping):
            if "seq" not in item or "op" not in item:
                raise ValueError("change-set items need seq and op")
            payload = item.get("payload")
            if payload is None:
                payload = {}
            elif not isinstance(payload, Mapping):
                raise ValueError(
                    f"change-set item payload must be a JSON object, got {type(payload).__name__}"
                )
            rows.append(
                {
                    "seq": item["seq"],
                    "op": item["op"],
                    "target_ref": item.get("target_ref"),
                    "expected_version": item.get("expected_version"),
                    "payload": payload,
                }
            )
        else:
            raise TypeError(f"unsupported change-set item type {type(item).__name__}")
    seqs = [r["seq"] for r in rows]
    if any(isinstance(s, bool) or not isinstance(s, int) for s in seqs):
        raise ValueError("item seq must be an integer")
    if len(set(seqs)) != len(seqs):
        raise ValueError("duplicate item seq in change set")
    return sorted(rows, key=lambda r: r["seq"])


def change_set_hash(items: Sequence[_ItemBase | Mapping[str, Any]]) -> str:
    """``H = sha256_hex([{seq, op, target_ref, expected_version, payload} …])`` ordered by seq."""
    return sha256_hex(hash_items(items))


def idempotency_key(tenant: str, change_set_id: str, revision: int) -> str:
    """``sha256("<tenant>|<change_set_id>|<revision>")`` hex — the apply idempotency key.

    ``|`` is the field separator, so it may not appear in ``tenant`` or
    ``change_set_id`` (otherwise two different change sets could share a key and
    Merxa would replay the wrong apply).
    """
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise TypeError("revision must be int")
    for name, value in (("tenant", tenant), ("change_set_id", change_set_id)):
        if not value or "|" in value:
            raise ValueError(f"{name} must be non-empty and must not contain '|'")
    return hashlib.sha256(f"{tenant}|{change_set_id}|{revision}".encode()).hexdigest()


class ChangeSet(ContractModel):
    """An engine change-set (one per plan option + revision) targeting one connector."""

    id: Code
    tenant: TenantSlug
    connector: Code
    revision: PositiveInt
    items: Annotated[list[ChangeSetItem], Field(min_length=1)]

    @model_validator(mode="after")
    def _seq_contiguous(self) -> ChangeSet:
        seqs = [i.seq for i in self.items]
        if seqs != list(range(1, len(seqs) + 1)):
            raise ValueError("items must be ordered with seq 1..n (no gaps or duplicates)")
        return self

    @property
    def hash(self) -> str:
        """``H`` — must equal Merxa's ``preview_hash`` after validation."""
        return change_set_hash(self.items)

    @property
    def idempotency_key(self) -> str:
        return idempotency_key(self.tenant, self.id, self.revision)

    def tool_items(self) -> list[dict[str, Any]]:
        """Items for ``add_change_set_items`` in seq order."""
        return [item.tool_item() for item in self.items]

    @property
    def ops(self) -> set[str]:
        return {item.op for item in self.items}


# ─────────────────────────────── bundles (M5) ───────────────────────────────

MAX_BUNDLE_CONNECTORS: Final = 16
BUNDLE_HASH_VERSION: Final = 1
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")


def bundle_hash(members: Sequence[tuple[str, str]]) -> str:
    """Hash binding an ordered bundle: ``members`` = ``[(connector, H), …]`` in apply order.

    One member → that member's ``H`` unchanged (single-system approvals keep the M1 hash).
    Several → ``sha256_hex({"v": 1, "change_sets": [{"connector", "hash"}, …]})``, so the
    order, the connectors and every ``H`` are bound."""
    if not members:
        raise ValueError("a bundle has at least one change set")
    connectors = [c for c, _ in members]
    if len(set(connectors)) != len(connectors):
        raise ValueError("a bundle has one change set per connector")
    for connector, h in members:
        if not connector or not isinstance(h, str) or not _HASH_RE.match(h):
            raise ValueError(f"invalid bundle member ({connector!r}, {h!r})")
    if len(members) == 1:
        return members[0][1]
    return sha256_hex(
        {"v": BUNDLE_HASH_VERSION, "change_sets": [{"connector": c, "hash": h} for c, h in members]}
    )


class AtomicGroup(ContractModel):
    """Items that must be applied together (``optimizer.Action.group``): one connector,
    adjacent ``seqs`` (in order) of that connector's change set."""

    group: Code
    connector: Code
    seqs: Annotated[list[PositiveInt], Field(min_length=1)]

    @model_validator(mode="after")
    def _contiguous(self) -> AtomicGroup:
        if self.seqs != list(range(self.seqs[0], self.seqs[0] + len(self.seqs))):
            raise ValueError(f"atomic group {self.group!r} must list adjacent seqs in order")
        return self


class ChangeSetBundle(ContractModel):
    """The change sets one plan option compiles to: one per target connector, ``change_sets``
    in apply order, one approval for all (:attr:`hash`). Atomic groups are never split: each
    group appears once, names one connector of the bundle, and its seqs exist in that
    connector's change set. If any member's dry-run fails, nothing in the bundle is applied
    (engine rule)."""

    id: Code
    tenant: TenantSlug
    change_sets: Annotated[list[ChangeSet], Field(min_length=1, max_length=MAX_BUNDLE_CONNECTORS)]
    atomic_groups: list[AtomicGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ChangeSetBundle:
        connectors = [cs.connector for cs in self.change_sets]
        if len(set(connectors)) != len(connectors):
            raise ValueError("a bundle has one change set per connector")
        ids = [cs.id for cs in self.change_sets]
        if len(set(ids)) != len(ids):
            raise ValueError("bundle change set ids must be unique")
        if any(cs.tenant != self.tenant for cs in self.change_sets):
            raise ValueError("every change set in a bundle belongs to the bundle's tenant")
        names = [g.group for g in self.atomic_groups]
        if len(set(names)) != len(names):
            raise ValueError("an atomic group appears once (it is never split across connectors)")
        by_connector = {cs.connector: cs for cs in self.change_sets}
        claimed: dict[tuple[str, int], str] = {}
        for g in self.atomic_groups:
            member = by_connector.get(g.connector)
            if member is None:
                raise ValueError(f"atomic group {g.group!r} names connector {g.connector!r} outside the bundle")
            seqs = {item.seq for item in member.items}
            if missing := [n for n in g.seqs if n not in seqs]:
                raise ValueError(f"atomic group {g.group!r} names seqs {missing} not in {g.connector}'s change set")
            for n in g.seqs:
                other = claimed.setdefault((g.connector, n), g.group)
                if other != g.group:
                    raise ValueError(f"{g.connector} seq {n} is in atomic groups {other!r} and {g.group!r}")
        return self

    @property
    def connectors(self) -> list[str]:
        """Target connectors in apply order."""
        return [cs.connector for cs in self.change_sets]

    @property
    def hash(self) -> str:
        """The hash the bundle's single approval binds to (:func:`bundle_hash`)."""
        return bundle_hash([(cs.connector, cs.hash) for cs in self.change_sets])

    @property
    def ops(self) -> set[str]:
        return {op for cs in self.change_sets for op in cs.ops}

    @property
    def lines(self) -> int:
        return sum(len(cs.items) for cs in self.change_sets)

    def change_set(self, connector: str) -> ChangeSet | None:
        return next((cs for cs in self.change_sets if cs.connector == connector), None)
