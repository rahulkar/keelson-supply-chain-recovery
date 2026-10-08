"""Disruption scenarios: selectors, windows and the generic perturbation union.

Every disruption compiles to the same small set of primitives (plan §3.2)::

    capacity_scale · lead_time_shift · demand_scale · cost_scale
    yield_scale · compliance_block · inventory_hold

Numeric conventions:

* scale factors are **basis points** (``factor_bp``; ``10000`` = x1.0);
* the primary value of each perturbation is its **p50** (e.g. ``days``,
  ``factor_bp``, ``qty``); optional ``p10`` / ``p90`` carry the uncertainty band
  in the *same unit* and must satisfy ``p10 <= p50 <= p90``.

The JSON shape is a superset of the optimizer contract (``40-optimizer.md``)::

    {"op":"lead_time_shift","target":{"kinds":["receipt"],"ids":["PO-26-0101#1"]},"days":7,"prov":"decision"}
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import Field, StrictBool, StrictInt, model_validator

from .common import (
    CaseMode,
    ContractModel,
    EntityRef,
    EventFamily,
    EvidenceId,
    IsoDate,
    IsoWeek,
    NonNegInt,
    Provenance,
    TenantSlug,
    sha256_hex,
)

__all__ = [
    "Assumption",
    "CapacityScale",
    "ComplianceBlock",
    "CostScale",
    "DemandScale",
    "DisruptionScenario",
    "FINGERPRINT_SCHEME",
    "InventoryHold",
    "LeadTimeShift",
    "PERTURBATION_OPS",
    "Perturbation",
    "Selector",
    "SelectorKind",
    "Window",
    "YieldScale",
    "fingerprint",
]

SelectorKind = Literal[
    "receipt",
    "po",
    "po_line",
    "sku",
    "style",
    "cluster",
    "store",
    "dc",
    "lane",
    "supplier",
    "factory",
    "component",
    "port",
    "chokepoint",
    "market",
]
"""What a perturbation targets. Lane ids are ``"<from_dc>><to_dc>"`` (e.g. ``DE1>NL1``);
receipt / PO line ids are ``"<po_code>#<line_no>"``."""


class Selector(ContractModel):
    """Explicit set of entities a perturbation applies to (ids are always resolved codes)."""

    kinds: Annotated[list[SelectorKind], Field(min_length=1)]
    ids: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> Selector:
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("selector kinds must be unique")
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("selector ids must be unique")
        return self


class Window(ContractModel):
    """Inclusive date window during which a perturbation is active (``end=None`` = open-ended)."""

    start: IsoDate
    end: IsoDate | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Window:
        if self.end is not None and self.end < self.start:
            raise ValueError("window end must be >= start")
        return self


class _PerturbationBase(ContractModel):
    target: Selector
    prov: Provenance
    window: Window | None = None


class _Banded(_PerturbationBase):
    """Perturbation whose primary value is the p50 of an optional p10/p90 band."""

    p10: StrictInt | None = None
    p90: StrictInt | None = None

    def _p50(self) -> int | None:  # overridden by subclasses
        raise NotImplementedError

    @model_validator(mode="after")
    def _band_ordered(self) -> _Banded:
        p50 = self._p50()
        if p50 is None:
            if self.p10 is not None or self.p90 is not None:
                raise ValueError("p10/p90 require a primary (p50) value")
            return self
        if self.p10 is not None and self.p10 > p50:
            raise ValueError(f"p10 ({self.p10}) must be <= p50 ({p50})")
        if self.p90 is not None and self.p90 < p50:
            raise ValueError(f"p90 ({self.p90}) must be >= p50 ({p50})")
        return self


class CapacityScale(_Banded):
    """Scale throughput capacity (lanes, DCs, factories, chokepoints). ``0`` = closed."""

    op: Literal["capacity_scale"]
    factor_bp: NonNegInt

    def _p50(self) -> int:
        return self.factor_bp


class LeadTimeShift(_Banded):
    """Shift arrival/lead time by ``days`` (negative = earlier)."""

    op: Literal["lead_time_shift"]
    days: StrictInt

    def _p50(self) -> int:
        return self.days


class DemandScale(_Banded):
    """Scale demand; ``weeks=None`` applies to every week in the horizon/window."""

    op: Literal["demand_scale"]
    factor_bp: NonNegInt
    weeks: Annotated[list[IsoWeek], Field(min_length=1)] | None = None

    def _p50(self) -> int:
        return self.factor_bp

    @model_validator(mode="after")
    def _unique_weeks(self) -> DemandScale:
        if self.weeks is not None and len(set(self.weeks)) != len(self.weeks):
            raise ValueError("demand_scale weeks must be unique")
        return self


class CostScale(_Banded):
    """Scale a cost component (unit cost, freight, duty, or all)."""

    op: Literal["cost_scale"]
    factor_bp: NonNegInt
    cost_component: Literal["unit_cost", "freight", "duty", "all"] = "all"

    def _p50(self) -> int:
        return self.factor_bp


class YieldScale(_Banded):
    """Scale production yield (good units per planned unit)."""

    op: Literal["yield_scale"]
    factor_bp: NonNegInt

    def _p50(self) -> int:
        return self.factor_bp


class ComplianceBlock(_PerturbationBase):
    """Entities that must not be used (sanctions, failed audit, customs hold) during ``window``."""

    op: Literal["compliance_block"]
    reason: Annotated[str, Field(min_length=1)]


class InventoryHold(_Banded):
    """Stock that cannot be shipped (quality hold); ``qty=None`` holds everything at the target."""

    op: Literal["inventory_hold"]
    qty: Annotated[StrictInt, Field(ge=1)] | None = None
    reason: Annotated[str, Field(min_length=1)]

    def _p50(self) -> int | None:
        return self.qty


Perturbation = Annotated[
    CapacityScale
    | LeadTimeShift
    | DemandScale
    | CostScale
    | YieldScale
    | ComplianceBlock
    | InventoryHold,
    Field(discriminator="op"),
]
"""Discriminated (on ``op``) union of every perturbation primitive."""

PERTURBATION_OPS: Final = (
    "capacity_scale",
    "lead_time_shift",
    "demand_scale",
    "cost_scale",
    "yield_scale",
    "compliance_block",
    "inventory_hold",
)


def _is_false(value: Any) -> bool:
    return value is False


def _is_empty(value: Any) -> bool:
    return not value


class Assumption(ContractModel):
    """A scenario assumption shown on the approach card (``{key,label,value_text,prov}``).

    M4 (``90`` §5): ``refine_scenario`` turns an inference-backed scenario update into an
    assumption with ``unconfirmed=True`` and the evidence it came from
    (``source_evidence_ids``). Apply stays disabled until the user confirms it (case
    action ``confirm_assumptions`` → :meth:`DisruptionScenario.confirm_assumptions`).
    Both fields are omitted from dumps at their defaults, so pre-M4 assumptions (and the
    scenario fingerprint) serialize exactly as before.
    """

    key: Annotated[str, Field(min_length=1)]
    label: str
    value_text: str
    prov: Provenance
    unconfirmed: StrictBool = Field(default=False, exclude_if=_is_false)
    source_evidence_ids: list[EvidenceId] = Field(default=[], exclude_if=_is_empty)

    @model_validator(mode="after")
    def _unconfirmed_needs_confirmation(self) -> Assumption:
        if self.unconfirmed and self.prov not in (Provenance.INFERENCE, Provenance.ASSUMPTION):
            raise ValueError("unconfirmed assumptions must have prov 'inference' or 'assumption'")
        if len(set(self.source_evidence_ids)) != len(self.source_evidence_ids):
            raise ValueError("source_evidence_ids must be unique")
        return self


class DisruptionScenario(ContractModel):
    """The typed scenario a case reasons about (built deterministically by ``frame_case``)."""

    id: Annotated[str, Field(min_length=1)]
    tenant: TenantSlug
    mode: CaseMode
    event_family: EventFamily
    event_type: Annotated[str, Field(min_length=1)]
    description: str
    perturbations: list[Perturbation]
    matched: list[EntityRef]
    assumptions: list[Assumption]
    snapshot_ref: str | None = None
    config_version: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_assumption_keys(self) -> DisruptionScenario:
        keys = [a.key for a in self.assumptions]
        if len(set(keys)) != len(keys):
            raise ValueError("assumption keys must be unique")
        return self

    @property
    def unconfirmed_assumptions(self) -> list[Assumption]:
        """Assumptions still labelled ``inference``/``assumption`` (block auto-approval)."""
        return [
            a for a in self.assumptions if a.prov in (Provenance.INFERENCE, Provenance.ASSUMPTION)
        ]

    @property
    def pending_confirmation(self) -> list[Assumption]:
        """Assumptions flagged ``unconfirmed`` (M4): Apply stays disabled while any exist."""
        return [a for a in self.assumptions if a.unconfirmed]

    def confirm_assumptions(self, keys: list[str] | tuple[str, ...]) -> DisruptionScenario:
        """A copy where the given assumptions are user-confirmed: ``prov`` becomes
        ``decision`` and ``unconfirmed`` is cleared (so the fingerprint changes).

        Raises ``ValueError`` for an empty key list or a key that is not an assumption.
        """
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            raise ValueError("no assumption keys to confirm")
        known = {a.key for a in self.assumptions}
        if unknown := [k for k in wanted if k not in known]:
            raise ValueError(f"unknown assumption keys: {unknown}")
        assumptions = [
            Assumption.model_validate({**a.model_dump(), "prov": Provenance.DECISION, "unconfirmed": False})
            if a.key in wanted
            else a
            for a in self.assumptions
        ]
        return self.model_copy(update={"assumptions": assumptions})

    def fingerprint(self) -> str:
        return fingerprint(self)


FINGERPRINT_SCHEME: Final = "scenario-fp/1"


def fingerprint(scenario: DisruptionScenario) -> str:
    """Assumptions fingerprint of a scenario (``sha256_hex``), used for targeted re-planning.

    Covers everything that changes the computation: tenant, mode, event family/type,
    perturbations, matched entities, assumptions (including their provenance, so a
    user confirmation changes the fingerprint), snapshot and config version.
    Excluded: ``id`` and the free-text ``description``.

    Order-insensitive where order carries no meaning: perturbations and matched
    entities are sorted by their canonical JSON, assumptions by key, and inside each
    perturbation the selector ``kinds``/``ids`` and ``weeks`` (all sets) are sorted.
    ``None`` fields are dropped so adding a new optional field later does not change
    old fingerprints.
    """

    def dump(model: ContractModel) -> Any:
        return model.model_dump(mode="json", exclude_none=True)

    def dump_perturbation(p: ContractModel) -> Any:
        data = dump(p)
        target = data["target"]
        data["target"] = {**target, "kinds": sorted(target["kinds"]), "ids": sorted(target["ids"])}
        if "weeks" in data:
            data["weeks"] = sorted(set(data["weeks"]))
        return data

    perturbations = sorted((dump_perturbation(p) for p in scenario.perturbations), key=sha256_hex)
    matched = sorted((dump(m) for m in scenario.matched), key=sha256_hex)
    assumptions = sorted((dump(a) for a in scenario.assumptions), key=lambda a: a["key"])
    payload: dict[str, Any] = {
        "scheme": FINGERPRINT_SCHEME,
        "tenant": scenario.tenant,
        "mode": scenario.mode,
        "event_family": scenario.event_family,
        "event_type": scenario.event_type,
        "perturbations": perturbations,
        "matched": matched,
        "assumptions": assumptions,
        "config_version": scenario.config_version,
    }
    if scenario.snapshot_ref is not None:
        payload["snapshot_ref"] = scenario.snapshot_ref
    return sha256_hex(payload)
