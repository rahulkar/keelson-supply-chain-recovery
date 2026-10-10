"""Render blocks: the envelope plus typed props for every M1 and M3 block kind.

Wire shape (``30-engine-api-events.md``)::

    {"id":"blk_…","kind":"kpi_strip","v":1,"title":"Exposure","placement":"inline|canvas",
     "provenance":{"kind":"simulated","engine":"keelson-opt@0.1","inputs_hash":"…","as_of":"2026-10-05"},
     "data_ref":null,"props":{…}}

Blocks are versioned by ``kind@v`` (e.g. ``kpi_strip@1``). :data:`BlockEnvelope` is a
union discriminated on that key, :data:`BLOCK_PROPS_REGISTRY` maps keys to props
models, and :func:`make_block` builds a validated envelope. Only deterministic
engine nodes create blocks; all numbers are already resolved and units explicit.

M3 adds the canvas visuals ``network_map@1``, ``impact_graph@1``,
``stockout_timeline@1`` and ``scenario_compare@1`` (``80-m3-deterministic-core.md`` §5).
The first three keep their large payload behind ``data_ref``; its models are in
:data:`BLOCK_DATA_REGISTRY` (validate with :func:`parse_block_data`).

M5 (``95-m5-remediation.md``) adds ``remediation_checklist@1`` (Guide mode) and additive,
omitted-at-default fields: ``approval_card@1`` quorum progress / rejection / bundle members,
``commit_tracker@1`` Undo action + ``rolled_back`` status + rollback conflicts,
``recommended_option@1`` apply/guide line counts, ``narrative@1`` re-plan banner + plan
version links. Pre-M5 blocks serialize byte-identically.

M6 (``100-m6-signals.md``) adds ``alert_origin@1`` (the "Opened from alert" card of a case
opened from a Signal Hub alert).

M7 (``110-m7-backoffice-generic.md`` §4) adds an ``advisory`` label (omitted while unset) to
``recommended_option@1``, ``scenario_compare@1`` and ``narrative@1``: plans built on a crawled
(tier-A) snapshot say "Data gathered by paging, not a consistent snapshot" (:func:`advisory_for`).

M8 (``126-m8-co2-robustness.md``) adds, all omitted while unset: the KPI unit ``kg_co2e``, a
p10–p90 ``band`` on :class:`KpiItem`, and on ``scenario_compare@1`` the weight ``profiles``, each
option's ``scores`` and ``win_rate_bp``, an ``uncertainty`` note and an ``emissions`` note.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StrictBool,
    StrictInt,
    Tag,
    TypeAdapter,
    model_validator,
)

from .common import (
    BlockId,
    CanonicalJsonError,
    CaseMode,
    Cents,
    ContractModel,
    EntityRef,
    EventFamily,
    EvidenceId,
    IsoDate,
    IsoWeek,
    NonNegInt,
    PositiveInt,
    Provenance,
    Sha256Hex,
    canonical_json,
    new_block_id,
)
from .changeset import bundle_hash
from .optimizer import (
    ImpactGraph,
    IntegerFallbackReason,
    LatE6,
    LonE6,
    NodeStatus,
    Scorecard,
    ScorecardMetric,
)
from .policy import QuorumView, RoleName
from .runs import MAX_DECISION_REASON_CHARS, ApproachEdit
from .scenario import Assumption
from .scn import CRAWLED_ADVISORY_TEXT, AsOfRange, SnapshotLabel
from .signals import AlertSeverity, SignalKind, Uuid

__all__ = [
    "Advisory",
    "advisory_for",
    "AlertOriginBlock",
    "AlertOriginProps",
    "M6_BLOCK_KEYS",
    "ApprovalBundleMember",
    "ApprovalRejection",
    "ChecklistExpected",
    "ChecklistProgress",
    "ChecklistStep",
    "ChecklistStepState",
    "M5_BLOCK_KEYS",
    "DEEP_LINK_PATTERN",
    "MAX_CHECKLIST_STEPS",
    "MAX_DEEP_LINK_CHARS",
    "PlanStatus",
    "PlanVersionLink",
    "RemediationChecklistBlock",
    "RemediationChecklistProps",
    "RemediationMix",
    "ReplanBanner",
    "RollbackConflict",
    "TrackerAction",
    "TrackerActionType",
    "TrackerStatus",
    "is_safe_deep_link",
    "BLOCK_DATA_REGISTRY",
    "BLOCK_KEYS",
    "CompareAxes",
    "CompareInteger",
    "CompareOption",
    "CompareProfile",
    "EmissionsNote",
    "KpiBand",
    "ProfileScore",
    "SampledInput",
    "UncertaintyNote",
    "DATA_REF_REQUIRED_KINDS",
    "Footprint",
    "FootprintFeature",
    "FootprintMultiPolygon",
    "FootprintPolygon",
    "FootprintProperties",
    "ImpactGraphBlock",
    "ImpactGraphData",
    "ImpactGraphProps",
    "ImpactGraphSummary",
    "LonLatE6",
    "M3_BLOCK_KEYS",
    "MAX_TIMELINE_ROWS",
    "MapLegendItem",
    "MapLink",
    "MapLinkKind",
    "MapNode",
    "MapNodeKind",
    "MapSummary",
    "NetworkMapBlock",
    "NetworkMapData",
    "NetworkMapProps",
    "ScenarioCompareBlock",
    "ScenarioCompareProps",
    "StockoutRow",
    "StockoutTimelineBlock",
    "StockoutTimelineData",
    "StockoutTimelineProps",
    "TimelineCell",
    "TimelineOption",
    "block_data_ref",
    "block_data_to_wire",
    "parse_block_data",
    "AlternativeOption",
    "ApprovalCardBlock",
    "ApprovalCardProps",
    "ApproachCardBlock",
    "ApproachCardProps",
    "approach_edit_violations",
    "ApproachAgent",
    "ApproachCheck",
    "EditableKind",
    "EditableValue",
    "OptionActionType",
    "UnconfirmedValue",
    "BLOCK_ENVELOPE_ADAPTER",
    "BlockBase",
    "BLOCK_ENVELOPE_REGISTRY",
    "BLOCK_PROPS_REGISTRY",
    "BlockEnvelope",
    "BlockProvenance",
    "CANVAS_ONLY_KINDS",
    "ChangeTotals",
    "CommitTrackerBlock",
    "CommitTrackerProps",
    "ConnectState",
    "DisruptionEventSummary",
    "EvidenceItem",
    "EvidencePackBlock",
    "EvidencePackProps",
    "KpiItem",
    "KpiStripBlock",
    "KpiStripProps",
    "KpiUnit",
    "M1_BLOCK_KEYS",
    "MatchedEntities",
    "NarrativeBlock",
    "NarrativeProps",
    "OptionAction",
    "OptionSummary",
    "PlanDiffBlock",
    "PlanDiffProps",
    "PlanDiffRow",
    "Placement",
    "RecommendedOptionBlock",
    "RecommendedOptionProps",
    "block_key",
    "block_to_wire",
    "make_block",
    "parse_block",
]

Placement = Literal["inline", "canvas"]
KpiUnit = Literal["units", "eur_cents", "days", "pct_bp", "count", "kg_co2e"]
"""M8 adds ``kg_co2e`` (kilograms of CO₂-equivalent)."""
KpiKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
NonEmptyStr = Annotated[str, Field(min_length=1)]


def _require_unique(values: list[Any], what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must be unique")


def _is_empty(value: Any) -> bool:
    return not value


def _is_none(value: Any) -> bool:
    return value is None


def _m4_list() -> Any:
    """An additive (M4) list field: defaults to ``[]`` and is omitted from dumps while empty,
    so blocks built before M4 serialize exactly as before."""
    return Field(default=[], exclude_if=_is_empty)


_m5_list = _m4_list
"""An additive (M5) list field (same rule: ``[]`` by default, omitted while empty)."""


def _m5_optional() -> Any:
    """An additive (M5) object field: ``None`` by default and omitted from dumps while unset."""
    return Field(default=None, exclude_if=_is_none)


_m7_optional = _m5_optional
"""An additive (M7) object field (same rule: ``None`` by default, omitted while unset)."""

_m8_optional = _m5_optional
"""An additive (M8) object field (same rule: ``None`` by default, omitted while unset)."""
_m8_list = _m4_list
"""An additive (M8) list field (same rule: ``[]`` by default, omitted while empty)."""

ShareBp = Annotated[StrictInt, Field(ge=0, le=10_000)]
ProfileId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]


# ─────────────────────────────── shared parts ──────────────────────────────


class KpiBand(ContractModel):
    """M8: the likely range of a KPI — p10 and p90 over ``samples`` sampled futures (same unit
    as the KPI's ``value``)."""

    p10: StrictInt
    p90: StrictInt
    samples: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> KpiBand:
        if self.p10 > self.p90:
            raise ValueError("band p10 must be <= p90")
        return self


class KpiItem(ContractModel):
    """One KPI tile. ``value``/``delta`` are integers in ``unit``."""

    key: KpiKey
    label: NonEmptyStr
    value: StrictInt
    unit: KpiUnit
    delta: StrictInt | None = None
    prov: Provenance
    preliminary: StrictBool
    band: KpiBand | None = _m8_optional()
    """M8: p10–p90 range of the value (robustness ``mc/1``)."""


class ChangeTotals(ContractModel):
    lines: NonNegInt
    units: NonNegInt
    value_cents: Cents


# ─────────────────────────────── props models ──────────────────────────────


class KpiStripProps(ContractModel):
    """``kpi_strip@1``."""

    items: Annotated[list[KpiItem], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_keys(self) -> KpiStripProps:
        _require_unique([i.key for i in self.items], "kpi keys")
        return self


class DisruptionEventSummary(ContractModel):
    family: EventFamily
    type: NonEmptyStr
    description: str
    source: NonEmptyStr


class MatchedEntities(ContractModel):
    count: NonNegInt
    sample: list[EntityRef]

    @model_validator(mode="after")
    def _sample_within_count(self) -> MatchedEntities:
        if len(self.sample) > self.count:
            raise ValueError("matched.sample cannot be larger than matched.count")
        return self


ApproachButton = Literal["confirm", "edit", "what_if", "cancel"]
EditableKind = Literal["days", "pct", "ids"]
"""How an editable approach value is entered: whole days, whole percent, or entity ids."""


class ApproachAgent(ContractModel):
    """An agent the plan will consult (``slug`` from the AgentSpec, friendly display name)."""

    slug: NonEmptyStr
    display_name: NonEmptyStr


class ApproachCheck(ContractModel):
    """One plan task as the user reads it (``sentence`` from the task objective)."""

    task_id: NonEmptyStr
    sentence: NonEmptyStr


class EditableValue(ContractModel):
    """A scenario value the user may change with **Edit** (``key`` is the assumption key).

    ``days``/``pct`` carry ``value_int`` (current value; ``null`` = not set); ``ids`` carries
    ``value_ids``. The edited value comes back as a typed resume edit
    (:class:`~keelson_contracts.runs.ApproachEdit`).
    """

    key: KpiKey
    label: NonEmptyStr
    kind: EditableKind
    value_int: StrictInt | None = None
    value_ids: list[NonEmptyStr] | None = None

    @model_validator(mode="after")
    def _value_matches_kind(self) -> EditableValue:
        if self.kind == "ids":
            if self.value_int is not None:
                raise ValueError("ids values carry value_ids, not value_int")
            if self.value_ids is not None:
                _require_unique(self.value_ids, "value_ids")
        elif self.value_ids is not None:
            raise ValueError(f"{self.kind} values carry value_int, not value_ids")
        return self


class ApproachCardProps(ContractModel):
    """``approach_card@1`` — the scenario framing the user confirms before investigation.

    M4 additions (``90`` §2, omitted while empty): ``agents`` to be consulted, ``checks`` as
    ``{task_id, sentence}`` per plan task (plain strings from M1 cards stay valid), and
    ``editable`` values for **Edit**. ``mode`` drives the what-if badge.
    """

    interrupt_id: NonEmptyStr
    event: DisruptionEventSummary
    mode: CaseMode
    matched: MatchedEntities
    assumptions: list[Assumption]
    checks: list[str | ApproachCheck]
    buttons: Annotated[list[ApproachButton], Field(min_length=1)]
    agents: list[ApproachAgent] = _m4_list()
    editable: list[EditableValue] = _m4_list()

    @model_validator(mode="after")
    def _unique(self) -> ApproachCardProps:
        _require_unique(self.buttons, "buttons")
        _require_unique([a.key for a in self.assumptions], "assumption keys")
        _require_unique([c.task_id for c in self.checks if isinstance(c, ApproachCheck)], "check task ids")
        _require_unique([a.slug for a in self.agents], "agent slugs")
        _require_unique([e.key for e in self.editable], "editable keys")
        return self


_EDIT_INT_MIN: Final[Mapping[str, int]] = {"days": 0, "pct": -100}


def approach_edit_violations(
    card: ApproachCardProps,
    edits: Sequence[ApproachEdit] = (),
    removed_checks: Sequence[str] = (),
) -> list[str]:
    """Problems with a typed **Edit** of ``card`` (empty = OK); the engine applies an Edit
    only when this is empty.

    Every edit must name one of the card's ``editable`` keys and fit its kind
    (``days`` → ``value_int >= 0``, ``pct`` → ``value_int >= -100``, ``ids`` →
    ``value_ids``); every removed check must be the ``task_id`` of one of the card's
    checks, and at least one check must remain.
    """
    editable = {e.key: e for e in card.editable}
    problems: list[str] = []
    for i, edit in enumerate(edits):
        spec = editable.get(edit.key)
        if spec is None:
            problems.append(f"edits/{i}: {edit.key!r} is not editable on this card")
        elif spec.kind == "ids":
            if edit.value_ids is None:
                problems.append(f"edits/{i}: {edit.key} takes value_ids")
        elif edit.value_int is None:
            problems.append(f"edits/{i}: {edit.key} takes value_int")
        elif edit.value_int < _EDIT_INT_MIN[spec.kind]:
            problems.append(f"edits/{i}: {edit.key} must be >= {_EDIT_INT_MIN[spec.kind]}")
    task_ids = [c.task_id for c in card.checks if isinstance(c, ApproachCheck)]
    if unknown := sorted(set(removed_checks) - set(task_ids)):
        problems.append(f"removed_checks not on this card: {unknown}")
    elif removed_checks and set(task_ids) <= set(removed_checks):
        problems.append("at least one check must remain")
    return problems


class Advisory(ContractModel):
    """M7: the plan was built on data that is not a consistent snapshot (a tier-A crawl), so the
    options are advisory. ``text`` is what the UI shows; ``as_of_range`` is when the data was
    read."""

    reason: Literal["crawled_snapshot"]
    text: NonEmptyStr
    as_of_range: AsOfRange


def advisory_for(label: SnapshotLabel) -> Advisory | None:
    """The advisory label for plans built on a snapshot (``None`` for consistent snapshots)."""
    if label.consistency != "crawled" or label.as_of_range is None:
        return None
    return Advisory(reason="crawled_snapshot", text=CRAWLED_ADVISORY_TEXT, as_of_range=label.as_of_range)


class OptionSummary(ContractModel):
    id: NonEmptyStr
    title: NonEmptyStr
    summary_bullets: list[str]
    kpis: list[KpiItem]


class AlternativeOption(ContractModel):
    id: NonEmptyStr
    title: NonEmptyStr
    one_liner: str


OptionActionType = Literal["apply", "guide", "confirm_assumptions", "promote"]


class OptionAction(ContractModel):
    """A button on the recommended option; disabled actions must say why.

    M4 adds ``confirm_assumptions`` (confirm the unconfirmed values, re-runs impact +
    simulate) and ``promote`` (turn a what-if case into an actual one)."""

    type: OptionActionType
    label: NonEmptyStr
    enabled: StrictBool
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _reason_iff_disabled(self) -> OptionAction:
        if not self.enabled and not self.disabled_reason:
            raise ValueError("disabled actions require disabled_reason")
        if self.enabled and self.disabled_reason is not None:
            raise ValueError("enabled actions must not carry disabled_reason")
        return self


class UnconfirmedValue(ContractModel):
    """A value the plan relies on that a human must confirm first ("Confirm before applying")."""

    key: NonEmptyStr
    label: NonEmptyStr
    value_text: NonEmptyStr
    source: NonEmptyStr
    """Where it came from, as the user reads it (e.g. "Supplier message, 6 Oct")."""


class RemediationMix(ContractModel):
    """How the option's changes split (M5, ``95`` §2): lines the connectors apply for the user
    and lines the user performs with a guided checklist ("Apply 3 changes for me · Guide me
    through 2")."""

    apply_lines: NonNegInt
    guide_lines: NonNegInt

    @model_validator(mode="after")
    def _some_lines(self) -> RemediationMix:
        if self.apply_lines + self.guide_lines == 0:
            raise ValueError("a remediation mix has at least one line")
        return self


class RecommendedOptionProps(ContractModel):
    """``recommended_option@1``.

    M4 additions (omitted at their defaults): ``unconfirmed`` values and ``what_if``. While
    either is set, an ``apply`` action must be disabled (with its reason); a
    ``confirm_assumptions`` action needs ``unconfirmed`` values and ``promote`` needs
    ``what_if``.

    M5 addition (omitted while unset): ``remediation_mix`` — apply vs guide line counts. When
    it is set, an enabled ``apply`` needs ``apply_lines > 0``.

    M7 addition (omitted while unset): ``advisory`` — the option was computed on crawled data.
    Applying stays possible (per-op, with approval as usual).
    """

    plan_version: PositiveInt
    option: OptionSummary
    alternatives: list[AlternativeOption]
    actions: list[OptionAction]
    unconfirmed: list[UnconfirmedValue] = _m4_list()
    what_if: StrictBool = Field(default=False, exclude_if=_is_empty)
    remediation_mix: RemediationMix | None = _m5_optional()
    advisory: Advisory | None = _m7_optional()

    @model_validator(mode="after")
    def _consistent(self) -> RecommendedOptionProps:
        ids = [self.option.id, *(a.id for a in self.alternatives)]
        _require_unique(ids, "option ids (recommended + alternatives)")
        _require_unique([a.type for a in self.actions], "action types")
        _require_unique([u.key for u in self.unconfirmed], "unconfirmed keys")
        types = {a.type: a for a in self.actions}
        apply = types.get("apply")
        if apply is not None and apply.enabled and (self.what_if or self.unconfirmed):
            raise ValueError("apply must be disabled for what-if cases and while values are unconfirmed")
        if "confirm_assumptions" in types and not self.unconfirmed:
            raise ValueError("confirm_assumptions requires unconfirmed values")
        if "promote" in types and not self.what_if:
            raise ValueError("promote is only offered on what-if cases")
        mix = self.remediation_mix
        if mix is not None and apply is not None and apply.enabled and mix.apply_lines == 0:
            raise ValueError("apply cannot be enabled when no line can be applied (remediation_mix.apply_lines == 0)")
        return self


class PlanDiffRow(ContractModel):
    seq: PositiveInt
    op: NonEmptyStr
    target_label: NonEmptyStr
    before_text: str
    after_text: str
    qty: StrictInt | None = None
    system: NonEmptyStr
    """Target system display name (``BRAND_VENDOR_NAME``, ``"Merxa"`` in M1)."""


class PlanDiffProps(ContractModel):
    """``plan_diff@1`` — the change-set as the user reads it."""

    plan_version: PositiveInt
    rows: list[PlanDiffRow]
    totals: ChangeTotals

    @model_validator(mode="after")
    def _unique_seq(self) -> PlanDiffProps:
        _require_unique([r.seq for r in self.rows], "row seq")
        return self


class ConnectState(ContractModel):
    """The approver's delegated link to the target system."""

    status: Literal["active", "missing", "expired"]
    connect_url: str | None = None


ApprovalButton = Literal["approve", "reject", "revise"]


class ApprovalRejection(ContractModel):
    """Who rejected the change set and why (M5; ``reason`` is the person's own note)."""

    sub: NonEmptyStr
    name: str
    role: RoleName | None = None
    at: AwareDatetime
    reason: Annotated[str, Field(min_length=1, max_length=MAX_DECISION_REASON_CHARS)] | None = None


class ApprovalBundleMember(ContractModel):
    """One connector's change set inside a multi-system bundle (M5, ``95`` §5)."""

    connector: NonEmptyStr
    system: NonEmptyStr
    """Display name of the target system."""
    change_set_id: NonEmptyStr
    change_set_hash: Sha256Hex
    lines: PositiveInt


class ApprovalCardProps(ContractModel):
    """``approval_card@1``.

    M5 additions (omitted while unset/empty, ``95`` §1, §5, §7):

    * ``quorum`` — progress (:class:`~keelson_contracts.policy.QuorumView`); its roles are the
      card's ``required_roles``;
    * ``rejection`` — who rejected and why (only with no remaining quorum progress to show);
    * ``bundle`` — the per-connector change sets one approval covers, in apply order. With a
      bundle, ``change_set_hash`` is the bundle hash
      (:func:`~keelson_contracts.changeset.bundle_hash`) and ``summary.lines`` their total.
    """

    interrupt_id: NonEmptyStr
    change_set_id: NonEmptyStr
    change_set_hash: Sha256Hex
    plan_version: PositiveInt
    summary: ChangeTotals
    reason: str
    required_roles: list[NonEmptyStr]
    connect: ConnectState
    buttons: Annotated[list[ApprovalButton], Field(min_length=1)]
    quorum: QuorumView | None = _m5_optional()
    rejection: ApprovalRejection | None = _m5_optional()
    bundle: list[ApprovalBundleMember] = _m5_list()

    @model_validator(mode="after")
    def _unique(self) -> ApprovalCardProps:
        _require_unique(self.buttons, "buttons")
        if self.quorum is not None and set(self.quorum.required.roles) != set(self.required_roles):
            raise ValueError("required_roles must be the quorum's roles")
        if self.bundle:
            _require_unique([m.connector for m in self.bundle], "bundle connectors")
            _require_unique([m.change_set_id for m in self.bundle], "bundle change set ids")
            expected = bundle_hash([(m.connector, m.change_set_hash) for m in self.bundle])
            if self.change_set_hash != expected:
                raise ValueError("change_set_hash must be the bundle hash of the bundle members")
            if self.summary.lines != sum(m.lines for m in self.bundle):
                raise ValueError("summary.lines must equal the bundle members' lines")
        return self


TrackerStatus = Literal["applying", "applied", "conflict", "verified", "rolled_back"]
"""``rolled_back`` (M5): the change set was undone in the target system."""
TrackerActionType = Literal["undo"]
UNDOABLE_STATUSES: Final = frozenset({"applied", "verified"})


class TrackerAction(ContractModel):
    """A button on the commit tracker (M5: ``undo`` → case action ``rollback``); disabled
    actions must say why."""

    type: TrackerActionType
    label: NonEmptyStr
    enabled: StrictBool
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _reason_iff_disabled(self) -> TrackerAction:
        if not self.enabled and not self.disabled_reason:
            raise ValueError("disabled actions require disabled_reason")
        if self.enabled and self.disabled_reason is not None:
            raise ValueError("enabled actions must not carry disabled_reason")
        return self


class RollbackConflict(ContractModel):
    """A later change on an entity the rollback would touch (the target system refused)."""

    entity_ref: NonEmptyStr
    detail: NonEmptyStr


class CommitTrackerProps(ContractModel):
    """``commit_tracker@1``.

    M5 additions (omitted while empty): ``actions`` (``undo``; enabled only while the change
    set is ``applied``/``verified``), ``conflicts`` that blocked a rollback, and the status
    ``rolled_back``.
    """

    change_set_id: NonEmptyStr
    system: NonEmptyStr
    status: TrackerStatus
    ext_code: str | None = None
    ext_url: str | None = None
    applied: NonNegInt
    verified: NonNegInt
    actions: list[TrackerAction] = _m5_list()
    conflicts: list[RollbackConflict] = _m5_list()

    @model_validator(mode="after")
    def _verified_le_applied(self) -> CommitTrackerProps:
        if self.verified > self.applied:
            raise ValueError("verified cannot exceed applied")
        _require_unique([a.type for a in self.actions], "action types")
        if any(a.type == "undo" and a.enabled for a in self.actions) and self.status not in UNDOABLE_STATUSES:
            raise ValueError("undo can only be enabled while the change set is applied or verified")
        if self.conflicts and self.status not in UNDOABLE_STATUSES:
            raise ValueError("rollback conflicts are shown only while the change set is still applied or verified")
        return self


PlanStatus = Literal["proposed", "committed", "superseded", "rolled_back"]


class ReplanBanner(ContractModel):
    """"New information: … — plan updated (v2)" (M5, ``95`` §4): a re-plan triggered by a signal.

    ``text`` is composed by a deterministic node (numbers resolved); ``signal_id`` names the
    signal/webhook event that caused it, when there is one."""

    text: NonEmptyStr
    plan_version: PositiveInt
    previous_plan_version: PositiveInt
    signal_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _newer(self) -> ReplanBanner:
        if self.plan_version <= self.previous_plan_version:
            raise ValueError("plan_version must be newer than previous_plan_version")
        return self


class PlanVersionLink(ContractModel):
    """One entry of the plan version switcher (v1 ↔ v2)."""

    version: PositiveInt
    label: NonEmptyStr
    status: PlanStatus
    current: StrictBool


class NarrativeProps(ContractModel):
    """``narrative@1`` — already composed markdown (placeholders resolved).

    ``what_changed`` (M4, omitted when unset) is the one-line "What changed" diff a
    follow-up shows against the previous plan version (plain text, numbers resolved).

    M5 (omitted while unset/empty): ``banner`` for a signal-triggered re-plan and
    ``plan_versions`` (ascending, exactly one ``current``; the banner's versions are listed).

    M7 (omitted while unset): ``advisory`` — the plan was built on crawled data."""

    markdown: NonEmptyStr
    what_changed: NonEmptyStr | None = Field(default=None, exclude_if=_is_none)
    banner: ReplanBanner | None = _m5_optional()
    plan_versions: list[PlanVersionLink] = _m5_list()
    advisory: Advisory | None = _m7_optional()

    @model_validator(mode="after")
    def _versions(self) -> NarrativeProps:
        if self.plan_versions:
            versions = [p.version for p in self.plan_versions]
            _require_unique(versions, "plan versions")
            if versions != sorted(versions):
                raise ValueError("plan_versions must be in ascending order")
            if sum(p.current for p in self.plan_versions) != 1:
                raise ValueError("exactly one plan version is current")
            if self.banner is not None and not {
                self.banner.plan_version, self.banner.previous_plan_version
            } <= set(versions):
                raise ValueError("the banner's plan versions must be in plan_versions")
        return self


class EvidenceItem(ContractModel):
    id: EvidenceId
    source: NonEmptyStr
    label: NonEmptyStr
    prov: Provenance
    retrieved_at: AwareDatetime


class EvidencePackProps(ContractModel):
    """``evidence_pack@1`` (canvas)."""

    items: list[EvidenceItem]

    @model_validator(mode="after")
    def _unique_ids(self) -> EvidencePackProps:
        _require_unique([i.id for i in self.items], "evidence ids")
        return self


# ─────────────────────────── M6 alert origin (100 §6, §8) ───────────────────


class AlertOriginProps(ContractModel):
    """``alert_origin@1`` — the "Opened from alert" card at the top of a case thread.

    Deterministic fields copied from the Signal Hub alert/event (no LLM text): ``kind`` is the
    event kind, ``source_names`` the attributed sources (e.g. ``GDACS``, ``Demo Freight Daily``),
    ``simulated`` shows the DEMO badge."""

    alert_id: Uuid
    title: Annotated[str, Field(min_length=1, max_length=300)]
    severity: AlertSeverity
    kind: SignalKind
    source_names: Annotated[list[Annotated[str, Field(min_length=1, max_length=120)]], Field(max_length=20)]
    simulated: StrictBool
    opened_at: AwareDatetime

    @model_validator(mode="after")
    def _unique_sources(self) -> AlertOriginProps:
        _require_unique(self.source_names, "source_names")
        return self


# ─────────────────────────── M5 remediation checklist (95 §2) ───────────────

MAX_CHECKLIST_STEPS: Final = 100
ChecklistStepState = Literal["pending", "done", "verified", "skipped"]
"""``done``/``skipped`` are set by a person (audited); ``verified`` only by read-back."""
_DEEP_LINK_ABS = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s]*)?$")
MAX_DEEP_LINK_CHARS: Final = 2000
DEEP_LINK_PATTERN: Final = (
    r"^(https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\x00-\x20\x7f\\]*)?"
    r"|/([^/\x00-\x20\x7f\\][^\x00-\x20\x7f\\]*)?)$"
)
"""JSON-Schema form of :func:`is_safe_deep_link`, so the generated Zod schema rejects
``javascript:``/``data:`` and protocol-relative links before the web renders an ``href``."""
DeepLink = Annotated[str, Field(min_length=1, max_length=MAX_DEEP_LINK_CHARS, pattern=DEEP_LINK_PATTERN)]


def is_safe_deep_link(url: str) -> bool:
    """A deep link a client may open: an absolute ``http(s)`` URL with a plain host (no
    userinfo, whitespace, control characters or backslashes) or a same-origin absolute path
    (``/…``, never ``//…``)."""
    if not url or len(url) > MAX_DEEP_LINK_CHARS or "\\" in url or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in url):
        return False
    if url.startswith("/"):
        return not url.startswith("//")
    return bool(_DEEP_LINK_ABS.match(url))


class ChecklistExpected(ContractModel):
    """What read-back must find for the step to verify: an entity of ``entity_type`` (and
    ``entity_ref`` when known up front) whose fields match ``match`` (JSON, no floats)."""

    entity_type: NonEmptyStr
    entity_ref: NonEmptyStr | None
    match: dict[str, Any]

    @model_validator(mode="after")
    def _json_match(self) -> ChecklistExpected:
        try:
            canonical_json(self.match)
        except (CanonicalJsonError, TypeError, ValueError) as exc:
            raise ValueError(f"expected.match must be JSON without floats: {exc}") from exc
        return self


class ChecklistStep(ContractModel):
    """One manual step. ``verified`` carries ``verified_at``; only verified steps carry an
    ``evidence_id`` (the read-back evidence row)."""

    seq: PositiveInt
    title: NonEmptyStr
    instructions_md: NonEmptyStr
    deep_link: DeepLink | None
    expected: ChecklistExpected
    status: ChecklistStepState
    verified_at: AwareDatetime | None
    evidence_id: EvidenceId | None

    @model_validator(mode="after")
    def _check(self) -> ChecklistStep:
        if self.deep_link is not None and not is_safe_deep_link(self.deep_link):
            raise ValueError("deep_link must be an http(s) URL with a plain host or a same-origin path")
        if self.status == "verified":
            if self.verified_at is None:
                raise ValueError("a verified step carries verified_at")
        elif self.verified_at is not None or self.evidence_id is not None:
            raise ValueError("verified_at and evidence_id are only set on verified steps")
        return self


class ChecklistProgress(ContractModel):
    """``done`` = steps no longer pending (done, verified or skipped); ``total`` = all steps."""

    done: NonNegInt
    total: PositiveInt


class RemediationChecklistProps(ContractModel):
    """``remediation_checklist@1`` — Guide mode: the lines the user performs by hand in
    ``connector``'s UI, ticked ``verified`` by read-back (webhook or poll) or ``done``/
    ``skipped`` by hand. ``progress`` must match the steps; the guide is complete when every
    step is ``verified`` or ``skipped`` (:attr:`completed`)."""

    checklist_id: NonEmptyStr
    connector: NonEmptyStr
    steps: Annotated[list[ChecklistStep], Field(min_length=1, max_length=MAX_CHECKLIST_STEPS)]
    progress: ChecklistProgress

    @model_validator(mode="after")
    def _check(self) -> RemediationChecklistProps:
        seqs = [st.seq for st in self.steps]
        if seqs != list(range(1, len(seqs) + 1)):
            raise ValueError("steps must be ordered with seq 1..n")
        if self.progress.total != len(self.steps):
            raise ValueError("progress.total must equal the number of steps")
        if self.progress.done != sum(st.status != "pending" for st in self.steps):
            raise ValueError("progress.done must count the steps that are no longer pending")
        return self

    @property
    def completed(self) -> bool:
        return all(st.status in ("verified", "skipped") for st in self.steps)

    @staticmethod
    def progress_of(steps: Sequence[ChecklistStep]) -> ChecklistProgress:
        return ChecklistProgress(done=sum(st.status != "pending" for st in steps), total=len(steps))


# ─────────────────────────── M3 canvas visuals (80 §5) ─────────────────────
#
# Large payloads travel through ``data_ref`` (``GET /api/blocks/{id}/data``); the props
# carry a summary. Coordinates are integer degrees x 1e6 (``lat_e6``/``lon_e6``), points
# are ``[lon_e6, lat_e6]`` pairs, money is integer cents.

LonLatE6 = tuple[LonE6, LatE6]
"""A map position ``[lon_e6, lat_e6]`` (GeoJSON axis order, degrees x 1e6)."""

MapNodeKind = Literal[
    "dc", "cluster", "store", "factory", "supplier", "port", "airport", "chokepoint"
]
MapLinkKind = Literal["inbound", "transfer", "sea_air", "nearshore"]
LegendKind = Literal["node", "link", "footprint"]

MAX_TIMELINE_ROWS: Final = 40
"""``stockout_timeline@1`` keeps the top rows by shortage."""


class MapSummary(ContractModel):
    nodes: NonNegInt
    at_risk_nodes: NonNegInt
    lanes: NonNegInt

    @model_validator(mode="after")
    def _at_risk_within_nodes(self) -> MapSummary:
        if self.at_risk_nodes > self.nodes:
            raise ValueError("at_risk_nodes cannot exceed nodes")
        return self


class MapLegendItem(ContractModel):
    """A legend entry; ``key`` is the node status / link kind / footprint label it explains."""

    key: NonEmptyStr
    label: NonEmptyStr
    kind: LegendKind


class NetworkMapProps(ContractModel):
    """``network_map@1`` (canvas; data in :class:`NetworkMapData` via ``data_ref``).

    ``bbox`` is ``[west, south, east, north]`` in degrees x 1e6."""

    title: str
    summary: MapSummary
    bbox: tuple[LonE6, LatE6, LonE6, LatE6]
    legend: list[MapLegendItem]

    @model_validator(mode="after")
    def _check(self) -> NetworkMapProps:
        west, south, east, north = self.bbox
        if west > east or south > north:
            raise ValueError("bbox must be [west, south, east, north] with west <= east and south <= north")
        _require_unique([(i.kind, i.key) for i in self.legend], "legend (kind, key)")
        return self


class MapNode(ContractModel):
    id: NonEmptyStr
    kind: MapNodeKind
    label: str
    lat_e6: LatE6
    lon_e6: LonE6
    status: NodeStatus
    units_at_risk: NonNegInt | None = None


class MapLink(ContractModel):
    """A lane on the map. ``from``/``to`` are node ids; ``path`` runs from ``from`` to
    ``to`` (sea lanes follow catalogued waypoints, road lanes are straight).
    ``option_id`` marks links proposed by a plan option (``null`` = current network)."""

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)

    id: NonEmptyStr
    kind: MapLinkKind
    from_: Annotated[str, Field(alias="from", min_length=1)]
    to: NonEmptyStr
    path: Annotated[list[LonLatE6], Field(min_length=2)]
    status: NodeStatus
    units: NonNegInt | None = None
    option_id: str | None = None


def _check_ring(ring: list[tuple[int, int]]) -> None:
    if len(ring) < 4 or ring[0] != ring[-1]:
        raise ValueError("polygon rings need >= 4 positions and must be closed (first == last)")


class FootprintPolygon(ContractModel):
    type: Literal["Polygon"]
    coordinates: Annotated[list[list[LonLatE6]], Field(min_length=1)]

    @model_validator(mode="after")
    def _rings(self) -> FootprintPolygon:
        for ring in self.coordinates:
            _check_ring(ring)
        return self


class FootprintMultiPolygon(ContractModel):
    type: Literal["MultiPolygon"]
    coordinates: Annotated[list[Annotated[list[list[LonLatE6]], Field(min_length=1)]], Field(min_length=1)]

    @model_validator(mode="after")
    def _rings(self) -> FootprintMultiPolygon:
        for polygon in self.coordinates:
            for ring in polygon:
                _check_ring(ring)
        return self


class FootprintProperties(ContractModel):
    label: str
    status: NodeStatus


class FootprintFeature(ContractModel):
    type: Literal["Feature"]
    geometry: Annotated[FootprintPolygon | FootprintMultiPolygon, Field(discriminator="type")]
    properties: FootprintProperties


class Footprint(ContractModel):
    """Disruption footprint: a GeoJSON-shaped ``FeatureCollection`` of (multi)polygons whose
    positions are ``[lon_e6, lat_e6]`` integers (divide by 1e6 for GeoJSON degrees)."""

    type: Literal["FeatureCollection"]
    features: list[FootprintFeature]


class NetworkMapData(ContractModel):
    """``network_map@1`` data (``data_ref``)."""

    nodes: list[MapNode]
    links: list[MapLink]
    footprint: Footprint | None = None

    @model_validator(mode="after")
    def _consistent(self) -> NetworkMapData:
        ids = [n.id for n in self.nodes]
        _require_unique(ids, "map node ids")
        _require_unique([ln.id for ln in self.links], "map link ids")
        known = set(ids)
        bad = [ln.id for ln in self.links if ln.from_ not in known or ln.to not in known]
        if bad:
            raise ValueError(f"map links reference unknown nodes: {bad[:10]}")
        return self


class ImpactGraphSummary(ContractModel):
    nodes: NonNegInt
    disrupted: NonNegInt
    impacted: NonNegInt

    @model_validator(mode="after")
    def _within_nodes(self) -> ImpactGraphSummary:
        if self.disrupted + self.impacted > self.nodes:
            raise ValueError("disrupted + impacted cannot exceed nodes")
        return self


class ImpactGraphProps(ContractModel):
    """``impact_graph@1`` (canvas; data = :class:`~keelson_contracts.optimizer.ImpactGraph`,
    at most 300 nodes)."""

    summary: ImpactGraphSummary


ImpactGraphData = ImpactGraph
"""``impact_graph@1`` data (``data_ref``): the optimizer's grouped impact graph."""


class TimelineOption(ContractModel):
    id: NonEmptyStr
    title: NonEmptyStr


class StockoutTimelineProps(ContractModel):
    """``stockout_timeline@1`` (canvas; rows in :class:`StockoutTimelineData`)."""

    weeks: Annotated[list[IsoWeek], Field(min_length=1)]
    options: list[TimelineOption]

    @model_validator(mode="after")
    def _unique(self) -> StockoutTimelineProps:
        _require_unique(self.weeks, "weeks")
        if self.weeks != sorted(self.weeks):
            raise ValueError("weeks must be in chronological order")
        _require_unique([o.id for o in self.options], "option ids")
        return self


class TimelineCell(ContractModel):
    """One row x week cell; ``shortage_units > 0`` exactly when ``status`` is ``short``."""

    week: IsoWeek
    status: Literal["ok", "tight", "short"]
    shortage_units: NonNegInt

    @model_validator(mode="after")
    def _status_matches_shortage(self) -> TimelineCell:
        if (self.status == "short") != (self.shortage_units > 0):
            raise ValueError("shortage_units must be > 0 exactly when status is 'short'")
        return self


class StockoutRow(ContractModel):
    id: NonEmptyStr
    dc_code: NonEmptyStr
    cluster_code: NonEmptyStr
    style_code: NonEmptyStr
    label: str
    cells: list[TimelineCell]
    first_breach_week: IsoWeek | None = None
    by_option: dict[str, IsoWeek | None] = Field(default_factory=dict)
    """First breach week per option id (``null`` = no breach under that option)."""

    @model_validator(mode="after")
    def _unique_weeks(self) -> StockoutRow:
        _require_unique([c.week for c in self.cells], f"cell weeks of row {self.id}")
        return self


class StockoutTimelineData(ContractModel):
    """``stockout_timeline@1`` data (``data_ref``): top rows by shortage."""

    rows: Annotated[list[StockoutRow], Field(max_length=MAX_TIMELINE_ROWS)]

    @model_validator(mode="after")
    def _unique_ids(self) -> StockoutTimelineData:
        _require_unique([r.id for r in self.rows], "row ids")
        return self


class ProfileScore(ContractModel):
    """M8: an option's score (cents, higher is better) and rank (1 = best) under one profile."""

    profile_id: ProfileId
    score_cents: StrictInt
    rank: PositiveInt


class CompareOption(ContractModel):
    """A plan option on the Pareto view; guided-only options must say why."""

    id: NonEmptyStr
    title: NonEmptyStr
    levers: list[NonEmptyStr]
    scorecard: Scorecard
    recommended: StrictBool
    guided_only: StrictBool
    guided_reason: str | None = None
    integer: CompareInteger | None = _m5_optional()
    """M8 (125; ``integer_stage="cpsat"`` options with re-sourcing only, omitted while unset): how
    the option's whole-unit buys and splits were found."""
    pareto: StrictBool
    win_rate_bp: ShareBp | None = _m8_optional()
    """M8: share of the sampled futures in which this option has the highest net benefit ("Do
    nothing" wins a future in which no option gains). Set on every option or on none."""
    scores: list[ProfileScore] = _m8_list()
    """M8: the option's score and rank under each weight profile (computed by the engine)."""

    @model_validator(mode="after")
    def _reason_iff_guided(self) -> CompareOption:
        if self.guided_only and not self.guided_reason:
            raise ValueError("guided_only options require guided_reason")
        if not self.guided_only and self.guided_reason is not None:
            raise ValueError("guided_reason is only allowed for guided_only options")
        _require_unique(self.levers, "levers")
        _require_unique([s.profile_id for s in self.scores], "score profile ids")
        return self


class CompareProfile(ContractModel):
    """M8: a weight profile the options are ranked by (``keelson_contracts.scoring``)."""

    id: ProfileId
    label: Annotated[str, Field(min_length=1, max_length=40)]
    default: StrictBool
    """The tenant's default profile: the recommendation and the LP weights follow it."""
    lost_margin_bp: Annotated[StrictInt, Field(ge=0, le=50_000)]
    cost_bp: Annotated[StrictInt, Field(ge=0, le=50_000)]
    carbon_price_cents_per_t: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    risk_aversion_bp: ShareBp


SampledInput = Literal["disruption_duration", "disruption_severity", "lead_time", "demand"]


class UncertaintyNote(ContractModel):
    """M8: how the bands were computed (``mc/1``: sampled futures of the same plan)."""

    method: Literal["mc/1"]
    samples: PositiveInt
    sampled: Annotated[list[SampledInput], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> UncertaintyNote:
        _require_unique(list(self.sampled), "sampled inputs")
        return self


class EmissionsNote(ContractModel):
    """M8: where the CO₂e figures come from (transport only)."""

    source_label: Annotated[str, Field(min_length=1, max_length=160)]
    basis: Literal["WTW", "TTW"]
    indicative: StrictBool
    """The factor set is not verified against its published source: show the figures as
    indicative."""
    coverage_bp: ShareBp
    """Share of the moved units the figures cover (lanes and unit weights known)."""


class CompareAxes(ContractModel):
    x: ScorecardMetric = "incremental_cost_cents"
    y: ScorecardMetric = "lost_units"

    @model_validator(mode="after")
    def _distinct(self) -> CompareAxes:
        if self.x == self.y:
            raise ValueError("axes x and y must differ")
        return self


class CompareInteger(ContractModel):
    """M8 (125): how a ``cpsat`` option found its whole units. ``cpsat_optimal`` /
    ``cpsat_feasible``: the buys and splits come from CP-SAT (proven optimal for the planning
    model, or the best found within ``gap_bp`` of the best bound); ``lp_rounding``: CP-SAT fell
    back to the LP rounding and repair for ``fallback_reason``."""

    method: Literal["cpsat_optimal", "cpsat_feasible", "lp_rounding"]
    gap_bp: Annotated[StrictInt, Field(ge=0, le=10_000)] | None = None
    fallback_reason: IntegerFallbackReason | None = None

    @model_validator(mode="after")
    def _consistent(self) -> CompareInteger:
        if self.method == "lp_rounding" and self.gap_bp is not None:
            raise ValueError("lp_rounding options have no CP-SAT gap")
        if self.method != "lp_rounding" and self.fallback_reason is not None:
            raise ValueError("fallback_reason is only allowed for lp_rounding options")
        return self


class ScenarioCompareProps(ContractModel):
    """``scenario_compare@1`` (canvas): every candidate option with its scorecard.

    M7 (omitted while unset): ``advisory`` — the options were computed on crawled data.

    M8 (omitted while unset): ``profiles`` (options' ``scores`` reference them), ``uncertainty``
    and ``emissions`` notes. An axis on ``incremental_co2e_kg`` needs it on every option."""

    options: Annotated[list[CompareOption], Field(min_length=1)]
    axes: CompareAxes = Field(default_factory=CompareAxes)
    advisory: Advisory | None = _m7_optional()
    profiles: Annotated[list[CompareProfile], Field(max_length=8)] = _m8_list()
    uncertainty: UncertaintyNote | None = _m8_optional()
    emissions: EmissionsNote | None = _m8_optional()

    @model_validator(mode="after")
    def _check(self) -> ScenarioCompareProps:
        _require_unique([o.id for o in self.options], "option ids")
        if sum(o.recommended for o in self.options) > 1:
            raise ValueError("at most one option can be recommended")
        _require_unique([p.id for p in self.profiles], "profile ids")
        if sum(p.default for p in self.profiles) > 1:
            raise ValueError("at most one profile can be the default")
        known = {p.id for p in self.profiles}
        unknown = sorted({s.profile_id for o in self.options for s in o.scores} - known)
        if unknown:
            raise ValueError(f"scores reference unknown profiles: {unknown}")
        rates = [o.win_rate_bp for o in self.options]
        if any(r is not None for r in rates):
            if any(r is None for r in rates):
                raise ValueError("win_rate_bp must be set on every option or on none")
            if sum(r for r in rates if r is not None) != 10_000:
                raise ValueError("win rates must sum to 10000 bp")
        if "incremental_co2e_kg" in (self.axes.x, self.axes.y):
            missing = [o.id for o in self.options if o.scorecard.incremental_co2e_kg is None]
            if missing:
                raise ValueError(f"the incremental_co2e_kg axis needs CO2e on every option (missing: {missing})")
        return self


# ────────────────────────────────── envelope ────────────────────────────────


class BlockProvenance(ContractModel):
    kind: Provenance
    engine: NonEmptyStr
    """Producer and version, e.g. ``keelson-opt@0.1`` or ``keelson-compose@0.1``."""
    inputs_hash: str | None = None
    as_of: IsoDate


class BlockBase(ContractModel):
    id: BlockId
    title: str
    placement: Placement
    provenance: BlockProvenance
    data_ref: str | None = None


class KpiStripBlock(BlockBase):
    kind: Literal["kpi_strip"]
    v: Literal[1]
    props: KpiStripProps


class ApproachCardBlock(BlockBase):
    kind: Literal["approach_card"]
    v: Literal[1]
    props: ApproachCardProps


class RecommendedOptionBlock(BlockBase):
    kind: Literal["recommended_option"]
    v: Literal[1]
    props: RecommendedOptionProps


class PlanDiffBlock(BlockBase):
    kind: Literal["plan_diff"]
    v: Literal[1]
    props: PlanDiffProps


class ApprovalCardBlock(BlockBase):
    kind: Literal["approval_card"]
    v: Literal[1]
    props: ApprovalCardProps


class CommitTrackerBlock(BlockBase):
    kind: Literal["commit_tracker"]
    v: Literal[1]
    props: CommitTrackerProps


class NarrativeBlock(BlockBase):
    kind: Literal["narrative"]
    v: Literal[1]
    props: NarrativeProps


class RemediationChecklistBlock(BlockBase):
    kind: Literal["remediation_checklist"]
    v: Literal[1]
    props: RemediationChecklistProps


class AlertOriginBlock(BlockBase):
    kind: Literal["alert_origin"]
    v: Literal[1]
    props: AlertOriginProps


class EvidencePackBlock(BlockBase):
    kind: Literal["evidence_pack"]
    v: Literal[1]
    props: EvidencePackProps

    @model_validator(mode="after")
    def _canvas_only(self) -> EvidencePackBlock:
        if self.placement != "canvas":
            raise ValueError("evidence_pack blocks are canvas-only")
        return self


def block_data_ref(block_id: str) -> str:
    """The ``data_ref`` of a block: its own same-origin data endpoint
    ``/api/blocks/{block_id}/data`` (``30-engine-api-events.md``)."""
    return f"/api/blocks/{block_id}/data"


class _CanvasVisualBlock(BlockBase):
    """M3 canvas visuals: canvas-only; the data-backed ones require ``data_ref``, and a
    ``data_ref`` must be the block's own endpoint (:func:`block_data_ref`) — never an
    absolute or foreign URL a client could be steered to."""

    @model_validator(mode="after")
    def _canvas_visual(self) -> _CanvasVisualBlock:
        kind = getattr(self, "kind", "?")
        if self.placement != "canvas":
            raise ValueError(f"{kind} blocks are canvas-only")
        if kind in DATA_REF_REQUIRED_KINDS and not self.data_ref:
            raise ValueError(f"{kind} blocks carry their data in data_ref (required)")
        if self.data_ref is not None and self.data_ref != block_data_ref(self.id):
            raise ValueError(f"{kind} data_ref must be {block_data_ref(self.id)!r} (the block's own data endpoint)")
        return self


class NetworkMapBlock(_CanvasVisualBlock):
    kind: Literal["network_map"]
    v: Literal[1]
    props: NetworkMapProps


class ImpactGraphBlock(_CanvasVisualBlock):
    kind: Literal["impact_graph"]
    v: Literal[1]
    props: ImpactGraphProps


class StockoutTimelineBlock(_CanvasVisualBlock):
    kind: Literal["stockout_timeline"]
    v: Literal[1]
    props: StockoutTimelineProps


class ScenarioCompareBlock(_CanvasVisualBlock):
    kind: Literal["scenario_compare"]
    v: Literal[1]
    props: ScenarioCompareProps


def block_key(kind: str, v: int) -> str:
    """Registry key ``"<kind>@<v>"``."""
    return f"{kind}@{v}"


def _envelope_discriminator(value: Any) -> str | None:
    if isinstance(value, Mapping):
        kind, v = value.get("kind"), value.get("v")
    else:
        kind, v = getattr(value, "kind", None), getattr(value, "v", None)
    if not isinstance(kind, str) or isinstance(v, bool) or not isinstance(v, int):
        return None
    return block_key(kind, v)


BlockEnvelope = Annotated[
    Annotated[KpiStripBlock, Tag("kpi_strip@1")]
    | Annotated[ApproachCardBlock, Tag("approach_card@1")]
    | Annotated[RecommendedOptionBlock, Tag("recommended_option@1")]
    | Annotated[PlanDiffBlock, Tag("plan_diff@1")]
    | Annotated[ApprovalCardBlock, Tag("approval_card@1")]
    | Annotated[CommitTrackerBlock, Tag("commit_tracker@1")]
    | Annotated[NarrativeBlock, Tag("narrative@1")]
    | Annotated[EvidencePackBlock, Tag("evidence_pack@1")]
    | Annotated[NetworkMapBlock, Tag("network_map@1")]
    | Annotated[ImpactGraphBlock, Tag("impact_graph@1")]
    | Annotated[StockoutTimelineBlock, Tag("stockout_timeline@1")]
    | Annotated[ScenarioCompareBlock, Tag("scenario_compare@1")]
    | Annotated[RemediationChecklistBlock, Tag("remediation_checklist@1")]
    | Annotated[AlertOriginBlock, Tag("alert_origin@1")],
    Discriminator(
        _envelope_discriminator,
        custom_error_type="unknown_block",
        custom_error_message="unknown block kind@v",
        custom_error_context={"known": "see BLOCK_PROPS_REGISTRY"},
    ),
]
"""Any known render block (M1 + M3 + M5 + M6), discriminated on ``kind@v``."""

BLOCK_ENVELOPE_ADAPTER: Final[TypeAdapter[Any]] = TypeAdapter(BlockEnvelope)

BLOCK_ENVELOPE_REGISTRY: Final[Mapping[str, type[BlockBase]]] = {
    "kpi_strip@1": KpiStripBlock,
    "approach_card@1": ApproachCardBlock,
    "recommended_option@1": RecommendedOptionBlock,
    "plan_diff@1": PlanDiffBlock,
    "approval_card@1": ApprovalCardBlock,
    "commit_tracker@1": CommitTrackerBlock,
    "narrative@1": NarrativeBlock,
    "evidence_pack@1": EvidencePackBlock,
    "network_map@1": NetworkMapBlock,
    "impact_graph@1": ImpactGraphBlock,
    "stockout_timeline@1": StockoutTimelineBlock,
    "scenario_compare@1": ScenarioCompareBlock,
    "remediation_checklist@1": RemediationChecklistBlock,
    "alert_origin@1": AlertOriginBlock,
}

BLOCK_PROPS_REGISTRY: Final[Mapping[str, type[ContractModel]]] = {
    key: model.model_fields["props"].annotation  # type: ignore[misc]
    for key, model in BLOCK_ENVELOPE_REGISTRY.items()
}

M1_BLOCK_KEYS: Final = (
    "kpi_strip@1",
    "approach_card@1",
    "recommended_option@1",
    "plan_diff@1",
    "approval_card@1",
    "commit_tracker@1",
    "narrative@1",
    "evidence_pack@1",
)
M3_BLOCK_KEYS: Final = ("network_map@1", "impact_graph@1", "stockout_timeline@1", "scenario_compare@1")
M5_BLOCK_KEYS: Final = ("remediation_checklist@1",)
M6_BLOCK_KEYS: Final = ("alert_origin@1",)
BLOCK_KEYS: Final = tuple(BLOCK_ENVELOPE_REGISTRY)
"""Every known ``kind@v`` (M1 + M3 + M5 + M6)."""

BLOCK_DATA_REGISTRY: Final[Mapping[str, type[BaseModel]]] = {
    "network_map@1": NetworkMapData,
    "impact_graph@1": ImpactGraphData,
    "stockout_timeline@1": StockoutTimelineData,
}
"""``kind@v`` -> model of the payload served at ``data_ref`` (``GET /api/blocks/{id}/data``)."""

CANVAS_ONLY_KINDS: Final = frozenset(
    {"evidence_pack", "network_map", "impact_graph", "stockout_timeline", "scenario_compare"}
)
"""Block kinds that may only be placed on the canvas."""

DATA_REF_REQUIRED_KINDS: Final = frozenset(
    key.split("@")[0] for key in ("network_map@1", "impact_graph@1", "stockout_timeline@1")
)
"""Block kinds whose payload lives in ``data_ref`` (the envelope must carry one)."""


def make_block(
    kind: str,
    props: ContractModel | Mapping[str, Any],
    *,
    title: str,
    provenance: BlockProvenance | Mapping[str, Any],
    placement: Placement | None = None,
    v: int = 1,
    data_ref: str | None = None,
    block_id: str | None = None,
) -> BlockBase:
    """Build and validate a block envelope.

    ``props`` may be the props model for ``kind@v`` or a plain mapping; passing a
    props model of another kind raises ``TypeError``. A new ``blk_`` id is minted
    unless ``block_id`` is given (use :func:`~keelson_contracts.common.stable_block_id`
    to re-emit/replace an existing block). ``placement`` defaults to ``"canvas"``
    for canvas-only kinds (:data:`CANVAS_ONLY_KINDS`) and ``"inline"`` otherwise;
    ``data_ref`` defaults to :func:`block_data_ref` for the kinds that require one
    (:data:`DATA_REF_REQUIRED_KINDS`).
    """
    key = block_key(kind, v)
    props_model = BLOCK_PROPS_REGISTRY.get(key)
    if props_model is None:
        raise KeyError(f"unknown block {key!r}; known: {', '.join(BLOCK_KEYS)}")
    if placement is None:
        placement = "canvas" if kind in CANVAS_ONLY_KINDS else "inline"
    block_id = block_id or new_block_id()
    if data_ref is None and kind in DATA_REF_REQUIRED_KINDS:
        data_ref = block_data_ref(block_id)
    if isinstance(props, ContractModel):
        if not isinstance(props, props_model):
            raise TypeError(
                f"props for {key} must be {props_model.__name__}, got {type(props).__name__}"
            )
        props_data: Any = props.model_dump(mode="python")
    else:
        props_data = dict(props)
    envelope = {
        "id": block_id,
        "kind": kind,
        "v": v,
        "title": title,
        "placement": placement,
        "provenance": provenance,
        "data_ref": data_ref,
        "props": props_data,
    }
    return BLOCK_ENVELOPE_REGISTRY[key].model_validate(envelope)


def parse_block(data: Mapping[str, Any] | str | bytes) -> BlockBase:
    """Validate a wire block (dict or JSON text) into its typed envelope model."""
    if isinstance(data, (str, bytes)):
        return BLOCK_ENVELOPE_ADAPTER.validate_json(data)  # type: ignore[no-any-return]
    return BLOCK_ENVELOPE_ADAPTER.validate_python(data)  # type: ignore[no-any-return]


def block_to_wire(block: BlockBase) -> dict[str, Any]:
    """JSON-ready dict of a block (dates as ISO strings, ``null`` kept for ``data_ref``)."""
    return block.model_dump(mode="json")


def parse_block_data(key: str, data: Mapping[str, Any] | str | bytes) -> BaseModel:
    """Validate the payload served at a block's ``data_ref`` for ``kind@v`` ``key``."""
    model = BLOCK_DATA_REGISTRY.get(key)
    if model is None:
        raise KeyError(f"block {key!r} has no data_ref payload; known: {', '.join(BLOCK_DATA_REGISTRY)}")
    if isinstance(data, (str, bytes)):
        return model.model_validate_json(data)
    return model.model_validate(data)


def block_data_to_wire(data: BaseModel) -> dict[str, Any]:
    """JSON-ready dict of a ``data_ref`` payload (``from`` keeps its wire name)."""
    out: dict[str, Any] = data.model_dump(mode="json", by_alias=True)
    return out
