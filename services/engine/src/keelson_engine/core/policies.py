"""Approval policy rules: defaults, storage in the agent spec, admin guards and the evaluator port
(``docs/contracts/95-m5-remediation.md`` §1).

**Where the rules live.** The versioned agent spec carries ``policy.version`` + ``policy.rules``
(``[keelson_contracts.policy.PolicyRule]``); a spec without rules uses :data:`DEFAULT_POLICY`
(:func:`effective_policy` — the runtime and the admin API agree on it). ``PUT
/api/admin/policies`` stores a new rule set as a new spec version (``agent_specs.put_spec``,
optimistic concurrency on the spec version) with a new policy version (receipts name it:
``approved_by: policy:<rule_id>@<policy version>``).

**Evaluator port.** CEL evaluation lives in the runtime (``runtime/policy.py``, cel-expr-python).
The platform calls it only through :func:`evaluate`, which dispatches to the function registered
with :func:`register_policy_evaluator`::

    evaluate_policy(rules: PolicySet, context: PolicyContext) -> PolicyDecision   # sync or async

It must implement first-match-wins with the hard floors (``PolicySet.decide``) and **raise** on a
CEL compile/evaluation error (fail closed). engine-api registers
``keelson_engine.runtime.policy.evaluate_policy`` at startup (:func:`install_runtime_evaluator`,
also tried lazily on first use); without it the test bench answers 503 and rules are stored
without the compile check.

**Guards** (:func:`guarded_changes` → ``[{field, reason, code}]`` with ``code`` an M7
``GuardReasonCode``; a tenant-admin's guarded change becomes a four-eyes change request, M7 —
previously 403 ``guarded_change``; platform-admins apply directly): anything that
could make more change sets auto-approved or fewer change sets refused / approved by fewer or
different people — a new auto rule, an auto rule whose condition got wider (raised caps) or that
moved ahead of another rule, a refuse rule removed / narrowed / weakened, an approval rule
removed / narrowed / weakened (fewer approvers, other roles, initiator no longer excluded), and
a new, moved or widened approval rule that now shadows a refuse rule or a stronger approval rule
after it. Lowering auto
caps, removing auto rules, adding refuse rules, tightening requirements and editing
descriptions are tenant-admin changes.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, get_args

from pydantic import BaseModel, ValidationError

from keelson_contracts.policy import (
    PolicyCase,
    PolicyChangeSet,
    PolicyContext,
    PolicyDecision,
    PolicyInitiator,
    PolicyRule,
    PolicySet,
    Requirement,
)
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.logs import get_logger

__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_POLICY_VERSION",
    "DEFAULT_RULES",
    "FALLBACK_REQUIREMENT",
    "PolicyEvaluationError",
    "PolicyEvaluator",
    "PolicyEvaluatorUnavailable",
    "condition_change",
    "effective_policy",
    "evaluate",
    "evaluator_available",
    "get_policies",
    "guarded_changes",
    "install_runtime_evaluator",
    "next_version",
    "put_policies",
    "register_policy_evaluator",
    "requirement_at_least",
    "sample_context",
    "spec_supports_policy_rules",
    "test_policy",
]

log = get_logger(__name__)

DEFAULT_POLICY_VERSION = "p-1"
"""``AgentSpec.policy.version`` default (``runtime.spec.PolicyConfig``)."""
FALLBACK_REQUIREMENT = Requirement(roles=["approver"], count=1, exclude_initiator=False)
"""What applies when no rule matches (``PolicySet.decide(fallback=…)``)."""

_FOUR_EYES = Requirement(roles=["approver", "compliance"], count=2, exclude_initiator=True)

DEFAULT_RULES: tuple[PolicyRule, ...] = (
    PolicyRule(
        id="refuse-unconfirmed",
        description="What-if cases and plans that rely on unconfirmed values are never committed",
        when='case.mode == "what_if" || case.unconfirmed > 0',
        require="refuse",
        priority=10,
    ),
    PolicyRule(
        id="four-eyes",
        description="Large or structural changes need two different people (an approver and compliance)",
        when=('cs.incremental_cost_cents > 1000000 || '
              'cs.ops.exists(o, o in ["po.create", "po_line.cancel", "production_order.resequence"])'),
        require=_FOUR_EYES,
        priority=20,
    ),
    PolicyRule(
        id="human-untrusted",
        description="Changes from a signal or webhook, or from untrusted input, always need an approver",
        when='case.origin != "chat" || case.tainted',
        require=FALLBACK_REQUIREMENT,
        priority=30,
    ),
    PolicyRule(
        id="auto-small-transfer",
        description="Small transfer and allocation changes started by a planner are applied automatically",
        when=('cs.ops.all(o, o in ["allocation.set_qty", "transfer_order.create"]) && cs.lines <= 10 && '
              'cs.units <= 500 && initiator.roles.exists(r, r == "planner")'),
        require="auto",
        priority=40,
    ),
    PolicyRule(
        id="default",
        description="Changes need approval by someone with the approver role",
        when="true",
        require=FALLBACK_REQUIREMENT,
        priority=50,
    ),
)
"""Doc §1 rules 1–5 (the same set ``runtime.policy.default_rules`` builds from the default
``PolicyConfig``; the pack ``value-fashion-eu`` stores it explicitly as ``p-2``). Rule 2 (never auto
for non-chat/tainted) is ordered **after** four-eyes so a webhook re-plan that creates POs still
needs both people (first match wins); ``PolicySet.decide`` never auto-approves such runs anyway."""

DEFAULT_POLICY = PolicySet(version=DEFAULT_POLICY_VERSION, rules=list(DEFAULT_RULES))


# ───────────────────────────── evaluator port ─────────────────────────────

PolicyEvaluator = Callable[[PolicySet, PolicyContext], PolicyDecision | Awaitable[PolicyDecision]]
RuleChecker = Callable[[str], list[str]]
"""Static check of one CEL ``when`` → problems (``[]`` = fine): ``runtime.policy.check_rule``."""
_evaluator: PolicyEvaluator | None = None
_checker: RuleChecker | None = None
_install_tried = False


class PolicyEvaluatorUnavailable(RuntimeError):
    """No CEL evaluator is registered in this process."""


class PolicyEvaluationError(ValueError):
    """The evaluator failed (CEL compile/evaluation error): fail closed."""

    def __init__(self, message: str, *, rule_id: str | None = None) -> None:
        super().__init__(message)
        self.rule_id = rule_id


def register_policy_evaluator(fn: PolicyEvaluator | None, check: RuleChecker | None = None) -> None:
    """Install the CEL evaluator (the runtime's ``evaluate_policy``) and optionally its static rule
    check (``check_rule``); ``None`` removes them."""
    global _evaluator, _checker, _install_tried
    _evaluator = fn
    _checker = check if fn is not None else None
    _install_tried = fn is not None


def install_runtime_evaluator() -> bool:
    """Register ``keelson_engine.runtime.policy.evaluate_policy`` when the runtime provides it."""
    global _install_tried
    _install_tried = True
    if _evaluator is not None:
        return True
    try:
        from keelson_engine.runtime import policy as runtime_policy
    except Exception as exc:  # noqa: BLE001 - optional at import time (e.g. cel library missing)
        log.warning("policy evaluator unavailable", error=f"{type(exc).__name__}: {exc}")
        return False
    fn = getattr(runtime_policy, "evaluate_policy", None)
    if not callable(fn):
        log.warning("policy evaluator unavailable", error="runtime.policy has no evaluate_policy")
        return False
    check = getattr(runtime_policy, "check_rule", None)
    register_policy_evaluator(fn, check=check if callable(check) else None)
    log.info("policy evaluator registered", evaluator=f"{fn.__module__}.{fn.__qualname__}")
    return True


def evaluator_available() -> bool:
    if _evaluator is None and not _install_tried:
        install_runtime_evaluator()
    return _evaluator is not None


async def evaluate(rules: PolicySet, context: PolicyContext) -> PolicyDecision:
    """Run the registered evaluator; raises :class:`PolicyEvaluatorUnavailable` /
    :class:`PolicyEvaluationError` (never a silent match)."""
    if not evaluator_available():
        raise PolicyEvaluatorUnavailable("no policy evaluator is registered")
    assert _evaluator is not None
    try:
        result = _evaluator(rules, context)
        if inspect.isawaitable(result):
            result = await result
    except (PolicyEvaluatorUnavailable, PolicyEvaluationError):
        raise
    except Exception as exc:  # noqa: BLE001 - CEL errors etc.: fail closed with a short message
        raise PolicyEvaluationError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    if not isinstance(result, PolicyDecision):
        try:
            result = PolicyDecision.model_validate(result)
        except ValidationError as exc:
            raise PolicyEvaluationError("the evaluator returned an invalid decision") from exc
    # Defence in depth: the hard floors hold whatever the registered evaluator answered.
    if context.must_refuse and result.outcome != "refuse":
        log.error("policy evaluator ignored the refuse floor", rule_id=result.rule_id)
        return PolicyDecision(outcome="refuse", rule_id=None, policy_version=result.policy_version,
                              requirement=None,
                              reason="What-if cases and plans with unconfirmed values are never committed")
    if result.outcome == "auto" and not context.auto_allowed:
        log.error("policy evaluator ignored the never-auto floor", rule_id=result.rule_id)
        raise PolicyEvaluationError("the evaluator auto-approved a context that must never be auto-approved",
                                    rule_id=result.rule_id)
    return result


# ───────────────────────────── effective policy ─────────────────────────────


def _runtime_policy_set(section: Mapping[str, Any]) -> PolicySet | None:
    """``runtime.policy.policy_set(PolicyConfig)``: the defaults built from the section's caps and
    role names (``None`` when the runtime is not importable)."""
    try:
        from keelson_engine.runtime.policy import policy_set
        from keelson_engine.runtime.spec import PolicyConfig
    except Exception:  # noqa: BLE001 - platform-only processes/tests
        return None
    try:
        result = policy_set(PolicyConfig.model_validate(dict(section)))
    except Exception:  # noqa: BLE001 - an older stored section: the platform defaults apply
        return None
    return result if isinstance(result, PolicySet) else None


def effective_policy(policy_section: Mapping[str, Any] | None) -> tuple[PolicySet, Literal["spec", "default"]]:
    """The rule set a spec's ``policy`` section stands for: its ``rules`` (with its ``version``) or,
    without rules, the defaults (``runtime.policy.policy_set`` of the section when the runtime is
    importable — the same rules the worker applies — else :data:`DEFAULT_RULES`). Raises
    ``ValidationError`` for invalid stored rules."""
    section = dict(policy_section or {})
    rules = section.get("rules")
    version = str(section.get("version") or DEFAULT_POLICY_VERSION)
    if rules:
        return PolicySet.model_validate({"version": version, "rules": list(rules)}), "spec"
    runtime = _runtime_policy_set(section)
    if runtime is not None:
        return runtime, "default"
    return PolicySet(version=version, rules=list(DEFAULT_RULES)), "default"


def _unwrap_model(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        found = _unwrap_model(arg)
        if found is not None:
            return found
    return None


def spec_supports_policy_rules() -> bool:
    """True when the runtime ``AgentSpec.policy`` model declares ``rules`` (M5)."""
    from keelson_engine.core import agent_specs

    field = agent_specs._model().model_fields.get("policy")
    if field is None:
        return False
    model = _unwrap_model(field.annotation)
    return model is not None and "rules" in model.model_fields


def strip_unsupported_policy(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Pack ``spec_overrides`` without ``policy.rules`` while the runtime spec model cannot store
    them (pre-M5 builds): applying a pack must not fail on the M5 default rules."""
    out = dict(overrides)
    policy = out.get("policy")
    if isinstance(policy, dict) and "rules" in policy and not spec_supports_policy_rules():
        trimmed = {k: v for k, v in policy.items() if k != "rules"}
        if trimmed:
            out["policy"] = trimmed
        else:
            out.pop("policy")
        log.warning("policy rules in pack overrides ignored: the agent spec model has no policy.rules")
    return out


def next_version(version: str) -> str:
    """``p-2`` → ``p-3``; ``v`` → ``v-2``."""
    m = re.match(r"^(.*?)(\d+)$", version)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + 1}"
    return f"{version}-2"


# ───────────────────────────── guards ─────────────────────────────

_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\d+|[A-Za-z_][A-Za-z0-9_.]*|<=|>=|==|!=|&&|\|\||\S')
_CMP = {"<", "<=", ">", ">="}
_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}

ConditionChange = Literal["same", "wider", "narrower", "unknown"]


def condition_change(old: str, new: str) -> ConditionChange:
    """How a CEL condition changed when only numeric literals in comparisons differ: ``wider``
    (true for more inputs), ``narrower``, ``same``; anything else is ``unknown``.

    ``x <= 10`` → ``x <= 20`` is wider, ``x > 1000000`` → ``x > 500000`` is wider. A negation
    (``!``), ``!=`` / ``==`` or arithmetic next to a changed number makes it ``unknown``."""
    a, b = _TOKEN_RE.findall(old), _TOKEN_RE.findall(new)
    if a == b:
        return "same"
    if len(a) != len(b):
        return "unknown"
    directions: set[str] = set()
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        if x == y:
            continue
        if not (x.isdigit() and y.isdigit()):
            return "unknown"
        if "!" in a:
            return "unknown"
        prev = a[i - 1] if i > 0 else ""
        nxt = a[i + 1] if i + 1 < len(a) else ""
        if prev in _CMP:
            op = prev
        elif nxt in _CMP:
            op = _FLIP[nxt]
        else:
            return "unknown"
        if (i >= 2 and a[i - 1] == "-") or prev in ("-", "+", "*", "/", "%"):
            return "unknown"
        grew = int(y) > int(x)
        # x <= N / x < N: a larger N admits more; x >= N / x > N: a smaller N admits more
        directions.add("wider" if grew == (op in ("<", "<=")) else "narrower")
    if len(directions) != 1:
        return "unknown"
    return "wider" if "wider" in directions else "narrower"


def requirement_at_least(a: Requirement, b: Requirement) -> bool:
    """``a`` is at least as strict as ``b`` (every quorum satisfying ``a`` satisfies ``b``)."""
    if a.count < b.count or (b.exclude_initiator and not a.exclude_initiator):
        return False
    if b.every_role:
        return a.every_role and set(b.roles) <= set(a.roles)
    return set(a.roles) <= set(b.roles)


def guarded_changes(old: PolicySet, new: PolicySet) -> list[dict[str, str]]:
    """Changes only a platform-admin may make (see module docstring): ``[{field, reason}]``."""
    out: list[dict[str, str]] = []
    old_by = {r.id: r for r in old.rules}
    new_by = {r.id: r for r in new.rules}

    def add(rule_id: str, reason: str, code: str = "policy_rule_weakened") -> None:
        out.append({"field": f"policy.rules.{rule_id}", "reason": reason, "code": code})

    def newly_preceded(r: PolicyRule) -> list[PolicyRule]:
        before = old_by.get(r.id)
        found = []
        for s in new.rules:
            if s.id == r.id or s.id not in old_by or r.priority >= s.priority:
                continue
            if before is not None and before.priority < old_by[s.id].priority:
                continue  # it already came first
            found.append(s)
        return found

    for o in old.rules:
        if o.id not in new_by and o.outcome != "auto":
            add(o.id, "removes a refuse rule" if o.outcome == "refuse" else "removes an approval rule",
                "policy_rule_removed")

    for r in new.rules:
        o = old_by.get(r.id)
        change = condition_change(o.when, r.when) if o is not None else "unknown"
        if r.outcome == "auto":
            if o is None or o.outcome != "auto":
                add(r.id, "adds an automatic-approval rule", "auto_rule_added")
            elif change in ("wider", "unknown"):
                add(r.id, "raises the automatic-approval caps", "auto_cap_raised")
            if newly_preceded(r):
                add(r.id, "moves an automatic-approval rule ahead of other rules", "auto_rule_added")
            continue
        if o is not None:
            if o.outcome == "refuse" and r.outcome != "refuse":
                add(r.id, "turns a refuse rule into an approval rule")
            elif o.outcome == r.outcome and change in ("narrower", "unknown"):
                add(r.id, "narrows when the rule applies",
                    "approval_threshold_raised" if r.outcome == "human" else "policy_rule_weakened")
            elif (o.outcome == "human" and r.outcome == "human" and isinstance(o.require, Requirement)
                  and isinstance(r.require, Requirement) and not requirement_at_least(r.require, o.require)):
                add(r.id, "weakens the approval requirement")
        if r.outcome == "human" and isinstance(r.require, Requirement):
            if o is not None and o.outcome == "human" and change == "wider":
                # a wider condition takes more change sets away from the stricter rules after it
                for s in new.rules:
                    if s.id == r.id or s.priority <= r.priority:
                        continue
                    if s.outcome == "refuse" or (isinstance(s.require, Requirement)
                                                 and not requirement_at_least(r.require, s.require)):
                        add(r.id, f"widens a rule that takes precedence over the stricter rule {s.id}")
                        break
            for s in newly_preceded(r):
                if s.outcome == "refuse":
                    add(r.id, f"would take precedence over the refuse rule {s.id}")
                elif isinstance(s.require, Requirement) and not requirement_at_least(r.require, s.require):
                    add(r.id, f"would take precedence over the stricter rule {s.id}")
    return out


# ───────────────────────────── admin API helpers ─────────────────────────────


def sample_context() -> PolicyContext:
    """A committable chat context (every rule kind is evaluated; used for the compile check)."""
    return PolicyContext(
        cs=PolicyChangeSet(lines=1, units=1, value_cents=0, ops=["transfer_order.create"], connectors=["merxa"],
                           incremental_cost_cents=0, lost_units_after=0, levers=["reallocate_inventory"]),
        case=PolicyCase(mode="actual", origin="chat", tainted=False, unconfirmed=0),
        initiator=PolicyInitiator(roles=["planner"]),
    )


async def check_rules_compile(policy: PolicySet) -> bool:
    """CEL errors surface at save time (422 ``policy_invalid`` with per-rule ``errors``): the
    registered static check (``check_rule``) per rule, else each rule evaluated on its own over
    :func:`sample_context`. Returns False when no evaluator is available (not checked)."""
    if not evaluator_available():
        return False
    if _checker is not None:
        errors = []
        for rule in policy.rules:
            try:
                problems = [str(p) for p in _checker(rule.when)]
            except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check
                problems = [f"{type(exc).__name__}: {str(exc)[:200]}"]
            errors.extend({"loc": f"rules.{rule.id}.when", "msg": p[:300]} for p in problems)
        if errors:
            raise AdminError(422, "policy_invalid", "some policy conditions are not valid CEL over the policy context",
                             errors=errors[:20])
        return True
    ctx = sample_context()
    for rule in policy.rules:
        single = PolicySet(version=policy.version, rules=[rule])
        try:
            await evaluate(single, ctx)
        except PolicyEvaluationError as exc:
            raise AdminError(422, "policy_invalid", f"rule {rule.id!r} cannot be evaluated: {exc}",
                             rule_id=rule.id) from exc
    return True


def _policy_view(spec: Mapping[str, Any]) -> tuple[PolicySet, str]:
    try:
        return effective_policy(spec.get("policy") if isinstance(spec, Mapping) else None)
    except ValidationError as exc:
        log.warning("stored policy rules are invalid; showing the defaults", error=str(exc)[:300])
        return DEFAULT_POLICY, "default"


async def get_policies(tenant: str) -> dict[str, Any]:
    """``GET /api/admin/policies``."""
    from keelson_engine.core import agent_specs

    current = await agent_specs.latest(tenant)
    policy, source = _policy_view(current["spec"])
    return {
        "spec_version": current["version"],
        "policy": policy.model_dump(mode="json"),
        "source": source,
        "editable": spec_supports_policy_rules(),
        "evaluator_available": evaluator_available(),
        "fallback": FALLBACK_REQUIREMENT.model_dump(mode="json"),
        "defaults": DEFAULT_POLICY.model_dump(mode="json"),
    }


async def put_policies(tenant: str, base_version: str, rules: Sequence[Mapping[str, Any] | PolicyRule],
                       actor: dict[str, Any], *, platform_admin: bool, version: str | None = None,
                       cosigned: bool = False) -> tuple[dict[str, Any], bool]:
    """``PUT /api/admin/policies``: validate, guard, compile-check and store as a new spec version.
    Returns ``(envelope, created)``. M7: a guarded change by a tenant-admin raises
    ``change_requests.GuardedChange`` (kind ``policy_set``) unless ``cosigned``."""
    from keelson_engine.core import agent_specs

    if not spec_supports_policy_rules():
        raise AdminError(409, "policy_rules_unsupported",
                         "this engine build cannot store policy rules yet (AgentSpec.policy.rules is missing)")
    current = await agent_specs.latest(tenant)
    if current["version"] != base_version:
        raise AdminError(409, "version_conflict", "the spec changed since you loaded it",
                         latest_version=current["version"])
    old, _source = _policy_view(current["spec"])
    raw_rules = [r.model_dump(mode="json") if isinstance(r, PolicyRule) else dict(r) for r in rules]
    try:
        draft = PolicySet.model_validate({"version": version or old.version, "rules": raw_rules})
    except ValidationError as exc:
        errors = [{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", ""))[:200]}
                  for e in exc.errors()[:20]]
        raise AdminError(422, "policy_invalid", "the policy rules are invalid", errors=errors) from exc
    same_rules = [r.model_dump(mode="json") for r in draft.rules] == [r.model_dump(mode="json") for r in old.rules]
    if same_rules and (version is None or version == old.version):
        policy = old
    else:
        if version is None:
            new_version = next_version(old.version) if not same_rules else old.version
        elif version == old.version and not same_rules:
            raise AdminError(422, "policy_version_unchanged",
                             "changed rules need a new policy version (omit version to bump it)")
        else:
            new_version = version
        policy = PolicySet(version=new_version, rules=draft.rules)
    guarded = guarded_changes(old, policy)
    compiled = await check_rules_compile(policy)
    patch = {"policy": {"version": policy.version, "rules": [r.model_dump(mode="json") for r in policy.rules]}}
    if guarded and not (platform_admin or cosigned):
        from keelson_engine.core.change_requests import GuardedChange, json_diff

        raise GuardedChange(
            tenant=tenant, actor=actor, kind="policy_set", target="policy_set",
            reasons=[{"code": g.get("code") or "policy_rule_weakened", "field": g["field"][:200],
                      "reason": g["reason"][:300]} for g in guarded],
            payload={"rules": raw_rules, "version": version},
            diff=json_diff(old.model_dump(mode="json"), policy.model_dump(mode="json"), prefix=("policy",)),
            base_version=base_version,
            message="raising automatic-approval caps or removing / weakening refuse and approval rules needs a "
                    "second admin")
    envelope, created = await agent_specs.put_spec(tenant, base_version, patch, actor, platform_admin=platform_admin,
                                                   cosigned=cosigned,
                                                   guard_kind="policy_set",
                                                   guard_payload={"rules": raw_rules, "version": version})
    view, source = _policy_view(envelope["spec"])
    return {
        "spec_version": envelope["version"],
        "policy": view.model_dump(mode="json"),
        "source": source,
        "compiled": compiled,
        "guarded": guarded,
    }, created


async def test_policy(tenant: str, context: Mapping[str, Any] | PolicyContext, *,
                      rules: Sequence[Mapping[str, Any]] | None = None, version: str | None = None
                      ) -> dict[str, Any]:
    """``POST /api/admin/policies/test``: evaluate the current (or a draft) rule set."""
    from keelson_engine.core import agent_specs

    try:
        ctx = context if isinstance(context, PolicyContext) else PolicyContext.model_validate(dict(context))
    except ValidationError as exc:
        errors = [{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", ""))[:200]}
                  for e in exc.errors()[:20]]
        raise AdminError(422, "context_invalid", "the sample context is invalid", errors=errors) from exc
    if rules is None:
        current = await agent_specs.latest(tenant)
        policy, _ = _policy_view(current["spec"])
        if version:
            policy = PolicySet(version=version, rules=policy.rules)
    else:
        try:
            policy = PolicySet.model_validate({"version": version or "draft", "rules": list(rules)})
        except ValidationError as exc:
            errors = [{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", ""))[:200]}
                      for e in exc.errors()[:20]]
            raise AdminError(422, "policy_invalid", "the policy rules are invalid", errors=errors) from exc
    try:
        decision = await evaluate(policy, ctx)
    except PolicyEvaluatorUnavailable as exc:
        raise AdminError(503, "policy_evaluator_unavailable", "the policy evaluator is not available") from exc
    except PolicyEvaluationError as exc:
        raise AdminError(422, "policy_invalid", f"the rules cannot be evaluated: {exc}") from exc
    matched = policy.rule(decision.rule_id) if decision.rule_id else None
    return {
        "policy_version": policy.version,
        "decision": decision.model_dump(mode="json"),
        "approved_by": decision.approved_by,
        "matched_rule": matched.model_dump(mode="json") if matched else None,
        "floors": {"must_refuse": ctx.must_refuse, "auto_allowed": ctx.auto_allowed},
    }
