"""Framing: ``frame_case`` → ``quick_exposure`` → ``approach_gate`` → ``approach_router``, plus the
case actions ``promote_case`` and ``confirm_assumptions`` (``docs/contracts/90-m4-investigation.md``
§1, §2, §5, §8).

``frame_case`` is a **generic template compiler**: the intake's ``event_type`` (a taxonomy key,
``data/taxonomy/events.yaml``) selects a template whose perturbations are compiled into a typed
:class:`DisruptionScenario` deterministically — no scenario-specific branches:

* entity mentions resolve deterministically — chokepoints by code / alias
  (``data/geo/chokepoints.json``), ports by UN/LOCODE or name (snapshot locations + the geo
  catalogue), countries and multi-country regions by name / alias / ISO code
  (``data/geo/regions.json``, expanded to the suppliers, factories, ports and DCs located there in
  the snapshot), lanes by code and components by code or material name
  (``data/taxonomy/synonyms.yaml``) — and with the connector's read tools (``get_entity`` /
  ``search_entities``) for suppliers, POs, SKUs, styles, DCs, stores and factories;
* ``resolve: receipts`` templates walk the BOM / PO data to the affected open PO lines;
* provenance: values the user stated (or edited) → ``decision``; template priors → ``assumption``
  (p10/p90 kept as the perturbation band); resolved entities and lane facts → ``fact``; anything
  that could not be resolved → ``inference``;
* ``is_hypothetical`` ("what if …") or a what-if case → ``mode: what_if``.

``promote_case`` (what-if → actual) re-builds the problem on the **current** snapshot and hands
the scenario to ``impact`` → ``simulate``; ``confirm_assumptions`` confirms ``unconfirmed``
assumptions (``refine_scenario``) and re-runs ``impact`` + ``simulate`` on the plan's snapshot.
Node inputs/outputs are documented in ``M4_NODES.md``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from google.adk.agents.context import Context
from google.adk.events.request_input import RequestInput
from keelson_contracts.agui import approach_response_schema
from keelson_contracts.blocks import (
    ApproachAgent,
    ApproachCardProps,
    ApproachCheck,
    DisruptionEventSummary,
    EditableValue,
    KpiItem,
    KpiStripProps,
    MatchedEntities,
    approach_edit_violations,
)
from keelson_contracts.common import (
    EVENT_TYPE_KEYS,
    EntityRef,
    Provenance,
    event_family_of,
    factor_bp_from_pct_change,
    iso_week_of,
    week_range,
)
from keelson_contracts.llm import DisruptionExtract, EntityMention, IntakeResult, InvestigationPlan
from keelson_contracts.optimizer import (
    ImpactKpis,
    ImpactRequest,
    ImpactResponse,
    ProblemData,
)
from keelson_contracts.runs import ApproachEdit
from keelson_contracts.scenario import (
    Assumption,
    CapacityScale,
    DemandScale,
    DisruptionScenario,
    LeadTimeShift,
    Selector,
    Window,
)
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from ..agents import fill_nullable
from ..context import RunContext, current_run
from ..errors import McpToolError, RuntimeConfigError
from ..geo import GeoCatalog, get_geo, normalize_name
from ..mcp_client import get_gateway
from ..optimizer_client import get_optimizer
from ..problem import ProblemBundle, ToolCaller, gather_problem_data
from ..problem_scn import build_problem_from_snapshot, receipts_for_entity
from ..scn_store import SnapshotRef
from ..signal_hub import EVENT_SOURCE_ALERT, SIGNAL_STATE_KEY
from .common import (
    ENGINE_FRAME,
    ENGINE_OPT,
    as_of,
    block_event,
    case_event,
    input_dict,
    progress_event,
    routed,
    say,
)
from .investigate import FINDINGS_TOOL
from .refine import (
    ASSUMPTION_LABELS,
    FINDINGS_STATE_KEY,
    capacity_text,
    confirm_scenario_keys,
    date_text,
    days_text,
    pct_text,
    receipts_text,
    scenario_value,
)
from .taxonomy import (
    EventTemplate,
    Gazetteer,
    PerturbationTemplate,
    Synonyms,
    Taxonomy,
    TaxonomyError,
    ValueSpec,
    get_gazetteer,
    get_synonyms,
    get_taxonomy,
    normalize_term,
)

__all__ = [
    "APPROACH_CARD_STATE_KEY",
    "APPROACH_RESUME_SCHEMA",
    "BUILD_ERRORS",
    "DATA_SOURCE_STATE_KEY",
    "SNAPSHOT_LABEL_STATE_KEY",
    "EVIDENCE_STATE_KEY",
    "INTAKE_STATE_KEY",
    "PROBLEM_STATE_KEY",
    "ROUTE_DONE",
    "ROUTE_RESIMULATE",
    "SNAPSHOT_ERRORS",
    "SNAPSHOT_STATE_KEY",
    "PlaceIndex",
    "SnapshotDirectory",
    "Stated",
    "approach_gate",
    "approach_interrupt_id",
    "approach_router",
    "build_scenario",
    "case_findings",
    "coerce_legacy_intake",
    "compile_template",
    "confirm_assumptions",
    "current_snapshot",
    "data_source_assumption",
    "editable_values",
    "frame_case",
    "frame_problem_bundle",
    "impact_kpi_items",
    "is_signal_scenario",
    "lane_effects",
    "load_case_bundle",
    "load_problem",
    "load_problem_bundle",
    "load_snapshot_directory",
    "overrides_from",
    "parse_edits",
    "place_index",
    "promote_case",
    "quick_exposure",
    "region_members",
    "resimulate_payload",
    "resolve_component_groups",
    "resolve_components",
    "resolve_mentions",
    "resolve_static",
    "resolve_static_groups",
    "retarget_scenario",
    "run_impact",
]

log = structlog.get_logger(__name__)

INTAKE_STATE_KEY = "intake"
PROBLEM_STATE_KEY = "problem_evidence_id"
EVIDENCE_STATE_KEY = "frame_evidence_ids"
SNAPSHOT_STATE_KEY = "scn_snapshot_id"
"""Snapshot the case's ProblemData was built from (``None`` = live MCP reads, advisory)."""
DATA_SOURCE_STATE_KEY = "problem_source"
SNAPSHOT_LABEL_STATE_KEY = "scn_snapshot_label"
"""M7: ``{consistency, as_of_range}`` of the snapshot the ProblemData came from (plans keep it in
``plan.kpis.snapshot_label``; ``crawled`` → advisory blocks and narrative)."""
APPROACH_CARD_STATE_KEY = "approach_card"
"""The last approach card's props (typed **Edit** decisions are checked against it)."""
ROUTE_RESIMULATE: Final = "RESIMULATE"
"""``promote_case`` / ``confirm_assumptions`` → ``impact`` (→ simulate → recommend → compose)."""
ROUTE_DONE: Final = "DONE"
"""Nothing to re-simulate (no scenario / nothing to confirm): the run ends after the message."""
SNAPSHOT_ERRORS = (SQLAlchemyError, ValidationError, RuntimeConfigError, LookupError, ValueError, OSError)
"""Failures of the snapshot path that fall back to the MCP gatherer (labelled advisory)."""
BUILD_ERRORS = (*SNAPSHOT_ERRORS, TypeError, ArithmeticError)
"""Building ProblemData from a snapshot also falls back on rows of an unexpected shape (e.g. a
``NULL`` where the builder needs a value): the case runs on live reads instead of failing."""
APPROACH_RESUME_SCHEMA = {
    "type": "object",
    "properties": {"decision": {"type": "string"}, "interrupt_id": {"type": "string"}},
    "required": ["decision", "interrupt_id"],
}
"""Server-built resume shape ``{decision, interrupt_id}`` (validated by ADK on resume)."""

_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9#_.-]*\d[A-Z0-9#_.-]*$")
_DAYS_RE = re.compile(r"(?<![\w.])(\d{1,3})\s*(?:days?|d)\b", re.I)
_WEEKS_RE = re.compile(r"(?<![\w.])(\d{1,2})\s*weeks?\b", re.I)
_PCT_RE = re.compile(r"(?<![\w.])([+-]?\d{1,3})\s*%")
_LOCODE_RE = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")
_ISO2_RE = re.compile(r"^[A-Z]{2}$")
_PORT_WORDS = frozenset({"port", "of", "harbour", "harbor", "terminal", "container"})

MCP_ENTITY_TYPES = frozenset({"supplier", "po", "sku", "style", "dc", "store", "factory"})
"""Mention types resolved with the connector's ``get_entity`` / ``search_entities``."""
ENTITY_TYPES = MCP_ENTITY_TYPES  # M1 name
STATIC_TYPES = frozenset({"chokepoint", "port", "region", "lane", "component", "other"})
"""Mention types resolved deterministically from the snapshot + ``data/geo`` (no tool call)."""
WALKED_KINDS = frozenset({"receipt", "po_line", "po", "supplier", "component", "factory"})
"""Kinds a ``resolve: receipts`` template walks to PO lines (other target kinds are direct)."""
LEGACY_EVENT_DEFAULTS: Final[Mapping[str, str]] = {"SUP": "SUP.delay", "LOG": "SUP.delay"}
"""Pre-M4 free-text event types (stored intakes of paused cases) by family: M1 compiled every
SUP/LOG event to a lead-time shift."""


# ───────────────────────────────── edits ─────────────────────────────────


def parse_edits(text: str | None) -> dict[str, int]:
    """Deterministic reading of free-text approach-card edits ("make it 10 days", "+20%")."""
    if not text:
        return {}
    out: dict[str, int] = {}
    if m := _DAYS_RE.search(text):
        out["delay_days"] = int(m.group(1))
    elif m := _WEEKS_RE.search(text):
        out["delay_days"] = 7 * int(m.group(1))
    if m := _PCT_RE.search(text):
        out["demand_change_pct"] = int(m.group(1))
    return out


def overrides_from(edits: Any) -> dict[str, Any]:
    """Approach **Edit** → overrides: free text (M1, :func:`parse_edits`) or typed edits (M4,
    ``[{key, value_int | value_ids}]``, keys = assumption keys)."""
    if edits is None or edits == "":
        return {}
    if isinstance(edits, str):
        return dict(parse_edits(edits))
    out: dict[str, Any] = {}
    for raw in edits:
        edit = raw if isinstance(raw, ApproachEdit) else ApproachEdit.model_validate(raw)
        out[edit.key] = list(edit.value_ids) if edit.value_ids is not None else int(edit.value_int)  # type: ignore[arg-type]
    return out


@dataclass(frozen=True)
class Stated:
    """Values the user stated (intake) or edited (approach card): they become ``decision``.

    M6 (``100`` §6): a case framed from a Signal Hub event states the event's own values (its
    window); ``provs`` gives those keys their provenance (``fact`` for an authoritative feed,
    ``inference`` for news) instead of ``decision``."""

    delay_days: int | None = None
    duration_days: int | None = None
    demand_change_pct: int | None = None
    capacity_pct: int | None = None
    start_date: dt.date | None = None
    receipts: tuple[str, ...] | None = None
    provs: Mapping[str, Provenance] = field(default_factory=dict)
    """Assumption key → provenance of a stated value that is not the user's own (default ``decision``)."""

    def prov(self, key: str) -> Provenance:
        return self.provs.get(key, Provenance.DECISION)

    @classmethod
    def of(cls, extract: DisruptionExtract, overrides: Mapping[str, Any], template: EventTemplate) -> Stated:
        def num(key: str, fallback: int | None) -> int | None:
            value = overrides.get(key, fallback)
            return None if value is None or isinstance(value, list) else int(value)

        delay = num("delay_days", extract.delay_days)
        duration = num("duration_days", extract.duration_days)
        params = template.params()
        if "delay_days" not in params and template.has_duration and duration is None and delay is not None:
            # "closed for 3 weeks" read as a delay by the intake / a free-text edit: it is the duration
            duration, delay = delay, None
        start = _iso_date(overrides.get("start_date")) or _iso_date(extract.start_date)
        receipts = overrides.get("affected_receipts")
        return cls(
            delay_days=None if delay is None else max(0, delay),
            duration_days=None if duration is None or duration < 1 else duration,
            demand_change_pct=num("demand_change_pct", extract.demand_change_pct),
            capacity_pct=num("capacity_pct", None),
            start_date=start,
            receipts=tuple(str(r) for r in receipts) if isinstance(receipts, list) and receipts else None,
        )


def _iso_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def coerce_legacy_intake(intake: Any) -> Any:
    """A stored pre-M4 intake whose ``event_type`` is free text (``"trim_delay"``) mapped onto the
    taxonomy so it validates (resume hazard after the M4 deploy): SUP/LOG → ``SUP.delay`` (what M1
    compiled them to), DEM → ``DEM.spike``/``DEM.drop`` by the stated change, else ``OTHER``."""
    if not isinstance(intake, Mapping) or not isinstance(intake.get("disruption"), Mapping):
        return intake
    disruption = dict(intake["disruption"])
    event_type = disruption.get("event_type")
    if event_type in EVENT_TYPE_KEYS:
        return intake
    family = str(disruption.get("event_family") or "OTHER")
    if family == "DEM":
        pct = disruption.get("demand_change_pct")
        mapped = "DEM.drop" if isinstance(pct, int) and pct < 0 else "DEM.spike"
    elif disruption.get("delay_days") is not None:
        mapped = "SUP.delay"
    else:
        mapped = LEGACY_EVENT_DEFAULTS.get(family, "OTHER")
    log.info("legacy intake event type mapped onto the taxonomy", legacy=str(event_type)[:60], mapped=mapped)
    disruption["event_type"] = mapped
    disruption["event_family"] = event_family_of(mapped)
    return {**intake, "disruption": disruption}


# ─────────────────────────── mention resolution ───────────────────────────


class _RecordingCaller:
    """Wraps a connector session: every read becomes an evidence row (prov fact)."""

    def __init__(self, session: ToolCaller, run: RunContext, connector: str) -> None:
        self._session = session
        self._run = run
        self._connector = connector
        self.evidence_ids: list[str] = []

    async def record(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> str:
        evidence_id = await self._run.deps.repo.save_evidence(
            tenant=self._run.tenant,
            case_id=self._run.case_id,
            run_id=self._run.run_id,
            connector=self._connector,
            tool=tool,
            args=args,
            result=result,
            prov="fact",
        )
        self.evidence_ids.append(evidence_id)
        return evidence_id

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self._session.call(tool, args or {})
        await self.record(tool, dict(args or {}), result)
        return result


Q_DIRECTORY_LOCATIONS: Final = """
SELECT kind, code, name, country
  FROM scn_location
 WHERE tenant = :t AND snapshot_id = :s AND kind IN ('supplier', 'factory', 'port', 'dc')
 ORDER BY kind, code
"""
Q_DIRECTORY_COMPONENTS: Final = """
SELECT code, family
  FROM scn_item
 WHERE tenant = :t AND snapshot_id = :s AND kind = 'component'
 ORDER BY code
"""
REGION_KINDS: Final = ("supplier", "factory", "port", "dc")
"""Entity kinds a region mention expands to (the ones located there)."""
REGION_WALK_KINDS: Final = ("supplier", "factory")
"""Kinds a region expands to for a ``resolve: receipts`` template (the origin side of a PO line)."""
_SEGMENT_RE = re.compile(r"[^A-Z0-9]+")
_ALTERNATIVES_RE = re.compile(r"\b(?:and|or|plus|with)\b")


@dataclass(frozen=True)
class SnapshotDirectory:
    """Where entities are and which components exist, read from the scn snapshot
    (``scn_location`` suppliers / factories / ports / DCs with their country; ``scn_item``
    components). ProblemData carries neither, so ``frame_case`` reads them next to it."""

    locations: tuple[Mapping[str, Any], ...] = ()
    """``{kind, code, name, country}``"""
    components: tuple[Mapping[str, Any], ...] = ()
    """``{code, family, name?}``"""


async def load_snapshot_directory(run: RunContext, snapshot_id: str | None) -> SnapshotDirectory | None:
    """The snapshot's entity directory, or ``None`` (no snapshot, no store, or unreadable: region
    mentions then expand to ports by their UN/LOCODE country only)."""
    if not snapshot_id or run.deps.scn is None:
        return None
    params = {"t": run.tenant, "s": str(snapshot_id)}
    try:
        async with run.deps.scn.reader(run.tenant) as reader:
            locations = await reader.fetch(Q_DIRECTORY_LOCATIONS, params)
            components = await reader.fetch(Q_DIRECTORY_COMPONENTS, params)
    except SNAPSHOT_ERRORS as exc:
        log.warning("snapshot directory unavailable; regions resolve to ports only", snapshot_id=snapshot_id,
                    error=type(exc).__name__)
        return None
    return SnapshotDirectory(locations=tuple(dict(r) for r in locations),
                             components=tuple(dict(r) for r in components))


def _gazetteer() -> Gazetteer:
    try:
        return get_gazetteer()
    except TaxonomyError as exc:  # checked at start-up; never fail a case over a missing dictionary
        log.warning("gazetteer unavailable; region names will not resolve", error=str(exc)[:200])
        return Gazetteer.empty()


def _synonyms() -> Synonyms:
    try:
        return get_synonyms()
    except TaxonomyError as exc:
        log.warning("material synonyms unavailable; components resolve by code and name only", error=str(exc)[:200])
        return Synonyms()


@dataclass
class PlaceIndex:
    """Deterministic lookup tables built from the ProblemData, the snapshot directory and ``data/geo``."""

    ports: dict[str, str] = field(default_factory=dict)
    """UN/LOCODE → name."""
    lanes: set[str] = field(default_factory=set)
    components: set[str] = field(default_factory=set)
    """Component codes (BOM + snapshot component items)."""
    regions: set[str] = field(default_factory=set)
    """ISO 3166-1 alpha-2 codes known to the geo catalogue."""
    located: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    """kind (supplier / factory / port / dc) → code → (label, ISO-2 country or "")."""
    component_labels: dict[str, str] = field(default_factory=dict)
    component_suppliers: dict[str, set[str]] = field(default_factory=dict)
    component_factories: dict[str, set[str]] = field(default_factory=dict)
    gazetteer: Gazetteer = field(default_factory=Gazetteer.empty)
    common_segments: set[str] = field(default_factory=set)
    """Code segments every component shares (a ``CMP`` prefix): they never identify a material."""

    def country_of(self, kind: str, code: str) -> str:
        return self.located.get(kind, {}).get(code, ("", ""))[1]

    def component_segments(self, code: str) -> set[str]:
        """The distinctive segments of a component code."""
        return set(_SEGMENT_RE.split(code.upper())) - self.common_segments - {""}


def place_index(problem: ProblemData | None, geo: GeoCatalog, directory: SnapshotDirectory | None = None,
                gazetteer: Gazetteer | None = None) -> PlaceIndex:
    gaz = gazetteer if gazetteer is not None else _gazetteer()
    idx = PlaceIndex(regions=set(geo.centroids), gazetteer=gaz)
    for code, (name, kind, _pt) in geo.places.items():
        if kind == "port":
            idx.ports[code] = name
    if problem is not None:
        for loc in problem.locations:
            if loc.kind == "port":
                idx.ports[loc.code] = loc.name or idx.ports.get(loc.code, loc.code)
        for il in problem.inbound_lanes:
            idx.lanes.add(il.code)
            for code in (il.origin, *il.via):
                if _LOCODE_RE.match(code) and code not in geo.chokepoints and code not in idx.ports:
                    idx.ports[code] = code
        idx.lanes |= {f"{ln.from_dc}>{ln.to_dc}" for ln in problem.lanes}
        idx.components = {b.component_code for b in problem.bom}
        for b in problem.bom:
            idx.component_suppliers.setdefault(b.component_code, set()).add(b.supplier_code)
            if b.factory_code:
                idx.component_factories.setdefault(b.component_code, set()).add(b.factory_code)
    if directory is not None:
        for row in directory.locations:
            kind, code = str(row.get("kind") or ""), str(row.get("code") or "")
            if kind not in REGION_KINDS or not code:
                continue
            label = str(row.get("name") or code)
            idx.located.setdefault(kind, {})[code] = (label, gaz.country_code(row.get("country")) or "")
        for row in directory.components:
            code = str(row.get("code") or "")
            if not code:
                continue
            idx.components.add(code)
            name = " ".join(str(row[k]) for k in ("name", "family") if row.get(k))
            if name:
                idx.component_labels[code] = name
    for code, name in idx.ports.items():  # ports without a directory row: the UN/LOCODE country prefix
        if _LOCODE_RE.match(code):
            idx.located.setdefault("port", {}).setdefault(code, (name, code[:2]))
    if len(idx.components) > 1:
        idx.common_segments = set.intersection(*(set(_SEGMENT_RE.split(c.upper())) for c in idx.components))
    return idx


def _resolve_port(text: str, idx: PlaceIndex) -> EntityRef | None:
    raw = text.strip()
    if raw.upper() in idx.ports and _LOCODE_RE.match(raw.upper()):
        return EntityRef(type="port", code=raw.upper(), label=idx.ports[raw.upper()])
    words = [w for w in normalize_name(raw).split() if w not in _PORT_WORDS]
    wanted = " ".join(words)
    if len(wanted) < 3:
        return None
    best: tuple[int, str] | None = None
    for code, name in sorted(idx.ports.items()):
        norm = normalize_name(name)
        if norm == wanted:
            return EntityRef(type="port", code=code, label=name)
        hit = f" {wanted} " in f" {norm} " or f" {norm} " in f" {wanted} "
        if hit and (best is None or len(norm) > best[0]):
            best = (len(norm), code)
    if best is None:
        return None
    return EntityRef(type="port", code=best[1], label=idx.ports[best[1]])


def _resolve_regions(text: str, idx: PlaceIndex, *, phrase: bool = True) -> list[EntityRef]:
    """The countries / multi-country regions a mention names (``data/geo/regions.json``; several
    for "Vietnam and Cambodia"), else a bare ISO-2 code the geo catalogue knows. ``phrase=False``:
    only when the whole mention is a place."""
    areas = idx.gazetteer.resolve_all(text, phrase=phrase)
    if areas:
        return [EntityRef(type="region", code=a.code, label=a.name) for a in areas]
    raw = text.strip().upper()
    if _ISO2_RE.match(raw) and raw in idx.regions:
        return [EntityRef(type="region", code=raw, label=raw)]
    return []


def _is_component(code: str, keys: Sequence[str], tokens: Sequence[str], idx: PlaceIndex, *,
                  catalogued: bool = False) -> bool:
    """``catalogued``: the tokens are a ``synonyms.yaml`` material's code segments, which match any
    segment (even one every component shares, e.g. a snapshot holding wadding items only); tokens
    taken from the user's words match distinctive segments only (never the shared ``CMP``)."""
    segments = set(_SEGMENT_RE.split(code.upper())) - {""} if catalogued else idx.component_segments(code)
    if segments & set(tokens):
        return True
    label = normalize_term(idx.component_labels.get(code, ""))
    return bool(label) and any(f" {k} " in f" {label} " for k in keys)


def resolve_component_groups(text: str, idx: PlaceIndex, synonyms: Synonyms | None = None) -> list[list[EntityRef]]:
    """Components a mention names, one group per material ("wadding and zips" → [wadding…], [zip…]):

    * an exact component code;
    * a material of ``data/taxonomy/synonyms.yaml`` (term or alias, simple plurals folded): the
      components whose code has one of its code segments, or whose name / family contains it;
    * else each part of the mention ("a and b") matched against component names / families, or a
      distinctive code segment equal to it ("zip" → ``…-ZIP-…``)."""
    raw = text.strip()
    if not raw or not idx.components:
        return []
    if raw.upper() in idx.components:
        code = raw.upper()
        return [[EntityRef(type="component", code=code, label=idx.component_labels.get(code, code))]]
    syn = synonyms if synonyms is not None else _synonyms()
    wanted: list[tuple[tuple[str, ...], tuple[str, ...]]] = [(m.keys, m.code_tokens) for m in syn.find(raw)]
    catalogued = bool(wanted)
    if not wanted:
        for part in _ALTERNATIVES_RE.split(normalize_term(raw)):
            part = part.strip()
            if len(part) >= 3:
                wanted.append(((part,), () if " " in part else (part.upper(),)))
    groups: list[list[EntityRef]] = []
    for keys, tokens in wanted:
        codes = sorted(c for c in idx.components if _is_component(c, keys, tokens, idx, catalogued=catalogued))
        if codes:
            groups.append([EntityRef(type="component", code=c, label=idx.component_labels.get(c, c)) for c in codes])
    return groups


def resolve_components(text: str, idx: PlaceIndex, synonyms: Synonyms | None = None) -> list[EntityRef]:
    return _dedupe([r for g in resolve_component_groups(text, idx, synonyms) for r in g])


def resolve_static_groups(mention: EntityMention, idx: PlaceIndex, geo: GeoCatalog,
                          synonyms: Synonyms | None = None) -> list[list[EntityRef]]:
    """Deterministic resolution (no tool call) of chokepoint / port / region / lane / component /
    other mentions, as groups of refs (a material can name several components). A chokepoint alias
    wins for ``port`` (exact name only) / ``region`` / ``other`` / ``lane`` mentions ("Suez" typed as
    a region is the Suez Canal)."""
    text = mention.text.strip()
    if not text:
        return []
    if mention.type == "port":
        # a port mention is a chokepoint only when it *is* a chokepoint name ("Suez"): "Port of
        # Dover" / "Panama City port" are ports, never the strait or canal next to them
        cp = geo.resolve_chokepoint(text, exact=True)
        if cp is not None:
            return [[EntityRef(type="chokepoint", code=cp.code, label=cp.name)]]
        port = _resolve_port(text, idx)
        return [[port]] if port is not None else []
    if mention.type in ("chokepoint", "region", "other", "lane"):
        cp = geo.resolve_chokepoint(text)
        if cp is not None:
            return [[EntityRef(type="chokepoint", code=cp.code, label=cp.name)]]
    if mention.type in ("other", "region"):
        port = _resolve_port(text, idx)
        if port is not None and port.code == text.upper():
            return [[port]]
        regions = _resolve_regions(text, idx)
        if regions:
            return [[r] for r in regions]
    if mention.type in ("lane", "other") and text.upper() in idx.lanes:
        return [[EntityRef(type="lane", code=text.upper(), label=text.upper())]]
    if mention.type in ("component", "other"):
        return resolve_component_groups(text, idx, synonyms)
    return []


def resolve_static(mention: EntityMention, idx: PlaceIndex, geo: GeoCatalog,
                   synonyms: Synonyms | None = None) -> EntityRef | None:
    """The first ref of :func:`resolve_static_groups` (``None`` = unresolved)."""
    groups = resolve_static_groups(mention, idx, geo, synonyms)
    return groups[0][0] if groups and groups[0] else None


async def _resolve_mcp(caller: ToolCaller, mention: EntityMention) -> EntityRef | None:
    if mention.type not in MCP_ENTITY_TYPES:
        return None
    text = mention.text.strip()
    if not text:
        return None
    if _CODE_RE.match(text.upper()):
        try:
            entity = await caller.call("get_entity", {"type": mention.type, "code": text.upper()})
        except McpToolError as exc:
            if exc.code not in ("not_found", "invalid_parameter"):
                raise
        else:
            data = entity.get("data") or {}
            label = str(data.get("name") or data.get("label") or entity.get("code"))
            return EntityRef(type=mention.type, code=str(entity["code"]), label=label)
    found = await caller.call("search_entities", {"query": text, "types": [mention.type], "limit": 5})
    items = found.get("items") or []
    if not items:
        return None
    best = items[0]
    return EntityRef(type=best["type"], code=str(best["code"]), label=str(best.get("label") or best["code"]))


PRODUCT_MENTION_TYPES: Final = frozenset({"sku", "style", "component"})
"""Mention types that name a product or material: a place word inside one describes the product
("Egyptian cotton", "French terry"), so they are read as a region only when the whole mention is a
place."""


def _fallback_groups(mention: EntityMention, idx: PlaceIndex, synonyms: Synonyms) -> list[list[EntityRef]]:
    """A mention the intake typed loosely (nothing resolved it as typed): a material typed as a SKU
    / style is a component; a place named by a business-entity mention ("the Bangladesh POs" as a
    PO, "Vietnamese supplier") is a region. A product mention is a region only when it is nothing
    but the place."""
    if mention.type in ("chokepoint", "port", "lane"):
        return []
    if mention.type in ("sku", "style"):
        found = resolve_component_groups(mention.text, idx, synonyms)
        if found:
            return found
    regions = _resolve_regions(mention.text, idx, phrase=mention.type not in PRODUCT_MENTION_TYPES)
    return [[r] for r in regions]


def _reading_text(mention: EntityMention, groups: Sequence[Sequence[EntityRef]]) -> str:
    regions = [r.label for g in groups for r in g if r.type == "region"]
    what = ", ".join(regions) if regions else "a material (component items)"
    return f"'{mention.text.strip()[:80]}' as {what}"


def _narrow_components(groups: list[list[EntityRef]], idx: PlaceIndex) -> list[list[EntityRef]]:
    """A material that names several components ("wadding" → three wadding items) keeps the ones
    supplied by a supplier, used at a factory or sourced from a region named in the same message;
    with no such context (or none fitting) every match stays."""
    refs = [r for g in groups for r in g]
    suppliers = {r.code for r in refs if r.type == "supplier"}
    factories = {r.code for r in refs if r.type == "factory"}
    countries = {c for r in refs if r.type == "region" for c in idx.gazetteer.countries_of(r.code)}
    if not (suppliers or factories or countries):
        return groups

    def fits(code: str) -> bool:
        sups = idx.component_suppliers.get(code, set())
        facs = idx.component_factories.get(code, set())
        return bool(sups & suppliers or facs & factories
                    or any(idx.country_of("supplier", s) in countries for s in sups)
                    or any(idx.country_of("factory", f) in countries for f in facs))

    out: list[list[EntityRef]] = []
    for group in groups:
        comps = [r for r in group if r.type == "component"]
        if len(comps) <= 1:
            out.append(group)
            continue
        kept = [r for r in comps if fits(r.code)] or comps
        out.append([r for r in group if r.type != "component"] + kept)
    return out


async def resolve_mentions(
    caller: ToolCaller, mentions: Sequence[EntityMention], idx: PlaceIndex, geo: GeoCatalog,
    synonyms: Synonyms | None = None,
    readings: list[str] | None = None,
) -> tuple[list[EntityRef], list[str]]:
    """``(matched refs, unmatched mention texts)``: static (data/geo + snapshot) first, then the
    connector for business entities, then the loose fallbacks (material, region). A mention is
    unmatched only when all of them fail. Each mention resolved only by a fallback is appended to
    ``readings`` ("'the Bangladesh POs' as Bangladesh"): it is an interpretation, shown to the user
    as an inference, not a fact."""
    syn = synonyms if synonyms is not None else _synonyms()
    groups: list[list[EntityRef]] = []
    unmatched: list[str] = []
    for mention in mentions:
        found = resolve_static_groups(mention, idx, geo, syn) if mention.type in STATIC_TYPES else []
        if not found and mention.type in MCP_ENTITY_TYPES:
            ref = await _resolve_mcp(caller, mention)
            found = [[ref]] if ref is not None else []
        if not found:
            found = _fallback_groups(mention, idx, syn)
            if found and readings is not None:
                readings.append(_reading_text(mention, found))
        if found:
            groups += found
        else:
            unmatched.append(mention.text)
    return _dedupe([r for g in _narrow_components(groups, idx) for r in g]), unmatched


def region_members(code: str, kinds: Sequence[str], idx: PlaceIndex) -> list[EntityRef]:
    """Suppliers / factories / ports / DCs of the given kinds located in a region's countries."""
    countries = set(idx.gazetteer.countries_of(code))
    out: list[EntityRef] = []
    for kind in kinds:
        if kind not in REGION_KINDS:
            continue
        for member, (label, country) in sorted(idx.located.get(kind, {}).items()):
            if country and country in countries:
                out.append(EntityRef(type=kind, code=member, label=label))  # type: ignore[arg-type]
    return out


def _dedupe(refs: Sequence[EntityRef]) -> list[EntityRef]:
    seen: set[tuple[str, str]] = set()
    out: list[EntityRef] = []
    for ref in refs:
        if (ref.type, ref.code) not in seen:
            seen.add((ref.type, ref.code))
            out.append(ref)
    return out


# ───────────────────────────── template compiler ─────────────────────────────


@dataclass(frozen=True)
class LaneEffect:
    lane: str
    alt_lane: str | None
    extra_days: int | None
    open_lines: int


def lane_effects(problem: ProblemData | None, ids: set[str]) -> list[LaneEffect]:
    """Inbound lanes passing (origin / via / destination) any of ``ids`` with their catalogued
    alternative and its extra transit (alt − base transit days) — data, not LLM output."""
    if problem is None:
        return []
    by_code = {il.code: il for il in problem.inbound_lanes}
    lines: dict[str, int] = {}
    for pl in problem.po_lines:
        if pl.lane_code:
            lines[pl.lane_code] = lines.get(pl.lane_code, 0) + 1
    out: list[LaneEffect] = []
    for il in sorted(problem.inbound_lanes, key=lambda x: x.code):
        if not ids & {il.origin, *il.via, il.dest_dc}:
            continue
        alt = by_code.get(il.alt_lane_code) if il.alt_lane_code else None
        out.append(LaneEffect(il.code, alt.code if alt else None,
                              None if alt is None else alt.transit_days - il.transit_days, lines.get(il.code, 0)))
    return out


def _lane_effects_text(effects: Sequence[LaneEffect], limit: int = 6) -> str:
    parts = []
    for e in effects[:limit]:
        if e.alt_lane is None:
            parts.append(f"{e.lane} (no alternative lane)")
        else:
            sign = "+" if (e.extra_days or 0) >= 0 else "-"
            parts.append(f"{e.lane} → {e.alt_lane} ({sign}{days_text(abs(e.extra_days or 0))})")
    more = f" and {len(effects) - limit} more" if len(effects) > limit else ""
    lines = sum(e.open_lines for e in effects)
    tail = f"; {lines} open PO line" + ("" if lines == 1 else "s") + " ship on them" if lines else ""
    return "; ".join(parts) + more + tail


def _value_of(spec: ValueSpec, stated: Stated, prior_p50: Mapping[str, int]) -> tuple[int, Any, Provenance | None]:
    """``(p50, band | None, prov)`` — prov ``None`` = a fixed value that defines the event."""
    given = {
        "delay_days": stated.delay_days,
        "demand_change_pct": stated.demand_change_pct,
        "factor_bp": None if stated.capacity_pct is None else 100 * stated.capacity_pct,
    }[spec.param]
    if given is not None:
        key = "capacity_pct" if spec.param == "factor_bp" else spec.param
        return int(given), None, stated.prov(key)
    if spec.fixed is not None:
        return spec.fixed, None, None
    prior = spec.prior
    assert prior is not None  # ValueSpec guarantees exactly one of prior / fixed
    band = prior.with_p50(prior_p50[spec.param]) if spec.param in prior_p50 else prior
    return band.p50, band, Provenance.ASSUMPTION


@dataclass
class _Compiled:
    perturbations: list[Any] = field(default_factory=list)
    assumptions: dict[str, Assumption] = field(default_factory=dict)

    def note(self, key: str, label: str, text: str, prov: Provenance) -> None:
        self.assumptions.setdefault(key, Assumption(key=key, label=label, value_text=text, prov=prov))


def _weakest(provs: Sequence[Provenance | None]) -> Provenance:
    real = [p for p in provs if p is not None]
    for weak in (Provenance.INFERENCE, Provenance.ASSUMPTION):
        if weak in real:
            return weak
    return Provenance.DECISION


def _region_kinds(tpl: PerturbationTemplate) -> list[str]:
    """Kinds a region mention expands to for this template: the origin side (supplier, factory)
    for a receipts walk, else the template's target kinds that have a location."""
    kinds = REGION_WALK_KINDS if tpl.resolve == "receipts" else REGION_KINDS
    return [k for k in kinds if k in tpl.target_kinds]


def _direct_targets(tpl: PerturbationTemplate, matched: Sequence[EntityRef], idx: PlaceIndex) -> list[EntityRef]:
    refs = [m for m in matched if m.type in tpl.target_kinds]
    kinds = _region_kinds(tpl)
    for region in (m for m in matched if m.type == "region"):
        refs += region_members(region.code, kinds, idx)
    return _dedupe(refs)


def _selector(refs: Sequence[EntityRef]) -> Selector:
    ids = list(dict.fromkeys(r.code for r in refs))
    return Selector(kinds=sorted({r.type for r in refs}), ids=sorted(ids))  # type: ignore[arg-type]


def compile_template(
    *,
    template: EventTemplate,
    stated: Stated,
    matched: Sequence[EntityRef],
    receipts: Sequence[str],
    idx: PlaceIndex,
    problem: ProblemData | None,
    all_clusters: Sequence[str],
    planning_date: dt.date,
    prior_p50: Mapping[str, int],
    vendor: str,
) -> tuple[list[Any], list[Assumption], list[EntityRef]]:
    """Compile an event template into ``(perturbations, assumptions, extra matched refs)``.

    Pure and deterministic. ``receipts`` are the open PO lines walked from the matched supplier /
    component / factory / PO mentions (only used by ``resolve: receipts`` templates)."""
    out = _Compiled()
    extra_refs: list[EntityRef] = []
    window: Window | None = None
    window_provs: list[Provenance | None] = []
    windowed = any(p.windowed for p in template.perturbations)
    if windowed and (stated.duration_days is not None or template.duration_days is not None):
        if stated.duration_days is not None:
            duration, d_prov = stated.duration_days, stated.prov("duration_days")
        else:
            duration, d_prov = template.duration_days.p50, Provenance.ASSUMPTION  # type: ignore[union-attr]
        start, s_prov = ((stated.start_date, stated.prov("start_date")) if stated.start_date
                         else (planning_date, Provenance.ASSUMPTION))
        window = Window(start=start, end=start + dt.timedelta(days=duration - 1))
        window_provs = [d_prov, s_prov]
        out.note("duration_days", ASSUMPTION_LABELS["duration_days"], days_text(duration), d_prov)
        out.note("start_date", ASSUMPTION_LABELS["start_date"], date_text(start), s_prov)
    for tpl in template.perturbations:
        value, band, v_prov = _value_of(tpl.value, stated, prior_p50)
        provs: list[Provenance | None] = [v_prov]
        target: Selector | None = None
        if tpl.resolve == "receipts":
            if stated.receipts:
                target = Selector(kinds=["receipt"], ids=sorted(set(stated.receipts)))
                out.note("affected_receipts", ASSUMPTION_LABELS["affected_receipts"], receipts_text(target.ids),
                         stated.prov("affected_receipts"))
            elif receipts:
                target = Selector(kinds=["receipt"], ids=sorted(set(receipts)))
                out.note("affected_receipts", ASSUMPTION_LABELS["affected_receipts"], receipts_text(target.ids),
                         Provenance.FACT)
            else:
                fallback = [m for m in matched if m.type in tpl.target_kinds and m.type not in WALKED_KINDS]
                if fallback:
                    target = _selector(fallback)
                    out.note("affected_receipts", ASSUMPTION_LABELS["affected_receipts"], ", ".join(target.ids),
                             Provenance.FACT)
                else:
                    out.note("affected_receipts", ASSUMPTION_LABELS["affected_receipts"],
                             "No open purchase-order lines matched the affected supplier, factory or orders",
                             Provenance.INFERENCE)
        else:
            refs = _direct_targets(tpl, matched, idx)
            named = {(m.type, m.code) for m in matched}
            extra_refs += [r for r in refs if (r.type, r.code) not in named]  # located in a named region
            if refs:
                target = _selector(refs)
            elif tpl.default_scope == "all_clusters" and all_clusters:
                target = Selector(kinds=["cluster"], ids=sorted(set(all_clusters)))
            else:
                kinds = " or ".join(k.replace("_", " ") for k in tpl.target_kinds)
                out.note(f"targets_{tpl.op}", "Affected locations",
                         f"No {kinds} named in the message could be matched", Provenance.INFERENCE)
        if target is None:
            continue
        use_window = window if tpl.windowed else None
        if use_window is not None:
            provs += window_provs
        prov = _weakest(provs)
        common: dict[str, Any] = {"target": target, "prov": prov, "window": use_window}
        if tpl.op == "lead_time_shift":
            out.perturbations.append(LeadTimeShift(op="lead_time_shift", days=value, p10=band and band.p10,
                                                   p90=band and band.p90, **common))
            out.note("delay_days", ASSUMPTION_LABELS["delay_days"], days_text(value), v_prov or Provenance.DECISION)
        elif tpl.op == "capacity_scale":
            out.perturbations.append(CapacityScale(op="capacity_scale", factor_bp=value, p10=band and band.p10,
                                                   p90=band and band.p90, **common))
            if v_prov is not None:
                out.note("capacity_pct", ASSUMPTION_LABELS["capacity_pct"], capacity_text(value // 100), v_prov)
            ids = set(target.ids) if set(target.kinds) & {"chokepoint", "port"} else set()
            if ids:
                effects = lane_effects(problem, ids)
                labels = {r.code: r.label for r in (*matched, *_direct_targets(tpl, matched, idx))}
                names = ", ".join(labels.get(i, i) for i in sorted(ids))
                if effects:
                    out.note("affected_lanes", "Inbound lanes affected", _lane_effects_text(effects), Provenance.FACT)
                elif problem is not None and problem.inbound_lanes:
                    out.note("affected_lanes", "Inbound lanes affected",
                             f"No inbound lane in the {vendor} data passes {names}",
                             Provenance.FACT)
                else:
                    out.note("affected_lanes", "Inbound lanes affected",
                             f"No inbound lane catalogue in the {vendor} data, so the lanes through "
                             f"{names} cannot be traced", Provenance.INFERENCE)
        else:  # demand_scale
            factor = max(0, factor_bp_from_pct_change(value))
            out.perturbations.append(DemandScale(
                op="demand_scale", factor_bp=factor,
                p10=None if band is None else max(0, factor_bp_from_pct_change(band.p10)),
                p90=None if band is None else max(0, factor_bp_from_pct_change(band.p90)), **common))
            out.note("demand_change_pct", ASSUMPTION_LABELS["demand_change_pct"], pct_text(value),
                     v_prov or Provenance.DECISION)
    return out.perturbations, list(out.assumptions.values()), _dedupe(extra_refs)


async def _walk_receipts(
    caller: ToolCaller, matched: Sequence[EntityRef], po_lines: Sequence[Mapping[str, Any]],
    snapshot_data: ProblemData | None, problem: ProblemData | None = None,
) -> list[str]:
    receipt_ids: set[str] = set()
    for ref in matched:
        if ref.type in ("supplier", "component", "factory") and snapshot_data is not None:
            # deterministic BOM walk over the snapshot the plan is built from (no live read)
            receipt_ids |= receipts_for_entity(snapshot_data, ref.type, ref.code)
        elif ref.type == "component" and problem is not None and problem.bom:
            receipt_ids |= receipts_for_entity(problem, "component", ref.code)  # the gathered BOM (advisory)
        elif ref.type == "supplier":
            bom = await caller.call("get_bom_where_used", {"supplier_code": ref.code})
            for line in bom.get("open_po_lines") or []:
                receipt_ids.add(f"{line['po_code']}#{line['line_no']}")
        elif ref.type == "factory":
            receipt_ids |= {f"{ln['po_code']}#{ln['line_no']}" for ln in po_lines if ln.get("factory_code") == ref.code}
        elif ref.type == "po":
            receipt_ids |= {f"{ln['po_code']}#{ln['line_no']}" for ln in po_lines if ln["po_code"] == ref.code}
    open_refs = {f"{ln['po_code']}#{ln['line_no']}" for ln in po_lines}
    return sorted(receipt_ids & open_refs)


async def build_scenario(
    run: RunContext,
    caller: ToolCaller,
    *,
    extract: DisruptionExtract,
    overrides: Mapping[str, Any],
    po_lines: Sequence[Mapping[str, Any]],
    all_clusters: Sequence[str],
    scenario_id: str,
    snapshot_data: ProblemData | None = None,
    data_source: Assumption | None = None,
    problem: ProblemData | None = None,
    taxonomy: Taxonomy | None = None,
    geo: GeoCatalog | None = None,
    directory: SnapshotDirectory | None = None,
    gazetteer: Gazetteer | None = None,
    synonyms: Synonyms | None = None,
) -> DisruptionScenario:
    """Resolve the mentions and compile the event type's template into a scenario. ``directory``
    (the snapshot's locations with their country and its component items) lets region mentions
    expand to the suppliers / factories / ports / DCs located there."""
    tax = taxonomy or get_taxonomy()
    geo = geo or get_geo()
    event_type = extract.event_type
    template = tax.template(event_type)
    data = problem if problem is not None else snapshot_data
    idx = place_index(data, geo, directory, gazetteer)
    readings: list[str] = []
    matched, unmatched = await resolve_mentions(caller, extract.entity_mentions, idx, geo, synonyms, readings)
    stated = Stated.of(extract, overrides, template)
    if stated.receipts and po_lines:
        # a user-chosen scope keeps only open PO lines (an unknown id would silently match nothing)
        open_refs = {f"{ln['po_code']}#{ln['line_no']}" for ln in po_lines}
        kept = tuple(r for r in stated.receipts if r in open_refs)
        stated = dataclasses.replace(stated, receipts=kept or None)
    needs_walk = any(t.resolve == "receipts" for t in template.perturbations) and not stated.receipts
    regions = [m for m in matched if m.type == "region"]
    if needs_walk and regions:
        # "the Bangladesh POs": the suppliers and factories located there are walked like named ones
        kinds = list(dict.fromkeys(k for t in template.perturbations if t.resolve == "receipts"
                                   for k in _region_kinds(t)))
        matched = _dedupe([*matched, *(r for reg in regions for r in region_members(reg.code, kinds, idx))])
    receipts = await _walk_receipts(caller, matched, po_lines, snapshot_data, data) if needs_walk else []
    perturbations, assumptions, extra = compile_template(
        template=template, stated=stated, matched=matched, receipts=receipts, idx=idx, problem=data,
        all_clusters=all_clusters, planning_date=as_of(run),
        prior_p50={"delay_days": run.spec.planning.default_delay_days}, vendor=run.spec.vendor_name,
    )
    target_receipts: list[str] = next((p.target.ids for p in perturbations if p.op == "lead_time_shift"
                            and p.target.kinds == ["receipt"]), [])
    for code in sorted({r.split("#", 1)[0] for r in target_receipts}):
        if not any(m.type == "po" and m.code == code for m in matched):
            matched.append(EntityRef(type="po", code=code, label=f"Purchase order {code}"))
    matched = _dedupe([*matched, *extra])
    if note := _empty_regions_note(template, regions, idx, run.spec.vendor_name):
        assumptions.append(note)
    if not template.perturbations:
        assumptions.append(Assumption(
            key="event_type", label="Event type",
            value_text="Not a disruption type I can simulate, so I will investigate it as a question",
            prov=Provenance.INFERENCE,
        ))
    if data_source is not None:
        assumptions.append(data_source)
    if readings:
        assumptions.append(Assumption(key="mention_readings", label="Read as", value_text="; ".join(readings),
                                      prov=Provenance.INFERENCE))
    if unmatched:
        assumptions.append(
            Assumption(
                key="unmatched_mentions",
                label=f"Not found in {run.spec.vendor_name}",
                value_text=", ".join(sorted(set(unmatched))),
                prov=Provenance.INFERENCE,
            )
        )
    mode = "what_if" if (extract.is_hypothetical or run.case_mode == "what_if") else "actual"
    return DisruptionScenario(
        id=scenario_id,
        tenant=run.tenant,
        mode=mode,
        event_family=event_family_of(event_type),
        event_type=event_type,
        description=extract.description,
        perturbations=perturbations,
        matched=matched,
        assumptions=assumptions,
        config_version=f"{run.spec.version}/{run.spec.policy.version}",
    )


def _empty_regions_note(template: EventTemplate, regions: Sequence[EntityRef], idx: PlaceIndex,
                        vendor: str) -> Assumption | None:
    """An honest note for a named region that holds nothing this event type acts on (inference)."""
    kinds = list(dict.fromkeys(k for t in template.perturbations for k in _region_kinds(t)))
    if not kinds:
        return None
    empty = [r.label for r in regions if not region_members(r.code, kinds, idx)]
    if not empty:
        return None
    what = " or ".join("DC" if k == "dc" else k for k in kinds)
    places = ", ".join(empty)
    if not any(country for k in kinds for _label, country in idx.located.get(k, {}).values()):
        text = f"The {vendor} data has no {what} locations, so I cannot tell which are in {places}"
    else:
        text = f"No {what} in the {vendor} data is located in {places}"
    return Assumption(key="region_scope", label="Region", prov=Provenance.INFERENCE, value_text=text)


def retarget_scenario(scenario: DisruptionScenario, problem: ProblemData, vendor: str) -> DisruptionScenario:
    """Re-resolve the walked receipt targets and the lane facts on a newer ProblemData (promote):
    receipts the walk found (``affected_receipts`` = ``fact``) are recomputed from the matched
    supplier / component / factory / PO entities; user-chosen receipts (``decision``) are kept."""
    if not problem.po_lines:
        return scenario
    receipts_prov = next((a.prov for a in scenario.assumptions if a.key == "affected_receipts"), None)
    perturbations = list(scenario.perturbations)
    assumptions = {a.key: a for a in scenario.assumptions}
    if receipts_prov == Provenance.FACT:
        refs: set[str] = set()
        open_refs = {pl.ref for pl in problem.po_lines}
        for m in scenario.matched:
            if m.type in ("supplier", "component", "factory"):
                refs |= receipts_for_entity(problem, m.type, m.code)
            elif m.type == "po":
                refs |= {pl.ref for pl in problem.po_lines if pl.po_code == m.code}
        refs &= open_refs
        if refs:
            perturbations = [
                p.model_copy(update={"target": Selector(kinds=["receipt"], ids=sorted(refs))})
                if p.op == "lead_time_shift" and p.target.kinds == ["receipt"] else p
                for p in perturbations
            ]
            assumptions["affected_receipts"] = Assumption(
                key="affected_receipts", label=ASSUMPTION_LABELS["affected_receipts"],
                value_text=receipts_text(sorted(refs)), prov=Provenance.FACT)
    ids = {i for p in perturbations if p.op == "capacity_scale" and set(p.target.kinds) & {"chokepoint", "port"}
           for i in p.target.ids}
    if ids and (effects := lane_effects(problem, ids)):
        assumptions["affected_lanes"] = Assumption(key="affected_lanes", label="Inbound lanes affected",
                                                   value_text=_lane_effects_text(effects), prov=Provenance.FACT)
    return DisruptionScenario.model_validate({
        **scenario.model_dump(mode="json"),
        "perturbations": [p.model_dump(mode="json", exclude_none=True) for p in perturbations],
        "assumptions": [a.model_dump(mode="json") for a in assumptions.values()],
    })


# ─────────────────────────────── problem data ───────────────────────────────


def bundle_ids(bundle: Any) -> list[str]:
    return list(getattr(bundle, "evidence_ids", []) or [])


def data_source_assumption(bundle: ProblemBundle, vendor: str) -> Assumption:
    """Where the numbers come from: a synced snapshot, a crawl (advisory, ``110`` §4), or live
    reads (advisory, ``80`` §4). The crawled value is the explicit advisory assumption of the plan."""
    if bundle.crawled:
        return Assumption(
            key="data_source", label="Data source",
            value_text=f"{vendor} data gathered by paging, not a consistent snapshot, so the options are advisory",
            prov=Provenance.FACT,
        )
    if bundle.source == "scn":
        when = f" as of {bundle.snapshot_as_of.isoformat()}" if bundle.snapshot_as_of else ""
        return Assumption(key="data_source", label="Data source", value_text=f"{vendor} snapshot{when}",
                          prov=Provenance.FACT)
    return Assumption(
        key="data_source", label="Data source",
        value_text=f"Live reads from {vendor} (no synced snapshot yet), so the figures are advisory",
        prov=Provenance.FACT,
    )


async def current_snapshot(run: RunContext, connector: str) -> SnapshotRef | None:
    """The connector's ready snapshot, or ``None`` (no store, none synced yet, or unreadable)."""
    scn = run.deps.scn
    if scn is None:
        return None
    try:
        return await scn.current_snapshot(tenant=run.tenant, connector=connector)
    except SNAPSHOT_ERRORS as exc:
        log.warning("current snapshot unavailable; using live reads (advisory)", connector=connector,
                    error=type(exc).__name__)
        return None


async def _snapshot_bundle(run: RunContext, connector: str, *, info: Any, planning_date: Any,
                           snapshot: SnapshotRef | None = None) -> ProblemBundle | None:
    snapshot = snapshot or await current_snapshot(run, connector)
    if snapshot is None or run.deps.scn is None:
        return None
    try:
        async with run.deps.scn.reader(run.tenant) as reader:
            return await build_problem_from_snapshot(
                run.tenant, reader, snapshot=snapshot, lanes=info.lanes, as_of=planning_date,
                horizon_weeks=run.spec.planning.horizon_weeks, planning=run.spec.planning,
                connector_config=getattr(info, "config", None) or {},
            )
    except BUILD_ERRORS as exc:
        log.warning("snapshot ProblemData failed; using live reads (advisory)", snapshot_id=snapshot.id,
                    error=type(exc).__name__, detail=str(exc)[:300])
        return None


async def _persist_problem(run: RunContext, bundle: ProblemBundle, *, connector: str, scenario_version: int,
                           planning_date: dt.date) -> str:
    repo = run.deps.repo
    problem_evidence_id = await repo.save_evidence(
        tenant=run.tenant,
        case_id=run.case_id,
        run_id=run.run_id,
        connector="keelson",
        tool="problem_data",
        args={"scenario_version": scenario_version, "as_of": planning_date.isoformat(), "source": bundle.source,
              "snapshot_id": bundle.snapshot_id, "snapshot_label": bundle.snapshot_label},
        result=bundle.data.model_dump(mode="json"),
        prov="fact",
    )
    if bundle.snapshot_id is not None:
        snapshot_evidence = await repo.save_evidence(
            tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector=connector, tool="scn_snapshot",
            args={"snapshot_id": bundle.snapshot_id},
            result={"snapshot_id": bundle.snapshot_id,
                    "as_of": bundle.snapshot_as_of.isoformat() if bundle.snapshot_as_of else None,
                    "counts": dict(bundle.counts), **(bundle.snapshot_label or {})},
            prov="fact",
        )
        bundle.evidence_ids.append(snapshot_evidence)
    return problem_evidence_id


SIGNAL_ASSUMPTION_KEY: Final = "signal_event"
"""Assumption key every scenario framed from a Signal Hub event carries (``signal_frame``)."""


def is_signal_scenario(scenario: DisruptionScenario | None) -> bool:
    """The scenario was framed from a Signal Hub event (alert / signal run), not from chat: the
    approach card waits for a person and the case is never auto-approved (origin ``signal``)."""
    return scenario is not None and any(a.key == SIGNAL_ASSUMPTION_KEY for a in scenario.assumptions)


async def frame_problem_bundle(run: RunContext) -> tuple[Any, dt.date, ProblemBundle | None, SnapshotDirectory | None]:
    """``(connector info, planning date, snapshot bundle | None, snapshot directory | None)`` for
    framing (the live-read fallback needs an MCP session, opened by the caller)."""
    connector = run.spec.connector
    info = await run.deps.connectors.get_connector(tenant=run.tenant, slug=connector)
    planning_date = as_of(run)
    bundle = await _snapshot_bundle(run, connector, info=info, planning_date=planning_date)
    directory = await load_snapshot_directory(run, bundle.snapshot_id) if bundle is not None else None
    return info, planning_date, bundle, directory


def _set_problem_state(ctx: Context, bundle: ProblemBundle, problem_evidence_id: str, extra_ids: Sequence[str]) -> None:
    ctx.state[PROBLEM_STATE_KEY] = problem_evidence_id
    ctx.state[EVIDENCE_STATE_KEY] = sorted(set(extra_ids) | set(bundle_ids(bundle)))
    ctx.state[SNAPSHOT_STATE_KEY] = bundle.snapshot_id
    ctx.state[DATA_SOURCE_STATE_KEY] = bundle.source
    ctx.state[SNAPSHOT_LABEL_STATE_KEY] = bundle.snapshot_label if bundle.source == "scn" else None


async def _pinned_ref(run: RunContext, connector: str, snapshot_id: str) -> SnapshotRef:
    """The plan's snapshot with its label (M7: a crawled snapshot stays advisory on every later
    run of the case); a bare reference when the store cannot describe it."""
    getter = getattr(run.deps.scn, "get_snapshot", None)
    if callable(getter):
        try:
            found = await getter(tenant=run.tenant, snapshot_id=snapshot_id)
        except SNAPSHOT_ERRORS as exc:
            log.warning("snapshot description unavailable", snapshot_id=snapshot_id, error=type(exc).__name__)
            found = None
        if found is not None and found.id == snapshot_id:
            return found
    return SnapshotRef(id=snapshot_id, connector=connector)


async def load_case_bundle(run: RunContext, *, snapshot_id: str | None = None) -> ProblemBundle:
    """ProblemData for a later run of the case: the given (pinned) snapshot, else the current one,
    else live MCP reads (advisory)."""
    connector = run.spec.connector
    info = await run.deps.connectors.get_connector(tenant=run.tenant, slug=connector)
    planning_date = as_of(run)
    ref = await _pinned_ref(run, connector, str(snapshot_id)) if snapshot_id else None
    bundle = await _snapshot_bundle(run, connector, info=info, planning_date=planning_date, snapshot=ref)
    if bundle is None and ref is not None:
        bundle = await _snapshot_bundle(run, connector, info=info, planning_date=planning_date)
    if bundle is not None:
        return bundle
    async with get_gateway().session(tenant=run.tenant, connector=connector, principal="reader") as session:
        caller = _RecordingCaller(session, run, connector)
        return await gather_problem_data(
            run.tenant, session, lanes=info.lanes, as_of=planning_date,
            horizon_weeks=run.spec.planning.horizon_weeks, record=caller.record,
        )


_TASK_REV_RE: Final = re.compile(r"^(?P<base>.+)-r(?P<rev>\d+)$")


async def case_findings(run: RunContext) -> list[dict[str, Any]]:
    """The case's latest **verified** findings for a later run (promote / confirm / follow-ups).

    1. The persisted findings state (``investigation_findings`` evidence, written by ``critic``
       after ``verify_numbers`` dropped unresolvable claims), in plan order.
    2. Else the repo's ``list_investigation_tasks`` rows, **latest revision per task only**
       (the rows hold every revision, oldest first; older revisions are stale).
    3. Else ``[]``. Findings only enrich the narrative: a failing read never fails the run."""
    repo = run.deps.repo
    latest = getattr(repo, "latest_evidence", None)
    try:
        row = await latest(tenant=run.tenant, case_id=run.case_id, tool=FINDINGS_TOOL) if latest else None
    except Exception as exc:
        log.warning("could not load the case's verified findings", error=type(exc).__name__)
        row = None
    result = (row or {}).get("result") or {}
    by_task = result.get("findings_by_task") if isinstance(result, Mapping) else None
    if isinstance(by_task, Mapping) and by_task:
        plan = result.get("plan") if isinstance(result.get("plan"), Mapping) else {}
        order = [str(t.get("id")) for t in (plan or {}).get("tasks") or [] if isinstance(t, Mapping)]
        ordered = [by_task[t] for t in order if t in by_task] or list(by_task.values())
        return [dict(f) for f in ordered if isinstance(f, Mapping)]
    lister = getattr(repo, "list_investigation_tasks", None)
    if lister is None:
        return []
    try:
        rows = await lister(tenant=run.tenant, case_id=run.case_id)
    except Exception as exc:  # findings only enrich the narrative: never fail the re-simulation
        log.warning("could not load the case's findings", error=type(exc).__name__)
        return []
    best: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for i, r in enumerate(rows or []):
        if not isinstance(r.get("findings"), Mapping):
            continue
        m = _TASK_REV_RE.match(str(r.get("task_id") or ""))
        base, rev = (m.group("base"), int(m.group("rev"))) if m else (str(r.get("task_id") or i), 0)
        held = best.get(base)
        if held is None or (rev, i) >= (held[0], held[1]):
            best[base] = (rev, i, dict(r["findings"]))
    return [f for _, _, f in sorted(best.values(), key=lambda x: x[1])]


async def load_problem_bundle(run: RunContext, evidence_id: str) -> tuple[ProblemData, dict[str, Any]]:
    """ProblemData plus its evidence args (``source``, ``snapshot_id``)."""
    rows = await run.deps.repo.get_evidence(tenant=run.tenant, ids=[evidence_id])
    if not rows:
        raise LookupError(f"problem data evidence {evidence_id} not found")
    return ProblemData.model_validate(rows[0]["result"]), dict(rows[0].get("args") or {})


async def load_problem(run: RunContext, evidence_id: str) -> ProblemData:
    rows = await run.deps.repo.get_evidence(tenant=run.tenant, ids=[evidence_id])
    if not rows:
        raise LookupError(f"problem data evidence {evidence_id} not found")
    return ProblemData.model_validate(rows[0]["result"])


# ─────────────────────────────── frame_case ───────────────────────────────


async def frame_case(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """Input ``{intake, edits?, removed_checks?, edit_errors?}``; output ``{scenario_version,
    problem_evidence_id, mode}`` (+ ``removed_checks`` when the user removed plan checks).

    M6: ``{signal: {event_id, alert_id}, edits?, …}`` (a ``signal`` run, or an **Edit** of a case
    framed from a Signal Hub event) is framed deterministically from the event
    (:func:`.signal_frame.frame_signal_case`); no intake, no LLM."""
    run = current_run()
    data = input_dict(node_input)
    if isinstance(data.get("signal"), Mapping):
        from .signal_frame import frame_signal_case  # noqa: PLC0415 - signal_frame imports this module

        async for event in frame_signal_case(ctx, run, data):
            yield event
        return
    intake = IntakeResult.model_validate(fill_nullable(IntakeResult, coerce_legacy_intake(data["intake"])))
    if intake.disruption is None:
        raise ValueError("frame_case needs an intake disruption extract")
    ctx.state[INTAKE_STATE_KEY] = intake.model_dump(mode="json")
    for problem in data.get("edit_errors") or []:
        log.info("approach edit refused", problem=str(problem)[:200])
    if data.get("edit_errors"):
        yield progress_event("Your edit did not fit the card, so I kept the previous values", prov="decision")
    overrides = overrides_from(data.get("edits"))
    connector = run.spec.connector
    info = await run.deps.connectors.get_connector(tenant=run.tenant, slug=connector)
    planning_date = as_of(run)
    bundle = await _snapshot_bundle(run, connector, info=info, planning_date=planning_date)
    directory = await load_snapshot_directory(run, bundle.snapshot_id) if bundle is not None else None
    async with get_gateway().session(tenant=run.tenant, connector=connector, principal="reader") as session:
        caller = _RecordingCaller(session, run, connector)
        if bundle is None:
            bundle = await gather_problem_data(
                run.tenant,
                session,
                lanes=info.lanes,
                as_of=planning_date,
                horizon_weeks=run.spec.planning.horizon_weeks,
                record=caller.record,
            )
        scenario = await build_scenario(
            run,
            caller,
            extract=intake.disruption,
            overrides=overrides,
            po_lines=bundle.po_lines,
            all_clusters=[c.code for c in bundle.data.clusters],
            scenario_id=f"scn-{run.run_id}-{ctx.run_id}",
            snapshot_data=bundle.data if bundle.source == "scn" else None,
            data_source=data_source_assumption(bundle, run.spec.vendor_name),
            problem=bundle.data,
            directory=directory,
        )
    repo = run.deps.repo
    version = await repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario)
    problem_evidence_id = await _persist_problem(run, bundle, connector=connector, scenario_version=version,
                                                 planning_date=planning_date)
    if scenario.mode != run.case_mode:
        await repo.set_case_mode(tenant=run.tenant, case_id=run.case_id, mode=scenario.mode)
        run.case_mode = scenario.mode
        yield case_event(mode=scenario.mode, status="open", title=run.case_title)
    _set_problem_state(ctx, bundle, problem_evidence_id, caller.evidence_ids)
    counts = bundle.counts
    origin = f"the {run.spec.vendor_name} snapshot" if bundle.source == "scn" else run.spec.vendor_name
    verb = "Loaded" if bundle.source == "scn" else "Read"
    yield progress_event(
        f"{verb} {counts['purchase_orders']} open purchase orders, {counts['inventory_positions']} inventory "
        f"positions and {counts['forecast_rows']} forecast rows from {origin}",
        evidence_refs=(caller.evidence_ids + bundle_ids(bundle))[:20],
    )
    out: dict[str, Any] = {"scenario_version": version, "problem_evidence_id": problem_evidence_id, "mode": scenario.mode}
    if data.get("removed_checks"):
        out["removed_checks"] = [str(c) for c in data["removed_checks"]]
    yield out


# ─────────────────────────────── exposure ───────────────────────────────


def impact_kpi_items(kpis: ImpactKpis, baseline: ImpactKpis | None, *, preliminary: bool) -> list[KpiItem]:
    def delta(key: str) -> int | None:
        return None if baseline is None else int(getattr(kpis, key)) - int(getattr(baseline, key))

    return [
        KpiItem(key="units_at_risk", label="Units at risk", value=kpis.units_at_risk, unit="units",
                delta=delta("units_at_risk"), prov=Provenance.SIMULATED, preliminary=preliminary),
        KpiItem(key="revenue_at_risk_cents", label="Revenue at risk", value=kpis.revenue_at_risk_cents,
                unit="eur_cents", delta=delta("revenue_at_risk_cents"), prov=Provenance.SIMULATED,
                preliminary=preliminary),
        KpiItem(key="margin_at_risk_cents", label="Margin at risk", value=kpis.margin_at_risk_cents,
                unit="eur_cents", delta=delta("margin_at_risk_cents"), prov=Provenance.SIMULATED,
                preliminary=preliminary),
        KpiItem(key="shortage_cluster_weeks", label="Short cluster-weeks", value=kpis.shortage_cluster_weeks,
                unit="count", delta=delta("shortage_cluster_weeks"), prov=Provenance.SIMULATED,
                preliminary=preliminary),
    ]


async def run_impact(
    run: RunContext, problem: ProblemData, scenario: DisruptionScenario, *, include_graph: bool = False
) -> ImpactResponse:
    return await get_optimizer().impact(
        ImpactRequest(data=problem, perturbations=list(scenario.perturbations), include_graph=include_graph)
    )


async def quick_exposure(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input)
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        raise LookupError("no scenario saved for the case")
    problem = await load_problem(run, data["problem_evidence_id"])
    impact = await run_impact(run, problem, record.scenario)
    yield await block_event(
        run,
        "kpi_strip",
        KpiStripProps(items=impact_kpi_items(impact.kpis, impact.baseline_kpis, preliminary=True)),
        title="Exposure (preliminary)",
        prov=Provenance.SIMULATED,
        engine=ENGINE_OPT,
        key=("exposure", str(data["problem_evidence_id"])),
        inputs_hash=impact.provenance.inputs_hash,
    )
    yield {**data, "preliminary_units_at_risk": impact.kpis.units_at_risk}


# ─────────────────────────────── approach card ───────────────────────────────


def approach_interrupt_id(case_id: str, version: int) -> str:
    return f"approach-{case_id}-{version}"


def editable_values(scenario: DisruptionScenario) -> list[EditableValue]:
    """The scenario values the user may change with **Edit** (keys = assumption keys): delay,
    duration, demand change and capacity (when the template gives them as numbers) and the
    affected receipts (when the scenario targets PO lines)."""
    out: list[EditableValue] = []
    keys = {a.key for a in scenario.assumptions}
    for key, kind in (("delay_days", "days"), ("duration_days", "days"), ("demand_change_pct", "pct"),
                      ("capacity_pct", "pct"), ("affected_receipts", "ids")):
        value = scenario_value(scenario, key)
        if value is None or key not in keys:
            continue
        if kind == "ids":
            out.append(EditableValue(key=key, label=ASSUMPTION_LABELS[key], kind="ids", value_ids=list(value)))  # type: ignore[arg-type]
        else:
            out.append(EditableValue(key=key, label=ASSUMPTION_LABELS[key], kind=kind, value_int=int(value)))  # type: ignore[arg-type]
    return out


def _plan_from(data: Mapping[str, Any]) -> InvestigationPlan | None:
    raw = data.get("plan")
    if not isinstance(raw, Mapping):
        return None
    try:
        return InvestigationPlan.model_validate(raw)
    except ValidationError:
        log.warning("approach_gate: ignoring a malformed investigation plan")
        return None


async def approach_gate(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """Shows the approach card. Input: ``quick_exposure``'s output (+ ``plan`` = the planner's
    :class:`InvestigationPlan`, M4). Auto-confirms only when every assumption is a fact or decision,
    the scenario has perturbations, few entities matched and the plan has at most two tasks."""
    run = current_run()
    data = input_dict(node_input)
    version = int(data["scenario_version"])
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        raise LookupError("no scenario saved for the case")
    scenario = record.scenario
    from_signal = is_signal_scenario(scenario)
    interrupt_id = approach_interrupt_id(run.case_id, version)
    plan = _plan_from(data)
    checks: list[str | ApproachCheck]
    agents: list[ApproachAgent] = []
    if plan is not None and plan.tasks:
        checks = [ApproachCheck(task_id=t.id, sentence=t.objective.strip() or t.id) for t in plan.tasks]
        for slug in dict.fromkeys(t.specialist for t in plan.tasks):
            agents.append(ApproachAgent(slug=slug, display_name=run.spec.display_name(slug)))
    else:
        weeks = week_range(iso_week_of(as_of(run)), run.spec.planning.horizon_weeks)
        vendor = run.spec.vendor_name
        checks = [
            f"Open purchase orders and lines in {vendor}",
            f"DC and store inventory (ATP) in {vendor}",
            f"Demand forecast {weeks[0]} to {weeks[-1]}",
            "Transfer lanes between your DCs",
        ]
        if ctx.state.get(DATA_SOURCE_STATE_KEY) == "scn":
            checks.append(f"Inbound lanes, production schedules and allocations in the {vendor} snapshot")
    props = ApproachCardProps(
        interrupt_id=interrupt_id,
        event=DisruptionEventSummary(
            family=scenario.event_family,
            type=scenario.event_type,
            description=scenario.description,
            source=EVENT_SOURCE_ALERT if from_signal else "chat",
        ),
        mode=scenario.mode,
        matched=MatchedEntities(count=len(scenario.matched), sample=list(scenario.matched[:8])),
        assumptions=list(scenario.assumptions),
        checks=checks,
        buttons=["confirm", "edit", "what_if", "cancel"],
        agents=agents,
        editable=editable_values(scenario),
    )
    ctx.state[APPROACH_CARD_STATE_KEY] = props.model_dump(mode="json")
    yield await block_event(
        run, "approach_card", props, title="How I will approach this", prov=Provenance.INFERENCE,
        engine=ENGINE_FRAME, key=("approach", version),
    )
    grounded = not scenario.unconfirmed_assumptions
    small_plan = plan is None or len(plan.tasks) <= run.spec.investigation.auto_confirm_max_tasks
    # M6: a case opened from an alert (or drafted by an alert rule) always waits at the card
    if (grounded and small_plan and len(scenario.matched) <= run.spec.policy.auto_confirm_max_matched
            and scenario.perturbations and not from_signal):
        yield progress_event(
            "Approach confirmed automatically: every input is a fact or your own statement", prov="decision"
        )
        yield {"decision": "confirm", "interrupt_id": interrupt_id, "auto": True}
        return
    yield RequestInput(
        interrupt_id=interrupt_id,
        message="Please confirm how I should frame this disruption before I investigate.",
        payload={
            "kind": "approach",
            "scenario_version": version,
            "quorum_spec": {
                "roles": list(run.spec.policy.approach_roles), "count": 1, "exclude_initiator": False
            },
            "plan_version": None,
            "change_set_hash": None,
            "response_schema": approach_response_schema(),
        },
        response_schema=APPROACH_RESUME_SCHEMA,
    )


def _typed_edit_errors(ctx: Context, edits: Any, removed: Sequence[str]) -> list[str]:
    """Typed edits / removed checks against the stored card (``approach_edit_violations``)."""
    if not isinstance(edits, list) and not removed:
        return []
    raw = ctx.state.get(APPROACH_CARD_STATE_KEY)
    if not isinstance(raw, Mapping):
        return ["the approach card this edit answers is not available"]
    try:
        card = ApproachCardProps.model_validate(raw)
        typed = [e if isinstance(e, ApproachEdit) else ApproachEdit.model_validate(e) for e in (edits or [])] \
            if isinstance(edits, list) else []
    except ValidationError as exc:
        return [f"invalid edit: {exc.errors()[0].get('msg', 'invalid')}"]
    return approach_edit_violations(card, typed, list(removed))


async def approach_router(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """CONFIRM→investigate | WHAT_IF→investigate (what-if) | EDIT→frame_case | CANCEL/DEFAULT→close_case."""
    run = current_run()
    data = input_dict(node_input)
    decision = str(data.get("decision") or "").lower()
    interrupt_id = str(data.get("interrupt_id") or "")
    if run.resume is not None and not data.get("auto") and run.resume.interrupt_id != interrupt_id:
        yield routed({"reason": "cancelled", "message": "That decision belongs to another card."}, "STALE")
        return
    repo = run.deps.repo
    if decision == "edit":
        intake = ctx.state.get(INTAKE_STATE_KEY)
        edits = run.resume.edits if run.resume is not None else None
        removed = list(run.resume.removed_checks) if run.resume is not None else []
        errors = _typed_edit_errors(ctx, edits, removed)
        out: dict[str, Any] = {"intake": intake, "edits": None if errors else _wire_edits(edits)}
        signal = ctx.state.get(SIGNAL_STATE_KEY)
        if isinstance(signal, Mapping) and signal.get("event_id"):
            out["signal"] = {"event_id": signal.get("event_id"), "alert_id": signal.get("alert_id")}
        if errors:
            out["edit_errors"] = errors
        elif removed:
            out["removed_checks"] = removed
        yield routed(out, "EDIT")
        return
    if decision == "cancel":
        yield routed({"reason": "cancelled"}, "CANCEL")
        return
    if decision not in ("confirm", "what_if"):
        yield routed({"reason": "cancelled", "message": "Unknown decision; nothing was changed."}, "UNKNOWN")
        return
    record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        raise LookupError("no scenario saved for the case")
    scenario = record.scenario
    updated = scenario
    if not data.get("auto"):
        # the user confirmed the card: its inferences / assumptions become decisions (re-validated)
        updated = confirm_scenario_keys(scenario, [a.key for a in scenario.unconfirmed_assumptions])
    mode = "what_if" if decision == "what_if" else scenario.mode
    if mode != updated.mode:
        updated = updated.model_copy(update={"mode": mode})
    version = record.version
    if updated != scenario:
        version = await repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=updated)
    if mode != run.case_mode:
        await repo.set_case_mode(tenant=run.tenant, case_id=run.case_id, mode=mode)
        run.case_mode = mode
        yield case_event(mode=mode, status="open", title=run.case_title)
    yield routed({"scenario_version": version, "mode": mode}, "WHAT_IF" if decision == "what_if" else "CONFIRM")


def _wire_edits(edits: Any) -> Any:
    if isinstance(edits, list):
        return [e.model_dump(mode="json", exclude_none=True) if isinstance(e, ApproachEdit) else dict(e) for e in edits]
    return edits


# ─────────────────────────── case actions (M4) ───────────────────────────


async def resimulate_payload(
    ctx: Context, run: RunContext, scenario_version: int, bundle: ProblemBundle, *, mode: str, **extra: Any
) -> dict[str, Any]:
    problem_evidence_id = await _persist_problem(run, bundle, connector=run.spec.connector,
                                                 scenario_version=scenario_version, planning_date=as_of(run))
    _set_problem_state(ctx, bundle, problem_evidence_id, [])
    findings = await case_findings(run)
    ctx.state[FINDINGS_STATE_KEY] = findings
    return {"scenario_version": scenario_version, "problem_evidence_id": problem_evidence_id, "mode": mode,
            "findings": findings, **extra}


async def promote_case(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """Case action ``promote`` (what-if → actual): marks the case and scenario ``actual``,
    supersedes open interrupts, rebuilds ProblemData on the **current** snapshot (re-resolving
    walked receipts and lane facts) and routes ``RESIMULATE`` → impact → simulate → … so Apply is
    enabled only on options computed for reality. ``DONE`` when the case has no scenario."""
    run = current_run()
    repo = run.deps.repo
    await repo.set_case_mode(tenant=run.tenant, case_id=run.case_id, mode="actual")
    await run.deps.interrupts.supersede_open(tenant=run.tenant, case_id=run.case_id, reason="case promoted")
    run.case_mode = "actual"
    yield case_event(mode="actual", status="open", title=run.case_title)
    record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        yield await say(run, "promoted", "This case now plans for real. Tell me about the disruption to plan for.")
        yield routed({"promoted": True}, ROUTE_DONE)
        return
    bundle = await load_case_bundle(run)
    scenario = retarget_scenario(record.scenario, bundle.data, run.spec.vendor_name)
    scenario = scenario.model_copy(update={"mode": "actual"})
    version = await repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario)
    source = f"the current {run.spec.vendor_name} snapshot" if bundle.source == "scn" else f"live {run.spec.vendor_name} data"
    yield progress_event(f"Promoted to an actual case; re-simulating the options on {source}", prov="decision")
    yield routed(await resimulate_payload(ctx, run, version, bundle, mode="actual", promoted=True), ROUTE_RESIMULATE)


async def confirm_assumptions(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """Case action ``confirm_assumptions`` (``{assumption_keys: [...]}``; empty = every pending one):
    the ``unconfirmed`` assumptions become ``decision`` (and the perturbations that carry them), then
    ``RESIMULATE`` → impact + simulate on the plan's snapshot (investigations are reused).
    ``DONE`` when nothing is pending."""
    run = current_run()
    data = input_dict(node_input) if node_input is not None else {}
    repo = run.deps.repo
    record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    pending = [] if record is None else [a.key for a in record.scenario.pending_confirmation]
    requested = [str(k) for k in data.get("assumption_keys") or []]
    keys = [k for k in requested if k in pending] if requested else pending
    if record is None or not keys:
        yield await say(run, "nothing_to_confirm", "There is nothing left to confirm on this case.")
        yield routed({"confirmed": []}, ROUTE_DONE)
        return
    scenario = confirm_scenario_keys(record.scenario, keys)
    version = await repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario)
    plan = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
    snapshot_id = ((plan or {}).get("kpis") or {}).get("snapshot_id")
    bundle = await load_case_bundle(run, snapshot_id=str(snapshot_id) if snapshot_id else None)
    labels = ", ".join(a.label.lower() for a in scenario.assumptions if a.key in keys)
    yield progress_event(f"Confirmed {labels}; re-running the impact and the options", prov="decision")
    yield routed(await resimulate_payload(ctx, run, version, bundle, mode=scenario.mode, confirmed=keys), ROUTE_RESIMULATE)
