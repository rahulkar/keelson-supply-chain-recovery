"""M5 approval policy (``95`` §1): the CEL evaluator (cel-expr-python), every default rule incl.
four-eyes and auto, the hard floors (signal / tainted / what-if never auto), fail-closed evaluation,
the static rule check and the platform port registration."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from fakes.m5 import BOOKING, PO_CREATE, RESCHEDULE, TO, scenario
from keelson_contracts.policy import PolicyContext, PolicyRule, PolicySet, Requirement
from keelson_contracts.runs import Actor

from keelson_engine.runtime import policy as policy_mod
from keelson_engine.runtime.commit import compile_changeset
from keelson_engine.runtime.policy import (
    CEL_ENGINE,
    PolicyEvaluationError,
    build_context,
    check_rule,
    decide_approval,
    default_rules,
    eval_when,
    evaluate_policy,
    policy_set,
    register_policy_evaluator,
)
from keelson_engine.runtime.spec import AgentSpec, PolicyConfig

PLANNER = Actor(sub="s-p", username="priya", name="Priya Shah", roles=["planner"])
APPROVER = Actor(sub="s-m", username="marco", name="Marco Rossi", roles=["planner", "approver"])
ADMIN = Actor(sub="s-a", username="ada", name="Ada", roles=["tenant-admin"])


def cs(actions: list[dict[str, Any]], cs_id: str = "cs-1", connector: str = "merxa") -> Any:
    return compile_changeset(tenant="demo-retail", change_set_id=cs_id, connector=connector,
                             actions=[{**a, "seq": i} for i, a in enumerate(actions, start=1)])


def ctx(**over: Any) -> PolicyContext:
    base: dict[str, Any] = {
        "cs": {"lines": 1, "units": 300, "value_cents": 0, "ops": ["transfer_order.create"], "connectors": ["merxa"],
               "incremental_cost_cents": 10_500, "lost_units_after": 0, "levers": ["reallocate_inventory"]},
        "case": {"mode": "actual", "origin": "chat", "tainted": False, "unconfirmed": 0},
        "initiator": {"roles": ["planner"]},
    }
    for key, value in over.items():
        section, field = key.split("__")
        base[section][field] = value
    return PolicyContext.model_validate(base)


def test_engine_is_cel_expr_python() -> None:
    assert CEL_ENGINE == "cel-expr-python"
    import cel_expr_python  # noqa: F401  (installed wheel for CPython 3.14)

    assert eval_when('cs.ops.exists(o, o == "transfer_order.create") && cs.lines <= 10', ctx().activation())


def test_default_rules_are_ordered_and_parameterised() -> None:
    rules = default_rules(PolicyConfig(auto_max_lines=7, auto_max_units=250, approver_role="approver"))
    assert [r.id for r in sorted(rules, key=lambda r: r.priority)] == [
        "refuse-unconfirmed", "four-eyes", "human-untrusted", "auto-small-transfer", "default"]
    auto = next(r for r in rules if r.id == "auto-small-transfer")
    assert "cs.lines <= 7" in auto.when and "cs.units <= 250" in auto.when and auto.outcome == "auto"
    four = next(r for r in rules if r.id == "four-eyes")
    assert four.require == Requirement(roles=["approver", "compliance"], count=2, exclude_initiator=True)
    for rule in rules:
        assert check_rule(rule.when) == [], rule.id


@pytest.mark.parametrize(
    ("over", "outcome", "rule"),
    [
        ({"case__mode": "what_if"}, "refuse", "refuse-unconfirmed"),
        ({"case__unconfirmed": 2}, "refuse", "refuse-unconfirmed"),
        ({"cs__incremental_cost_cents": 1_000_001}, "human", "four-eyes"),
        ({"cs__ops": ["po.create", "po_line.change_qty"]}, "human", "four-eyes"),
        ({"cs__ops": ["po_line.cancel"]}, "human", "four-eyes"),
        ({"cs__ops": ["production_order.resequence"]}, "human", "four-eyes"),
        ({"case__origin": "webhook"}, "human", "human-untrusted"),
        ({"case__origin": "signal"}, "human", "human-untrusted"),
        ({"case__tainted": True}, "human", "human-untrusted"),
        ({}, "auto", "auto-small-transfer"),
        ({"cs__ops": ["allocation.set_qty", "transfer_order.create"]}, "auto", "auto-small-transfer"),
        ({"cs__lines": 11}, "human", "default"),
        ({"cs__units": 501}, "human", "default"),
        ({"initiator__roles": ["approver"]}, "human", "default"),
        ({"cs__ops": ["po_line.reschedule"]}, "human", "default"),
    ],
)
def test_every_default_rule(over: dict[str, Any], outcome: str, rule: str) -> None:
    decision = evaluate_policy(policy_set(PolicyConfig()), ctx(**over),
                               fallback=Requirement(roles=["approver"], count=1, exclude_initiator=False))
    assert (decision.outcome, decision.rule_id) == (outcome, rule)
    if outcome == "auto":
        assert decision.approved_by == "policy:auto-small-transfer@p-1"
    if rule == "four-eyes":
        assert decision.requirement is not None and decision.requirement.count == 2
        assert decision.requirement.exclude_initiator


def test_four_eyes_beats_untrusted_origin() -> None:
    # a webhook re-plan that creates a PO still needs two people (four-eyes before human-untrusted)
    decision = evaluate_policy(policy_set(PolicyConfig()), ctx(case__origin="webhook", cs__ops=["po.create"]))
    assert decision.rule_id == "four-eyes" and decision.requirement.count == 2  # type: ignore[union-attr]


def test_floors_hold_even_against_a_permissive_rule_set() -> None:
    """A tenant rule set that auto-approves everything still never auto-approves signal runs, tainted
    or what-if plans, and never commits a what-if / unconfirmed plan."""
    rules = PolicySet(version="p-x", rules=[
        PolicyRule(id="yolo", description="everything auto", when="true", require="auto", priority=1),
    ])
    fallback = Requirement(roles=["approver"], count=1, exclude_initiator=False)
    assert evaluate_policy(rules, ctx(), fallback=fallback).outcome == "auto"
    for over in ({"case__origin": "signal"}, {"case__origin": "webhook"}, {"case__tainted": True}):
        decision = evaluate_policy(rules, ctx(**over), fallback=fallback)
        assert decision.outcome == "human" and decision.rule_id is None, over
    for over in ({"case__mode": "what_if"}, {"case__unconfirmed": 1}):
        assert evaluate_policy(rules, ctx(**over), fallback=fallback).outcome == "refuse", over
    # rollbacks pass allow_auto=False: always a person
    assert evaluate_policy(rules, ctx(), fallback=fallback, allow_auto=False).outcome == "human"


def test_fail_closed_on_broken_rules() -> None:
    fallback = Requirement(roles=["approver"], count=1, exclude_initiator=False)
    for when, code in (("cs.lines +", "compile"), ("cs.nope > 3", "unknown_field"), ("cs.lines", "eval"),
                       ("1/0 == 1", "eval")):
        rules = [PolicyRule(id="bad", description="broken", when=when, require="refuse", priority=1)]
        with pytest.raises(PolicyEvaluationError) as err:
            evaluate_policy(rules, ctx(), fallback=fallback)
        assert err.value.rule_id == "bad" and err.value.code == code, when


def test_check_rule_reports_problems() -> None:
    assert check_rule('cs.ops.exists(o, o == "po.create")') == []
    assert check_rule('case.origin == "cs.bogus"') == []  # field names inside strings are not fields
    assert "unknown field cs.bogus" in check_rule("cs.bogus > 1")
    assert any("Syntax error" in p or "mismatched" in p for p in check_rule("cs.lines >"))
    assert any("not a bool" in p for p in check_rule("size(cs.ops)"))


def test_decide_approval_contexts_from_change_sets() -> None:
    config = PolicyConfig()
    small = decide_approval(change_set=cs([TO]), scenario=scenario(), case_mode="actual", initiator=PLANNER,
                            config=config)
    assert small.outcome == "auto" and small.approved_by == "policy:auto-small-transfer@p-1"
    assert small.context is not None and small.context.cs.units == 300 and small.context.cs.lines == 1
    # signal-origin runs are never auto-approved, whatever the size
    signal = decide_approval(change_set=cs([TO]), scenario=scenario(), case_mode="actual", initiator=None,
                             config=config, origin="webhook")
    assert signal.outcome == "human" and signal.rule_id == "human-untrusted"
    tainted = decide_approval(change_set=cs([TO]), scenario=scenario(), case_mode="actual", initiator=PLANNER,
                              config=config, tainted=True)
    assert tainted.outcome == "human"
    what_if = decide_approval(change_set=cs([TO]), scenario=scenario(mode="what_if"), case_mode="actual",
                              initiator=PLANNER, config=config)
    assert what_if.outcome == "refuse" and "what-if" in what_if.reason
    unconfirmed = decide_approval(change_set=cs([TO]), scenario=scenario(prov="inference"), case_mode="actual",
                                  initiator=PLANNER, config=config)
    assert unconfirmed.outcome == "refuse" and "Delay" in unconfirmed.reason
    four = decide_approval(change_set=cs(PO_CREATE), scenario=scenario(), case_mode="actual", initiator=PLANNER,
                           config=config)
    assert four.outcome == "human" and four.rule_id == "four-eyes"
    assert four.quorum == {"roles": ["approver", "compliance"], "count": 2, "exclude_initiator": True}
    assert "near-shore" in four.reason and "2 different people" in four.reason
    costly = decide_approval(change_set=cs([TO]), scenario=scenario(), case_mode="actual", initiator=PLANNER,
                             config=config, option={"scorecard": {"incremental_cost_cents": 2_000_000}})
    assert costly.rule_id == "four-eyes"
    other = decide_approval(change_set=cs([RESCHEDULE]), scenario=scenario(), case_mode="actual", initiator=PLANNER,
                            config=config)
    assert other.outcome == "human" and other.rule_id == "default" and "resequencing" in other.reason


def test_bundle_context_counts_every_member() -> None:
    a, b = cs([TO], "cs-1", "merxa"), cs([BOOKING], "cs-2", "tms")
    context = build_context(change_sets=[a, b], scenario=scenario(), case_mode="actual", initiator_roles=["planner"])
    assert context.cs.lines == 2 and context.cs.units == 700
    assert context.cs.connectors == ["merxa", "tms"]
    assert context.cs.ops == ["capacity_booking.create", "transfer_order.create"]
    decision = decide_approval(change_sets=[a, b], scenario=scenario(), case_mode="actual", initiator=PLANNER,
                               config=PolicyConfig())
    assert decision.outcome == "human" and decision.rule_id == "default"  # a booking is never auto


def test_tenant_rules_from_the_spec() -> None:
    spec = AgentSpec.from_row("demo-retail", "v9", {"policy": {"version": "p-9", "rules": [
        {"id": "everything-four-eyes", "description": "two people", "priority": 5, "when": "cs.lines > 0",
         "require": {"roles": ["approver", "compliance"], "count": 2, "exclude_initiator": True}},
    ]}})
    decision = decide_approval(change_set=cs([TO]), scenario=scenario(), case_mode="actual", initiator=PLANNER,
                               config=spec.policy)
    assert decision.rule_id == "everything-four-eyes" and decision.policy_version == "p-9"
    with pytest.raises(ValueError):
        AgentSpec.from_row("demo-retail", "v9", {"policy": {"rules": []}})
    with pytest.raises(ValueError):
        AgentSpec.from_row("demo-retail", "v9", {"policy": {"rules": [
            {"id": "a", "description": "x", "priority": 1, "when": "true", "require": "auto"},
            {"id": "b", "description": "y", "priority": 1, "when": "true", "require": "refuse"}]}})


def test_register_policy_evaluator_with_platform_port(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: dict[str, Any] = {}
    fake = types.ModuleType("keelson_engine.core.policies")

    def register(fn: Any, *, check: Any = None) -> None:
        registered["fn"], registered["check"] = fn, check

    fake.register_policy_evaluator = register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keelson_engine.core.policies", fake)
    assert policy_mod.register_policy_evaluator() is True
    assert registered["fn"] is evaluate_policy and registered["check"] is check_rule
    # the registered function accepts the admin test-bench shapes (set JSON + context JSON)
    result = registered["fn"]({"version": "p-1", "rules": [r.model_dump(mode="json") for r in default_rules(PolicyConfig())]},
                              ctx().model_dump(mode="json"))
    assert result.rule_id == "auto-small-transfer"


def test_register_without_platform_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keelson_engine.core.policies", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "keelson_engine.core.policy", None)  # type: ignore[arg-type]
    assert register_policy_evaluator() is False
