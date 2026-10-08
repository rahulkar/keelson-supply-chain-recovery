"""``verify_numbers`` → ``critic`` (``docs/contracts/90-m4-investigation.md`` §5).

Both share **one** REVISE budget per case run (``ctx.state["revise_budget"]``, default 1, set by
the planner / first investigation); a REVISE re-runs only the named tasks (``investigate`` with
``{revise_task_ids, notes}``, ``rev+1``) and is never granted twice.

* ``verify_numbers`` (deterministic): every claim that carries a value — a placeholder, a
  ``json_pointer`` / ``calc_id`` or a number in its text — must resolve: its ``evidence_id`` +
  ``json_pointer`` (and each ``{{ev:…}}`` placeholder) must point into an evidence row of the
  case, its ``calc_id`` (and each ``{{calc:…}}``) must be a calc row, and its text may hold no
  free number. Unresolvable claims are dropped and counted; when more than
  ``investigation.verify_max_failed_bp`` (20%) of the numeric claims fail and budget is left →
  ``REVISE`` the tasks that produced them. ``fact`` claims citing nothing or citing an
  ``inference`` / untrusted evidence row are downgraded to ``inference``. Malformed or
  unresolvable ``scenario_updates`` are dropped.
* ``critic`` (``critic-default``, tool-free, :class:`~keelson_contracts.llm.Critique`): checks
  alternative explanations, missing systems, stale data and double counting. A critique with
  :func:`~keelson_contracts.llm.critique_violations` (or a failed critic) counts as ``PASS`` (the
  budget is never spent on it). ``REVISE`` with budget left → ``investigate`` the listed tasks;
  otherwise ``PASS`` → ``refine_scenario`` with the verified findings and the critique.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from typing import Any, Final

import structlog
from google.adk.agents.context import Context
from google.adk.workflow import FunctionNode
from google.adk.workflow._errors import DynamicNodeFailError
from keelson_contracts.llm import (
    Claim,
    Critique,
    Findings,
    InvestigationPlan,
    critique_violations,
)
from pydantic import ValidationError

from ...public_errors import report_exception
from ..agents import KeelsonLlmAgent, fill_nullable
from ..compose import PLACEHOLDER_RE, allowed_tokens_from, numeric_lint
from ..context import RunContext, current_run
from ..jsonptr import JsonPointerError, resolve_pointer
from ..quarantine import is_untrusted_evidence, is_untrusted_text_row
from .common import input_dict, progress_event, routed
from .investigate import (
    FINDINGS_BY_TASK_KEY,
    PLAN_STATE_KEY,
    REVISE_BUDGET_KEY,
    load_plan,
    save_findings_state,
)

log = structlog.get_logger(__name__)

__all__ = [
    "CRITIQUE_STATE_KEY",
    "ROUTE_OK",
    "ROUTE_PASS",
    "ROUTE_REVISE",
    "VERIFY_STATE_KEY",
    "VerifyReport",
    "make_critic",
    "revise_is_warranted",
    "verify_findings",
    "verify_numbers",
]

ROUTE_OK: Final = "OK"
ROUTE_PASS: Final = "PASS"
ROUTE_REVISE: Final = "REVISE"
VERIFY_STATE_KEY: Final = "verify_report"
CRITIQUE_STATE_KEY: Final = "critique"
CRITIC_RUNS_KEY: Final = "critic_runs"
REVISE_NOTE: Final = (
    "Some figures could not be traced to evidence. Cite every value with its evidence_id and json_pointer, "
    "or a calc_id; never write numbers as digits."
)


# ─────────────────────────────────── verify ──────────────────────────────────


class VerifyReport(dict[str, Any]):
    """``{numeric, failed, dropped_updates, downgraded, dropped_citations, failed_by_task}`` (JSON)."""


def _evidence_refs(findings: Iterable[Findings]) -> set[str]:
    refs: set[str] = set()
    for f in findings:
        for c in f.claims:
            for ref in (c.evidence_id, c.calc_id):
                if ref:
                    refs.add(ref)
            for m in PLACEHOLDER_RE.finditer(c.text):
                if m.group(1) in ("ev", "calc"):
                    refs.add(m.group(2).partition("#")[0].strip())
        for u in f.scenario_updates:
            refs.add(u.evidence_id)
    return refs


def _resolves(
    rows: Mapping[str, Mapping[str, Any]], evidence_id: str | None, pointer: str | None, *, cited: bool = False
) -> bool:
    """The pointer resolves to a value. ``cited`` (a claim's value, rendered into text): inside an
    untrusted text row only an integer counts — a string there is supplier / document text, which
    must never be pasted into a narrative through a placeholder."""
    row = rows.get(evidence_id or "")
    if row is None:
        return False
    try:
        value = resolve_pointer(row.get("result"), pointer or "")
    except JsonPointerError:
        return False
    if value is None:
        return False
    if cited and is_untrusted_text_row(row):
        return isinstance(value, int) and not isinstance(value, bool)
    return True


def _is_calc(rows: Mapping[str, Mapping[str, Any]], calc_id: str | None, run: RunContext) -> bool:
    if not calc_id:
        return False
    if calc_id in run.calc_values:
        return True
    row = rows.get(calc_id)
    return row is not None and row.get("tool") == "calc"


_VALUE_TOKEN_RE = re.compile(r"(?<![\w-])[€$£]?\d[\d,.]*(?:%|k|m)?(?![\w-])")
"""A number standing on its own ("27", "16,918", "€5k", "30%") — not part of an entity code
("PO-BD-0001", "F-BD-01", "W46") or a date."""


def _states_a_value(text: str) -> bool:
    return bool(_VALUE_TOKEN_RE.search(PLACEHOLDER_RE.sub(" ", text)))


def citation_resolves(claim: Claim, rows: Mapping[str, Mapping[str, Any]]) -> bool:
    """A claim that states no value but cites ``evidence_id`` (+ ``json_pointer``): the citation
    must point at something in this case's evidence (it is shown as a source, never rendered into
    text); inside an untrusted text row only at an item or a number, never at a string."""
    if not claim.evidence_id:
        return not claim.json_pointer
    row = rows.get(claim.evidence_id)
    if row is None:
        return False
    try:
        value = resolve_pointer(row.get("result"), claim.json_pointer or "")
    except JsonPointerError:
        return False
    # into untrusted text, a citation may name an item but never a string (a body, a quote)
    return value is not None and not (is_untrusted_text_row(row) and isinstance(value, str))


def _claim_problems(
    claim: Claim, rows: Mapping[str, Mapping[str, Any]], run: RunContext, allowed: set[str]
) -> tuple[bool, list[str]]:
    """``(numeric, problems)`` for one claim. A claim is *numeric* when it carries a value: a
    placeholder, a ``calc_id``, a free number, or a pointer together with a number in its text. A
    pointer alone on a qualitative claim is a citation (``citation_resolves``), not a value."""
    placeholders = [(m.group(1), m.group(2).strip()) for m in PLACEHOLDER_RE.finditer(claim.text)]
    free = [v for v in numeric_lint(claim.text, allowed) if v.startswith("free number")]
    numeric = bool(placeholders or claim.calc_id or free or (claim.json_pointer and _states_a_value(claim.text)))
    if not numeric:
        return False, []
    problems = list(free)
    for kind, ref in placeholders:
        if kind == "ev":
            evidence_id, _, pointer = ref.partition("#")
            if not _resolves(rows, evidence_id.strip(), pointer.strip(), cited=True):
                problems.append(f"unresolvable placeholder ev:{ref[:80]}")
        elif kind == "calc" and not _is_calc(rows, ref, run):
            problems.append(f"unknown calc {ref[:80]}")
        elif kind == "kpi":
            problems.append("kpi placeholders are not available to analysts")
    if claim.calc_id and not _is_calc(rows, claim.calc_id, run):
        problems.append("unknown calc_id")
    if claim.evidence_id or claim.json_pointer:
        if not _resolves(rows, claim.evidence_id, claim.json_pointer, cited=True):
            problems.append("evidence_id/json_pointer does not resolve")
    elif not claim.calc_id and not placeholders:
        problems.append("a value without evidence")
    return True, problems


def verify_findings(
    findings: Sequence[Findings], rows: Mapping[str, Mapping[str, Any]], run: RunContext, *, extra_allowed: Iterable[Any] = ()
) -> tuple[list[Findings], VerifyReport]:
    """Pure core of ``verify_numbers`` (see the module docstring)."""
    allowed = allowed_tokens_from([*(r.get("result") for r in rows.values()), *extra_allowed,
                                   *[[a.model_dump(mode="json") for a in f.affected] for f in findings]])
    report = VerifyReport(numeric=0, failed=0, dropped_updates=0, downgraded=0, dropped_citations=0, failed_by_task={})
    out: list[Findings] = []
    for f in findings:
        kept: list[Claim] = []
        for claim in f.claims:
            numeric, problems = _claim_problems(claim, rows, run, allowed)
            if not numeric and not citation_resolves(claim, rows):
                report["dropped_citations"] += 1  # an untraceable source, not a figure: not in the ratio
                continue
            if numeric:
                report["numeric"] += 1
            if problems:
                report["failed"] += 1
                report["failed_by_task"].setdefault(f.task_id, []).extend(problems[:3])
                continue
            sources = [rows.get(claim.evidence_id or "")] + [
                rows.get(m.group(2).partition("#")[0].strip()) for m in PLACEHOLDER_RE.finditer(claim.text)
                if m.group(1) == "ev"
            ]
            untrusted = any(row is not None and is_untrusted_evidence(row) for row in sources)
            if claim.kind == "fact" and (not claim.is_cited or untrusted):
                claim = claim.model_copy(update={"kind": "inference"})
                report["downgraded"] += 1
            kept.append(claim)
        updates = []
        for u in f.scenario_updates:
            if u.shape_error() is not None or not _resolves(rows, u.evidence_id, u.json_pointer):
                report["dropped_updates"] += 1
                continue
            updates.append(u)
        out.append(f.model_copy(update={"claims": kept, "scenario_updates": updates}))
    return out, report


def _findings_of(data: Mapping[str, Any]) -> list[Findings]:
    return [Findings.model_validate(fill_nullable(Findings, f)) for f in data.get("findings") or []]


async def verify_numbers(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    data = input_dict(node_input)
    findings = _findings_of(data)
    refs = sorted(_evidence_refs(findings))
    rows = {
        str(r["id"]): r for r in await run.deps.repo.get_evidence(tenant=run.tenant, ids=refs)
        if r.get("case_id") in (None, run.case_id)
    }
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    scenario_dump = record.scenario.model_dump(mode="json") if record is not None else {}
    verified, report = verify_findings(findings, rows, run, extra_allowed=[scenario_dump])
    by_task = dict(ctx.state.get(FINDINGS_BY_TASK_KEY) or {})
    for f in verified:
        by_task[f.task_id] = f.model_dump(mode="json")
    ctx.state[FINDINGS_BY_TASK_KEY] = by_task
    ctx.state[VERIFY_STATE_KEY] = dict(report)
    await run.deps.repo.save_evidence(
        tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector="keelson", tool="verify_numbers",
        args={"tasks": [f.task_id for f in findings]}, result=dict(report), prov="fact",
    )
    numeric, failed = int(report["numeric"]), int(report["failed"])
    budget = int(ctx.state.get(REVISE_BUDGET_KEY) or 0)
    too_many = numeric > 0 and failed * 10_000 > numeric * run.spec.investigation.verify_max_failed_bp
    label = f"Checked {numeric} figures against the evidence" + (f"; dropped {failed} that could not be traced" if failed else "")
    yield progress_event(label, prov="fact")
    out = {**{k: v for k, v in data.items() if k != "findings"}, "findings": [f.model_dump(mode="json") for f in verified],
           "verify": dict(report)}
    if too_many and budget > 0:
        ctx.state[REVISE_BUDGET_KEY] = budget - 1
        tasks = sorted(report["failed_by_task"])
        notes = {
            tid: [REVISE_NOTE]
            for tid in tasks
        }
        yield routed({"scenario_version": data.get("scenario_version"), "revise_task_ids": tasks, "notes": notes,
                      "reason": "verify"}, ROUTE_REVISE)
        return
    yield routed(out, ROUTE_OK)


# ─────────────────────────────────── critic ──────────────────────────────────


def _critic_brief(plan: InvestigationPlan | None, findings: Sequence[Findings], report: Mapping[str, Any],
                  run: RunContext) -> str:
    """The critic's user turn (token diet): the plan as its tasks (id, specialist, objective — no
    rationale, budget or tool lists), each task's findings (claims capped, empty lists left out) and
    the number check. ``planning_date`` only when the tenant fixed one."""
    planning = run.spec.planning.as_of
    tasks = [{"id": t.id, "specialist": t.specialist, "objective": t.objective,
              **({"depends_on": list(t.depends_on)} if t.depends_on else {})}
             for t in (plan.tasks if plan is not None else [])]
    entries = []
    for f in findings:
        entry: dict[str, Any] = {
            "task_id": f.task_id,
            "status": f.status,
            "claims": [{"text": c.text, "kind": c.kind, "cited": c.is_cited, "confidence": c.confidence}
                       for c in f.claims][:MAX_CRITIC_CLAIMS],
        }
        if f.affected:
            entry["affected"] = [f"{a.type}:{a.code}" for a in f.affected][:MAX_CRITIC_AFFECTED]
        if f.gaps:
            entry["gaps"] = f.gaps[:6]
        if f.scenario_updates:
            entry["scenario_updates"] = [u.key for u in f.scenario_updates]
        entries.append(entry)
    brief: dict[str, Any] = {
        "plan": {"tasks": tasks} if plan is not None else None,
        "findings": entries,
        "number_check": {k: report.get(k) for k in ("numeric", "failed", "downgraded", "dropped_updates")},
    }
    if planning:
        brief["planning_date"] = planning.isoformat()
    return json.dumps(brief, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


MAX_CRITIC_CLAIMS: Final = 12
MAX_CRITIC_AFFECTED: Final = 15


def revise_is_warranted(critique: Critique) -> bool:
    """Token diet: a critic REVISE buys a second specialist round only for a ``high`` severity
    issue about the whole investigation or about one of the tasks it asks to re-run. Medium and low
    remarks stay in the critique (and the evidence row) but the run goes on (PASS)."""
    wanted = set(critique.revise_task_ids)
    return any(i.severity == "high" and (i.task_id is None or i.task_id in wanted) for i in critique.issues)


def make_critic(critic_agent: KeelsonLlmAgent) -> FunctionNode:
    async def critic(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        run = current_run()
        data = input_dict(node_input)
        findings = _findings_of(data)
        plan = await load_plan(ctx, run)
        budget = int(ctx.state.get(REVISE_BUDGET_KEY) or 0)
        runs = int(ctx.state.get(CRITIC_RUNS_KEY) or 0)
        critique = Critique(verdict="PASS", issues=[], revise_task_ids=[])
        if runs and budget <= 0:
            log.info("critic skipped: the revise budget is spent and the critic already reviewed this run")
        else:
            ctx.state[CRITIC_RUNS_KEY] = runs + 1
            try:
                raw = await ctx.run_node(
                    critic_agent, node_input=_critic_brief(plan, findings, data.get("verify") or {}, run),
                    run_id="review",
                )
                candidate = Critique.model_validate(fill_nullable(Critique, raw if isinstance(raw, Mapping) else {}))
                problems = critique_violations(candidate, plan) if plan is not None else ["no plan"]
                if problems and candidate.verdict == "REVISE":
                    log.warning("unusable critique treated as PASS", problems=problems[:10])
                    critique = Critique(verdict="PASS", issues=candidate.issues if plan is not None else [],
                                        revise_task_ids=[])
                elif problems:
                    critique = Critique(verdict="PASS", issues=[], revise_task_ids=[])
                else:
                    critique = candidate
            except (DynamicNodeFailError, ValidationError) as exc:
                report_exception(log, "critic failed; treating the review as PASS", exc)
        if critique.issues:
            await run.deps.repo.save_evidence(
                tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector="keelson", tool="critic",
                args={"plan": bool(plan)}, result=critique.model_dump(mode="json"), prov="inference",
            )
        if critique.verdict == "REVISE" and budget > 0 and not revise_is_warranted(critique):
            log.info("critic REVISE without a high-severity issue: no second specialist round",
                     tasks=list(critique.revise_task_ids), severities=[i.severity for i in critique.issues])
            critique = Critique(verdict="PASS", issues=critique.issues, revise_task_ids=[])
        ctx.state[CRITIQUE_STATE_KEY] = critique.model_dump(mode="json")
        if critique.verdict == "REVISE" and budget > 0:
            ctx.state[REVISE_BUDGET_KEY] = budget - 1
            notes: dict[str, list[str]] = {}
            for issue in critique.issues:
                targets = [issue.task_id] if issue.task_id else critique.revise_task_ids
                for tid in targets:
                    if tid in critique.revise_task_ids:
                        notes.setdefault(tid, []).append(issue.issue)
            yield progress_event("The review asked for a second look at some checks", prov="inference")
            yield routed({"scenario_version": data.get("scenario_version"),
                          "revise_task_ids": list(critique.revise_task_ids), "notes": notes, "reason": "critic"},
                         ROUTE_REVISE)
            return
        await save_findings_state(ctx, run)
        yield routed({**data, "critique": critique.model_dump(mode="json"),
                      "plan": ctx.state.get(PLAN_STATE_KEY)}, ROUTE_PASS)

    return FunctionNode(func=critic, name="critic", rerun_on_resume=True)
