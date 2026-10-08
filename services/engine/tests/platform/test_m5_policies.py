"""M5 approval policies (``95`` §1): defaults (core = pack = runtime), the guards (raising auto
caps / removing or weakening refuse and approval rules need a platform-admin, also through PUT
/agents/spec), versioning, the evaluator port and ``POST /api/admin/policies/test``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import HAVE_PACKS, TENANT_A
from keelson_contracts.policy import PolicyContext, PolicyDecision, PolicyRule, PolicySet, Requirement
from keelson_engine.core import policies
from keelson_engine.core.packs import load_pack

pytestmark = pytest.mark.asyncio(loop_scope="session")

URL = "/api/admin/policies"


def _rules() -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in policies.DEFAULT_RULES]


def _by_id(rules: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in rules}


def _set(rules: list[dict[str, Any]], version: str = "p-9") -> PolicySet:
    return PolicySet.model_validate({"version": version, "rules": rules})


def _ctx(**over: Any) -> dict[str, Any]:
    ctx = policies.sample_context().model_dump(mode="json")
    for path, value in over.items():
        head, _, tail = path.partition("__")
        ctx[head][tail] = value
    return ctx


# ───────────────────────────── pure helpers ─────────────────────────────


async def test_condition_change_directions() -> None:
    cc = policies.condition_change
    assert cc("cs.lines <= 10", "cs.lines <= 10") == "same"
    assert cc("cs.lines <= 10 && cs.units <= 500", "cs.lines <= 20 && cs.units <= 500") == "wider"
    assert cc("cs.lines <= 10", "cs.lines <= 5") == "narrower"
    assert cc("cs.incremental_cost_cents > 1000000", "cs.incremental_cost_cents > 5000000") == "narrower"
    assert cc("cs.incremental_cost_cents > 1000000", "cs.incremental_cost_cents > 1000") == "wider"
    assert cc("10 >= cs.lines", "20 >= cs.lines") == "wider"  # number on the left
    assert cc("cs.lines <= 10 && cs.units <= 500", "cs.lines <= 20 && cs.units <= 100") == "unknown"  # mixed
    assert cc("!(cs.lines <= 10)", "!(cs.lines <= 20)") == "unknown"  # negation flips: never guess
    assert cc("cs.lines == 10", "cs.lines == 20") == "unknown"
    assert cc("cs.lines <= 10", "cs.units <= 10") == "unknown"
    assert cc('initiator.roles.exists(r, r == "planner")', 'initiator.roles.exists(r, r == "x")') == "unknown"


async def test_requirement_strength() -> None:
    four = Requirement(roles=["approver", "compliance"], count=2, exclude_initiator=True)
    one = Requirement(roles=["approver"], count=1, exclude_initiator=False)
    assert policies.requirement_at_least(four, four)
    assert not policies.requirement_at_least(one, four)
    assert not policies.requirement_at_least(Requirement(roles=["approver", "compliance"], count=2,
                                                         exclude_initiator=False), four)
    assert policies.requirement_at_least(Requirement(roles=["approver"], count=2, exclude_initiator=True), one)
    assert not policies.requirement_at_least(Requirement(roles=["approver", "planner"], count=1,
                                                         exclude_initiator=False), one)  # more people may approve


async def test_guarded_changes_matrix() -> None:
    base = _set(_rules(), "p-1")

    def guards(mutate: Any) -> list[str]:
        rules = _rules()
        mutate(rules)
        return [g["reason"] for g in policies.guarded_changes(base, _set(rules))]

    def auto(rules: list[dict[str, Any]]) -> dict[str, Any]:
        return _by_id(rules)["auto-small-transfer"]

    # tenant-admin changes
    assert guards(lambda r: None) == []
    assert guards(lambda r: auto(r).update(when=auto(r)["when"].replace("<= 500", "<= 200"))) == []
    assert guards(lambda r: r.remove(auto(r))) == []
    assert guards(lambda r: _by_id(r)["four-eyes"].update(description="Two people")) == []
    assert guards(lambda r: _by_id(r)["four-eyes"]["require"].update(count=3)) == []
    assert guards(lambda r: r.append({"id": "refuse-x", "description": "No", "when": "cs.lines > 900",
                                      "require": "refuse", "priority": 5})) == []
    assert guards(lambda r: _by_id(r)["four-eyes"].update(
        when=_by_id(r)["four-eyes"]["when"].replace("> 1000000", "> 500000"))) == []  # wider four-eyes
    # platform-admin changes
    assert guards(lambda r: auto(r).update(when=auto(r)["when"].replace("<= 500", "<= 5000"))) == [
        "raises the automatic-approval caps"]
    assert guards(lambda r: r.remove(_by_id(r)["refuse-unconfirmed"])) == ["removes a refuse rule"]
    assert guards(lambda r: r.remove(_by_id(r)["human-untrusted"])) == ["removes an approval rule"]
    assert guards(lambda r: _by_id(r)["four-eyes"]["require"].update(count=1, roles=["approver"])) == [
        "weakens the approval requirement"]
    assert guards(lambda r: _by_id(r)["four-eyes"].update(
        when=_by_id(r)["four-eyes"]["when"].replace("> 1000000", "> 9000000"))) == [
        "narrows when the rule applies"]
    assert guards(lambda r: _by_id(r)["refuse-unconfirmed"].update(require={
        "roles": ["approver"], "count": 1, "exclude_initiator": False})) == ["turns a refuse rule into an approval rule"]
    assert "adds an automatic-approval rule" in guards(lambda r: r.append(
        {"id": "auto-all", "description": "All", "when": "true", "require": "auto", "priority": 45}))
    assert "moves an automatic-approval rule ahead of other rules" in guards(
        lambda r: auto(r).update(priority=25))
    shadow = guards(lambda r: r.append({"id": "one-approver", "description": "x", "when": "true",
                                        "require": {"roles": ["approver"], "count": 1, "exclude_initiator": False},
                                        "priority": 15}))
    assert "would take precedence over the stricter rule four-eyes" in shadow
    assert guards(lambda r: r.append({"id": "stricter", "description": "x", "when": "cs.lines > 100",
                                      "require": {"roles": ["approver", "compliance"], "count": 3,
                                                  "exclude_initiator": True}, "priority": 15})) == []
    assert "would take precedence over the refuse rule refuse-unconfirmed" in guards(lambda r: r.append(
        {"id": "early", "description": "x", "when": "true", "require": {"roles": ["approver"], "count": 2,
                                                                         "exclude_initiator": True},
         "priority": 1}))


async def test_defaults_agree_with_pack_and_runtime() -> None:
    assert policies.DEFAULT_POLICY.version == "p-1"
    if HAVE_PACKS:
        pack_policy = load_pack("value-fashion-eu").spec_overrides["policy"]
        assert pack_policy["version"] == "p-2"
        assert [PolicyRule.model_validate(r) for r in pack_policy["rules"]] == list(policies.DEFAULT_RULES)
    try:
        from keelson_engine.runtime.policy import default_rules
        from keelson_engine.runtime.spec import PolicyConfig
    except ImportError:
        pytest.skip("runtime policy module not importable")
    assert default_rules(PolicyConfig()) == list(policies.DEFAULT_RULES)
    effective, source = policies.effective_policy({"version": "p-1"})
    assert source == "default" and effective.rules == list(policies.DEFAULT_RULES)
    custom, source = policies.effective_policy({"version": "p-7", "rules": _rules()[:2]})
    assert source == "spec" and custom.version == "p-7" and len(custom.rules) == 2


async def test_next_version() -> None:
    assert policies.next_version("p-2") == "p-3"
    assert policies.next_version("v9") == "v10"
    assert policies.next_version("custom") == "custom-2"


# ───────────────────────────── evaluator port ─────────────────────────────


@pytest.fixture
def fake_evaluator() -> Iterator[list[tuple[PolicySet, PolicyContext]]]:
    """A scripted evaluator: matches rules whose ``when`` is ``true`` or names the context origin."""
    calls: list[tuple[PolicySet, PolicyContext]] = []

    def evaluate(rules: PolicySet, ctx: PolicyContext) -> PolicyDecision:
        calls.append((rules, ctx))

        def matches(rule: PolicyRule) -> bool:
            if "BOOM" in rule.when:
                raise RuntimeError("no such function: BOOM")
            return rule.when == "true" or f'"{ctx.case.origin}"' in rule.when

        return rules.decide(ctx, matches, fallback=policies.FALLBACK_REQUIREMENT)

    def check(when: str) -> list[str]:
        return ["undeclared reference to 'BOOM'"] if "BOOM" in when else []

    policies.register_policy_evaluator(evaluate, check=check)
    yield calls
    policies.register_policy_evaluator(None)


async def test_evaluate_port(fake_evaluator: list[Any]) -> None:
    rules = _set([{"id": "web", "description": "w", "when": 'case.origin == "webhook"', "require": "refuse",
                   "priority": 1}, *_rules()[4:]])
    decision = await policies.evaluate(rules, PolicyContext.model_validate(_ctx(case__origin="webhook")))
    assert decision.outcome == "refuse" and decision.rule_id == "web"
    with pytest.raises(policies.PolicyEvaluationError):
        await policies.evaluate(_set([{"id": "x", "description": "x", "when": "BOOM()", "require": "refuse",
                                       "priority": 1}]), policies.sample_context())
    policies.register_policy_evaluator(None)
    monkey_tried = policies._install_tried
    policies._install_tried = True  # do not lazily import the runtime here
    try:
        with pytest.raises(policies.PolicyEvaluatorUnavailable):
            await policies.evaluate(rules, policies.sample_context())
    finally:
        policies._install_tried = monkey_tried


# ───────────────────────────── API ─────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, platform: None) -> dict[str, Browser]:
    await reset_specs(TENANT_A)
    return {n: await browser_factory(n) for n in ("sam", "ava", "priya")}


async def test_api_get_put_guards_and_versions(people: dict[str, Browser], fake_evaluator: list[Any]) -> None:
    sam, ava, priya = people["sam"], people["ava"], people["priya"]
    assert (await priya.get(URL)).status_code == 403
    cur = (await sam.get(URL)).json()
    assert cur["spec_version"] == "default" and cur["source"] == "default" and cur["editable"] is True
    assert [r["id"] for r in cur["policy"]["rules"]] == [r.id for r in policies.DEFAULT_RULES]
    assert cur["fallback"] == {"roles": ["approver"], "count": 1, "exclude_initiator": False}

    # tenant-admin: lower the auto caps → new spec version + bumped policy version
    rules = _rules()
    auto = _by_id(rules)["auto-small-transfer"]
    auto["when"] = auto["when"].replace("cs.units <= 500", "cs.units <= 300")
    r = await sam.put(URL, json={"base_version": "default", "rules": rules})
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["spec_version"] == "v1" and v1["policy"]["version"] == "p-2" and v1["source"] == "spec"
    assert v1["compiled"] is True and v1["guarded"] == []
    # same rules again: unchanged
    again = await sam.put(URL, json={"base_version": "v1", "rules": rules})
    assert again.status_code == 200 and again.json()["unchanged"] is True
    # stale base
    assert (await sam.put(URL, json={"base_version": "default", "rules": rules})).status_code == 409
    # raising the caps back is guarded for the tenant-admin …
    raised = _rules()
    r = await sam.put(URL, json={"base_version": "v1", "rules": raised})
    # M7 four-eyes: a pending policy_set change request (nothing applied) instead of 403
    assert r.status_code == 202 and r.json()["kind"] == "policy_set" and r.json()["base_version"] == "v1", r.text
    assert [(x["code"], x["field"]) for x in r.json()["reasons"]] == [
        ("auto_cap_raised", "policy.rules.auto-small-transfer")]
    assert r.json()["reasons"][0]["reason"] == "raises the automatic-approval caps"
    assert r.json()["payload"]["rules"] == raised
    assert (await sam.get(URL)).json()["spec_version"] == "v1"
    # … and the same edit through the agent spec cannot bypass the guard
    spec_put = await sam.put("/api/admin/agents/spec", json={"base_version": "v1", "spec_patch": {
        "policy": {"rules": raised}}})
    assert spec_put.status_code == 202 and spec_put.json()["kind"] == "agent_spec"
    assert "policy.rules.auto-small-transfer" in [x["field"] for x in spec_put.json()["reasons"]]
    # removing the refuse rule is guarded too; the platform admin may do both
    no_refuse = [x for x in _rules() if x["id"] != "refuse-unconfirmed"]
    removed = await sam.put(URL, json={"base_version": "v1", "rules": no_refuse})
    assert removed.status_code == 202 and removed.json()["reasons"][0]["code"] == "policy_rule_removed"
    r = await ava.put(URL, json={"base_version": "v1", "rules": raised, "version": "p-ops-1"})
    assert r.status_code == 201, r.text
    assert r.json()["policy"]["version"] == "p-ops-1" and r.json()["guarded"]
    # a version must change with the rules
    same_version = await ava.put(URL, json={"base_version": "v2", "rules": rules, "version": "p-ops-1"})
    assert same_version.status_code == 422 and same_version.json()["code"] == "policy_version_unchanged"
    # invalid CEL (static check from the evaluator) → 422 per rule
    bad = _rules()
    _by_id(bad)["default"]["when"] = "BOOM()"
    r = await ava.put(URL, json={"base_version": "v2", "rules": bad})
    assert r.status_code == 422 and r.json()["errors"][0]["loc"] == "rules.default.when"
    # invalid shapes
    r = await ava.put(URL, json={"base_version": "v2", "rules": [{**_rules()[0], "priority": 10},
                                                                 {**_rules()[1], "priority": 10}]})
    assert r.status_code == 422 and r.json()["code"] == "policy_invalid"
    # the stored versions are agent spec versions
    versions = (await sam.get("/api/admin/agents/spec/versions")).json()
    assert [v["version"] for v in versions] == ["v2", "v1"]


async def test_api_policy_test_bench(people: dict[str, Browser], fake_evaluator: list[Any]) -> None:
    sam, priya = people["sam"], people["priya"]
    r = await sam.post(f"{URL}/test", json={"context": _ctx(case__origin="webhook")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["outcome"] == "human" and body["floors"]["auto_allowed"] is False
    assert body["matched_rule"]["id"] == body["decision"]["rule_id"]
    draft = [{"id": "auto-chat", "description": "chat auto", "when": 'case.origin == "chat"', "require": "auto",
              "priority": 1}]
    r = await sam.post(f"{URL}/test", json={"context": _ctx(), "rules": draft, "version": "draft-1"})
    assert r.json()["decision"]["outcome"] == "auto" and r.json()["approved_by"] == "policy:auto-chat@draft-1"
    r = await sam.post(f"{URL}/test", json={"context": _ctx(case__mode="what_if"), "rules": draft})
    assert r.json()["decision"]["outcome"] == "refuse"  # hard floor whatever the rules say
    r = await sam.post(f"{URL}/test", json={"context": {"cs": {}}})
    assert r.status_code == 422 and r.json()["code"] == "context_invalid"
    assert (await priya.post(f"{URL}/test", json={"context": _ctx()})).status_code == 403
    no_csrf = await sam.client.post(f"{URL}/test", json={"context": _ctx()},
                                    headers={"Cookie": f"keelson_session={sam.cookie}", "Origin": "http://keelson.localhost"})
    assert no_csrf.status_code == 403
    policies.register_policy_evaluator(None)
    tried = policies._install_tried
    policies._install_tried = True
    try:
        r = await sam.post(f"{URL}/test", json={"context": _ctx()})
        assert r.status_code == 503 and r.json()["code"] == "policy_evaluator_unavailable"
    finally:
        policies._install_tried = tried


async def test_runtime_cel_evaluator_if_available(people: dict[str, Browser]) -> None:
    """With the runtime's cel-expr-python evaluator registered (as engine-api does at start-up)."""
    policies.register_policy_evaluator(None)
    policies._install_tried = False
    try:
        if not policies.install_runtime_evaluator():
            pytest.skip("runtime CEL evaluator not importable")
        rules = _set(_rules(), "p-2")
        small = await policies.evaluate(rules, PolicyContext.model_validate(_ctx()))
        assert small.outcome == "auto" and small.approved_by == "policy:auto-small-transfer@p-2"
        webhook = await policies.evaluate(rules, PolicyContext.model_validate(_ctx(case__origin="webhook")))
        assert webhook.outcome == "human" and webhook.rule_id == "human-untrusted"
        po = await policies.evaluate(rules, PolicyContext.model_validate(
            _ctx(cs__ops=["po.create"], case__origin="webhook")))
        assert po.rule_id == "four-eyes" and po.requirement is not None and po.requirement.count == 2
        what_if = await policies.evaluate(rules, PolicyContext.model_validate(_ctx(case__mode="what_if")))
        assert what_if.outcome == "refuse"
        r = await people["sam"].post(f"{URL}/test", json={"context": _ctx(cs__units=900)})
        assert r.status_code == 200 and r.json()["decision"]["rule_id"] == "default"
    finally:
        policies.register_policy_evaluator(None)
        policies._install_tried = False


async def test_widening_a_rule_that_shadows_a_stricter_one_is_guarded() -> None:
    """Regression: an approval rule placed (by a platform admin) ahead of a stricter rule could be
    widened by a tenant-admin unflagged, taking change sets away from four-eyes."""
    small = {"id": "small-one-approver", "description": "Tiny changes need one approver",
             "when": "cs.lines <= 1", "require": {"roles": ["approver"], "count": 1, "exclude_initiator": False},
             "priority": 15}
    base = _set([*_rules(), small], "p-1")
    wider = [*_rules(), {**small, "when": "cs.lines <= 100"}]
    reasons = [g["reason"] for g in policies.guarded_changes(base, _set(wider))]
    assert reasons == ["widens a rule that takes precedence over the stricter rule four-eyes"]
    narrower = [*_rules(), {**small, "when": "cs.lines <= 0"}]
    assert [g["reason"] for g in policies.guarded_changes(base, _set(narrower))] == [
        "narrows when the rule applies"]  # unchanged: a narrower approval rule may hand change sets to auto
    # a widened rule with only weaker rules after it stays a tenant-admin change
    tail = {**small, "id": "tail", "priority": 60}
    base2 = _set([*_rules(), tail], "p-1")
    assert policies.guarded_changes(base2, _set([*_rules(), {**tail, "when": "cs.lines <= 100"}])) == []


async def test_floors_hold_whatever_the_evaluator_answers() -> None:
    """Defence in depth: a registered evaluator that ignores ``PolicySet.decide``'s hard floors can
    neither commit a what-if / unconfirmed context nor auto-approve a signal-origin run."""

    def naive(rules: PolicySet, ctx: PolicyContext) -> PolicyDecision:  # first auto rule, no floors
        rule = next(r for r in rules.ordered() if r.outcome == "auto")
        return PolicyDecision.from_rule(rule, policy_version=rules.version)

    policies.register_policy_evaluator(naive)
    try:
        rules = _set(_rules())
        what_if = await policies.evaluate(rules, PolicyContext.model_validate(_ctx(case__mode="what_if")))
        assert what_if.outcome == "refuse" and what_if.requirement is None
        with pytest.raises(policies.PolicyEvaluationError):
            await policies.evaluate(rules, PolicyContext.model_validate(_ctx(case__origin="signal")))
        with pytest.raises(policies.PolicyEvaluationError):
            await policies.evaluate(rules, PolicyContext.model_validate(_ctx(case__tainted=True)))
        ok = await policies.evaluate(rules, policies.sample_context())
        assert ok.outcome == "auto" and ok.approved_by == f"policy:auto-small-transfer@{rules.version}"
    finally:
        policies.register_policy_evaluator(None)
