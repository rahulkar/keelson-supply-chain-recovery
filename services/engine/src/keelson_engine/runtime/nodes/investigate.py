"""``planner`` → (approach card) → ``investigate`` (``docs/contracts/90-m4-investigation.md`` §2–§3).

* ``planner`` (LLM ``reasoning-default``, tool-free, ``InvestigationPlan``) sees the scenario (no
  untrusted text) and the specialist catalogue of the AgentSpec. The plan is checked with
  :func:`~keelson_contracts.llm.plan_violations` (trivial budget/length issues are repaired
  first); an unusable plan falls back to the deterministic :func:`default_plan`. A spec with a
  single specialist needs no LLM call. After an approach-card **Edit** the planner reuses the
  plan of the card minus the checks the user removed. The plan is stored as an ``inference``
  evidence row (``tool="investigation_plan"``) so later runs of the case can reuse it.
* ``investigate`` runs plan tasks in dependency waves (:func:`~keelson_contracts.llm.plan_waves`):
  each wave with ``asyncio.gather`` + ``ctx.run_node(wrapper, run_id="task-<id>-r<rev>",
  use_sub_branch=True)``, at most ``budget.max_parallel`` at once. Each task coroutine binds a
  :class:`~keelson_engine.runtime.context.TaskBinding` (tool subset, per-task call cap, evidence
  index). A failing specialist yields ``Findings(status="failed")`` (failure isolation); any other
  error cancels the wave's other tasks and propagates. Inputs: the approach router's
  ``{scenario_version, mode}``, a REVISE ``{revise_task_ids, notes}`` (re-runs only those tasks,
  ``rev+1``) or a follow-up ``{task_ids, plan?, notes?}``.

Specialist pattern (``50-engine-workflow.md``): a function wrapper yields the ephemeral start
marker, then runs the tool-using *investigator* and the tool-free *structurer* (``Findings``)
via ``ctx.run_node``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any, Final

import structlog
from google.adk.agents.context import Context
from google.adk.workflow import FunctionNode
from google.adk.workflow._errors import DynamicNodeFailError
from google.genai import types
from keelson_contracts.blocks import ApproachAgent, ApproachCheck
from keelson_contracts.common import Provenance
from keelson_contracts.llm import (
    MAX_OBJECTIVE_CHARS,
    MAX_PLAN_PARALLEL,
    MAX_PLAN_TASKS,
    Findings,
    InvestigationPlan,
    InvestigationTask,
    PlanBudget,
    plan_violations,
    plan_waves,
)
from keelson_contracts.scenario import DisruptionScenario
from pydantic import ValidationError

from ...public_errors import report_exception
from ..agents import KeelsonLlmAgent, fill_nullable
from ..compose import allowed_tokens_from, numeric_lint
from ..context import RunContext, TaskBinding, bind_task, current_run
from ..spec import AgentSpec, Specialist
from .common import input_dict, marker_event, progress_event

_log = structlog.get_logger(__name__)

__all__ = [
    "FINDINGS_BY_TASK_KEY",
    "FINDINGS_TOOL",
    "PLAN_EVIDENCE_KEY",
    "PLAN_STATE_KEY",
    "PLAN_TOOL",
    "REVISE_BUDGET_KEY",
    "TASK_REVS_KEY",
    "default_plan",
    "lint_objectives",
    "load_plan",
    "make_investigate",
    "make_planner",
    "make_specialist_wrapper",
    "normalise_plan",
    "plan_agents",
    "plan_allows_auto_confirm",
    "plan_checks",
    "restore_findings",
    "save_findings_state",
    "scenario_brief",
    "task_id_for",
    "task_row_id",
    "task_run_id",
    "user_request",
]

PLAN_STATE_KEY: Final = "investigation_plan"
"""``ctx.state``: the current plan (``InvestigationPlan`` JSON)."""
PLAN_EVIDENCE_KEY: Final = "plan_evidence_id"
FINDINGS_BY_TASK_KEY: Final = "findings_by_task"
"""``ctx.state``: plan task id → latest (verified) ``Findings`` JSON."""
TASK_REVS_KEY: Final = "task_revs"
"""``ctx.state``: plan task id → revision of its latest run (``rev+1`` per re-run)."""
REVISE_BUDGET_KEY: Final = "revise_budget"
"""``ctx.state``: REVISE rounds left in this run, shared by ``verify_numbers`` and ``critic``."""
PLAN_TOOL: Final = "investigation_plan"
FINDINGS_TOOL: Final = "investigation_findings"
"""Evidence ``tool`` of the persisted findings state (``save_findings_state``; follow-ups)."""

MAX_USER_REQUEST_CHARS: Final = 1000


def user_request(run: RunContext) -> str | None:
    """The user's own latest message in the case (the report or follow-up being investigated), for
    the planner and the analysts: sources the user asked to consult ("check our playbooks and the
    latest carrier rate updates") must survive the intake's one-line summary. Data, not
    instructions; ``None`` when the case history holds no user message. A chat run uses its own
    message; a resume run (after the approach card) the latest user message of the case. A
    ``signal`` run (M5 re-plan, M6 alert) has no user request: nobody typed anything."""
    if run.kind == "signal":
        return None
    if run.kind == "chat":
        text = " ".join(str((run.payload or {}).get("text") or "").split())
        if text:
            return text[:MAX_USER_REQUEST_CHARS]
    for message in reversed(run.history):
        text = " ".join(str(message.get("text") or "").split())
        if message.get("role") == "user" and text:
            return text[:MAX_USER_REQUEST_CHARS]
    return None


_OBJECTIVES: Final[Mapping[str, str]] = {
    "inventory_demand": "Check stock, demand and open receipts at the affected DCs for the weeks at risk",
    "sourcing": "Check which purchase orders, factories and supplier messages the disruption affects",
    "logistics": "Check sailings, shipments and alternative lanes on the affected routes",
    "playbook": "Check which playbooks, policies and agreements apply to this disruption",
    "external_intel": "Check the hazard alert, its exposure of your sites and lanes and other hazards near them "
    "in Signal Hub",
}
MAX_CUSTOM_OBJECTIVE_CHARS: Final = 160


def objective_for(specialist: Specialist) -> str:
    """The standard objective of a specialist's task (deterministic plans; numbers-rule repair).
    A custom specialist's is derived from its catalogue description (admin data, one line)."""
    known = _OBJECTIVES.get(specialist.slug)
    if known is not None:
        return known
    text = " ".join(str(specialist.catalogue_description).split()).rstrip(".")
    if len(text) > MAX_CUSTOM_OBJECTIVE_CHARS:
        text = text[: MAX_CUSTOM_OBJECTIVE_CHARS - 1].rstrip() + "…"
    return f"Check {text[:1].lower()}{text[1:]}" if text else "Check the affected data"


SIGNAL_EVENT_ASSUMPTION: Final = "signal_event"
"""``framing.SIGNAL_ASSUMPTION_KEY`` (a scenario framed from a Signal Hub event; no import cycle)."""

_FAMILY_SPECIALISTS: Final[Mapping[str, tuple[str, ...]]] = {
    "SUP": ("inventory_demand", "sourcing", "playbook"),
    "LOG": ("inventory_demand", "logistics", "playbook"),
    "NAT": ("inventory_demand", "sourcing", "logistics", "playbook", "external_intel"),
    "DEM": ("inventory_demand", "playbook"),
}
"""Deterministic fallback: specialists consulted per event family (others: inventory & demand +
playbook). Only specialists present in the tenant's spec are used."""
_RUN_ID_RE: Final = re.compile(r"[^A-Za-z0-9_-]+")


def task_id_for(slug: str, scenario_version: int) -> str:
    """M1 task id (kept for stored M1–M3 findings and tests)."""
    return f"task-{slug}-r{scenario_version}"


def task_run_id(task_id: str, rev: int) -> str:
    """The dynamic node ``run_id`` of one task run (never all digits)."""
    safe = _RUN_ID_RE.sub("-", task_id)[:40] or "task"
    return f"task-{safe}-r{rev}"


def task_row_id(case_id: str, task_id: str, rev: int) -> str:
    """``investigation_task.id`` (a global primary key): unique per case, task and revision."""
    return f"{case_id}:{task_id}:r{rev}"


MAX_BRIEF_MATCHED: Final = 24
"""Matched entities always listed in a brief; more are listed while they fit
``MAX_BRIEF_MATCHED_CHARS`` (the rest are counted)."""
MAX_BRIEF_MATCHED_CHARS: Final = 2_400
MAX_BRIEF_TARGET_IDS: Final = 12
"""A perturbation target with at most this many ids keeps them all."""
MAX_BRIEF_TARGET_CHARS: Final = 1_200
"""… and so does a larger target whose ids fit this many JSON characters (~600 tokens): only a
really long one (a 540-line receipt target is ~9.7k) is summarised, so an analyst still sees every
affected id of an ordinary target."""
MAX_BRIEF_GROUPS: Final = 20


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _target_brief(target: Mapping[str, Any]) -> dict[str, Any]:
    """A perturbation target for a brief: all ids when few or short, else their count and the ids
    grouped by the code before ``#`` ("PO-BD-0001#7" → ``{"PO-BD-0001": 90}``) — a 540-line receipt
    target costs a few hundred characters instead of thousands on every analyst turn. Ids that do
    not group (distinct codes) are listed while they fit the budget, with the count of the rest."""
    ids = [str(i) for i in target.get("ids") or []]
    out: dict[str, Any] = {"kinds": list(target.get("kinds") or [])}
    if len(ids) <= MAX_BRIEF_TARGET_IDS or _json_size(ids) <= MAX_BRIEF_TARGET_CHARS:
        out["ids"] = ids
        return out
    groups: dict[str, int] = {}
    for i in ids:
        head = i.split("#", 1)[0]
        groups[head] = groups.get(head, 0) + 1
    out["id_count"] = len(ids)
    if len(groups) < len(ids):
        top = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_BRIEF_GROUPS]
        out["ids_by_code"] = dict(sorted(top))
        if len(groups) > MAX_BRIEF_GROUPS:
            out["codes_more"] = len(groups) - MAX_BRIEF_GROUPS
    else:
        shown = _fit(ids, MAX_BRIEF_TARGET_CHARS, minimum=MAX_BRIEF_TARGET_IDS)
        out["ids_sample"] = shown
    return out


def _fit(items: Sequence[str], budget: int, *, minimum: int) -> list[str]:
    """The longest prefix of ``items`` (at least ``minimum``) whose JSON fits ``budget`` chars."""
    out: list[str] = []
    used = 2
    for item in items:
        used += _json_size(item) + 1
        if len(out) >= minimum and used > budget:
            break
        out.append(item)
    return out


def scenario_brief(scenario: DisruptionScenario, *, task_id: str, scenario_version: int) -> dict[str, Any]:
    """The scenario as analysts and the planner read it (token diet): matched entities as
    ``type:code label`` strings, perturbations with their target summarised (:func:`_target_brief`),
    assumptions as ``{label, value, prov}``. The full scenario stays in the database (numbers are
    verified against it, never against this brief). ``scenario_version`` is not sent to the model."""
    matched = [f"{m.type}:{m.code} {m.label}".strip() for m in scenario.matched]
    perturbations = []
    for p in scenario.perturbations:
        data = p.model_dump(mode="json", exclude_none=True)
        data["target"] = _target_brief(data.get("target") or {})
        perturbations.append(data)
    shown = _fit(matched, MAX_BRIEF_MATCHED_CHARS, minimum=MAX_BRIEF_MATCHED)
    brief: dict[str, Any] = {
        "task_id": task_id,
        "mode": scenario.mode,
        "event": {"family": scenario.event_family, "type": scenario.event_type},
        "description": scenario.description,
        "matched": shown,
        "perturbations": perturbations,
        "assumptions": [{"label": a.label, "value": a.value_text, "prov": a.prov.value} for a in scenario.assumptions],
    }
    if len(matched) > len(shown):
        brief["matched_more"] = len(matched) - len(shown)
    return brief


# ─────────────────────────────────── plans ───────────────────────────────────


def default_plan(spec: AgentSpec, scenario: DisruptionScenario | None, *, reason: str = "default") -> InvestigationPlan:
    """Deterministic plan: the specialists the event family needs (present in the spec), no deps."""
    family = scenario.event_family if scenario is not None else "OTHER"
    wanted = _FAMILY_SPECIALISTS.get(family, ("inventory_demand", "playbook"))
    if scenario is not None and any(a.key == SIGNAL_EVENT_ASSUMPTION for a in scenario.assumptions):
        wanted = (*wanted, "external_intel")  # M6: a case opened from an alert consults Signal Hub
    available = [s for s in spec.all_specialists if s.allowlist]
    chosen = [s for s in available if s.slug in wanted] or available[:1] or list(spec.all_specialists[:1])
    chosen = chosen[: spec.investigation.max_tasks]
    tasks = [
        InvestigationTask(id=f"t{i + 1}", specialist=s.slug, objective=objective_for(s), tools=[], depends_on=[])
        for i, s in enumerate(chosen)
    ]
    return InvestigationPlan(
        rationale=f"Standard checks for this kind of event ({reason}).",
        tasks=tasks,
        budget=PlanBudget(max_parallel=min(spec.investigation.max_parallel, max(1, len(tasks))), max_tasks=len(tasks)),
    )


def normalise_plan(plan: InvestigationPlan, spec: AgentSpec) -> tuple[InvestigationPlan, list[str]]:
    """Repair trivial issues (budget bounds, long objectives), then ``plan_violations``.

    Returns the repaired plan and its remaining problems (empty = executable)."""
    inv = spec.investigation
    tasks = [
        t.model_copy(update={
            "id": t.id.strip(),
            "objective": t.objective.strip() if len(t.objective.strip()) <= MAX_OBJECTIVE_CHARS
            else t.objective.strip()[: MAX_OBJECTIVE_CHARS - 1].rstrip() + "…",
            "tools": list(dict.fromkeys(t.tools)),
            "depends_on": list(dict.fromkeys(t.depends_on)),
        })
        for t in plan.tasks
    ]
    max_tasks = min(inv.max_tasks, MAX_PLAN_TASKS)
    budget = PlanBudget(
        max_parallel=max(1, min(plan.budget.max_parallel, inv.max_parallel, MAX_PLAN_PARALLEL)),
        max_tasks=max(1, min(max(plan.budget.max_tasks, len(tasks)), max_tasks)),
    )
    fixed = plan.model_copy(update={"tasks": tasks, "budget": budget})
    return fixed, plan_violations(fixed, spec.catalogue())


def _standard_objective(slug: str, spec: AgentSpec | None) -> str:
    if spec is not None:
        try:
            return objective_for(spec.specialist(slug))
        except KeyError:
            pass
    return _OBJECTIVES.get(slug, "Check the affected data")


def lint_objectives(plan: InvestigationPlan, scenario: DisruptionScenario,
                    spec: AgentSpec | None = None) -> InvestigationPlan:
    """Numbers rule for the check sentences the user reads: an objective with a free number (not
    an entity code of the scenario) is replaced by the specialist's standard objective."""
    allowed = allowed_tokens_from([scenario.model_dump(mode="json")])
    tasks = [
        t if not [v for v in numeric_lint(t.objective, allowed) if v.startswith("free number")]
        else t.model_copy(update={"objective": _standard_objective(t.specialist, spec)})
        for t in plan.tasks
    ]
    return plan.model_copy(update={"tasks": tasks})


def plan_checks(plan: InvestigationPlan) -> list[ApproachCheck]:
    """``approach_card@1`` checks: one plain sentence per task (from its objective)."""
    return [ApproachCheck(task_id=t.id, sentence=t.objective) for t in plan.tasks]


def plan_agents(plan: InvestigationPlan, spec: AgentSpec) -> list[ApproachAgent]:
    """The agents the plan consults (plan order, unique), with their display names."""
    slugs = list(dict.fromkeys(t.specialist for t in plan.tasks))
    return [ApproachAgent(slug=s, display_name=spec.display_name(s)) for s in slugs]


def plan_allows_auto_confirm(plan: InvestigationPlan, spec: AgentSpec) -> bool:
    """``90`` §2: the approach card may be skipped only for small plans (≤ 2 tasks by default);
    the caller also requires every assumption to be ``fact``/``decision``."""
    return len(plan.tasks) <= spec.investigation.auto_confirm_max_tasks


def _without_checks(plan: InvestigationPlan, removed: Sequence[str]) -> InvestigationPlan:
    gone = set(removed)
    tasks = [
        t.model_copy(update={"depends_on": [d for d in t.depends_on if d not in gone]})
        for t in plan.tasks if t.id not in gone
    ]
    if not tasks:
        return plan  # at least one check must remain (approach_edit_violations refuses this edit)
    budget = plan.budget.model_copy(update={"max_tasks": max(1, min(plan.budget.max_tasks, len(tasks)))})
    return plan.model_copy(update={"tasks": tasks, "budget": budget})


async def load_plan(ctx: Context, run: RunContext) -> InvestigationPlan | None:
    """The plan of this run (``ctx.state``) or the case's latest stored plan."""
    data = ctx.state.get(PLAN_STATE_KEY)
    if not data:
        row = await run.deps.repo.latest_evidence(tenant=run.tenant, case_id=run.case_id, tool=PLAN_TOOL)
        if row is None:
            return None
        data = (row.get("result") or {}).get("plan")
        ctx.state[PLAN_EVIDENCE_KEY] = row.get("id")
    try:
        plan = InvestigationPlan.model_validate(fill_nullable(InvestigationPlan, data))
    except ValidationError:
        return None
    ctx.state[PLAN_STATE_KEY] = plan.model_dump(mode="json")
    return plan


async def _store_plan(ctx: Context, run: RunContext, plan: InvestigationPlan, *, source: str, version: int | None) -> str:
    evidence_id = await run.deps.repo.save_evidence(
        tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector="keelson", tool=PLAN_TOOL,
        args={"scenario_version": version, "source": source},
        result={"plan": plan.model_dump(mode="json"), "source": source}, prov="inference",
    )
    ctx.state[PLAN_STATE_KEY] = plan.model_dump(mode="json")
    ctx.state[PLAN_EVIDENCE_KEY] = evidence_id
    return evidence_id


def _planner_brief(spec: AgentSpec, scenario: DisruptionScenario, version: int, request: str | None = None) -> str:
    """The planner's user turn (token diet): the compact scenario (:func:`scenario_brief`), the
    specialist catalogue and ``user_request``. The limits are in the planner's instruction already."""
    brief: dict[str, Any] = {
        "scenario": {k: v for k, v in scenario_brief(scenario, task_id="plan", scenario_version=version).items()
                     if k not in ("task_id",)},
        "specialists": spec.planner_catalogue(),
    }
    if request:
        brief["user_request"] = request
    return json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_planner(planner_agent: KeelsonLlmAgent) -> FunctionNode:
    async def planner(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        run = current_run()
        data = input_dict(node_input)
        record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
        if record is None:
            raise LookupError("no scenario saved for the case")
        version = int(data.get("scenario_version") or record.version)
        spec = run.spec
        previous = ctx.state.get(PLAN_STATE_KEY)
        resume = run.resume
        plan: InvestigationPlan | None = None
        source = "planner"
        if resume is not None and resume.decision == "edit" and previous:
            # Approach-card Edit: same investigation, minus the checks the user removed.
            removed = [str(r) for r in data.get("removed_checks") or resume.removed_checks or []]
            plan = _without_checks(InvestigationPlan.model_validate(previous), removed)
            source = "edited"
        elif len([s for s in spec.all_specialists if s.allowlist]) <= 1:
            plan = default_plan(spec, record.scenario, reason="single specialist")
            source = "single_specialist"
        else:
            try:
                raw = await ctx.run_node(
                    planner_agent, node_input=_planner_brief(spec, record.scenario, version, user_request(run)),
                    run_id="plan",
                )
                candidate = InvestigationPlan.model_validate(
                    fill_nullable(InvestigationPlan, raw if isinstance(raw, Mapping) else {})
                )
                plan, problems = normalise_plan(candidate, spec)
                plan = lint_objectives(plan, record.scenario, spec)
                if problems:
                    _log.warning("planner output unusable; using the default plan", problems=problems[:10])
                    plan, source = None, "default"
            except (DynamicNodeFailError, ValidationError) as exc:
                report_exception(_log, "planner failed; using the default plan", exc)
                plan, source = None, "default"
            if plan is None:
                plan = default_plan(spec, record.scenario, reason="planner fallback")
        plan_evidence_id = await _store_plan(ctx, run, plan, source=source, version=version)
        ctx.state.setdefault(REVISE_BUDGET_KEY, spec.investigation.revise_budget)
        names = ", ".join(a.display_name for a in plan_agents(plan, spec))
        yield progress_event(f"Planned {len(plan.tasks)} checks with {names}", prov="inference",
                             evidence_refs=[plan_evidence_id])
        yield {
            **data,
            "scenario_version": version,
            "plan_evidence_id": plan_evidence_id,
            "plan_source": source,
            "plan": plan.model_dump(mode="json"),
            "checks": [c.model_dump(mode="json") for c in plan_checks(plan)],
            "agents": [a.model_dump(mode="json") for a in plan_agents(plan, spec)],
            "plan_task_count": len(plan.tasks),
            "plan_auto_confirm": plan_allows_auto_confirm(plan, spec),
        }

    return FunctionNode(func=planner, name="planner", rerun_on_resume=True)


# ─────────────────────────────── specialists ─────────────────────────────────


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, types.Content):
        return "".join(p.text or "" for p in value.parts or [] if not p.thought)
    return "" if value is None else str(value)


def make_specialist_wrapper(
    spec: Specialist, investigator: KeelsonLlmAgent, structurer: KeelsonLlmAgent
) -> FunctionNode:
    async def specialist(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        yield marker_event(spec.slug)
        brief = input_dict(node_input)
        task_id = str(brief["task_id"])
        notes = await ctx.run_node(investigator, node_input=_json(brief), run_id="investigator")
        header = f"TASK {task_id}\n\n{_text(notes) or '(the analyst returned no notes)'}"
        raw = await ctx.run_node(structurer, node_input=header, run_id="structurer")
        findings = Findings.model_validate(fill_nullable(Findings, raw if isinstance(raw, Mapping) else {}))
        if findings.task_id != task_id:
            findings = findings.model_copy(update={"task_id": task_id})
        yield findings.model_dump(mode="json")

    return FunctionNode(func=specialist, name=spec.slug, rerun_on_resume=True)


def _assumption_keys(spec: AgentSpec, specialist: str, scenario: DisruptionScenario) -> list[str]:
    keys = [a.key for a in scenario.assumptions]
    try:
        return spec.specialist(specialist).assumption_keys(keys)
    except KeyError:
        return keys


def _plan_task_id(task_id: str, known: set[str]) -> str | None:
    """A plan task id from a follow-up's id (``t1`` or ``t1-r2`` — the ``<base>-r<rev>`` form of
    ``list_investigation_tasks``); ``None`` when it names no task of the plan."""
    if task_id in known:
        return task_id
    m = _REV_SUFFIX_RE.match(task_id)
    return m.group(1) if m and m.group(1) in known else None


_REV_SUFFIX_RE: Final = re.compile(r"^(.+)-r\d+$")


def _json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _failed(task: InvestigationTask) -> Findings:
    # The gap text reaches the Strategist (and maybe the narrative): no exception text, no id
    # (digits would trip the numeric linter); the log has both.
    return Findings(
        task_id=task.id, status="failed", claims=[], affected=[],
        gaps=[f"The {task.specialist.replace('_', ' ')} analysis could not be completed."], scenario_updates=[],
    )


def _upstream(task: InvestigationTask, findings: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for dep in task.depends_on:
        f = findings.get(dep)
        if not f:
            continue
        out.append({
            "task_id": dep,
            "status": f.get("status"),
            "claims": [c.get("text") for c in f.get("claims") or []][:8],
            "affected": [f"{a.get('type')}:{a.get('code')}" for a in f.get("affected") or []][:20],
        })
    return out


async def restore_findings(ctx: Context, run: RunContext) -> dict[str, dict[str, Any]]:
    """Findings of this run, else the case's latest persisted findings state (follow-ups)."""
    current = ctx.state.get(FINDINGS_BY_TASK_KEY)
    if current:
        return dict(current)
    row = await run.deps.repo.latest_evidence(tenant=run.tenant, case_id=run.case_id, tool=FINDINGS_TOOL)
    result = (row or {}).get("result") or {}
    restored = dict(result.get("findings_by_task") or {})
    ctx.state[FINDINGS_BY_TASK_KEY] = restored
    ctx.state[TASK_REVS_KEY] = dict(result.get("task_revs") or {})
    return restored


async def save_findings_state(ctx: Context, run: RunContext) -> str:
    """Persist plan + verified findings + revisions (``tool="investigation_findings"``) so a later
    run of the case (follow-ups) can re-run only dirty tasks and reuse the rest."""
    return await run.deps.repo.save_evidence(
        tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector="keelson", tool=FINDINGS_TOOL,
        args={"plan_evidence_id": ctx.state.get(PLAN_EVIDENCE_KEY)},
        result={
            "plan": ctx.state.get(PLAN_STATE_KEY),
            "findings_by_task": ctx.state.get(FINDINGS_BY_TASK_KEY) or {},
            "task_revs": ctx.state.get(TASK_REVS_KEY) or {},
        },
        prov="inference",
    )


async def _recorded_revs(run: RunContext, *, exclude_run: str | None = None) -> dict[str, int]:
    """Plan task id → highest revision recorded in ``investigation_task`` for the case (rows
    written by the run ``exclude_run`` left out)."""
    out: dict[str, int] = {}
    for record in await run.deps.repo.list_investigation_tasks(tenant=run.tenant, case_id=run.case_id):
        payload = record.get("input") or {}
        if exclude_run is not None and payload.get("run_id") == exclude_run:
            continue
        tid, rev = payload.get("task_id"), payload.get("rev")
        if isinstance(tid, str) and isinstance(rev, int) and not isinstance(rev, bool):
            out[tid] = max(out.get(tid, 0), rev)
    return out


async def _gather_isolated(coros: Sequence[Any]) -> list[Any]:
    """``asyncio.gather`` over the wave; on an unexpected error the other tasks are cancelled
    and drained before it propagates (a specialist *failure* never gets here)."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def make_investigate(wrappers: Mapping[str, FunctionNode]) -> FunctionNode:
    async def investigate(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        run = current_run()
        spec = run.spec
        data = input_dict(node_input)
        record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
        if record is None:
            raise LookupError("no scenario saved for the case")
        version = int(data.get("scenario_version") or record.version)
        if isinstance(data.get("plan"), Mapping):
            plan = InvestigationPlan.model_validate(fill_nullable(InvestigationPlan, data["plan"]))
            await _store_plan(ctx, run, plan, source="followup", version=version)
        else:
            plan = await load_plan(ctx, run)
            if plan is None:
                plan = default_plan(spec, record.scenario)
                await _store_plan(ctx, run, plan, source="default", version=version)
        ctx.state.setdefault(REVISE_BUDGET_KEY, spec.investigation.revise_budget)
        subset = data.get("revise_task_ids") or data.get("task_ids") or data.get("dirty_task_ids") or []
        findings = await restore_findings(ctx, run) if subset else dict(ctx.state.get(FINDINGS_BY_TASK_KEY) or {})
        fresh = not ctx.state.get(TASK_REVS_KEY)
        revs: dict[str, int] = {str(k): int(v) for k, v in (ctx.state.get(TASK_REVS_KEY) or {}).items()}
        if subset or fresh:
            # A re-run (REVISE / follow-up) or a later run of the case writes rev = the latest
            # *recorded* revision + 1 — the ids ``prepare_reinvestigate`` announces — even when an
            # earlier run stopped before its findings state was persisted, and a re-framed case
            # never overwrites the task rows (global ids) of its earlier investigation. Rows of this
            # run itself are left out: a crash-retry re-runs the same revisions (same child run
            # ids, idempotent task-row upserts) instead of starting new ones.
            for tid, rev in (await _recorded_revs(run, exclude_run=run.run_id)).items():
                revs[tid] = max(revs.get(tid, 0), rev)
        known = {t.id for t in plan.tasks}
        wanted = [tid for tid in (_plan_task_id(str(t), known) for t in subset) if tid is not None]
        selected = set(wanted) if wanted else set(known)
        raw_notes = data.get("notes")
        notes: Mapping[str, Any] = raw_notes if isinstance(raw_notes, Mapping) else {}
        by_id = {t.id: t for t in plan.tasks}
        parallel = max(1, min(plan.budget.max_parallel, spec.investigation.max_parallel))
        gate = asyncio.Semaphore(parallel)

        request = user_request(run)

        async def run_task(task: InvestigationTask) -> Findings:
            rev = revs.get(task.id, 0) + 1
            revs[task.id] = rev
            wrapper = wrappers.get(task.specialist)
            if wrapper is None:
                return _failed(task)
            # token diet: the task's tools are in the investigator's instruction already; empty
            # upstream findings / revision notes are left out
            brief: dict[str, Any] = {
                **scenario_brief(record.scenario, task_id=task.id, scenario_version=version),
                "objective": task.objective,
            }
            upstream = _upstream(task, findings)
            if upstream:
                brief["upstream"] = upstream
            revision_notes = [str(n)[:300] for n in (notes.get(task.id) or [])][:5]
            if revision_notes:
                brief["revision_notes"] = revision_notes
            if request:
                brief["user_request"] = request
            binding = TaskBinding(task_id=task.id, specialist=task.specialist, tools=tuple(task.tools), rev=rev)
            async with gate:
                with bind_task(binding):
                    try:
                        result = await ctx.run_node(
                            wrapper, node_input=brief, run_id=task_run_id(task.id, rev), use_sub_branch=True
                        )
                        finding = Findings.model_validate(
                            fill_nullable(Findings, result if isinstance(result, Mapping) else {})
                        )
                    except DynamicNodeFailError as exc:
                        report_exception(_log, "specialist failed", exc.error, specialist=task.specialist,
                                         task_id=task.id)
                        finding = _failed(task)
            if finding.task_id != task.id:
                finding = finding.model_copy(update={"task_id": task.id})
            await run.deps.repo.save_findings(
                tenant=run.tenant, case_id=run.case_id, task_id=task_row_id(run.case_id, task.id, rev),
                specialist=task.specialist, run_id=run.run_id, findings=finding.model_dump(mode="json"),
                status="done" if finding.status == "ok" else "failed",
                task_input={
                    "task_id": task.id, "rev": rev, "objective": task.objective, "tools": list(task.tools),
                    "depends_on": list(task.depends_on),
                    # follow-up dirty set: the scenario keys this specialist's findings depend on
                    "assumption_keys": _assumption_keys(spec, task.specialist, record.scenario),
                    "scenario_version": version,
                    "run_id": run.run_id,
                },
            )
            return finding

        ran: list[str] = []
        for wave in plan_waves(plan):
            todo = [by_id[tid] for tid in wave if tid in selected]
            if not todo:
                continue
            results = await _gather_isolated([run_task(t) for t in todo])
            for task, finding in zip(todo, results, strict=True):
                findings[task.id] = finding.model_dump(mode="json")
                ran.append(task.id)
        ctx.state[FINDINGS_BY_TASK_KEY] = findings
        ctx.state[TASK_REVS_KEY] = revs
        ordered = [findings[t.id] for t in plan.tasks if t.id in findings]
        failed = sum(1 for tid in ran if findings[tid].get("status") != "ok")
        label = f"Ran {len(ran)} checks" + (f"; {failed} could not be completed" if failed else "")
        yield progress_event(label, prov=Provenance.INFERENCE.value)
        yield {
            "scenario_version": version,
            "mode": record.scenario.mode,
            "findings": ordered,
            "ran": ran,
            "plan_evidence_id": ctx.state.get(PLAN_EVIDENCE_KEY),
        }

    return FunctionNode(func=investigate, name="investigate", rerun_on_resume=True)
