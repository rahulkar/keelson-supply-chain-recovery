"""Versioned agent spec (per tenant): model aliases per role, specialist tool bindings,
budgets, policy thresholds and planning defaults.

The *topology* of the workflow stays in code (``runtime/workflow.py``); the spec only tunes it.
Rows live in the engine table ``agent_spec(tenant, version, spec jsonb)``; when no row exists
the built-in :func:`default_spec` is used (``version="default"``).

M6 (``100-m6-signals.md`` §6): the default ``external_intel`` specialist reads the Signal Hub
(connector :data:`SIGNAL_HUB_CONNECTOR`) only through the ``signal_hub`` quarantine wrappers.

M5 (``95-m5-remediation.md``): ``policy.rules`` (the tenant's CEL approval rules; ``None`` = the
default set built from the caps and role names), ``remediation.connectors`` (further W-tier write
connectors, routed by op ownership), the re-plan resync bound and the guide SOP excerpts.

M4 (``90-m4-investigation.md`` §2–§5): the ``sourcing`` / ``logistics`` / ``playbook`` specialists
(``SpecialistSpec.quarantined`` = untrusted sources read only through quarantine wrapper tools,
``description`` for the planner's catalogue, ``relies_on`` for the follow-up dirty set), the
``planner`` / ``critic`` / ``guard`` / ``extractor`` / ``followup`` aliases (the critic must differ
from the planner) and the ``investigation`` budgets (parallelism, task count, the shared REVISE
budget, verify threshold, quarantine limits).

M8 (``126-m8-co2-robustness.md`` §6): ``planning.emissions`` (CO₂e KPI), ``planning.robustness``
(p10 / p50 / p90 bands) and ``planning.scoring`` (weight profiles), see :mod:`.spec_m8`.

M3 (``80-m3-deterministic-core.md`` §4): ``planning.candidates`` (deterministic candidate plans,
default A–D), ``planning.enabled_levers`` / ``lever_params`` and the snapshot conversion priors
(``units_per_teu``, ``grams_per_unit``, ``alt_sourcing``).

M2 (``70-backoffice-m2.md`` §1): ``prompts`` (tenant prompt addenda, rendered as delimited data
by the instruction providers), ``SpecialistSpec.connector`` / ``display_name`` (a specialist may
read any registered connector) and ``answer_connector`` (the answer path's connector, e.g. a
Tier-A ``/mcp-lite``). ``connector`` stays the planning connector (problem data, change sets).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, get_args

from keelson_contracts.agents import CustomSpecialistSpec, custom_specialists_violations
from keelson_contracts.policy import PolicyRule
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .spec_m8 import DEFAULT_SCORING, EmissionsDefaults, RobustnessDefaults, ScoringConfig

__all__ = [
    "BUILTIN_SPECIALISTS",
    "CustomSpecialist",
    "KB_CONNECTOR",
    "Specialist",
    "wrapper_name",
    "RemediationDefaults",
    "ALL_LEVERS",
    "AgentSpec",
    "AltSourcingDefaults",
    "Budgets",
    "CandidateSpec",
    "DEFAULT_CANDIDATES",
    "DEFAULT_SPEC_VERSION",
    "INVENTORY_DEMAND_TOOLS",
    "InvestigationDefaults",
    "LOGISTICS_TOOLS",
    "QUARANTINE_TOOL_NAMES",
    "QuarantineSourceName",
    "QUARANTINE_TOOL_GROUPS",
    "SIGNAL_HUB_CONNECTOR",
    "SIGNAL_HUB_WRAPPER_TOOLS",
    "SOURCING_TOOLS",
    "SPECIALIST_DESCRIPTIONS",
    "SPECIALIST_RELIES_ON",
    "MAX_PROMPT_ADDENDUM_CHARS",
    "ModelAliases",
    "PlanningDefaults",
    "PolicyConfig",
    "PromptAddenda",
    "SpecialistSpec",
    "default_spec",
]

DEFAULT_SPEC_VERSION: Final = "default"

INVENTORY_DEMAND_TOOLS: Final = (
    "get_inventory",
    "get_demand_forecast",
    "list_allocations",
    "list_purchase_orders",
    "get_purchase_order",
    "list_locations",
    "get_bom_where_used",
)
"""M1 allowlist of the ``inventory_demand`` investigator (``50-engine-workflow.md``)."""

ANSWER_TOOLS: Final = (
    *INVENTORY_DEMAND_TOOLS,
    "list_stores",
    "list_transfer_orders",
    "get_entity",
    "search_entities",
)
"""Read tools the answer investigator may use (never ``untrusted_text`` tools)."""

SOURCING_TOOLS: Final = (
    "list_purchase_orders",
    "get_purchase_order",
    "get_bom_where_used",
    "get_factory_schedule",
    "search_entities",
    "get_entity",
)
"""M4 ``sourcing`` allowlist (``90`` §3): POs, BOM, factory schedule / production orders."""

LOGISTICS_TOOLS: Final = (
    "list_sailings",
    "list_shipments",
    "get_lane_options",
    "list_purchase_orders",
    "get_purchase_order",
)
"""M4 ``logistics`` allowlist (``90`` §3): sailings, lanes + options, shipments."""

QuarantineSourceName = Literal["supplier_messages", "events", "knowledge_base", "signal_hub"]
"""Untrusted sources a specialist may read *only* through its quarantine wrapper tool
(``runtime.quarantine``): never as raw text, never bound to an LLM that holds tools. M6
``signal_hub``: the Signal Hub MCP read tools (``100`` §5), one wrapper per tool."""

SIGNAL_HUB_CONNECTOR: Final = "signal-hub"
"""Connector slug of the Signal Hub (internal first-party connector, auth ``service_jwt``,
``100`` §6): the ``external_intel`` specialist reads it, signal runs fetch events from it."""

SIGNAL_HUB_WRAPPER_TOOLS: Final[tuple[str, ...]] = (
    "search_signals_structured",
    "get_event_structured",
    "hazards_near_node_structured",
    "assess_exposure_structured",
)
"""Wrappers of the Signal Hub MCP read tools ``search_signals`` / ``get_event`` /
``hazards_near_node`` / ``assess_exposure``: structured fields pass (validated, codes and numbers
only); titles, summaries and news text go through the quarantine (guard + extractor)."""

QUARANTINE_TOOL_NAMES: Final[dict[str, str]] = {
    "supplier_messages": "read_supplier_messages_structured",
    "events": "read_events_structured",
    "knowledge_base": "search_playbooks_structured",
    "signal_hub": "search_signals_structured",
}
"""Quarantine source → the (first) wrapper tool name the investigator sees."""

QUARANTINE_TOOL_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "signal_hub": SIGNAL_HUB_WRAPPER_TOOLS,
}
"""Sources that add several wrapper tools (every other source adds ``QUARANTINE_TOOL_NAMES[source]``)."""

SPECIALIST_DESCRIPTIONS: Final[dict[str, str]] = {
    "inventory_demand": "Stock and ATP at DCs and stores, the demand forecast, store allocations and open "
    "purchase-order lines arriving at the DCs.",
    "sourcing": "Purchase orders and their lines, which styles and factories use the affected components (BOM), "
    "factory production schedules and the suppliers' own messages.",
    "logistics": "Sailings, shipments, transport lanes and their alternatives (routes, transit times, "
    "remaining capacity) and recent logistics events.",
    "playbook": "The tenant's playbooks, policies and agreements in the knowledge base (continuity plans, "
    "expedite caps, approved alternative suppliers, transfer procedures).",
    "external_intel": "External hazard alerts from Signal Hub (storms, earthquakes, floods, port closures, strikes, "
    "news): the alert's event, its exposure of your sites and lanes, and other hazards near them.",
}
"""Default catalogue descriptions the planner reads (``SpecialistSpec.description`` overrides)."""

SPECIALIST_RELIES_ON: Final[dict[str, tuple[str, ...]]] = {
    "inventory_demand": ("delay_days", "duration_days", "demand_change_pct", "capacity_pct", "start_date",
                         "affected_receipts"),
    "sourcing": ("delay_days", "duration_days", "capacity_pct", "start_date", "affected_receipts"),
    "logistics": ("delay_days", "duration_days", "capacity_pct", "start_date", "affected_lanes"),
    "playbook": ("event_type",),
    "external_intel": ("event_type", "duration_days", "start_date", "capacity_pct"),
}
"""Scenario assumption keys a specialist's findings depend on (recorded per task as
``investigation_task.input.assumption_keys``; the follow-up dirty set re-runs a task only when one
of them changes). ``SpecialistSpec.relies_on`` overrides; tenant specialists default to every key."""

AliasName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
ConnectorSlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]

MAX_PROMPT_ADDENDUM_CHARS: Final = 4000
"""Upper bound of one tenant prompt addendum (``70-backoffice-m2.md`` §1)."""

PromptAddendum = Annotated[str, Field(max_length=MAX_PROMPT_ADDENDUM_CHARS)]


class _SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelAliases(_SpecModel):
    """LiteLLM aliases per LLM role (never provider model ids)."""

    intake: AliasName = "fast-default"
    answer_investigator: AliasName = "fast-default"
    answer_structurer: AliasName = "fast-default"
    investigator: AliasName = "fast-default"
    structurer: AliasName = "fast-default"
    recommender: AliasName = "reasoning-default"
    # ── M4 (90 §2–§5) ──
    planner: AliasName = "reasoning-default"
    critic: AliasName = "critic-default"
    """Must differ from the planner's alias (an independent second look)."""
    guard: AliasName = "guard-default"
    """Prompt-injection screen of untrusted text (quarantine step 2)."""
    extractor: AliasName = "extract-fast"
    """Quarantined extractor (no tools; quarantine step 3)."""
    followup: AliasName = "fast-default"
    """Follow-up classifier (``followup_delta``)."""


class PromptAddenda(_SpecModel):
    """Tenant prompt addenda per LLM role (M2, ``70-backoffice-m2.md`` §1).

    Each addendum is *data*: the InstructionProvider appends it as a delimited "Tenant guidance"
    block after the fixed instruction (never templated). The numbers rule, the tool rules and the
    output schemas always win over it. ``investigator`` / ``structurer`` apply to every
    specialist's investigator / structurer; the answer structurer takes none. M4 adds the
    tool-free ``planner`` (``InvestigationPlan``) and ``critic`` (``Critique``) slots; the guard,
    the quarantined extractor and the follow-up classifier take none (they read untrusted or
    user text and must stay fixed)."""

    intake: PromptAddendum | None = None
    answer_investigator: PromptAddendum | None = None
    investigator: PromptAddendum | None = None
    structurer: PromptAddendum | None = None
    recommender: PromptAddendum | None = None
    planner: PromptAddendum | None = None
    critic: PromptAddendum | None = None


SpecialistSlug = Literal["inventory_demand", "sourcing", "logistics", "playbook", "external_intel"]
"""Built-in specialist roles (``AGENT_DISPLAY_NAMES``); M6 adds ``external_intel`` (Signal Hub)."""


class SpecialistSpec(_SpecModel):
    slug: SpecialistSlug
    tools: tuple[str, ...]
    """Tool names on :attr:`connector` (admin-approved ``read`` + ``trusted_data`` tools only)."""
    max_tool_calls: Annotated[int, Field(ge=1, le=15)] = 15
    """Plan §7: investigators get at most 15 read tools / calls."""
    connector: ConnectorSlug = "merxa"
    """Connector whose tools this specialist reads (M2: any registered connector)."""
    display_name: Annotated[str, Field(min_length=1, max_length=60)] | None = None
    """User-facing name (``SUBAGENT_STARTED.description``); ``None`` = the role's default."""
    quarantined: tuple[QuarantineSourceName, ...] = ()
    """M4: untrusted sources read through quarantine wrapper tools (``QUARANTINE_TOOL_NAMES``)."""
    description: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    """What the specialist checks, for the planner's catalogue (``None`` = built-in default)."""
    relies_on: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] | None = None
    """Scenario assumption keys its findings depend on (``None`` = ``SPECIALIST_RELIES_ON``)."""

    def assumption_keys(self, scenario_keys: Sequence[str]) -> list[str]:
        """Keys recorded for a task of this specialist (all of ``scenario_keys`` when unknown)."""
        relies = self.relies_on if self.relies_on is not None else SPECIALIST_RELIES_ON.get(self.slug)
        return list(scenario_keys) if relies is None else list(relies)

    @field_validator("quarantined")
    @classmethod
    def _unique_sources(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("quarantined sources must be unique")
        return v

    @property
    def quarantine_tools(self) -> tuple[str, ...]:
        """Wrapper tool names of :attr:`quarantined` (never MCP tool names)."""
        return tuple(name for s in self.quarantined for name in QUARANTINE_TOOL_GROUPS.get(s, (QUARANTINE_TOOL_NAMES[s],)))

    @property
    def allowlist(self) -> tuple[str, ...]:
        """Everything this specialist's investigator may call: its read tools + its wrappers."""
        return (*self.tools, *self.quarantine_tools)

    @property
    def catalogue_description(self) -> str:
        return self.description or SPECIALIST_DESCRIPTIONS[self.slug]

    # ── M7: the interface shared with :class:`CustomSpecialist` ──
    @property
    def custom(self) -> bool:
        """Built-ins are never custom (tenant specialists live in ``custom_specialists``)."""
        return False

    @property
    def connectors(self) -> tuple[str, ...]:
        """Connectors the investigator reads (built-ins: exactly one)."""
        return (self.connector,)

    @property
    def model_alias(self) -> str | None:
        """Investigator alias override (built-ins use ``models.investigator``)."""
        return None

    @property
    def role_prompt(self) -> str | None:
        return None

    @property
    def max_turns(self) -> int | None:
        """Model-turn cap override (built-ins use ``investigation.max_investigator_turns``)."""
        return None


BUILTIN_SPECIALISTS: Final = frozenset(get_args(SpecialistSlug))

KB_CONNECTOR: Final = "keelson-kb"
"""Pseudo connector of knowledge-base reads (evidence rows, custom specialists' KB wrapper)."""

WRAPPER_SUFFIX: Final = "_structured"


def wrapper_name(tool: str) -> str:
    """Name of the quarantine wrapper a custom specialist sees for an ``untrusted_text`` tool."""
    return f"{tool}{WRAPPER_SUFFIX}"


@dataclass(frozen=True)
class CustomSpecialist:
    """M7 (``110`` §3): runtime view of one enabled tenant specialist
    (:class:`~keelson_contracts.agents.CustomSpecialistSpec`), with the same interface the
    workflow, planner, plugins and translator use for the built-ins.

    Its tools may span up to three connectors. Which of them are ``untrusted_text`` is only known
    from the approved tool snapshot at run time, so the allowlist names every tool **and** its
    quarantine wrapper (:func:`wrapper_name`); the toolset binds exactly one of the two per tool
    (the raw tool when it is approved trusted data, the wrapper otherwise) and the policy plugin
    refuses raw untrusted tools whatever the allowlist says."""

    spec: CustomSpecialistSpec

    custom: bool = True

    @property
    def slug(self) -> str:
        return self.spec.slug

    @property
    def display_name(self) -> str:
        return self.spec.display_name

    @property
    def description(self) -> str:
        return self.spec.description

    @property
    def catalogue_description(self) -> str:
        return self.spec.description

    @property
    def connectors(self) -> tuple[str, ...]:
        out = list(self.spec.connectors)
        if self.spec.kb_ids:
            out.append(KB_CONNECTOR)
        return tuple(out)

    @property
    def connector(self) -> str:
        """Primary connector (activity labels): the first one it reads."""
        return self.connectors[0] if self.connectors else KB_CONNECTOR

    def tools_on(self, connector: str) -> tuple[str, ...]:
        return tuple(t.tool for t in self.spec.tools if t.connector == connector)

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t.tool for t in self.spec.tools))

    @property
    def quarantined(self) -> tuple[str, ...]:
        return ()

    @property
    def quarantine_tools(self) -> tuple[str, ...]:
        names = [wrapper_name(t) for t in self.tools]
        if self.spec.kb_ids:
            names.append(QUARANTINE_TOOL_NAMES["knowledge_base"])
        return tuple(names)

    @property
    def allowlist(self) -> tuple[str, ...]:
        return (*self.tools, *self.quarantine_tools)

    @property
    def max_tool_calls(self) -> int:
        return self.spec.max_tool_calls

    @property
    def max_turns(self) -> int:
        return self.spec.max_turns

    @property
    def model_alias(self) -> str:
        return self.spec.model_alias

    @property
    def role_prompt(self) -> str:
        return self.spec.role_prompt

    @property
    def kb_ids(self) -> tuple[str, ...]:
        return tuple(self.spec.kb_ids)

    @property
    def relies_on(self) -> None:
        return None

    def assumption_keys(self, scenario_keys: Sequence[str]) -> list[str]:
        """Tenant specialists depend on every scenario key (``SPECIALIST_RELIES_ON`` note)."""
        return list(scenario_keys)


Specialist = SpecialistSpec | CustomSpecialist
"""A built-in or an enabled custom specialist (:attr:`AgentSpec.all_specialists`)."""


class InvestigationDefaults(_SpecModel):
    """Planning budgets of the M4 investigation (``90`` §2–§5)."""

    max_parallel: Annotated[int, Field(ge=1, le=4)] = 4
    """Upper bound of concurrent specialist tasks (the plan may ask for fewer)."""
    max_tasks: Annotated[int, Field(ge=1, le=6)] = 6
    revise_budget: Annotated[int, Field(ge=0, le=1)] = 1
    """REVISE rounds per case run, shared by ``verify_numbers`` and ``critic``. The critic spends it
    only for a critique with a ``high`` severity issue (token diet: a medium/low remark does not buy
    a second specialist round)."""
    max_investigator_turns: Annotated[int, Field(ge=2, le=20)] = 6
    """Model turns one investigator task may take (token diet): the last allowed turn runs with
    tool calling switched off, so the analyst writes its notes from the results it has. Each turn
    re-sends the whole conversation, so the late turns are the most expensive ones."""
    verify_max_failed_bp: Annotated[int, Field(ge=0, le=10_000)] = 2000
    """``verify_numbers`` asks for a REVISE when more than this share (basis points) of the
    numeric claims cannot be resolved (contract: 20%)."""
    auto_confirm_max_tasks: Annotated[int, Field(ge=0, le=6)] = 2
    """The approach card may be skipped only for plans with at most this many tasks."""
    quarantine_max_items: Annotated[int, Field(ge=1, le=20)] = 8
    """Untrusted texts one wrapper call screens and extracts (newest first)."""
    kb_top_k: Annotated[int, Field(ge=1, le=12)] = 6
    kb_ids: tuple[str, ...] | None = None
    """Knowledge bases the playbook analyst searches (``None`` = all of the tenant's)."""


class Budgets(_SpecModel):
    max_llm_calls: Annotated[int, Field(ge=1)] = 90
    """M4: planner + up to 6 parallel specialists (investigator turns, structurer, quarantine
    guard/extractor calls) + critic + one REVISE round + strategist (platform max 120)."""
    max_total_tokens: Annotated[int, Field(ge=1000)] = 1_200_000


class PolicyConfig(_SpecModel):
    """Approval policy (``95`` §1). ``rules`` = the tenant's CEL rule set; ``None`` = the pack
    default (:func:`keelson_engine.runtime.policy.default_rules`, built from the caps and role
    names below). Evaluated by ``runtime/policy.py`` (cel-expr-python)."""

    version: str = "p-1"
    auto_rule_id: str = "auto-small-transfer"
    auto_max_lines: Annotated[int, Field(ge=0)] = 10
    auto_max_units: Annotated[int, Field(ge=0)] = 500
    approver_role: str = "approver"
    approach_roles: tuple[str, ...] = ("planner", "approver", "tenant-admin")
    """Who may answer an approach card (any one of them)."""
    approval_quorum: Annotated[int, Field(ge=1)] = 1
    auto_confirm_max_matched: Annotated[int, Field(ge=0)] = 50
    # ── M5 (95 §1) ──
    rules: tuple[PolicyRule, ...] | None = None
    """Explicit rule set (``GET/PUT /api/admin/policies``); ``None`` = the default rules."""
    compliance_role: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")] = "compliance"
    """Second role of the default four-eyes rule."""
    auto_initiator_role: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")] = "planner"
    """Role the initiator must hold for the default auto rule."""
    four_eyes_cost_cents: Annotated[int, Field(ge=0)] = 1_000_000
    """Default four-eyes rule: incremental cost above this (EUR cents) needs two people."""
    four_eyes_ops: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")], ...] = (
        "po.create", "po_line.cancel", "production_order.resequence",
    )
    """Default four-eyes rule: any of these ops needs two people."""

    @field_validator("rules")
    @classmethod
    def _valid_rules(cls, v: tuple[PolicyRule, ...] | None) -> tuple[PolicyRule, ...] | None:
        if v is None:
            return v
        if not v:
            raise ValueError("rules must not be empty (omit them for the default rule set)")
        ids = [r.id for r in v]
        if len(set(ids)) != len(ids):
            raise ValueError("policy rule ids must be unique")
        priorities = [r.priority for r in v]
        if len(set(priorities)) != len(priorities):
            raise ValueError("policy rule priorities must be unique")
        return v


class RemediationDefaults(_SpecModel):
    """M5 remediation (``95`` §2–§5)."""

    connectors: tuple[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")], ...] = ()
    """Further W-tier connectors a plan may write to (after the planning ``connector``, in
    precedence order): each action goes to the first connector that applies its op."""
    replan_resync_timeout_s: Annotated[int, Field(ge=0, le=120)] = 20
    """A signal re-plan waits at most this long for a fresh snapshot, else uses the current one."""
    guide_sop_snippets: bool = True
    """Generated guide steps quote the tenant's SOP snippets (knowledge base, screened)."""


LeverCodeName = Literal[
    "reallocate_inventory",
    "expedite_mode_shift",
    "resource_alt_supplier",
    "prioritize_allocation",
    "resequence_production",
]
ALL_LEVERS: Final[tuple[LeverCodeName, ...]] = (
    "reallocate_inventory",
    "expedite_mode_shift",
    "resource_alt_supplier",
    "prioritize_allocation",
    "resequence_production",
)
"""The v1 lever set (``80-m3-deterministic-core.md`` §3), in catalogue order."""


class CandidateSpec(_SpecModel):
    """One deterministic candidate plan ``simulate`` evaluates (``80`` §4)."""

    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]
    title: Annotated[str, Field(min_length=1, max_length=80)]
    levers: tuple[LeverCodeName, ...] = ()
    all_enabled: bool = False
    """``True`` = every enabled lever (the "Hybrid" candidate); ``levers`` is then ignored."""

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v == "do-nothing":
            raise ValueError("'do-nothing' is reserved for the baseline option")
        return v


DEFAULT_CANDIDATES: Final[tuple[CandidateSpec, ...]] = (
    CandidateSpec(id="opt-A", title="Rebalance between DCs", levers=("reallocate_inventory",)),
    CandidateSpec(
        id="opt-B", title="Rebalance + targeted sea-air", levers=("reallocate_inventory", "expedite_mode_shift")
    ),
    CandidateSpec(
        id="opt-C", title="Near-shore top-up + resequence", levers=("resource_alt_supplier", "resequence_production")
    ),
    CandidateSpec(id="opt-D", title="Hybrid", all_enabled=True),
)
"""Default candidates A–D (``80`` §4); "Do nothing" is always added by ``simulate``."""


class AltSourcingDefaults(_SpecModel):
    """Tenant priors for alternate sources derived from the snapshot (``scn/1.0`` carries no
    sourcing terms): a factory with a factory-specific BOM for a style is an approved source of
    that style; these values fill lead time, cost, MOQ and capacity."""

    production_lead_time_days: Annotated[int, Field(ge=0, le=180)] = 21
    unit_cost_premium_bp: Annotated[int, Field(ge=0, le=100_000)] = 2500
    """Unit-cost premium over the style's current cost (basis points)."""
    moq_units: Annotated[int, Field(ge=0)] = 300
    weekly_capacity_units: Annotated[int, Field(ge=0)] = 3000
    """Used when the factory has no production orders to derive its capacity from."""
    blocked_factories: tuple[str, ...] = ()
    """Factories that fail compliance (``compliance_ok = false``)."""


class PlanningDefaults(_SpecModel):
    default_delay_days: Annotated[int, Field(ge=1, le=120)] = 7
    """Tenant prior for a supply delay whose length the user did not state (``assumption``)."""
    horizon_weeks: Annotated[int, Field(ge=1, le=26)] = 9
    rebalance_max_units_total: Annotated[int, Field(ge=0)] = 20_000
    rebalance_allowed_lanes: tuple[str, ...] | None = None
    """``None`` = every tenant lane."""
    solver_time_limit_s: Annotated[int, Field(ge=1, le=120)] = 5
    solver_seed: Annotated[int, Field(ge=0)] = 7
    as_of: dt.date | None = None
    """Fixed planning date (demo determinism); ``None`` = today."""
    # ── M3 (80-m3-deterministic-core.md §4) ──
    enabled_levers: tuple[LeverCodeName, ...] = ALL_LEVERS
    """Levers the tenant allows; candidates only use these."""
    candidates: tuple[CandidateSpec, ...] = DEFAULT_CANDIDATES
    lever_params: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {"prioritize_allocation": {"max_changes": 12}}
    )
    """Per-lever ``params`` (validated against the lever's params model). ``reallocate_inventory``
    defaults to ``rebalance_max_units_total`` / ``rebalance_allowed_lanes``."""
    units_per_teu: Annotated[int, Field(ge=1)] = 4000
    """Converts ``per_teu`` rate cards and TEU capacities to units."""
    grams_per_unit: Annotated[int, Field(ge=1)] = 800
    """Converts ``per_kg`` rate cards to a per-unit cost."""
    alt_sourcing: AltSourcingDefaults = Field(default_factory=AltSourcingDefaults)
    max_change_set_items: Annotated[int, Field(ge=1, le=10_000)] = 500
    """Largest change set the connector accepts (Merxa: 500 items)."""
    # ── M8 (126-m8-co2-robustness.md §6) ──
    emissions: EmissionsDefaults = Field(default_factory=EmissionsDefaults)
    robustness: RobustnessDefaults = Field(default_factory=RobustnessDefaults)
    scoring: ScoringConfig = DEFAULT_SCORING
    """Weight profiles; the default one sets the LP weights and ranks the options."""

    @field_validator("enabled_levers")
    @classmethod
    def _unique_levers(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("enabled_levers must be unique")
        return v

    @field_validator("candidates")
    @classmethod
    def _unique_candidates(cls, v: tuple[CandidateSpec, ...]) -> tuple[CandidateSpec, ...]:
        ids = [c.id for c in v]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids must be unique")
        return v

    @field_validator("lever_params")
    @classmethod
    def _valid_params(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        from keelson_contracts.optimizer import LEVERS  # noqa: PLC0415

        for code, params in v.items():
            lever = LEVERS.get(code)
            if lever is None:
                raise ValueError(f"lever_params: unknown lever {code!r}")
            lever.params_model.model_validate(params)
        return v


class AgentSpec(_SpecModel):
    tenant: str
    version: str = DEFAULT_SPEC_VERSION
    connector: str = "merxa"
    vendor_name: str = "Merxa"
    models: ModelAliases = Field(default_factory=ModelAliases)
    specialists: tuple[SpecialistSpec, ...] = (
        SpecialistSpec(slug="inventory_demand", tools=INVENTORY_DEMAND_TOOLS),
        SpecialistSpec(slug="sourcing", tools=SOURCING_TOOLS, quarantined=("supplier_messages",)),
        SpecialistSpec(slug="logistics", tools=LOGISTICS_TOOLS, quarantined=("events",)),
        SpecialistSpec(slug="playbook", tools=(), quarantined=("knowledge_base",)),
        SpecialistSpec(slug="external_intel", tools=(), quarantined=("signal_hub",), connector=SIGNAL_HUB_CONNECTOR),
    )
    investigation: InvestigationDefaults = Field(default_factory=InvestigationDefaults)
    answer_tools: tuple[str, ...] = ANSWER_TOOLS
    answer_connector: ConnectorSlug = "merxa"
    """Connector whose tools the answer investigator reads (e.g. a Tier-A ``/mcp-lite``)."""
    prompts: PromptAddenda = Field(default_factory=PromptAddenda)
    budgets: Budgets = Field(default_factory=Budgets)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    planning: PlanningDefaults = Field(default_factory=PlanningDefaults)
    history_messages: Annotated[int, Field(ge=0, le=50)] = 8
    remediation: RemediationDefaults = Field(default_factory=RemediationDefaults)
    # ── M7 (110 §3) ──
    custom_specialists: tuple[CustomSpecialistSpec, ...] = ()
    """Tenant-defined specialists (back-office builder). Enabled ones join the planner's catalogue
    and run the same investigator → structurer sub-workflow as the built-ins
    (:attr:`all_specialists`)."""

    def write_connectors(self) -> tuple[str, ...]:
        """Connectors a plan may write to: the planning connector first, then ``remediation.connectors``."""
        return tuple(dict.fromkeys((self.connector, *self.remediation.connectors)))

    @field_validator("specialists")
    @classmethod
    def _unique_specialists(cls, v: tuple[SpecialistSpec, ...]) -> tuple[SpecialistSpec, ...]:
        slugs = [s.slug for s in v]
        if len(set(slugs)) != len(slugs):
            raise ValueError("specialist slugs must be unique")
        if not v:
            raise ValueError("at least one specialist is required")
        return v

    @field_validator("custom_specialists")
    @classmethod
    def _custom_list(cls, v: tuple[CustomSpecialistSpec, ...]) -> tuple[CustomSpecialistSpec, ...]:
        problems = custom_specialists_violations(v)
        if problems:
            raise ValueError("; ".join(problems))
        return v

    @model_validator(mode="after")
    def _independent_critic(self) -> AgentSpec:
        if self.models.critic == self.models.planner:
            raise ValueError("models.critic must use a different alias than models.planner")
        return self

    @model_validator(mode="after")
    def _custom_slugs_free(self) -> AgentSpec:
        # the engine names agents ``<slug>_investigator``: a clash with a built-in would make
        # specialist_of() ambiguous (custom slugs already avoid every reserved role name)
        builtin = {s.slug for s in self.specialists}
        if clash := sorted(builtin & {c.slug for c in self.custom_specialists}):
            raise ValueError(f"custom specialist slugs {clash} are built-in specialist slugs")
        return self

    @property
    def all_specialists(self) -> tuple[Specialist, ...]:
        """Built-ins, then the **enabled** custom specialists (spec order)."""
        return (*self.specialists, *(CustomSpecialist(c) for c in self.custom_specialists if c.enabled))

    def catalogue(self) -> dict[str, tuple[str, ...]]:
        """Specialist slug → everything its investigator may call (``plan_violations`` input)."""
        return {s.slug: s.allowlist for s in self.all_specialists}

    def planner_catalogue(self) -> list[dict[str, Any]]:
        """What the planner sees per specialist: name, display name, description, tools. A custom
        specialist's description is admin-authored data (single line, ≤ 300 chars, validated)."""
        return [
            {"name": s.slug, "display_name": self.display_name(s.slug), "description": s.catalogue_description,
             "tools": list(s.allowlist), **({"custom": True} if s.custom else {})}
            for s in self.all_specialists
        ]

    def display_name(self, slug: str) -> str:
        """User-facing name of an LLM role (a specialist's ``display_name`` overrides the default)."""
        from keelson_contracts.agui import AGENT_DISPLAY_NAMES  # noqa: PLC0415

        for s in self.all_specialists:
            if s.slug == slug and s.display_name:
                return s.display_name
        return AGENT_DISPLAY_NAMES.get(slug, slug.replace("_", " ").capitalize())

    def is_custom(self, slug: str) -> bool:
        return any(s.slug == slug and s.custom for s in self.all_specialists)

    def specialist(self, slug: str) -> Specialist:
        for s in self.all_specialists:
            if s.slug == slug:
                return s
        raise KeyError(slug)

    def connectors(self) -> tuple[str, ...]:
        """Every connector the LLM agents read from (planning connector first, then by use)."""
        slugs = [self.connector, self.answer_connector,
                 *(c for s in self.all_specialists for c in s.connectors if c != KB_CONNECTOR)]
        return tuple(dict.fromkeys(slugs))

    def connector_of(self, agent_name: str) -> str | None:
        """Primary connector whose tools the LLM agent ``agent_name`` (ADK agent name) is bound to."""
        if agent_name == "answer_investigator":
            return self.answer_connector
        s = self.specialist_of(agent_name)
        return s.connector if s is not None else None

    def connectors_of(self, agent_name: str) -> frozenset[str]:
        """Every connector the agent may read (a custom specialist reads up to three + the KB)."""
        if agent_name == "answer_investigator":
            return frozenset({self.answer_connector})
        s = self.specialist_of(agent_name)
        return frozenset(s.connectors) if s is not None else frozenset()

    def tool_allowlist(self, agent_name: str) -> frozenset[str]:
        """Tools an LLM agent (by ADK agent name) may call; empty for tool-free agents."""
        if agent_name == "answer_investigator":
            return frozenset((*self.answer_tools, "calc"))
        s = self.specialist_of(agent_name)
        return frozenset(s.allowlist) if s is not None else frozenset()

    def specialist_of(self, agent_name: str) -> Specialist | None:
        for s in self.all_specialists:
            if agent_name == f"{s.slug}_investigator":
                return s
        return None

    @classmethod
    def from_row(cls, tenant: str, version: str, data: dict[str, Any] | None) -> AgentSpec:
        payload = dict(data or {})
        payload["tenant"] = tenant
        payload["version"] = version
        return cls.model_validate(payload)


def default_spec(tenant: str, *, vendor_name: str = "Merxa") -> AgentSpec:
    return AgentSpec(tenant=tenant, vendor_name=vendor_name)
