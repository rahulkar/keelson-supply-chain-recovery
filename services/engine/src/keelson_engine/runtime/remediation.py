"""Remediation helpers (M5, ``docs/contracts/95-m5-remediation.md`` §2, §4, §5) — pure functions.

* :func:`partition_actions` — a plan option's actions → **apply lines** per target connector
  (op ownership from the :class:`~keelson_engine.runtime.candidates.CapabilityTable`: the first
  write connector that applies the op through an idempotent change set) and **guide lines** per
  connector (everything else, or all lines in Guide mode). Atomic groups are never split: a
  group with any guide line, or with lines owned by different connectors, is guided whole; M7: so
  is a group on a per-op (tier-A) connector unless every member op is idempotency-enforced.
* :func:`remediation_mix` — the ``{apply_lines, guide_lines}`` counts shown on the option card.
* :func:`plan_dependencies` — a plan's dependency fingerprint (lanes, DCs, PO lines, connectors),
  stored in ``plan.kpis.dependencies`` and indexed through the plans port, so a
  ``lane.capacity_changed`` webhook finds the plans it affects.
* :func:`delta_actions` — the **delta change set** of a signal re-plan: the re-simulated
  option's lines that are not already applied, plus compensations of committed lines on the
  perturbed lane that no longer fit its capacity (``capacity_booking.cancel`` / smaller
  re-booking, ``po_line.reschedule`` of a withdrawn sea-air split).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from keelson_contracts.commit import atomic_group_bulk_allowed
from keelson_contracts.optimizer import ProblemData

from .candidates import CapabilityTable

__all__ = [
    "BOOKING_CANCEL_OP",
    "DeltaResult",
    "Partition",
    "action_lanes",
    "applied_items",
    "delta_actions",
    "lane_label",
    "natural_key",
    "partition_actions",
    "plan_dependencies",
    "remediation_mix",
]

BOOKING_CANCEL_OP = "capacity_booking.cancel"
"""Compensation of a capacity booking (Merxa M5 op; ``target_ref`` = the booking code)."""
LANE_OPS = frozenset({"po_line.split", "capacity_booking.create", BOOKING_CANCEL_OP})


# ───────────────────────────────── partition ─────────────────────────────────


@dataclass
class Partition:
    """``apply`` / ``guide``: connector → actions (plan order kept)."""

    apply: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    guide: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def apply_lines(self) -> int:
        return sum(len(v) for v in self.apply.values())

    @property
    def guide_lines(self) -> int:
        return sum(len(v) for v in self.guide.values())


def partition_actions(actions: Sequence[Mapping[str, Any]], table: CapabilityTable, *, guide_all: bool = False,
                      guide_connector: str | None = None) -> Partition:
    """Route each action to its connector (see the module docstring). Guide lines go to the
    connector that declares the op (else ``guide_connector`` / the planning connector)."""
    primary = guide_connector or table.primary.connector
    owners: list[str | None] = []
    for action in actions:
        owner = None if guide_all else table.owner(str(action["op"]))
        owners.append(owner.connector if owner is not None else None)
    # atomic groups: one connector, all applied, or guided whole
    groups: dict[str, set[str | None]] = defaultdict(set)
    for action, owner in zip(actions, owners, strict=True):
        if action.get("group"):
            groups[str(action["group"])].add(owner)
    # M7 (110 §4): an atomic group on a per-op (tier-A) connector is bulk-applied only when every
    # member op is idempotency-enforced; otherwise the whole group is guided
    unsafe: set[str] = set()
    for action, owner in zip(actions, owners, strict=True):
        group = action.get("group")
        caps = table.get(owner) if owner is not None else None
        if group and caps is not None and caps.per_op:
            if not atomic_group_bulk_allowed([caps.op_idempotency.get(str(a["op"]), "none")
                                              for a in actions if a.get("group") == group]):
                unsafe.add(str(group))
    out = Partition()
    for action, owner in zip(actions, owners, strict=True):
        group = action.get("group")
        if group and (len(groups[str(group)]) > 1 or None in groups[str(group)] or str(group) in unsafe):
            owner = None
        if owner is not None:
            out.apply.setdefault(owner, []).append(dict(action))
            continue
        declared = next((c.connector for c in table.connectors if str(action["op"]) in c.supported_ops), primary)
        out.guide.setdefault(declared, []).append(dict(action))
    return out


def remediation_mix(actions: Sequence[Mapping[str, Any]], table: CapabilityTable) -> dict[str, int]:
    part = partition_actions(actions, table)
    return {"apply_lines": part.apply_lines, "guide_lines": part.guide_lines}


# ─────────────────────────────── dependencies ───────────────────────────────


def action_lanes(action: Mapping[str, Any], po_lanes: Mapping[str, str | None] | None = None) -> set[str]:
    """Lanes an action uses: ``payload.lane_code`` (sea-air splits, bookings), the DC lane
    ``FROM>TO`` of a transfer, and the inbound lane of a PO line it changes."""
    payload = action.get("payload") or {}
    out: set[str] = set()
    if isinstance(payload, Mapping):
        if payload.get("lane_code"):
            out.add(str(payload["lane_code"]))
        if payload.get("from_dc") and payload.get("to_dc"):
            out.add(f"{payload['from_dc']}>{payload['to_dc']}")
    ref = action.get("target_ref")
    if po_lanes and isinstance(ref, str) and ref in po_lanes and po_lanes[ref]:
        out.add(str(po_lanes[ref]))
    return out


def plan_dependencies(options: Sequence[Mapping[str, Any]], problem: ProblemData | None,
                      connectors: Sequence[str] = (), committed: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """``{lanes, dcs, po_lines, connectors, by_option: {id: {lanes, po_lines}}, committed_lanes}``
    (sorted lists; ``committed`` = applied change-set items)."""
    po_lanes = {pl.ref: pl.lane_code for pl in problem.po_lines} if problem is not None else {}
    lanes: set[str] = set()
    dcs: set[str] = set()
    po_lines: set[str] = set()
    by_option: dict[str, dict[str, list[str]]] = {}
    for option in options:
        o_lanes: set[str] = set()
        o_lines: set[str] = set()
        for action in option.get("actions") or []:
            o_lanes |= action_lanes(action, po_lanes)
            ref = action.get("target_ref")
            if isinstance(ref, str) and "#" in ref:
                o_lines.add(ref)
            payload = action.get("payload") or {}
            for key in ("from_dc", "to_dc", "dest_dc"):
                if isinstance(payload, Mapping) and payload.get(key):
                    dcs.add(str(payload[key]))
        # receipts the option relies on travel on their PO lines' lanes
        for decision in option.get("decisions") or []:
            ref = decision.get("po_line_ref") if isinstance(decision, Mapping) else None
            if ref:
                o_lines.add(str(ref))
                if po_lanes.get(str(ref)):
                    o_lanes.add(str(po_lanes[str(ref)]))
        lanes |= o_lanes
        po_lines |= o_lines
        by_option[str(option.get("id"))] = {"lanes": sorted(o_lanes), "po_lines": sorted(o_lines)}
    committed_lanes: set[str] = set()
    for item in committed:
        committed_lanes |= action_lanes(item, po_lanes)
    return {
        "v": 1,
        "lanes": sorted(lanes | committed_lanes),
        "dcs": sorted(dcs),
        "po_lines": sorted(po_lines),
        "connectors": sorted(set(connectors)),
        "by_option": by_option,
        "committed_lanes": sorted(committed_lanes),
    }


def lane_label(mode: str | None) -> str:
    return {"sea_air": "sea-air", "air": "air", "sea": "sea", "road": "road"}.get(str(mode or ""), "lane")


# ───────────────────────────────── delta ─────────────────────────────────


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def natural_key(item: Mapping[str, Any]) -> tuple[str, ...]:
    """What an action changes (two items with the same key touch the same thing)."""
    op = str(item["op"])
    p = item.get("payload") or {}
    ref = str(item.get("target_ref") or "")
    if op == "transfer_order.create":
        return (op, str(p.get("from_dc")), str(p.get("to_dc")), str(p.get("ship_date")))
    if op == "capacity_booking.create":
        return (op, str(p.get("lane_code")), str(p.get("iso_week")))
    if op == "po_line.split":
        return (op, ref, str(p.get("lane_code")))
    if op == "po.create":
        return (op, str(p.get("supplier_code")), str(p.get("factory_code")), str(p.get("dest_dc")),
                str(p.get("eta_dc")))
    if op == "production_order.resequence":
        return (op, str(p.get("line_code")))
    if ref:
        return (op, ref)
    return (op, _canon(p))


def _lines(p: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for line in p.get("lines") or []:
        out[str(line["sku"])] += int(line["qty"])
    return dict(out)


def _qty(item: Mapping[str, Any]) -> int:
    p = item.get("payload") or {}
    if item["op"] == "capacity_booking.create":
        return int(p.get("qty_units") or 0)
    if "lines" in p:
        return sum(_lines(p).values())
    return int(p.get("qty") or 0)


@dataclass
class DeltaResult:
    actions: list[dict[str, Any]]
    """Delta lines in apply order (``seq`` 1..n, ``group`` kept)."""
    added: int = 0
    changed: int = 0
    cancelled_bookings: int = 0
    rebooked: int = 0
    rescheduled: int = 0
    unchanged: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> list[str]:
        parts: list[str] = []
        if self.cancelled_bookings:
            parts.append(f"cancel {self.cancelled_bookings} capacity booking{'s' if self.cancelled_bookings != 1 else ''}")
        if self.rebooked:
            parts.append(f"re-book {self.rebooked} smaller hold{'s' if self.rebooked != 1 else ''}")
        if self.rescheduled:
            parts.append(f"reschedule {self.rescheduled} PO line{'s' if self.rescheduled != 1 else ''}")
        if self.added:
            parts.append(f"add {self.added} new change{'s' if self.added != 1 else ''}")
        if self.changed:
            parts.append(f"update {self.changed} line{'s' if self.changed != 1 else ''}")
        return parts


def applied_items(change_sets: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Items of the case's applied/verified change sets, each with ``created_ref`` (the entity
    the target system created for it, when the read-back reported one) and ``change_set_id``."""
    out: list[dict[str, Any]] = []
    for cs in change_sets:
        if str(cs.get("status") or "") not in ("applied", "verified"):
            continue
        refs = ((cs.get("result") or {}).get("created_refs") or {}) if isinstance(cs.get("result"), Mapping) else {}
        for item in cs.get("items") or []:
            row = dict(item)
            row["change_set_id"] = cs.get("id")
            row["option_id"] = cs.get("option_id")
            row["plan_version"] = cs.get("plan_version")
            row["connector"] = cs.get("connector")
            ref = refs.get(str(item.get("seq")))
            if ref:
                row["created_ref"] = str(ref)
            out.append(row)
    return out


def delta_actions(
    new_actions: Sequence[Mapping[str, Any]],
    applied: Sequence[Mapping[str, Any]],
    *,
    dirty_lane: str,
    lane_capacity: int | None,
    po_etas: Mapping[str, str] | None = None,
    overbooked: Mapping[str, int] | None = None,
) -> DeltaResult:
    """The delta change set (see the module docstring).

    ``new_actions`` = the re-simulated option's actions on the current (post-apply) state, i.e.
    what is still needed; an action identical to an applied one (same natural key and value) is
    already in place. ``applied`` = :func:`applied_items`. Committed lines on ``dirty_lane``:
    bookings per week above ``lane_capacity`` (units per week after the cut; ``None`` = unknown,
    nothing is compensated) are cancelled (and re-booked at what still fits), and a sea-air split
    whose week no longer has any capacity is withdrawn with ``po_line.reschedule`` back to the
    line's planned ETA (``po_etas[ref]``). Applied decisions elsewhere stay fixed.

    ``overbooked`` (ISO week → units; the signal's fact over *all* bookings on the lane, other
    shippers' included): in such a week our holds give back at least that many units, even when
    they alone fit ``lane_capacity``."""
    by_key: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for item in applied:
        by_key[natural_key(item)].append(item)
    out: list[dict[str, Any]] = []
    result = DeltaResult(actions=out)

    # 1. compensations on the dirty lane (committed decisions that no longer fit its capacity)
    compensated = False
    over = {str(w): int(u) for w, u in (overbooked or {}).items() if int(u) > 0}
    if lane_capacity is not None or over:
        cap = max(0, int(lane_capacity)) if lane_capacity is not None else None
        weekly: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        splits: list[Mapping[str, Any]] = []
        # bookings an earlier (applied) delta already cancelled no longer hold capacity
        cancelled = Counter(str(i.get("target_ref")) for i in applied
                            if i["op"] == BOOKING_CANCEL_OP and i.get("target_ref"))
        for item in applied:
            p = item.get("payload") or {}
            if str(p.get("lane_code")) != dirty_lane:
                continue
            if item["op"] == "capacity_booking.create":
                ref = str(item.get("created_ref") or f"{dirty_lane}@{p.get('iso_week')}")
                if cancelled[ref] > 0:
                    cancelled[ref] -= 1  # one cancel releases one booking (fallback refs repeat per week)
                    continue
                weekly[str(p.get("iso_week"))].append(item)
            elif item["op"] == "po_line.split" and not _withdrawn(item, applied):
                splits.append(item)
        carried = 0  # units the lane still carries for the committed splits (after re-booking)
        for week in sorted(weekly):
            booked = sum(_qty(i) for i in weekly[week])
            fits = booked if cap is None else min(booked, cap)
            if week in over:
                fits = min(fits, max(0, booked - over[week]))
            if booked <= fits:
                carried += booked
                continue
            compensated = True
            for item in weekly[week]:
                ref = item.get("created_ref") or f"{dirty_lane}@{week}"
                out.append({"op": BOOKING_CANCEL_OP, "target_ref": str(ref), "expected_version": None, "payload": {}})
                result.cancelled_bookings += 1
            if fits > 0:
                out.append({"op": "capacity_booking.create", "target_ref": None, "expected_version": None,
                            "payload": {"lane_code": dirty_lane, "iso_week": week, "qty_units": fits}})
                result.rebooked += 1
            carried += fits
        if compensated:
            # withdraw the latest splits until the committed sea-air quantity fits what is still held
            remaining = sum(_qty(s) for s in splits)
            for item in sorted(splits, key=lambda s: int(s.get("seq") or 0), reverse=True):
                if remaining <= carried:
                    break
                ref = str(item.get("target_ref") or "")
                eta = (po_etas or {}).get(ref)
                remaining -= _qty(item)
                if not ref or not eta:
                    result.notes.append(f"the sea-air split of {ref or 'a PO line'} needs a manual follow-up")
                    continue
                out.append({"op": "po_line.reschedule", "target_ref": ref, "expected_version": None,
                            "payload": {"eta_dc": eta}})
                result.rescheduled += 1

    # 2. what the re-simulation still needs, minus what is already applied
    for action in sorted(new_actions, key=lambda a: int(a.get("seq") or 0)):
        key = natural_key(action)
        p = dict(action.get("payload") or {})
        same = by_key.get(key, [])
        if compensated and str(action["op"]) in LANE_OPS and str(p.get("lane_code")) == dirty_lane:
            # the cut lane is being re-fitted above: never re-add holds on it in the same delta
            result.unchanged += 1
            continue
        if not same:
            out.append(_copy(action))
            result.added += 1
            continue
        if action["op"] in ("transfer_order.create", "po.create"):
            have = defaultdict(int)
            for prev in same:
                for sku, qty in _lines(prev.get("payload") or {}).items():
                    have[sku] += qty
            extra = [{"sku": sku, "qty": qty - have.get(sku, 0)} for sku, qty in sorted(_lines(p).items())
                     if qty > have.get(sku, 0)]
            if not extra:
                result.unchanged += 1
                continue
            item = _copy(action)
            item["payload"] = {**p, "lines": extra}
            out.append(item)
            result.added += 1
            continue
        if action["op"] in ("capacity_booking.create", "po_line.split"):
            have_qty = sum(_qty(prev) for prev in same)
            want = _qty(action)
            if want <= have_qty:
                result.unchanged += 1
                continue
            item = _copy(action)
            key_name = "qty_units" if action["op"] == "capacity_booking.create" else "qty"
            item["payload"] = {**p, key_name: want - have_qty}
            out.append(item)
            result.added += 1
            continue
        if any(_canon(prev.get("payload") or {}) == _canon(p) for prev in same):
            result.unchanged += 1
            continue
        out.append(_copy(action))
        result.changed += 1
    for seq, item in enumerate(out, start=1):
        item["seq"] = seq
    return result


def _withdrawn(split: Mapping[str, Any], applied: Sequence[Mapping[str, Any]]) -> bool:
    """A sea-air split an applied later plan version already withdrew (``po_line.reschedule`` of
    the same PO line), so it no longer uses the lane."""
    ref = split.get("target_ref")
    version = int(split.get("plan_version") or 0)
    return bool(ref) and any(
        i["op"] == "po_line.reschedule" and i.get("target_ref") == ref and int(i.get("plan_version") or 0) > version
        for i in applied
    )


def _copy(action: Mapping[str, Any]) -> dict[str, Any]:
    item = {k: action.get(k) for k in ("op", "target_ref", "expected_version", "payload")}
    item["payload"] = dict(item["payload"] or {})
    if action.get("group"):
        item["group"] = action["group"]
    return item
