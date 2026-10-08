"""Independent review of the M5 contracts (``docs/contracts/95-m5-remediation.md``): regression
tests for the defects found in review.

* quorum counting: a person holding several required roles who approves first must not block
  a later single-role approver (maximum matching, :func:`quorum_assign`); the matching never
  lets two people of one role pass a four-eyes quorum, never counts a person twice or the
  excluded initiator, and is stable when approvals are appended;
* ``initiator_sub`` is a required argument (a forgotten initiator silently disabled
  ``exclude_initiator``);
* :meth:`PolicySet.decide`: first match by priority with hard floors (what-if / unconfirmed
  are refused, signal / webhook / tainted / rollback never auto) that no rule set can lift;
* ``remediation_checklist@1.deep_link`` carries a JSON-Schema pattern equivalent to
  :func:`is_safe_deep_link`, so the generated Zod schema rejects ``javascript:`` links too;
* approval-card rejection reasons are bounded like ``ResumePayload.reason``;
* commit-tracker rollback conflicts only accompany a change set that is still applied.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import random
import re
from typing import Any

import pytest
from pydantic import ValidationError

from examples import GOOD_PROPS, HASH, good_props
from keelson_contracts.blocks import (
    DEEP_LINK_PATTERN,
    ApprovalCardProps,
    CommitTrackerProps,
    RemediationChecklistProps,
    is_safe_deep_link,
)
from keelson_contracts.policy import (
    PolicyContext,
    PolicyRule,
    PolicySet,
    QuorumApproval,
    QuorumCandidate,
    QuorumView,
    Requirement,
    quorum_assign,
    quorum_remaining,
    quorum_role_for,
)
from keelson_contracts.runs import MAX_DECISION_REASON_CHARS

AT = dt.datetime(2026, 10, 7, 10, 0, tzinfo=dt.UTC)
FOUR_EYES = Requirement(roles=["approver", "compliance"], count=2, exclude_initiator=True)
ONE_APPROVER = Requirement(roles=["approver"], count=1, exclude_initiator=False)


def _cand(sub: str, *roles: str, minute: int = 0) -> QuorumCandidate:
    return QuorumCandidate(sub=sub, name=sub.title(), roles=list(roles), at=AT + dt.timedelta(minutes=minute))


# ───────────────────────────── quorum matching ──────────────────────────────


def test_dual_role_first_approver_does_not_block_single_role_approver() -> None:
    # Lena holds approver + compliance and approves first; Marco holds only approver.
    view, rejected = quorum_assign(
        FOUR_EYES, [_cand("u-lena", "approver", "compliance"), _cand("u-marco", "approver", minute=1)],
        initiator_sub="u-priya",
    )
    assert rejected == []
    assert [(a.sub, a.role) for a in view.approvals] == [("u-lena", "compliance"), ("u-marco", "approver")]
    assert view.met and view.missing_roles == []
    # the other single-role order works as well
    view, rejected = quorum_assign(
        FOUR_EYES, [_cand("u-lena", "approver", "compliance"), _cand("u-nina", "compliance", minute=1)],
        initiator_sub="u-priya",
    )
    assert rejected == [] and view.met
    assert {a.sub: a.role for a in view.approvals} == {"u-lena": "approver", "u-nina": "compliance"}


def test_two_people_of_one_role_never_pass_four_eyes() -> None:
    view, rejected = quorum_assign(
        FOUR_EYES, [_cand("u-marco", "approver"), _cand("u-anna", "approver", minute=1)], initiator_sub=None
    )
    assert rejected == ["u-anna"] and not view.met
    assert view.remaining == 1 and view.missing_roles == ["compliance"]


def test_same_person_initiator_and_outsiders_never_count() -> None:
    cands = [
        _cand("u-marco", "approver"),
        _cand("u-marco", "compliance", minute=1),  # same person again, another role
        _cand("u-priya", "compliance", minute=2),  # the initiator
        _cand("u-viewer", "planner", minute=3),  # holds no required role
    ]
    view, rejected = quorum_assign(FOUR_EYES, cands, initiator_sub="u-priya")
    assert [a.sub for a in view.approvals] == ["u-marco"]
    assert rejected == ["u-marco", "u-priya", "u-viewer"]
    assert not view.met
    # without exclude_initiator the initiator may approve
    lax = Requirement(roles=["approver", "compliance"], count=2, exclude_initiator=False)
    view, _ = quorum_assign(lax, cands, initiator_sub="u-priya")
    assert view.met and {a.sub for a in view.approvals} == {"u-marco", "u-priya"}


def test_nobody_counts_once_the_quorum_is_met() -> None:
    view, rejected = quorum_assign(
        FOUR_EYES,
        [_cand("u-marco", "approver"), _cand("u-lena", "compliance", minute=1), _cand("u-x", "approver", minute=2)],
        initiator_sub=None,
    )
    assert view.met and rejected == ["u-x"] and len(view.approvals) == 2
    view, rejected = quorum_assign(
        ONE_APPROVER, [_cand("u-marco", "approver"), _cand("u-anna", "approver", minute=1)], initiator_sub=None
    )
    assert view.met and rejected == ["u-anna"]


def test_quorum_role_for_requires_initiator_argument() -> None:
    with pytest.raises(TypeError, match="initiator_sub"):
        quorum_role_for(FOUR_EYES, [], sub="u-priya", roles=["approver"])  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="initiator_sub"):
        QuorumView.build(FOUR_EYES).with_approval(  # type: ignore[call-arg]
            sub="u-priya", name="Priya", roles=["approver"], at=AT
        )
    with pytest.raises(TypeError, match="initiator_sub"):
        quorum_assign(FOUR_EYES, [_cand("u-priya", "approver")])  # type: ignore[call-arg]
    assert quorum_role_for(FOUR_EYES, [], sub="u-priya", roles=["approver"], initiator_sub="u-priya") is None


def _best_remaining(req: Requirement, cands: list[tuple[str, frozenset[str]]], initiator: str | None) -> int:
    """Brute force: the least ``remaining`` any valid assignment of distinct people reaches."""
    seen: set[str] = set()
    people: list[list[str]] = []
    for sub, held in cands:
        ok = sub not in seen and not (req.exclude_initiator and sub == initiator)
        seen.add(sub)
        mine = [r for r in req.roles if r in held]
        if ok and mine:
            people.append(mine)
    best = quorum_remaining(req, [])
    for choice in itertools.product(*[[None, *p] for p in people]):
        best = min(best, quorum_remaining(req, [r for r in choice if r is not None]))
    return best


@pytest.mark.parametrize("seed", range(6))
def test_assignment_is_optimal_and_stable(seed: int) -> None:
    rng = random.Random(seed)
    roles = ["approver", "compliance", "finance"]
    subs = ["u-a", "u-b", "u-c", "u-d", "u-e"]
    for _ in range(120):
        k = rng.randint(1, 3)
        req = Requirement(roles=rng.sample(roles, k), count=rng.randint(1, 4), exclude_initiator=rng.random() < 0.5)
        initiator = rng.choice([None, *subs])
        raw = [
            (rng.choice(subs), frozenset(rng.sample([*roles, "planner"], rng.randint(0, 3))))
            for _ in range(rng.randint(0, 6))
        ]
        cands = [_cand(s, *sorted(h), minute=i) for i, (s, h) in enumerate(raw)]
        view, rejected = quorum_assign(req, cands, initiator_sub=initiator)
        assert len(view.approvals) + len(rejected) == len(cands)
        assert len({a.sub for a in view.approvals}) == len(view.approvals)
        if req.exclude_initiator and initiator is not None:
            assert initiator not in {a.sub for a in view.approvals}
        for a in view.approvals:
            held = next(h for s, h in raw if s == a.sub)
            assert a.role in held and a.role in req.roles
        # optimal: no assignment of the same people does better
        assert view.remaining == _best_remaining(req, raw, initiator)
        # stable: appending approvals never changes whether an earlier one counts
        for cut in range(len(cands)):
            prefix_view, prefix_rejected = quorum_assign(req, cands[:cut], initiator_sub=initiator)
            # (each candidate has its own ``at``, so (sub, at) identifies a candidate position)
            prefix_ids = {(c.sub, c.at) for c in cands[:cut]}
            counted_now = {(a.sub, a.at) for a in view.approvals}
            assert {(a.sub, a.at) for a in prefix_view.approvals} == counted_now & prefix_ids
            assert len(prefix_view.approvals) + len(prefix_rejected) == cut
            assert view.remaining <= prefix_view.remaining


def test_quorum_role_for_agrees_with_assign_for_single_role_people() -> None:
    rng = random.Random(7)
    for _ in range(200):
        req = Requirement(roles=["approver", "compliance"], count=rng.randint(1, 3), exclude_initiator=True)
        approvals: list[QuorumApproval] = []
        for i in range(rng.randint(0, 4)):
            sub = f"u-{i}"
            held = rng.sample(["approver", "compliance", "planner"], rng.randint(1, 2))
            role = quorum_role_for(req, approvals, sub=sub, roles=held, initiator_sub="u-0")
            cands = [*[_cand(a.sub, a.role) for a in approvals], _cand(sub, *held)]
            view, rejected = quorum_assign(req, cands, initiator_sub="u-0")
            if role is None:
                assert sub in rejected or quorum_remaining(req, [a.role for a in approvals]) == 0
            else:
                assert sub not in rejected
                approvals.append(QuorumApproval(sub=sub, name=sub, role=role, at=AT))
                assert QuorumView.build(req, approvals).remaining == view.remaining


# ───────────────────────────── policy decide floors ─────────────────────────


def _ctx(*, origin: str = "chat", tainted: bool = False, mode: str = "actual", unconfirmed: int = 0,
         ops: tuple[str, ...] = ("transfer_order.create",), cost: int = 0) -> PolicyContext:
    return PolicyContext.model_validate({
        "cs": {"lines": 2, "units": 100, "value_cents": 50_000, "ops": list(ops), "connectors": ["merxa"],
               "incremental_cost_cents": cost, "lost_units_after": 0, "levers": ["dc_transfer"]},
        "case": {"mode": mode, "origin": origin, "tainted": tainted, "unconfirmed": unconfirmed},
        "initiator": {"roles": ["planner"]},
    })


AUTO_ALWAYS = PolicySet(version="p-9", rules=[
    PolicyRule(id="auto-everything", description="Auto everything (misconfigured)", when="true",
               require="auto", priority=1),
])


def test_auto_rule_applies_to_a_clean_chat_run() -> None:
    d = AUTO_ALWAYS.decide(_ctx(), lambda r: True, fallback=ONE_APPROVER)
    assert d.outcome == "auto" and d.approved_by == "policy:auto-everything@p-9"


@pytest.mark.parametrize("ctx_kw", [{"origin": "signal"}, {"origin": "webhook"}, {"tainted": True}])
def test_signal_webhook_and_tainted_runs_are_never_auto(ctx_kw: dict[str, Any]) -> None:
    ctx = _ctx(**ctx_kw)
    assert not ctx.auto_allowed
    evaluated: list[str] = []

    def matches(rule: PolicyRule) -> bool:
        evaluated.append(rule.id)
        return True

    d = AUTO_ALWAYS.decide(ctx, matches, fallback=ONE_APPROVER)
    assert d.outcome == "human" and d.requirement == ONE_APPROVER and d.rule_id is None
    assert evaluated == []  # the auto rule is not even evaluated
    assert d.approved_by is None


def test_rollback_is_always_human() -> None:
    d = AUTO_ALWAYS.decide(_ctx(), lambda r: True, fallback=ONE_APPROVER, allow_auto=False)
    assert d.outcome == "human"


@pytest.mark.parametrize("ctx_kw", [{"mode": "what_if"}, {"unconfirmed": 1}])
def test_what_if_and_unconfirmed_are_refused_even_without_rule_1(ctx_kw: dict[str, Any]) -> None:
    ctx = _ctx(**ctx_kw)
    assert ctx.must_refuse and not ctx.auto_allowed
    for rules in (AUTO_ALWAYS, PolicySet(version="p-9", rules=[
        PolicyRule(id="one", description="Anyone", when="true", require=ONE_APPROVER, priority=1),
    ])):
        d = rules.decide(ctx, lambda r: True, fallback=ONE_APPROVER)
        assert d.outcome == "refuse" and d.requirement is None and d.approved_by is None
        # and when nothing matches either
        assert rules.decide(ctx, lambda r: False, fallback=ONE_APPROVER).outcome == "refuse"


def test_first_match_by_priority_and_fallback() -> None:
    rules = PolicySet(version="p-2", rules=[
        PolicyRule(id="four-eyes", description="Four eyes", when="x", require=FOUR_EYES, priority=20),
        PolicyRule(id="refuse-x", description="Refuse", when="y", require="refuse", priority=5),
        PolicyRule(id="one", description="One", when="z", require=ONE_APPROVER, priority=10),
    ])
    order: list[str] = []

    def matches(rule: PolicyRule) -> bool:
        order.append(rule.id)
        return rule.id in {"one", "four-eyes"}

    d = rules.decide(_ctx(), matches, fallback=ONE_APPROVER)
    assert order == ["refuse-x", "one"] and d.rule_id == "one" and d.requirement == ONE_APPROVER
    d = rules.decide(_ctx(), lambda r: False, fallback=FOUR_EYES)
    assert d.outcome == "human" and d.rule_id is None and d.requirement == FOUR_EYES and d.policy_version == "p-2"


def test_decide_propagates_evaluation_errors() -> None:
    def broken(rule: PolicyRule) -> bool:
        raise RuntimeError("CEL evaluation failed")

    with pytest.raises(RuntimeError):
        AUTO_ALWAYS.decide(_ctx(), broken, fallback=ONE_APPROVER)


# ───────────────────────────── deep link pattern ────────────────────────────

LINKS = [
    ("http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1&lines=SKU-1:120", True),
    ("https://merxa.localhost:8443/po-lines/PO-1/2?edit=qty", True),
    ("http://merxa.localhost", True),
    ("/guide/allocations/ALLOC-7", True),
    ("/", True),
    ("javascript:alert(1)", False),
    ("JavaScript:alert(1)", False),
    ("data:text/html,<script>alert(1)</script>", False),
    ("vbscript:msgbox", False),
    ("//evil.example/x", False),
    ("/\\evil.example", False),
    ("/x\\y", False),
    ("http://user@merxa.localhost/x", False),
    ("http://merxa.localhost/a b", False),
    ("http://merxa.localhost/a\tb", False),
    ("http://merxa.localhost/a\nb", False),
    ("/a\x00b", False),
    ("/a\x7fb", False),
    (" http://merxa.localhost/", False),
    ("http://merxa.localhost/\n", False),
    ("merxa.localhost/x", False),
    ("ftp://merxa.localhost/x", False),
    ("", False),
]


@pytest.mark.parametrize(("url", "ok"), LINKS)
def test_deep_link_pattern_matches_is_safe_deep_link(url: str, ok: bool) -> None:
    assert is_safe_deep_link(url) is ok
    # JSON Schema / JS semantics: no implicit trailing-newline match
    assert (re.fullmatch(DEEP_LINK_PATTERN, url) is not None) is ok


def test_deep_link_pattern_is_in_the_exported_schema() -> None:
    schema = RemediationChecklistProps.model_json_schema()
    step = schema["$defs"]["ChecklistStep"]["properties"]["deep_link"]
    patterns = [b.get("pattern") for b in step["anyOf"] if b.get("type") == "string"]
    assert patterns == [DEEP_LINK_PATTERN]
    text = json.dumps(schema)
    assert "javascript" not in text


@pytest.mark.parametrize(("url", "ok"), LINKS)
def test_checklist_step_validates_deep_link(url: str, ok: bool) -> None:
    props = good_props("remediation_checklist@1")
    props["steps"][1]["deep_link"] = url
    if ok:
        RemediationChecklistProps.model_validate(props)
    else:
        with pytest.raises(ValidationError):
            RemediationChecklistProps.model_validate(props)


# ─────────────────────────── approval card / tracker ────────────────────────


def _card(**extra: Any) -> dict[str, Any]:
    return {**GOOD_PROPS["approval_card@1"], "change_set_hash": HASH, **extra}


def test_rejection_reason_is_bounded_like_the_resume_payload() -> None:
    rej = {"sub": "u-lena", "name": "Lena Vogel", "role": "compliance", "at": "2026-10-07T10:00:00Z"}
    ApprovalCardProps.model_validate(_card(rejection={**rej, "reason": "x" * MAX_DECISION_REASON_CHARS}))
    ApprovalCardProps.model_validate(_card(rejection={**rej, "reason": None}))
    for bad in ("", "x" * (MAX_DECISION_REASON_CHARS + 1)):
        with pytest.raises(ValidationError):
            ApprovalCardProps.model_validate(_card(rejection={**rej, "reason": bad}))


def test_rollback_conflicts_only_while_still_applied() -> None:
    conflict = [{"entity_ref": "TO-0007", "detail": "Changed by Anna after this change set"}]
    undo_off = [{"type": "undo", "label": "Undo changes", "enabled": False, "disabled_reason": "Conflicts"}]
    for status in ("applied", "verified"):
        CommitTrackerProps.model_validate(
            {**GOOD_PROPS["commit_tracker@1"], "status": status, "verified": 1, "conflicts": conflict,
             "actions": undo_off}
        )
    for status in ("rolled_back", "applying", "conflict"):
        with pytest.raises(ValidationError, match="rollback conflicts"):
            CommitTrackerProps.model_validate(
                {**GOOD_PROPS["commit_tracker@1"], "status": status, "conflicts": conflict}
            )
