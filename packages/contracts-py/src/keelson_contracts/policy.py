"""Approval policy rules, their evaluation context and decision, and interrupt quorums (M5).

``docs/contracts/95-m5-remediation.md`` §1. ``AgentSpec.policy.rules`` is a list of
:class:`PolicyRule`::

    {id, description, when: <CEL>, require: {roles, count, exclude_initiator} | "auto" | "refuse",
     priority}

The engine (``runtime/policy.py``) evaluates the CEL ``when`` expressions over a
:class:`PolicyContext` (``cs`` / ``case`` / ``initiator``; :meth:`PolicyContext.activation` is
the CEL activation) in :meth:`PolicySet.ordered` order (ascending ``priority``, first match
wins) and returns a :class:`PolicyDecision`; :meth:`PolicySet.decide` implements that order
with two hard floors no rule set can lift: a what-if case or one with unconfirmed values is
refused, and ``"auto"`` rules only apply to a person's chat run on untainted input
(:attr:`PolicyContext.auto_allowed`). This package does not parse CEL (it depends on pydantic
only); it checks the shape, the ids and that priorities are unambiguous.

Quorum (four-eyes) semantics, shared by the API and the approval card
(:class:`QuorumView`):

* every approval is by a different person (distinct ``sub``) and is counted under one role
  of ``required.roles``;
* when ``count >= len(roles)`` every role must be covered by at least one approval
  ("one of each role"; e.g. ``{roles: [approver, compliance], count: 2}`` is Marco *and*
  Lena, never two approvers); otherwise any of the roles will do;
* ``remaining = max(count - approvals, roles still missing)`` and the quorum is met when it
  is 0. :func:`quorum_assign` counts recorded approvals (with the roles each person held)
  by maximum matching, so someone holding both roles never blocks a later single-role
  approver; :func:`quorum_role_for` is the one-step check for a new approval (or ``None``
  when it cannot advance the quorum). The excluded initiator never counts.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, model_validator

from .common import CaseMode, Cents, ContractModel, NonNegInt

__all__ = [
    "MAX_POLICY_RULES",
    "MAX_QUORUM_COUNT",
    "MAX_WHEN_CHARS",
    "POLICY_OUTCOMES",
    "POLICY_RULE_ID_PATTERN",
    "PolicyCase",
    "PolicyChangeSet",
    "PolicyContext",
    "PolicyDecision",
    "PolicyInitiator",
    "PolicyOutcome",
    "PolicyRule",
    "PolicySet",
    "QuorumApproval",
    "QuorumCandidate",
    "QuorumView",
    "Requirement",
    "RoleName",
    "RunOrigin",
    "quorum_assign",
    "quorum_missing_roles",
    "quorum_remaining",
    "quorum_role_for",
]

POLICY_RULE_ID_PATTERN: Final = r"^[a-z][a-z0-9_-]*$"
MAX_WHEN_CHARS: Final = 2000
"""Longest CEL ``when`` expression accepted (the admin editor and the evaluator agree on it)."""
MAX_POLICY_RULES: Final = 50
MAX_QUORUM_COUNT: Final = 10

PolicyOutcome = Literal["auto", "human", "refuse"]
POLICY_OUTCOMES: Final = ("auto", "human", "refuse")
RunOrigin = Literal["chat", "signal", "webhook"]
"""Where the run that produced the change set came from (``chat`` = a person in the thread)."""

NonEmptyStr = Annotated[str, Field(min_length=1)]
RoleName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
RuleId = Annotated[str, Field(min_length=1, max_length=64, pattern=POLICY_RULE_ID_PATTERN)]
OpName = Annotated[str, Field(min_length=1, max_length=128)]


def _unique(values: Sequence[Any], what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must be unique")


# ─────────────────────────────── requirement ────────────────────────────────


class Requirement(ContractModel):
    """Who must approve: ``count`` distinct people holding ``roles`` (see module docstring).

    ``exclude_initiator`` = the person who initiated the change set may not approve it
    (enforced server-side by the decision API). Also the ``interrupt.quorum_spec`` shape."""

    roles: Annotated[list[RoleName], Field(min_length=1, max_length=MAX_QUORUM_COUNT)]
    count: Annotated[StrictInt, Field(ge=1, le=MAX_QUORUM_COUNT)]
    exclude_initiator: StrictBool

    @model_validator(mode="after")
    def _check(self) -> Requirement:
        _unique(self.roles, "requirement roles")
        return self

    @property
    def every_role(self) -> bool:
        """``True`` when each role must be covered ("one of each role")."""
        return self.count >= len(self.roles)


def quorum_missing_roles(required: Requirement, approval_roles: Iterable[str]) -> list[str]:
    """Roles (in ``required.roles`` order) still uncovered; always ``[]`` for any-of quorums."""
    if not required.every_role:
        return []
    covered = set(approval_roles)
    return [r for r in required.roles if r not in covered]


def quorum_remaining(required: Requirement, approval_roles: Sequence[str]) -> int:
    """Approvals still needed: ``max(count - len(approvals), missing roles)``."""
    return max(required.count - len(approval_roles), len(quorum_missing_roles(required, approval_roles)), 0)


def _counted_roles(
    required: Requirement,
    candidates: Sequence[tuple[str, Iterable[str]]],
    initiator_sub: str | None,
) -> list[str | None]:
    """The role each candidate's approval counts under (``None`` = it does not count).

    ``candidates`` = ``[(sub, roles held), …]`` in approval order. A later duplicate ``sub``,
    the excluded initiator and people holding none of ``required.roles`` never count. For a
    "one of each role" quorum the roles are assigned by maximum bipartite matching (people in
    approval order, roles in ``required.roles`` order), so a person holding several required
    roles never blocks a later approver who holds only one of them; approvals beyond the
    matching count only while they still reduce ``remaining``. The assignment is stable: adding
    a later candidate never un-counts or changes whether an earlier one counts."""
    roles = list(required.roles)
    seen: set[str] = set()
    eligible: list[list[str] | None] = []
    for sub, held_roles in candidates:
        held = set(held_roles)
        ok = (
            sub not in seen
            and not (required.exclude_initiator and initiator_sub is not None and sub == initiator_sub)
        )
        seen.add(sub)
        usable = [r for r in roles if r in held] if ok else []
        eligible.append(usable or None)

    out: list[str | None] = [None] * len(candidates)
    if not required.every_role:
        n = 0
        for i, options in enumerate(eligible):
            if options is not None and n < required.count:
                out[i] = options[0]
                n += 1
        return out

    owner: dict[str, int] = {}  # role -> candidate index (the matching)

    def augment(i: int, visited: set[str]) -> bool:
        for r in eligible[i] or []:
            if r in visited:
                continue
            visited.add(r)
            if r not in owner or augment(owner[r], visited):
                owner[r] = i
                return True
        return False

    for i, options in enumerate(eligible):
        if options is not None:
            augment(i, set())
    for r, i in owner.items():
        out[i] = r
    matched = len(owner)
    missing = len(roles) - matched
    counted = matched
    for i, options in enumerate(eligible):
        if options is not None and out[i] is None and required.count - counted > missing:
            out[i] = options[0]
            counted += 1
    return out


def quorum_assign(
    required: Requirement,
    candidates: Sequence[QuorumCandidate],
    *,
    initiator_sub: str | None,
) -> tuple[QuorumView, list[str]]:
    """Count recorded approvals towards ``required``: the :class:`QuorumView` (approvals in
    approval order, each under the role it is assigned) and the subs that do not count.

    Use this with the roles each approver held when approving (the decision API refuses a new
    approval that would not count). ``initiator_sub`` is required: pass ``None`` only when the
    change set has no human initiator (a signal/webhook run)."""
    roles = _counted_roles(required, [(c.sub, c.roles) for c in candidates], initiator_sub)
    counted = [
        QuorumApproval(sub=c.sub, name=c.name, role=r, at=c.at)
        for c, r in zip(candidates, roles, strict=True)
        if r is not None
    ]
    rejected = [c.sub for c, r in zip(candidates, roles, strict=True) if r is None]
    return QuorumView.build(required, counted), rejected


def quorum_role_for(
    required: Requirement,
    approvals: Sequence[QuorumApproval],
    *,
    sub: str,
    roles: Iterable[str],
    initiator_sub: str | None,
) -> str | None:
    """The role under which ``sub`` (holding ``roles``) can approve now, or ``None``.

    ``None`` when the person already approved, is the excluded initiator, holds none of the
    required roles, the quorum is already met, or every remaining approval is reserved for a
    role they do not hold (a second ``approver`` cannot stand in for ``compliance``). A role
    still missing is preferred over an already covered one. Earlier ``approvals`` are taken
    with their recorded role only; when approvers may hold several required roles, use
    :func:`quorum_assign` with the roles each one held (it can re-assign earlier approvals).
    ``initiator_sub`` is required (``None`` only for runs without a human initiator)."""
    candidates: list[tuple[str, Iterable[str]]] = [(a.sub, (a.role,)) for a in approvals]
    candidates.append((sub, tuple(roles)))
    assigned = _counted_roles(required, candidates, initiator_sub)
    current = [a.role for a in approvals]
    if quorum_remaining(required, current) == 0:
        return None
    return assigned[-1]


# ───────────────────────────────── rules ────────────────────────────────────


class PolicyRule(ContractModel):
    """One approval rule. ``when`` is a CEL expression over :class:`PolicyContext`;
    ``require`` is a quorum, ``"auto"`` (commit under the autocommit principal, receipt
    ``approved_by: policy:<id>@<version>``) or ``"refuse"`` (no change set is compiled)."""

    id: RuleId
    description: NonEmptyStr
    when: Annotated[str, Field(min_length=1, max_length=MAX_WHEN_CHARS)]
    require: Requirement | Literal["auto", "refuse"]
    priority: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def _check(self) -> PolicyRule:
        if not self.when.strip():
            raise ValueError("when must be a CEL expression")
        if any(ord(ch) < 0x20 and ch not in "\t\n\r" for ch in self.when):
            raise ValueError("when must not contain control characters")
        return self

    @property
    def outcome(self) -> PolicyOutcome:
        if isinstance(self.require, Requirement):
            return "human"
        return self.require


class PolicySet(ContractModel):
    """The versioned rule set (``GET/PUT /api/admin/policies``; part of the agent spec)."""

    version: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    rules: Annotated[list[PolicyRule], Field(min_length=1, max_length=MAX_POLICY_RULES)]

    @model_validator(mode="after")
    def _check(self) -> PolicySet:
        _unique([r.id for r in self.rules], "rule ids")
        _unique([r.priority for r in self.rules], "rule priorities")
        return self

    def ordered(self) -> list[PolicyRule]:
        """Evaluation order: ascending ``priority`` (first match wins)."""
        return sorted(self.rules, key=lambda r: r.priority)

    def rule(self, rule_id: str) -> PolicyRule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def decide(
        self,
        ctx: PolicyContext,
        matches: Callable[[PolicyRule], bool],
        *,
        fallback: Requirement,
        allow_auto: bool = True,
    ) -> PolicyDecision:
        """First-match-wins evaluation with the hard floors applied.

        ``matches(rule)`` evaluates the rule's CEL ``when`` over ``ctx.activation()`` (the
        engine's evaluator; it should raise on an evaluation error so the caller fails closed).
        ``"auto"`` rules are skipped (not evaluated) unless ``allow_auto`` and
        :attr:`PolicyContext.auto_allowed` (rollbacks pass ``allow_auto=False``: always human).
        When no rule matches, ``fallback`` applies (``rule_id`` ``None``). Whatever matched, a
        context that :attr:`~PolicyContext.must_refuse` is refused."""
        auto_ok = allow_auto and ctx.auto_allowed
        decision: PolicyDecision | None = None
        for rule in self.ordered():
            if rule.outcome == "auto" and not auto_ok:
                continue
            if matches(rule):
                decision = PolicyDecision.from_rule(rule, policy_version=self.version)
                break
        if ctx.must_refuse and (decision is None or decision.outcome != "refuse"):
            return PolicyDecision(
                outcome="refuse",
                rule_id=None,
                policy_version=self.version,
                requirement=None,
                reason="What-if cases and plans with unconfirmed values are never committed",
            )
        if decision is None:
            return PolicyDecision(
                outcome="human",
                rule_id=None,
                policy_version=self.version,
                requirement=fallback,
                reason="No policy rule matched; the default approval requirement applies",
            )
        return decision


# ──────────────────────────────── context ───────────────────────────────────


class PolicyChangeSet(ContractModel):
    """``cs`` — facts about the change set (or bundle) being approved."""

    lines: NonNegInt
    units: NonNegInt
    value_cents: Cents
    ops: list[OpName]
    connectors: list[NonEmptyStr]
    incremental_cost_cents: Cents
    lost_units_after: NonNegInt
    levers: list[NonEmptyStr]

    @model_validator(mode="after")
    def _check(self) -> PolicyChangeSet:
        _unique(self.ops, "cs.ops")
        _unique(self.connectors, "cs.connectors")
        _unique(self.levers, "cs.levers")
        return self


class PolicyCase(ContractModel):
    """``case`` — the case the change set belongs to."""

    mode: CaseMode
    origin: RunOrigin
    tainted: StrictBool
    """Untrusted content (quarantined text) influenced the plan."""
    unconfirmed: NonNegInt
    """Number of values the plan relies on that are still unconfirmed."""


class PolicyInitiator(ContractModel):
    """``initiator`` — the person (or signal principal) who started the run."""

    roles: list[RoleName]

    @model_validator(mode="after")
    def _check(self) -> PolicyInitiator:
        _unique(self.roles, "initiator.roles")
        return self


class PolicyContext(ContractModel):
    """Typed CEL context ``{cs, case, initiator}`` (also the admin test-bench input)."""

    cs: PolicyChangeSet
    case: PolicyCase
    initiator: PolicyInitiator

    def activation(self) -> dict[str, Any]:
        """Plain JSON-ready variables for the CEL evaluator (ints, strings, bools, lists)."""
        return self.model_dump(mode="json")

    @property
    def must_refuse(self) -> bool:
        """What-if cases and plans resting on unconfirmed values never commit (hard floor,
        whatever the rules say; doc rule 1)."""
        return self.case.mode == "what_if" or self.case.unconfirmed > 0

    @property
    def auto_allowed(self) -> bool:
        """Auto-commit is possible only for a person's chat run on untainted, committable input
        (hard floor: signal/webhook origin, tainted, what-if or unconfirmed never auto; doc
        rule 2 and §4)."""
        return self.case.origin == "chat" and not self.case.tainted and not self.must_refuse


# ──────────────────────────────── decision ──────────────────────────────────


class PolicyDecision(ContractModel):
    """The evaluated outcome (``approval_gate`` and ``POST /api/admin/policies/test``).

    ``rule_id`` names the matched rule (``None`` only when no rule matched and the engine
    applied its built-in fallback); ``requirement`` is set exactly for ``human``."""

    outcome: PolicyOutcome
    rule_id: RuleId | None
    policy_version: NonEmptyStr
    requirement: Requirement | None
    reason: NonEmptyStr

    @model_validator(mode="after")
    def _check(self) -> PolicyDecision:
        if (self.outcome == "human") != (self.requirement is not None):
            raise ValueError("requirement is set exactly when the outcome is 'human'")
        if self.outcome == "auto" and self.rule_id is None:
            raise ValueError("an auto decision must name the rule that allowed it")
        return self

    @property
    def approved_by(self) -> str | None:
        """``policy:<rule_id>@<policy_version>`` for auto decisions (receipt ``approved_by``)."""
        if self.outcome != "auto" or self.rule_id is None:
            return None
        return f"policy:{self.rule_id}@{self.policy_version}"

    @classmethod
    def from_rule(cls, rule: PolicyRule, *, policy_version: str, reason: str | None = None) -> PolicyDecision:
        """The decision a matched rule produces (``reason`` defaults to its description)."""
        return cls(
            outcome=rule.outcome,
            rule_id=rule.id,
            policy_version=policy_version,
            requirement=rule.require if isinstance(rule.require, Requirement) else None,
            reason=reason or rule.description,
        )


# ───────────────────────────────── quorum ───────────────────────────────────


class QuorumApproval(ContractModel):
    """One approval counted towards a quorum (``role`` = the role it counts under)."""

    sub: NonEmptyStr
    name: str
    role: RoleName
    at: AwareDatetime


class QuorumCandidate(ContractModel):
    """A recorded approval with the roles the person held when approving (input to
    :func:`quorum_assign`; roles outside the requirement are ignored)."""

    sub: NonEmptyStr
    name: str
    roles: list[str]
    at: AwareDatetime


class QuorumView(ContractModel):
    """Quorum progress of an approval interrupt (API + ``approval_card@1.quorum``).

    ``remaining`` and ``missing_roles`` are derived from ``required`` and ``approvals`` and
    must agree with them (build with :meth:`build`). The card renders e.g.
    "1 of 2 approvals: Marco ✓ · waiting for Compliance"."""

    required: Requirement
    approvals: list[QuorumApproval]
    remaining: NonNegInt
    missing_roles: list[RoleName]

    @model_validator(mode="after")
    def _check(self) -> QuorumView:
        _unique([a.sub for a in self.approvals], "approval subs")
        allowed = set(self.required.roles)
        if bad := sorted({a.role for a in self.approvals} - allowed):
            raise ValueError(f"approvals counted under roles outside the requirement: {bad}")
        roles = [a.role for a in self.approvals]
        if self.remaining != quorum_remaining(self.required, roles):
            raise ValueError("remaining does not match required and approvals")
        if self.missing_roles != quorum_missing_roles(self.required, roles):
            raise ValueError("missing_roles does not match required and approvals")
        return self

    @property
    def met(self) -> bool:
        return self.remaining == 0

    @classmethod
    def build(
        cls,
        required: Requirement | Mapping[str, Any],
        approvals: Sequence[QuorumApproval | Mapping[str, Any]] = (),
    ) -> QuorumView:
        req = required if isinstance(required, Requirement) else Requirement.model_validate(required)
        items = [a if isinstance(a, QuorumApproval) else QuorumApproval.model_validate(a) for a in approvals]
        roles = [a.role for a in items]
        return cls(
            required=req,
            approvals=items,
            remaining=quorum_remaining(req, roles),
            missing_roles=quorum_missing_roles(req, roles),
        )

    def with_approval(self, *, sub: str, name: str, roles: Iterable[str], at: dt.datetime,
                      initiator_sub: str | None) -> QuorumView:
        """A copy with ``sub``'s approval added (raises ``ValueError`` when it cannot count)."""
        role = quorum_role_for(self.required, self.approvals, sub=sub, roles=roles, initiator_sub=initiator_sub)
        if role is None:
            raise ValueError(f"{sub} cannot advance this quorum")
        return QuorumView.build(self.required, [*self.approvals, QuorumApproval(sub=sub, name=name, role=role, at=at)])

