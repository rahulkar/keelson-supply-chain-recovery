"""``build_workflow(spec)`` — the M4 graph (``50-engine-workflow.md`` + ``90-m4-investigation.md``)::

    START → pre_route: CHAT→intake | APPLY / GUIDE→compile_changeset | PROMOTE→promote_case
                       | CONFIRM_ASSUMPTIONS→confirm_assumptions | ROLLBACK→prepare_rollback
                       | CHECKLIST_STEP→checklist_step | SIGNAL→prepare_replan
                       | SIGNAL_EVENT→frame_case (M6: a Signal Hub event, framed without an LLM)
                       | DEFAULT→reject_run
    intake → route_intake: QUESTION→answer_investigator→answer_structurer→compose_answer
                           FOLLOWUP (case has a plan)→followup_delta | NEW_DISRUPTION→frame_case
                           | DEFAULT→compose_smalltalk
    frame_case → quick_exposure → planner → approach_gate → approach_router:
        CONFIRM→investigate | WHAT_IF→investigate | EDIT→frame_case | CANCEL→close_case | DEFAULT→close_case
    investigate → verify_numbers: OK→critic | REVISE→investigate | DEFAULT→critic
    critic: PASS→refine_scenario | REVISE→investigate | DEFAULT→refine_scenario
    refine_scenario → impact → simulate → recommend → compose
    promote_case / confirm_assumptions: RESIMULATE→impact | DEFAULT (DONE)→end_turn
    followup_delta: EXPLAIN→explain | RESIMULATE→prepare_resimulate | REINVESTIGATE→prepare_reinvestigate
                    | DEFAULT→answer_investigator
    explain / prepare_resimulate: RESIMULATE→impact | DEFAULT (DONE)→end_turn
    prepare_reinvestigate: REINVESTIGATE→investigate (dirty tasks only) | DEFAULT (DONE)→end_turn
    compile_changeset: COMPILED→dry_run | GUIDED→end_turn | DEFAULT→close_case
    prepare_rollback: ROLLBACK→approval_gate | DEFAULT→close_case
    prepare_replan (signal runs, no LLM): COMPILE→compile_changeset | DONE→end_turn | DEFAULT→close_case
    dry_run: VALIDATED→approval_gate | DEFAULT→close_case
    approval_gate → approval_check: APPROVED→execute | REVISE→close_case | REJECT→close_case | DEFAULT→close_case
    execute → reconcile

``verify_numbers`` and ``critic`` share ONE REVISE budget per run (``investigation.revise_budget``).
Every router has a ``DEFAULT_ROUTE`` edge. LLM agents are ``single_turn`` with
``include_contents='none'``; only investigators hold (read) tools and they never have an
``output_schema``. Untrusted sources reach investigators only through the quarantine wrappers
(``runtime.quarantine``: guard + quarantined extractor, both tool-free).

Framing-owned nodes (``nodes/framing.py`` ``frame_case``/``promote_case``/``confirm_assumptions``,
``nodes/refine.py`` ``refine_scenario``, ``nodes/followup.py`` ``followup_delta``/``explain``/
``prepare_resimulate``/``prepare_reinvestigate``) are bound by name; until a module provides one,
the conservative stand-in of :mod:`.nodes.m4_defaults` (or ``routing.promote_case``) is used.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import structlog
from google.adk.workflow import DEFAULT_ROUTE, START, FunctionNode, Workflow
from keelson_contracts.llm import (
    Critique,
    Findings,
    GuardVerdict,
    IntakeResult,
    InvestigationPlan,
    Recommendation,
    UntrustedExtraction,
)

from .agents import KeelsonLlmAgent, make_agent
from .errors import RuntimeConfigError
from .llm import ModelFactory
from .nodes import answer, commit_nodes, framing, guide_nodes, m4_defaults, planning, replan, review, routing
from .nodes.investigate import make_investigate, make_planner, make_specialist_wrapper
from .nodes.prompts import (
    answer_investigator_instruction,
    answer_structurer_instruction,
    critic_instruction,
    extractor_instruction,
    guard_instruction,
    intake_instruction,
    investigator_instruction,
    planner_instruction,
    recommender_instruction,
    structurer_instruction,
)
from .custom_specialists import custom_investigator
from .quarantine import quarantine_tools_for
from .spec import AgentSpec, CustomSpecialist
from .toolsets import CalcTool, RegistryToolset

__all__ = ["LLM_ROLE_NODES", "WORKFLOW_NAME", "WorkflowBindings", "build_workflow", "framing_node"]

log = structlog.get_logger(__name__)

WORKFLOW_NAME = "keelson"

LLM_ROLE_NODES = {"intake": "intake", "recommend": "recommender", "critic": "critic"}
"""Static node name → AG-UI agent slug (``AGENT_DISPLAY_NAMES``)."""


@dataclass(frozen=True)
class WorkflowBindings:
    models: ModelFactory


def _node(func: object, *, name: str, rerun_on_resume: bool = False) -> FunctionNode:
    return FunctionNode(func=func, name=name, rerun_on_resume=rerun_on_resume)  # type: ignore[arg-type]


def _optional_module(name: str) -> ModuleType | None:
    """A framing-owned node module, or ``None`` while it does not exist yet (a module that exists
    but fails to import is a bug and raises)."""
    full = f"{__package__}.nodes.{name}"
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name != full:
            raise
        return None


def framing_node(module: str, name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
    """The framing engineer's node function ``<module>.<name>`` or the stand-in ``fallback``."""
    mod = framing if module == "framing" else _optional_module(module)
    func = getattr(mod, name, None) if mod is not None else None
    if func is None:
        log.info("framing-owned node not available; using the stand-in", module=module, node=name)
        return fallback
    return func  # type: ignore[no-any-return]


def _followup_delta(spec: AgentSpec, models: ModelFactory) -> FunctionNode:
    """``followup.make_followup_delta(make_followup_classifier(models, alias=…))`` (``M4_NODES.md``),
    a plain ``followup_delta`` node function, or the stand-in."""
    mod = _optional_module("followup")
    factory = getattr(mod, "make_followup_delta", None) if mod is not None else None
    classifier = getattr(mod, "make_followup_classifier", None) if mod is not None else None
    if factory is not None and classifier is not None:
        node = factory(classifier(models, alias=spec.models.followup))
        if not isinstance(node, FunctionNode) or node.name != "followup_delta":
            raise RuntimeConfigError("followup.make_followup_delta must return FunctionNode 'followup_delta'")
        return node
    return _node(framing_node("followup", "followup_delta", m4_defaults.followup_delta), name="followup_delta",
                 rerun_on_resume=True)


def build_workflow(spec: AgentSpec, bindings: WorkflowBindings) -> Workflow:
    models = bindings.models
    aliases = spec.models

    intake = make_agent(
        name="intake", alias=aliases.intake, models=models, instruction=intake_instruction,
        output_schema=IntakeResult, description="Classifies the message and extracts the disruption",
    )
    answer_investigator = make_agent(
        name="answer_investigator", alias=aliases.answer_investigator, models=models,
        instruction=answer_investigator_instruction,
        tools=[
            RegistryToolset(connector=spec.answer_connector, allowlist=spec.answer_tools, owner="answer_investigator"),
            CalcTool(),
        ],
    )
    answer_structurer = make_agent(
        name="answer_structurer", alias=aliases.answer_structurer, models=models,
        instruction=answer_structurer_instruction, output_schema=Findings,
    )
    recommender = make_agent(
        name="recommend", alias=aliases.recommender, models=models, instruction=recommender_instruction,
        output_schema=Recommendation, description="Strategist: recommends one plan option",
    )
    recommender_repair = make_agent(
        name="recommender_repair", alias=aliases.recommender, models=models, instruction=recommender_instruction,
        output_schema=Recommendation,
    )
    planner_agent = make_agent(
        name="planner", alias=aliases.planner, models=models, instruction=planner_instruction,
        output_schema=InvestigationPlan, description="Plans the investigation",
    )
    critic_agent = make_agent(
        name="critic", alias=aliases.critic, models=models, instruction=critic_instruction,
        output_schema=Critique, description="Reviews the investigation",
    )
    guard = make_agent(
        name="guard", alias=aliases.guard, models=models, instruction=guard_instruction, output_schema=GuardVerdict,
    )
    extractor = make_agent(
        name="extractor", alias=aliases.extractor, models=models, instruction=extractor_instruction,
        output_schema=UntrustedExtraction,
    )

    wrappers: dict[str, FunctionNode] = {}
    for specialist in spec.all_specialists:
        owner = f"{specialist.slug}_investigator"
        investigator: KeelsonLlmAgent
        if isinstance(specialist, CustomSpecialist):
            # M7 (110 §3): tenant specialist — its own alias, tools on up to three connectors
            # (untrusted_text tools auto-wrapped in the quarantine), its role prompt as data
            investigator = custom_investigator(specialist, spec=spec, models=models, guard=guard,
                                               extractor=extractor)
        else:
            investigator = make_agent(
                name=owner, alias=aliases.investigator, models=models,
                instruction=investigator_instruction(specialist.slug, spec.display_name(specialist.slug)),
                tools=[
                    RegistryToolset(connector=specialist.connector, allowlist=specialist.tools, owner=owner),
                    *quarantine_tools_for(
                        specialist.quarantined, connector=specialist.connector, guard=guard, extractor=extractor,
                        owner=owner,
                    ),
                ],
            )
        structurer = make_agent(
            name=f"{specialist.slug}_structurer", alias=aliases.structurer, models=models,
            instruction=structurer_instruction(specialist.slug), output_schema=Findings,
        )
        wrappers[specialist.slug] = make_specialist_wrapper(specialist, investigator, structurer)

    pre_route = _node(routing.pre_route, name="pre_route")
    route_intake = _node(routing.route_intake, name="route_intake")
    reject_run = _node(routing.reject_run, name="reject_run")
    promote_case = _node(framing_node("framing", "promote_case", routing.promote_case), name="promote_case")
    confirm_assumptions = _node(
        framing_node("framing", "confirm_assumptions", m4_defaults.confirm_assumptions), name="confirm_assumptions"
    )
    end_turn = _node(m4_defaults.end_turn, name="end_turn")
    compose_smalltalk = _node(routing.compose_smalltalk, name="compose_smalltalk")
    close_case = _node(routing.close_case, name="close_case")
    compose_answer = _node(answer.compose_answer, name="compose_answer")
    frame_case = _node(framing.frame_case, name="frame_case")
    quick_exposure = _node(framing.quick_exposure, name="quick_exposure")
    planner = make_planner(planner_agent)
    approach_gate = _node(framing.approach_gate, name="approach_gate")
    approach_router = _node(framing.approach_router, name="approach_router")
    investigate = make_investigate(wrappers)
    verify_numbers = _node(review.verify_numbers, name="verify_numbers")
    critic = review.make_critic(critic_agent)
    refine_scenario = _node(
        framing_node("refine", "refine_scenario", m4_defaults.refine_scenario), name="refine_scenario"
    )
    followup_delta = _followup_delta(spec, models)
    explain = _node(framing_node("followup", "explain", m4_defaults.explain), name="explain", rerun_on_resume=True)
    prepare_resimulate = _node(
        framing_node("followup", "prepare_resimulate", m4_defaults.prepare_resimulate), name="prepare_resimulate"
    )
    prepare_reinvestigate = _node(
        framing_node("followup", "prepare_reinvestigate", m4_defaults.prepare_reinvestigate),
        name="prepare_reinvestigate",
    )
    impact = _node(planning.impact, name="impact")
    simulate = _node(planning.simulate, name="simulate")
    compose = planning.make_compose(recommender_repair)
    compile_changeset = _node(commit_nodes.compile_changeset, name="compile_changeset")
    dry_run = _node(commit_nodes.dry_run, name="dry_run")
    approval_gate = _node(commit_nodes.approval_gate, name="approval_gate")
    approval_check = _node(commit_nodes.approval_check, name="approval_check")
    execute = _node(commit_nodes.execute, name="execute", rerun_on_resume=True)
    reconcile = _node(commit_nodes.reconcile, name="reconcile")
    prepare_rollback = _node(commit_nodes.prepare_rollback, name="prepare_rollback")
    checklist_step = _node(guide_nodes.checklist_step, name="checklist_step")
    prepare_replan = _node(replan.prepare_replan, name="prepare_replan")

    edges: list[Any] = [
        (
            START,
            pre_route,
            {
                routing.ROUTE_CHAT: intake,
                routing.ROUTE_APPLY: compile_changeset,
                routing.ROUTE_PROMOTE: promote_case,
                routing.ROUTE_CONFIRM_ASSUMPTIONS: confirm_assumptions,
                routing.ROUTE_GUIDE: compile_changeset,
                routing.ROUTE_ROLLBACK: prepare_rollback,
                routing.ROUTE_CHECKLIST_STEP: checklist_step,
                routing.ROUTE_SIGNAL: prepare_replan,
                routing.ROUTE_SIGNAL_EVENT: frame_case,
                DEFAULT_ROUTE: reject_run,
            },
        ),
        (
            intake,
            route_intake,
            {
                routing.ROUTE_QUESTION: answer_investigator,
                routing.ROUTE_FOLLOWUP: followup_delta,
                routing.ROUTE_NEW_DISRUPTION: frame_case,
                DEFAULT_ROUTE: compose_smalltalk,
            },
        ),
        (answer_investigator, answer_structurer, compose_answer),
        (
            frame_case,
            quick_exposure,
            planner,
            approach_gate,
            approach_router,
            {
                "CONFIRM": investigate,
                "WHAT_IF": investigate,
                "EDIT": frame_case,
                "CANCEL": close_case,
                DEFAULT_ROUTE: close_case,
            },
        ),
        (investigate, verify_numbers, {review.ROUTE_OK: critic, review.ROUTE_REVISE: investigate, DEFAULT_ROUTE: critic}),
        (critic, {review.ROUTE_PASS: refine_scenario, review.ROUTE_REVISE: investigate, DEFAULT_ROUTE: refine_scenario}),
        (refine_scenario, impact, simulate, recommender, compose),
        (promote_case, {routing.ROUTE_RESIMULATE: impact, DEFAULT_ROUTE: end_turn}),
        (confirm_assumptions, {routing.ROUTE_RESIMULATE: impact, DEFAULT_ROUTE: end_turn}),
        (
            followup_delta,
            {
                "EXPLAIN": explain,
                "RESIMULATE": prepare_resimulate,
                "REINVESTIGATE": prepare_reinvestigate,
                DEFAULT_ROUTE: answer_investigator,
            },
        ),
        (explain, {routing.ROUTE_RESIMULATE: impact, DEFAULT_ROUTE: end_turn}),
        (prepare_resimulate, {routing.ROUTE_RESIMULATE: impact, DEFAULT_ROUTE: end_turn}),
        (prepare_reinvestigate, {"REINVESTIGATE": investigate, DEFAULT_ROUTE: end_turn}),
        (compile_changeset, {"COMPILED": dry_run, commit_nodes.ROUTE_GUIDED: end_turn, DEFAULT_ROUTE: close_case}),
        (prepare_rollback, {commit_nodes.ROUTE_ROLLBACK: approval_gate, DEFAULT_ROUTE: close_case}),
        (prepare_replan, {replan.ROUTE_COMPILE: compile_changeset, framing.ROUTE_DONE: end_turn,
                          DEFAULT_ROUTE: close_case}),
        (dry_run, {"VALIDATED": approval_gate, DEFAULT_ROUTE: close_case}),
        (
            approval_gate,
            approval_check,
            {"APPROVED": execute, "REVISE": close_case, "REJECT": close_case, DEFAULT_ROUTE: close_case},
        ),
        (execute, reconcile),
    ]
    return Workflow(name=WORKFLOW_NAME, edges=edges)
