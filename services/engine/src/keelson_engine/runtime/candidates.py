"""Deterministic candidate plans and the connector capability check (``80-m3-deterministic-core.md`` §4).

``simulate`` evaluates every candidate of ``AgentSpec.planning.candidates`` (default A–D) whose
levers are enabled for the tenant, plus "Do nothing". M5 (``95`` §2): a lever applies only when
every op it emits applies (its decisions are atomic groups); a candidate none of whose levers
applies is ``guided_only`` (Apply disabled, Guide only) with a reason, while a candidate with at
least one applicable lever stays applicable — ``compile_changeset`` applies those lines and
guides the rest (``Candidate.guide_ops``). The capability table
(:class:`CapabilityTable`) lists, per write connector (planning connector first, then
``AgentSpec.remediation.connectors``), the ops it applies:

* the connector must expose approved change-set tools (``create_change_set``,
  ``add_change_set_items``, ``validate_change_set``, ``apply_change_set``);
* every op of every lever (:data:`keelson_contracts.optimizer.LEVER_OPS`) must be supported. The
  supported ops come from the connector config (``supported_ops``), else from the
  ``add_change_set_items`` input schema (an ``op`` enum, or the ``a.b | c.d`` list in the ``op``
  description, as Merxa publishes it), else the M1 ops;
* (M5) ``apply_change_set`` must have ``idempotency = enforced`` (replays are safe); otherwise
  every line of that connector becomes a guide step.
* (M7, ``110`` §4) a **tier-A** connector without change-set tools applies **per op** the ops its
  action mapping maps to an approved write tool (with an approved read tool for the read-back);
  ``op_idempotency`` keeps each op's effective idempotency (the mapping's claim, never above the
  tool's confirmed one). Unmapped ops are guide steps.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from keelson_contracts.changeset import M1_OPS
from keelson_contracts.optimizer import LEVER_OPS, LEVERS, PlanSpec

from .robustness import plan_weights
from .spec import ALL_LEVERS, PlanningDefaults

__all__ = [
    "CHANGE_SET_TOOLS",
    "Candidate",
    "CapabilityTable",
    "capability_table",
    "ConnectorCapabilities",
    "DO_NOTHING_ID",
    "build_candidates",
    "capability_check",
    "connector_capabilities",
    "guide_ops",
    "lever_specs",
    "ops_from_schema",
]

DO_NOTHING_ID = "do-nothing"
CHANGE_SET_TOOLS = ("create_change_set", "add_change_set_items", "validate_change_set", "apply_change_set")
_OP_TOKEN = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b")


@dataclass(frozen=True)
class ConnectorCapabilities:
    connector: str
    vendor: str
    missing_tools: tuple[str, ...]
    supported_ops: frozenset[str]
    ops_source: str
    """``config`` | ``tool_schema`` | ``default`` (M1 ops)."""
    apply_idempotent: bool = True
    """M5: ``apply_change_set`` replays are safe (``idempotency = enforced``)."""
    tools: frozenset[str] = frozenset()
    """Approved tool names of the connector (e.g. ``get_operator_guide``, ``rollback_change_set``)."""
    commit_mode: str = "change_set"
    """``change_set`` (tier W) or ``per_op`` (M7 tier A with an action mapping)."""
    op_idempotency: Mapping[str, str] = field(default_factory=dict)
    """M7 per-op: op → effective idempotency (``enforced | best_effort | none``)."""
    action_mapping_version: int | None = None

    @property
    def per_op(self) -> bool:
        return self.commit_mode == "per_op"

    @property
    def can_apply(self) -> bool:
        if self.per_op:
            return bool(self.supported_ops)
        return not self.missing_tools and self.apply_idempotent

    def applies(self, op: str) -> bool:
        """The connector applies ``op`` through its change sets (capability table entry)."""
        return self.can_apply and op in self.supported_ops

    def has_tool(self, name: str) -> bool:
        return name in self.tools


@dataclass(frozen=True)
class CapabilityTable:
    """Write connectors in precedence order (planning connector first)."""

    connectors: tuple[ConnectorCapabilities, ...]

    @property
    def primary(self) -> ConnectorCapabilities:
        return self.connectors[0]

    def owner(self, op: str) -> ConnectorCapabilities | None:
        """The first connector that applies ``op`` (``None`` = a guide line)."""
        return next((c for c in self.connectors if c.applies(op)), None)

    def get(self, connector: str) -> ConnectorCapabilities | None:
        return next((c for c in self.connectors if c.connector == connector), None)

    @property
    def supported_ops(self) -> frozenset[str]:
        return frozenset(op for c in self.connectors if c.can_apply for op in c.supported_ops)


@dataclass(frozen=True)
class Candidate:
    id: str
    title: str
    levers: tuple[str, ...]
    plan_spec: PlanSpec
    ops: tuple[str, ...]
    guided_only: bool
    guided_reason: str | None
    guide_ops: tuple[str, ...] = ()
    """M5: ops of this candidate no connector applies (their lines become guide steps)."""


def ops_from_schema(schema: Any) -> set[str]:
    """Ops a change-set tool declares for its items' ``op``: an ``enum``/``const`` or the op names
    listed in the field's description (``transfer_order.create | po.create | …``)."""
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            props = node.get("properties")
            if isinstance(props, Mapping) and isinstance(props.get("op"), Mapping):
                op = props["op"]
                for key in ("enum",):
                    for value in op.get(key) or ():
                        if isinstance(value, str):
                            found.add(value)
                if isinstance(op.get("const"), str):
                    found.add(op["const"])
                if not op.get("enum") and isinstance(op.get("description"), str):
                    found.update(_OP_TOKEN.findall(op["description"]))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return {op for op in found if not op.startswith("revert.")}


_IDEMPOTENCY_RANK = {"none": 0, "best_effort": 1, "enforced": 2}


def per_op_capabilities(mapping: Any, tools: Mapping[str, Any]) -> dict[str, str]:
    """M7: op → effective idempotency for the ops an action mapping can apply on this connector:
    the action tool is an approved write/destructive tool, the read-back tool (when the mapping
    has one) an approved read tool, and the effective idempotency is the mapping's claim capped by
    the tool's confirmed one (unconfirmed tools: the claim, already checked by the admin PUT)."""
    out: dict[str, str] = {}
    for op_map in getattr(mapping, "ops", None) or ():
        tool = tools.get(op_map.tool)
        if tool is None or tool.access == "read" or tool.status not in ("approved", "approval_gate"):
            continue
        rb = op_map.readback
        if rb is not None:
            rb_tool = tools.get(rb.tool)
            if rb_tool is None or rb_tool.access != "read" or rb_tool.status != "approved":
                continue
        claim = str(getattr(op_map.idempotency, "value", op_map.idempotency))
        confirmed = getattr(tool, "idempotency", None)
        if confirmed in _IDEMPOTENCY_RANK and _IDEMPOTENCY_RANK[confirmed] < _IDEMPOTENCY_RANK[claim]:
            claim = confirmed
        if claim != "enforced" and rb is None:
            continue  # unknown outcomes could never be read back: guided
        if claim == "enforced" and op_map.request_id_arg is None:
            continue
        out[str(op_map.op)] = claim
    return out


async def connector_capabilities(deps: Any, *, tenant: str, connector: str, vendor: str) -> ConnectorCapabilities:
    tools = {t.name: t for t in await deps.connectors.list_approved_tools(tenant=tenant, connector=connector)}
    missing = tuple(name for name in CHANGE_SET_TOOLS if name not in tools)
    if missing:
        # M7: no change sets — a tier-A connector with an action mapping applies per op
        from .tier_a import load_action_mapping  # noqa: PLC0415 - import cycle (tier_a → errors only)

        mapping = await load_action_mapping(deps, tenant=tenant, connector=connector)
        if mapping is not None:
            ops = per_op_capabilities(mapping, tools)
            if ops:
                return ConnectorCapabilities(
                    connector, vendor, missing, frozenset(ops), "action_mapping", True, frozenset(tools),
                    commit_mode="per_op", op_idempotency=dict(ops),
                    action_mapping_version=getattr(mapping, "version", None),
                )
    apply_tool = tools.get("apply_change_set")
    idempotent = apply_tool is None or bool(getattr(apply_tool, "idempotency_enforced", True))
    names = frozenset(tools)
    info = await deps.connectors.get_connector(tenant=tenant, slug=connector)
    declared = (getattr(info, "config", None) or {}).get("supported_ops")
    if declared:
        return ConnectorCapabilities(connector, vendor, missing, frozenset(str(o) for o in declared), "config",
                                     idempotent, names)
    add = tools.get("add_change_set_items")
    ops = ops_from_schema(add.input_schema) if add is not None else set()
    if ops:
        return ConnectorCapabilities(connector, vendor, missing, frozenset(ops), "tool_schema", idempotent, names)
    return ConnectorCapabilities(connector, vendor, missing, frozenset(M1_OPS), "default", idempotent, names)


async def capability_table(deps: Any, *, tenant: str, connectors: Sequence[str], vendor: str) -> CapabilityTable:
    """The capability table of the write connectors (``connectors[0]`` = the planning connector,
    labelled ``vendor``; the others carry their registry label). A further connector whose registry
    entry cannot be read is left out (its lines then go to another connector or become guide steps)."""
    out: list[ConnectorCapabilities] = []
    for i, slug in enumerate(dict.fromkeys(connectors)):
        label = vendor
        if i:
            try:
                label = (await deps.connectors.get_connector(tenant=tenant, slug=slug)).label
            except Exception:  # an unreadable extra connector never blocks the planning connector
                continue
        out.append(await connector_capabilities(deps, tenant=tenant, connector=slug, vendor=label))
    return CapabilityTable(tuple(out))


def _caps_list(caps: ConnectorCapabilities | CapabilityTable) -> tuple[ConnectorCapabilities, ...]:
    return caps.connectors if isinstance(caps, CapabilityTable) else (caps,)


def _lever_applies(code: str, table: Sequence[ConnectorCapabilities]) -> bool:
    """A lever's lines are applied only when every op it emits applies (its ops form one atomic
    group per decision, which is never split: one unsupported op guides the whole group)."""
    ops = LEVER_OPS.get(code, ())
    return bool(ops) and all(any(c.applies(op) for c in table) for op in ops)


def capability_check(levers: Iterable[str], caps: ConnectorCapabilities | CapabilityTable) -> tuple[bool, str | None]:
    """``(guided_only, reason)`` for a candidate using ``levers``: guided-only when none of its
    levers can be applied (M5: a candidate with at least one applicable lever stays applicable;
    the other levers' lines are guide steps, :func:`guide_ops`)."""
    levers = list(levers)
    if not levers:
        return False, None
    table = _caps_list(caps)
    primary = table[0]
    if not any(c.can_apply for c in table):
        if primary.missing_tools:
            return True, (
                f"{primary.vendor} change-set tools are not available to Keelson ({', '.join(primary.missing_tools)}), "
                "so this option can only be followed as guided steps."
            )
        return True, (f"{primary.vendor} cannot safely replay change-set applies, "
                      "so this option can only be followed as guided steps.")
    if any(_lever_applies(code, table) for code in levers):
        return False, None
    needed = sorted({op for code in levers for op in LEVER_OPS.get(code, ())})
    unsupported = [op for op in needed if not any(c.applies(op) for c in table)] or needed
    return True, (
        f"{primary.vendor} cannot apply {', '.join(unsupported)} through its change sets, "
        "so this option can only be followed as guided steps."
    )


def guide_ops(levers: Iterable[str], caps: ConnectorCapabilities | CapabilityTable) -> tuple[str, ...]:
    """Ops of the levers that cannot be applied (their lines are guided, M5)."""
    table = _caps_list(caps)
    return tuple(sorted({op for code in levers if not _lever_applies(code, table) for op in LEVER_OPS.get(code, ())}))


def lever_specs(codes: Sequence[str], planning: PlanningDefaults) -> list[dict[str, Any]]:
    """``plan_spec.levers`` entries with the tenant's params (validated by the lever model)."""
    out: list[dict[str, Any]] = []
    for code in codes:
        params: dict[str, Any] = {}
        if code == "reallocate_inventory":
            params = {
                "max_units_total": planning.rebalance_max_units_total,
                "allowed_lanes": list(planning.rebalance_allowed_lanes) if planning.rebalance_allowed_lanes else None,
            }
        params.update(planning.lever_params.get(code) or {})
        LEVERS[code].params_model.model_validate(params)
        out.append({"code": code, "params": params})
    return out


def build_candidates(planning: PlanningDefaults, caps: ConnectorCapabilities | CapabilityTable) -> list[Candidate]:
    """Candidates restricted to the enabled levers (catalogue order), deduplicated by lever set;
    a candidate left without levers is dropped. "Do nothing" is not included. M8 (``126`` §4): the
    LP weights are the default weight profile's (``planning.scoring``; 10000 / 10000 by default)."""
    enabled = [code for code in ALL_LEVERS if code in planning.enabled_levers]
    seen: set[tuple[str, ...]] = set()
    out: list[Candidate] = []
    for cand in planning.candidates:
        wanted = set(enabled) if cand.all_enabled else set(cand.levers)
        levers = tuple(code for code in enabled if code in wanted)
        if not levers or levers in seen:
            continue
        seen.add(levers)
        guided, reason = capability_check(levers, caps)
        spec = PlanSpec.model_validate({"id": cand.id, "title": cand.title, "levers": lever_specs(levers, planning),
                                        "weights": plan_weights(planning)})
        ops = tuple(dict.fromkeys(op for code in levers for op in LEVER_OPS[code]))
        out.append(Candidate(cand.id, cand.title, levers, spec, ops, guided, reason,
                             () if guided else guide_ops(levers, caps)))
    return out
