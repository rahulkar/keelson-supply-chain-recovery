"""Approval policy (``docs/contracts/95-m5-remediation.md`` §1): CEL rules, quorums, auto-commit.

``AgentSpec.policy`` carries the tenant's versioned rule set (:class:`~keelson_contracts.policy.PolicyRule`
``{id, description, when: <CEL>, require: {roles, count, exclude_initiator} | "auto" | "refuse",
priority}``); without explicit ``rules`` the default set of :func:`default_rules` applies (built
from the config's caps and role names). Rules are evaluated with **cel-expr-python** (the CEL C++
runtime, ``cel_expr_python.cel``; wheels for CPython 3.14) over the typed
:class:`~keelson_contracts.policy.PolicyContext`::

    cs        {lines, units, value_cents, ops, connectors, incremental_cost_cents, lost_units_after, levers}
    case      {mode, origin: chat|signal|webhook, tainted, unconfirmed}
    initiator {roles}

Evaluation is :meth:`PolicySet.decide` (ascending priority, first match wins) with its two hard
floors that no rule set can lift: a what-if case or one with unconfirmed values is refused, and
``"auto"`` rules are skipped unless the run is a person's chat on untainted input (signal/webhook
runs, tainted or what-if plans are never auto-approved; rollbacks pass ``allow_auto=False``).

Fail closed: a rule that does not compile, reads an unknown field or does not return a bool
raises :class:`PolicyEvaluationError` (``approval_gate`` then refuses; nothing is applied).

The default rules (priority order)::

    10 refuse-unconfirmed   case.mode == "what_if" || case.unconfirmed > 0                       → refuse
    20 four-eyes            cs.incremental_cost_cents > 1000000 || cs.ops.exists(o, o in [po.create,
                            po_line.cancel, production_order.resequence])                       → approver + compliance, 2, not the initiator
    30 human-untrusted      case.origin != "chat" || case.tainted                                → 1 approver
    40 auto-small-transfer  only transfer/allocation ops, lines ≤ 10, units ≤ 500, planner initiator → auto
    50 default              true                                                                 → approver × approval_quorum

(``four-eyes`` comes before ``human-untrusted`` so a webhook re-plan that creates a PO still needs
two people; the floors already keep signal runs away from ``auto``.)

:func:`register_policy_evaluator` hands :func:`evaluate_policy` to the platform (``POST
/api/admin/policies/test``, ``PUT /api/admin/policies`` validation) at start-up.
"""

from __future__ import annotations

import functools
import importlib
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import structlog
from keelson_contracts.changeset import ChangeSet
from keelson_contracts.policy import (
    PolicyCase,
    PolicyChangeSet,
    PolicyContext,
    PolicyDecision,
    PolicyInitiator,
    PolicyRule,
    PolicySet,
    Requirement,
    RunOrigin,
)
from keelson_contracts.runs import Actor
from keelson_contracts.scenario import DisruptionScenario

from .spec import PolicyConfig

__all__ = [
    "AUTO_OPS",
    "CEL_ENGINE",
    "CONTEXT_FIELDS",
    "FOUR_EYES_OPS",
    "HUMAN_ONLY_OPS",
    "ApprovalDecision",
    "PolicyEvaluationError",
    "build_context",
    "change_set_totals",
    "check_rule",
    "decide_approval",
    "default_rules",
    "evaluate_policy",
    "fallback_requirement",
    "policy_set",
    "register_policy_evaluator",
    "requirement_text",
]

log = structlog.get_logger(__name__)

CEL_ENGINE: Final = "cel-expr-python"
"""The CEL implementation in use (``cel_expr_python`` 0.1.3, CEL C++ runtime; ``cel-python`` is
the documented fallback and is not needed on CPython 3.14)."""

AUTO_OPS: Final = frozenset({"transfer_order.create", "allocation.set_qty"})
"""Ops the default ``auto-small-transfer`` rule may auto-commit (Merxa enforces its own caps too)."""
FOUR_EYES_OPS: Final = ("po.create", "po_line.cancel", "production_order.resequence")
HUMAN_ONLY_OPS: dict[str, str] = {
    "po_line.split": "expedite",
    "capacity_booking.create": "expedite",
    "po.create": "near-shore",
    "po_line.change_qty": "near-shore",
    "po_line.cancel": "near-shore",
    "production_order.resequence": "resequencing",
    "po_line.reschedule": "resequencing",
}
"""Op → lever family of ops that always need a person (named in the approval reason)."""

CONTEXT_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "cs": frozenset(PolicyChangeSet.model_fields),
    "case": frozenset(PolicyCase.model_fields),
    "initiator": frozenset(PolicyInitiator.model_fields),
}
"""The CEL variables and the fields a rule may read (anything else fails the static check)."""

_FIELD_RE = re.compile(r"(?<![\w.])(cs|case|initiator)\s*\.\s*([A-Za-z_]\w*)")
_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


class PolicyEvaluationError(ValueError):
    """A rule could not be evaluated (compile error, unknown field, error value, non-bool)."""

    def __init__(self, rule_id: str | None, code: str, detail: str) -> None:
        super().__init__(f"policy rule {rule_id or '<expr>'}: {code}: {detail}")
        self.rule_id = rule_id
        self.code = code
        self.detail = detail


# ─────────────────────────────── CEL engine ────────────────────────────────

_env_lock = threading.Lock()
_env: Any = None


def _cel() -> Any:
    from cel_expr_python import cel  # noqa: PLC0415 - native module, loaded on first use

    return cel


def _environment() -> Any:
    global _env
    with _env_lock:
        if _env is None:
            cel = _cel()
            dyn_map = cel.Type.Map(cel.Type.STRING, cel.Type.DYN)
            _env = cel.NewEnv(variables={name: dyn_map for name in CONTEXT_FIELDS})
        return _env


@functools.lru_cache(maxsize=512)
def _compiled(expression: str) -> Any:
    try:
        return _environment().compile(expression)
    except Exception as exc:  # the C++ binding raises RuntimeError with the parser/checker message
        raise PolicyEvaluationError(None, "compile", str(exc).splitlines()[0][:300]) from exc


def check_rule(when: str) -> list[str]:
    """Static problems of a CEL ``when`` (``[]`` = fine): it must compile, read only the context
    fields (:data:`CONTEXT_FIELDS`) and produce a bool."""
    problems: list[str] = []
    stripped = _STRING_RE.sub("''", when)
    for var, name in _FIELD_RE.findall(stripped):
        if name not in CONTEXT_FIELDS[var]:
            problems.append(f"unknown field {var}.{name}")
    try:
        expr = _compiled(when)
    except PolicyEvaluationError as exc:
        return [*problems, exc.detail]
    kind = expr.return_type().name()
    if kind not in ("bool", "BOOL", "dyn", "DYN"):
        problems.append(f"the expression returns {kind}, not a bool")
    return sorted(set(problems))


def eval_when(when: str, activation: Mapping[str, Any], *, rule_id: str | None = None) -> bool:
    """Evaluate one ``when``; raises :class:`PolicyEvaluationError` (fail closed) unless it
    yields a bool."""
    problems = [p for p in check_rule(when) if p.startswith("unknown field")]
    if problems:
        raise PolicyEvaluationError(rule_id, "unknown_field", "; ".join(problems))
    try:
        expr = _compiled(when)
    except PolicyEvaluationError as exc:
        raise PolicyEvaluationError(rule_id, exc.code, exc.detail) from exc
    try:
        value = expr.eval(data=dict(activation))
    except Exception as exc:
        raise PolicyEvaluationError(rule_id, "eval", str(exc)[:300]) from exc
    result = value.value()
    if value.type().name() in ("error", "ERROR") or not isinstance(result, bool):
        raise PolicyEvaluationError(rule_id, "eval", f"did not produce a bool ({str(result)[:200]})")
    return result


# ─────────────────────────────── rule sets ────────────────────────────────


def _list(values: Iterable[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def fallback_requirement(config: PolicyConfig) -> Requirement:
    """What applies when no rule matches: ``approval_quorum`` people with the approver role."""
    return Requirement(roles=[config.approver_role], count=config.approval_quorum, exclude_initiator=False)


def default_rules(config: PolicyConfig) -> list[PolicyRule]:
    """The pack-default rule set, parameterised by the config's caps and role names."""
    approver, compliance = config.approver_role, config.compliance_role
    four_eyes_roles = [approver] if compliance == approver else [approver, compliance]
    return [
        PolicyRule(
            id="refuse-unconfirmed", priority=10,
            description="What-if cases and plans that rely on unconfirmed values are never committed",
            when='case.mode == "what_if" || case.unconfirmed > 0', require="refuse",
        ),
        PolicyRule(
            id="four-eyes", priority=20,
            description="Large or structural changes need two different people (an approver and compliance)",
            when=(f"cs.incremental_cost_cents > {config.four_eyes_cost_cents} || "
                  f"cs.ops.exists(o, o in {_list(config.four_eyes_ops)})"),
            require=Requirement(roles=four_eyes_roles, count=2, exclude_initiator=True),
        ),
        PolicyRule(
            id="human-untrusted", priority=30,
            description="Changes from a signal or webhook, or from untrusted input, always need an approver",
            when='case.origin != "chat" || case.tainted',
            require=Requirement(roles=[approver], count=1, exclude_initiator=False),
        ),
        PolicyRule(
            id=config.auto_rule_id, priority=40,
            description="Small transfer and allocation changes started by a planner are applied automatically",
            when=(f"cs.ops.all(o, o in {_list(sorted(AUTO_OPS))}) && cs.lines <= {config.auto_max_lines} && "
                  f"cs.units <= {config.auto_max_units} && "
                  f"initiator.roles.exists(r, r == \"{config.auto_initiator_role}\")"),
            require="auto",
        ),
        PolicyRule(
            id="default", priority=50, description="Changes need approval by someone with the approver role",
            when="true", require=fallback_requirement(config),
        ),
    ]


def policy_set(config: PolicyConfig) -> PolicySet:
    """The tenant's rule set: ``config.rules`` when set, else :func:`default_rules`."""
    rules = list(config.rules) if config.rules else default_rules(config)
    return PolicySet(version=config.version, rules=rules)


def _as_set(rules: PolicySet | Mapping[str, Any] | Sequence[PolicyRule | Mapping[str, Any]],
            version: str | None) -> PolicySet:
    if isinstance(rules, PolicySet):
        return rules
    if isinstance(rules, Mapping):
        return PolicySet.model_validate(rules)
    return PolicySet.model_validate({"version": version or "test", "rules": [
        r.model_dump(mode="json") if isinstance(r, PolicyRule) else dict(r) for r in rules
    ]})


def evaluate_policy(
    rules: PolicySet | Mapping[str, Any] | Sequence[PolicyRule | Mapping[str, Any]],
    context: PolicyContext | Mapping[str, Any],
    *,
    fallback: Requirement | Mapping[str, Any] | None = None,
    allow_auto: bool = True,
    version: str | None = None,
) -> PolicyDecision:
    """The policy port: evaluate ``rules`` (a :class:`PolicySet`, its JSON, or a rule list) over
    ``context`` → :class:`PolicyDecision` (matched rule + requirement). Raises
    :class:`PolicyEvaluationError` when a rule that had to be evaluated fails (fail closed)."""
    rule_set = _as_set(rules, version)
    ctx = context if isinstance(context, PolicyContext) else PolicyContext.model_validate(context)
    req = fallback if isinstance(fallback, Requirement) else (
        Requirement.model_validate(fallback) if fallback is not None
        else Requirement(roles=["approver"], count=1, exclude_initiator=False)
    )
    activation = ctx.activation()
    return rule_set.decide(ctx, lambda rule: eval_when(rule.when, activation, rule_id=rule.id),
                           fallback=req, allow_auto=allow_auto)


_registered = False


def register_policy_evaluator() -> bool:
    """Register :func:`evaluate_policy` (and :func:`check_rule`) with the platform's policy port
    (``core.policies.register_policy_evaluator(evaluate, check=)``, also accepted on
    ``core.policy``). Returns ``False`` while the platform has no port (logged once)."""
    global _registered
    for module in ("keelson_engine.core.policies", "keelson_engine.core.policy"):
        try:
            mod = importlib.import_module(module)
        except ImportError:
            continue
        fn = getattr(mod, "register_policy_evaluator", None) or getattr(mod, "set_policy_evaluator", None)
        if callable(fn):
            try:
                fn(evaluate_policy, check=check_rule)
            except TypeError:
                fn(evaluate_policy)
            _registered = True
            return True
    if not _registered:
        log.info("no platform policy port to register the CEL evaluator with (core.policies missing)")
    return False


# ─────────────────────────────── context ────────────────────────────────


def change_set_totals(change_set: ChangeSet) -> tuple[int, int]:
    """``(lines, units)`` — lines = items, units = transferred/set quantities."""
    units = 0
    for item in change_set.items:
        payload = item.payload
        if hasattr(payload, "lines"):
            units += sum(int(line.qty) for line in payload.lines)
        elif hasattr(payload, "qty"):
            units += int(payload.qty)
        elif isinstance(payload, Mapping):
            if isinstance(payload.get("lines"), list):
                units += sum(int(line.get("qty") or 0) for line in payload["lines"] if isinstance(line, Mapping))
            for key in ("qty", "qty_units"):
                if isinstance(payload.get(key), int) and not isinstance(payload.get(key), bool):
                    units += int(payload[key])
        elif hasattr(payload, "qty_units"):
            units += int(payload.qty_units)
    return len(change_set.items), units


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_context(
    *,
    change_sets: Sequence[ChangeSet],
    scenario: DisruptionScenario | None,
    case_mode: str,
    initiator_roles: Iterable[str],
    origin: RunOrigin = "chat",
    tainted: bool = False,
    option: Mapping[str, Any] | None = None,
    value_cents: int = 0,
    extra_unconfirmed: Sequence[str] = (),
) -> PolicyContext:
    """The CEL context of one change set or a bundle (every member counted)."""
    lines = units = 0
    ops: list[str] = []
    for cs in change_sets:
        n, u = change_set_totals(cs)
        lines += n
        units += u
        ops.extend(item.op for item in cs.items)
    option = option or {}
    card = option.get("scorecard") or {}
    cost = card.get("incremental_cost_cents")
    if cost is None:
        cost = (option.get("kpi_delta") or {}).get("transfer_cost_cents", 0)
    mode = "what_if" if case_mode == "what_if" or (scenario is not None and scenario.mode == "what_if") else "actual"
    unconfirmed = len(scenario.unconfirmed_assumptions) if scenario is not None else 0
    roles = sorted({str(r) for r in initiator_roles if re.fullmatch(r"[a-z][a-z0-9_-]*", str(r) or "")})
    return PolicyContext(
        cs=PolicyChangeSet(
            lines=lines, units=units, value_cents=max(0, _int(value_cents)),
            ops=list(dict.fromkeys(sorted(ops))), connectors=list(dict.fromkeys(cs.connector for cs in change_sets)),
            incremental_cost_cents=_int(cost),
            lost_units_after=max(0, _int((option.get("kpis_after") or {}).get("units_at_risk"))),
            levers=list(dict.fromkeys(str(x) for x in option.get("levers") or [])),
        ),
        case=PolicyCase(mode=mode, origin=origin, tainted=bool(tainted), unconfirmed=unconfirmed + len(extra_unconfirmed)),
        initiator=PolicyInitiator(roles=roles),
    )


# ─────────────────────────────── decision ────────────────────────────────


def requirement_text(req: Requirement) -> str:
    roles = " and ".join(req.roles) if req.every_role else " or ".join(req.roles)
    who = f"{req.count} different people ({roles})" if req.count > 1 else f"someone with the {roles} role"
    if req.exclude_initiator:
        who += ", not the person who started it"
    return f"Needs approval by {who}."


@dataclass(frozen=True)
class ApprovalDecision:
    """``approval_gate``'s outcome: ``refuse`` (nothing is compiled/applied), ``auto`` (the
    autocommit principal applies; receipt ``approved_by = policy:<rule>@<version>``) or ``human``
    (an approval interrupt with :attr:`requirement` as its quorum)."""

    outcome: Literal["refuse", "auto", "human"]
    reason: str
    policy_version: str
    rule_id: str | None = None
    requirement: Requirement | None = None
    context: PolicyContext | None = field(default=None, compare=False)

    @property
    def quorum(self) -> dict[str, Any]:
        return self.requirement.model_dump(mode="json") if self.requirement is not None else {}

    @property
    def required_roles(self) -> tuple[str, ...]:
        return tuple(self.requirement.roles) if self.requirement is not None else ()

    @property
    def approved_by(self) -> str | None:
        if self.outcome != "auto" or self.rule_id is None:
            return None
        return f"policy:{self.rule_id}@{self.policy_version}"


def _refuse_reason(ctx: PolicyContext, scenario: DisruptionScenario | None, extra: Sequence[str]) -> str | None:
    if ctx.case.mode == "what_if":
        return "This is a what-if case; promote it to an actual case before applying changes."
    if ctx.case.unconfirmed > 0:
        names = [a.label or a.key for a in (scenario.unconfirmed_assumptions if scenario else [])] + list(extra)
        listed = ", ".join(sorted(names)) if names else f"{ctx.case.unconfirmed} value(s)"
        return f"The plan depends on unconfirmed assumptions: {listed}."
    return None


def decide_approval(
    *,
    change_set: ChangeSet | None = None,
    change_sets: Sequence[ChangeSet] = (),
    scenario: DisruptionScenario | None,
    case_mode: str,
    initiator: Actor | None,
    config: PolicyConfig,
    extra_unconfirmed: Sequence[str] = (),
    origin: RunOrigin = "chat",
    tainted: bool = False,
    option: Mapping[str, Any] | None = None,
    value_cents: int = 0,
    allow_auto: bool = True,
    matches: Callable[[PolicyRule, PolicyContext], bool] | None = None,
) -> ApprovalDecision:
    """Evaluate the tenant's rules for a change set (or bundle) → :class:`ApprovalDecision`.

    Raises :class:`PolicyEvaluationError` when a rule cannot be evaluated (fail closed)."""
    members = [*([change_set] if change_set is not None else []), *change_sets]
    if not members:
        raise ValueError("decide_approval needs at least one change set")
    ctx = build_context(
        change_sets=members, scenario=scenario, case_mode=case_mode,
        initiator_roles=initiator.roles if initiator is not None else (), origin=origin, tainted=tainted,
        option=option, value_cents=value_cents, extra_unconfirmed=extra_unconfirmed,
    )
    rules = policy_set(config)
    fallback = fallback_requirement(config)
    if matches is None:
        activation = ctx.activation()
        decision = rules.decide(ctx, lambda r: eval_when(r.when, activation, rule_id=r.id),
                                fallback=fallback, allow_auto=allow_auto)
    else:
        decision = rules.decide(ctx, lambda r: matches(r, ctx), fallback=fallback, allow_auto=allow_auto)
    if decision.outcome == "refuse":
        reason = _refuse_reason(ctx, scenario, extra_unconfirmed) or decision.reason
        return ApprovalDecision("refuse", reason, decision.policy_version, decision.rule_id, None, ctx)
    if decision.outcome == "auto":
        reason = f"Within the auto-approval policy {decision.rule_id}@{decision.policy_version}: {decision.reason}."
        return ApprovalDecision("auto", reason, decision.policy_version, decision.rule_id, None, ctx)
    assert decision.requirement is not None
    families = sorted({HUMAN_ONLY_OPS[op] for op in ctx.cs.ops if op in HUMAN_ONLY_OPS})
    parts = [decision.reason.rstrip(".") + "."]
    if families:
        parts.append(f"Includes {', '.join(families)} changes, which always need a person with the "
                     f"{config.approver_role} role.")
    parts.append(requirement_text(decision.requirement))
    return ApprovalDecision("human", " ".join(parts), decision.policy_version, decision.rule_id,
                            decision.requirement, ctx)
