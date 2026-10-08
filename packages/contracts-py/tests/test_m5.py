"""M5 contracts (``docs/contracts/95-m5-remediation.md``): approval policy rules + CEL context +
decision, quorum / four-eyes, receipt ``co_approved_by``, Guide checklist, commit-tracker
Undo, mixed apply/guide counts, re-plan banner, approval-card quorum and bundles, the new
case actions, change-set bundles and the demo reset log.

Everything is additive: pre-M5 payloads validate and serialize byte-identically (checked
against the exact input dicts and pinned hashes)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from pydantic import ValidationError

from examples import GOOD_PROPS, HASH, envelope, good_props
from keelson_contracts.agui import approval_response_schema
from keelson_contracts.blocks import (
    ApprovalCardProps,
    CommitTrackerProps,
    NarrativeProps,
    RecommendedOptionProps,
    RemediationChecklistBlock,
    RemediationChecklistProps,
    block_to_wire,
    is_safe_deep_link,
    make_block,
    parse_block,
)
from keelson_contracts.changeset import (
    AtomicGroup,
    ChangeSet,
    ChangeSetBundle,
    bundle_hash,
    change_set_hash,
)
from keelson_contracts.common import sha256_hex
from keelson_contracts.demo import CONSENT_STEPS, DEMO_RESET_STEPS, DemoResetRequest, DemoResetResult
from keelson_contracts.policy import (
    PolicyContext,
    PolicyDecision,
    PolicyRule,
    PolicySet,
    QuorumApproval,
    QuorumView,
    Requirement,
    quorum_missing_roles,
    quorum_remaining,
    quorum_role_for,
)
from keelson_contracts.receipts import ApprovalReceiptClaims, approved_by_policy, approved_by_user
from keelson_contracts.runs import (
    ACTION_TYPES,
    MAX_DECISION_REASON_CHARS,
    OPTION_ACTION_TYPES,
    ActionPayload,
    ResumePayload,
    RunRequest,
)

AT = dt.datetime(2026, 10, 7, 10, 0, tzinfo=dt.UTC)
FOUR_EYES = {"roles": ["approver", "compliance"], "count": 2, "exclude_initiator": True}
ONE_APPROVER = {"roles": ["approver"], "count": 1, "exclude_initiator": False}

# ───────────────────────────────── policy rules ──────────────────────────────

DOC_RULES: list[dict[str, Any]] = [
    {"id": "refuse-unsafe", "description": "What-if or unconfirmed: never commit",
     "when": 'case.mode == "what_if" || case.unconfirmed > 0', "require": "refuse", "priority": 1},
    {"id": "human-untrusted", "description": "Signals and tainted plans need a person",
     "when": 'case.origin != "chat" || case.tainted', "require": ONE_APPROVER, "priority": 2},
    {"id": "four-eyes", "description": "Large or structural changes need two people",
     "when": 'cs.incremental_cost_cents > 1000000 || cs.ops.exists(o, o in '
             '["po.create","po_line.cancel","production_order.resequence"])',
     "require": FOUR_EYES, "priority": 3},
    {"id": "auto-small-transfer", "description": "Small transfer-only changes by a planner",
     "when": 'cs.ops.all(o, o in ["transfer_order.create","allocation.set_qty"]) && cs.lines <= 10 '
             '&& cs.units <= 500 && initiator.roles.exists(r, r == "planner")',
     "require": "auto", "priority": 4},
    {"id": "default", "description": "One approver", "when": "true", "require": ONE_APPROVER, "priority": 5},
]


def test_doc_default_rules_validate_and_order_by_priority() -> None:
    shuffled = [DOC_RULES[i] for i in (4, 2, 0, 3, 1)]
    policy = PolicySet.model_validate({"version": "p-2", "rules": shuffled})
    assert [r.id for r in policy.ordered()] == [r["id"] for r in DOC_RULES]
    outcomes = [r.outcome for r in policy.ordered()]
    assert outcomes == ["refuse", "human", "human", "auto", "human"]
    four_eyes = policy.rule("four-eyes")
    assert four_eyes is not None and isinstance(four_eyes.require, Requirement)
    assert four_eyes.require.every_role and four_eyes.require.exclude_initiator
    assert policy.rule("nope") is None
    # round trip (the admin API stores the rule set in the agent spec)
    assert PolicySet.model_validate_json(policy.model_dump_json()) == policy


@pytest.mark.parametrize(
    ("why", "mutation"),
    [
        ("bad id", {"id": "Four Eyes"}),
        ("empty when", {"when": ""}),
        ("blank when", {"when": "   "}),
        ("control char in when", {"when": "true\x00"}),
        ("too long when", {"when": "x" * 2001}),
        ("unknown require literal", {"require": "human"}),
        ("negative priority", {"priority": -1}),
        ("float priority", {"priority": 1.0}),
        ("count 0", {"require": {**FOUR_EYES, "count": 0}}),
        ("no roles", {"require": {**FOUR_EYES, "roles": []}}),
        ("duplicate roles", {"require": {**FOUR_EYES, "roles": ["approver", "approver"]}}),
        ("requirement missing exclude_initiator", {"require": {"roles": ["approver"], "count": 1}}),
        ("bad role name", {"require": {**FOUR_EYES, "roles": ["Approver"]}}),
        ("extra field", {"effect": "allow"}),
    ],
)
def test_policy_rule_rejects(why: str, mutation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicyRule.model_validate({**DOC_RULES[2], **mutation})


def test_policy_set_rejects_ambiguous_rules() -> None:
    with pytest.raises(ValidationError, match="rule ids"):
        PolicySet.model_validate({"version": "p-2", "rules": [DOC_RULES[0], {**DOC_RULES[1], "id": "refuse-unsafe"}]})
    with pytest.raises(ValidationError, match="priorities"):
        PolicySet.model_validate({"version": "p-2", "rules": [DOC_RULES[0], {**DOC_RULES[1], "priority": 1}]})
    with pytest.raises(ValidationError):
        PolicySet.model_validate({"version": "p-2", "rules": []})
    with pytest.raises(ValidationError):
        PolicySet.model_validate({"version": "p 2", "rules": DOC_RULES})


CONTEXT: dict[str, Any] = {
    "cs": {"lines": 4, "units": 380, "value_cents": 1_300_000, "ops": ["transfer_order.create"],
           "connectors": ["merxa"], "incremental_cost_cents": 63_000, "lost_units_after": 420,
           "levers": ["dc_rebalance"]},
    "case": {"mode": "actual", "origin": "chat", "tainted": False, "unconfirmed": 0},
    "initiator": {"roles": ["planner"]},
}


def test_policy_context_activation_is_plain_json() -> None:
    ctx = PolicyContext.model_validate(CONTEXT)
    act = ctx.activation()
    assert act == CONTEXT
    json.dumps(act, allow_nan=False)


@pytest.mark.parametrize(
    "mutation",
    [
        {"cs": {**CONTEXT["cs"], "lines": -1}},
        {"cs": {**CONTEXT["cs"], "units": 1.5}},
        {"cs": {**CONTEXT["cs"], "ops": ["po.create", "po.create"]}},
        {"cs": {**CONTEXT["cs"], "value_cents": "13000"}},
        {"cs": {k: v for k, v in CONTEXT["cs"].items() if k != "levers"}},
        {"case": {**CONTEXT["case"], "origin": "email"}},
        {"case": {**CONTEXT["case"], "mode": "rehearsal"}},
        {"case": {**CONTEXT["case"], "tainted": "no"}},
        {"case": {**CONTEXT["case"], "unconfirmed": -1}},
        {"initiator": {"roles": ["planner", "planner"]}},
        {"extra": 1},
    ],
)
def test_policy_context_rejects(mutation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicyContext.model_validate({**CONTEXT, **mutation})


def test_policy_decision_shapes() -> None:
    rules = {r["id"]: PolicyRule.model_validate(r) for r in DOC_RULES}
    auto = PolicyDecision.from_rule(rules["auto-small-transfer"], policy_version="p-2")
    assert auto.outcome == "auto" and auto.requirement is None
    assert auto.approved_by == approved_by_policy("auto-small-transfer", "p-2") == "policy:auto-small-transfer@p-2"
    human = PolicyDecision.from_rule(rules["four-eyes"], policy_version="p-2", reason="Includes a new PO")
    assert human.outcome == "human" and human.requirement == Requirement.model_validate(FOUR_EYES)
    assert human.reason == "Includes a new PO" and human.approved_by is None
    refuse = PolicyDecision.from_rule(rules["refuse-unsafe"], policy_version="p-2")
    assert refuse.outcome == "refuse" and refuse.requirement is None and refuse.approved_by is None
    for d in (auto, human, refuse):
        assert PolicyDecision.model_validate_json(d.model_dump_json()) == d


@pytest.mark.parametrize(
    "raw",
    [
        {"outcome": "human", "rule_id": "x", "policy_version": "p-2", "requirement": None, "reason": "r"},
        {"outcome": "auto", "rule_id": "x", "policy_version": "p-2", "requirement": ONE_APPROVER, "reason": "r"},
        {"outcome": "refuse", "rule_id": "x", "policy_version": "p-2", "requirement": ONE_APPROVER, "reason": "r"},
        {"outcome": "auto", "rule_id": None, "policy_version": "p-2", "requirement": None, "reason": "r"},
        {"outcome": "human", "rule_id": None, "policy_version": "p-2", "requirement": ONE_APPROVER, "reason": ""},
        {"outcome": "allow", "rule_id": None, "policy_version": "p-2", "requirement": None, "reason": "r"},
    ],
)
def test_policy_decision_rejects_inconsistent(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(raw)


def test_fallback_human_decision_without_rule() -> None:
    d = PolicyDecision.model_validate(
        {"outcome": "human", "rule_id": None, "policy_version": "p-2", "requirement": ONE_APPROVER,
         "reason": "No rule matched; one approver required."}
    )
    assert d.approved_by is None


# ──────────────────────────────────── quorum ─────────────────────────────────


def _appr(sub: str, role: str, name: str = "") -> QuorumApproval:
    return QuorumApproval(sub=sub, name=name or sub.title(), role=role, at=AT)


def test_four_eyes_needs_one_of_each_role() -> None:
    req = Requirement.model_validate(FOUR_EYES)
    view = QuorumView.build(req)
    assert view.remaining == 2 and view.missing_roles == ["approver", "compliance"] and not view.met
    view = view.with_approval(sub="u-marco", name="Marco Rossi", roles=["approver"], at=AT, initiator_sub="u-priya")
    assert view.remaining == 1 and view.missing_roles == ["compliance"]
    # a second approver cannot stand in for compliance
    assert quorum_role_for(req, view.approvals, sub="u-anna", roles=["approver"], initiator_sub=None) is None
    with pytest.raises(ValueError, match="cannot advance"):
        view.with_approval(sub="u-anna", name="Anna", roles=["approver"], at=AT, initiator_sub=None)
    # the same person cannot approve twice, even with another role
    assert quorum_role_for(req, view.approvals, sub="u-marco", roles=["compliance"], initiator_sub=None) is None
    # the initiator is excluded
    assert quorum_role_for(req, view.approvals, sub="u-priya", roles=["compliance"], initiator_sub="u-priya") is None
    view = view.with_approval(sub="u-lena", name="Lena Vogel", roles=["compliance", "approver"], at=AT, initiator_sub=None)
    assert [a.role for a in view.approvals] == ["approver", "compliance"]
    assert view.met and view.remaining == 0 and view.missing_roles == []
    # once met, nobody else counts
    assert quorum_role_for(req, view.approvals, sub="u-x", roles=["approver"], initiator_sub=None) is None


def test_role_preference_and_order_independence() -> None:
    req = Requirement.model_validate(FOUR_EYES)
    # Lena (compliance) first, then Marco: same outcome as the other order
    first = quorum_role_for(req, [], sub="u-lena", roles=["approver", "compliance"], initiator_sub=None)
    assert first == "approver"  # both missing: requirement order wins
    lena_only_compliance = quorum_role_for(req, [], sub="u-lena", roles=["compliance"], initiator_sub=None)
    assert lena_only_compliance == "compliance"
    view = QuorumView.build(req, [_appr("u-lena", "compliance")])
    assert quorum_role_for(req, view.approvals, sub="u-marco", roles=["approver", "compliance"], initiator_sub=None) == "approver"


def test_any_of_quorum_and_counts_above_roles() -> None:
    approach = Requirement.model_validate({"roles": ["planner", "approver", "tenant-admin"], "count": 1,
                                           "exclude_initiator": False})
    assert not approach.every_role
    assert quorum_missing_roles(approach, []) == []
    assert quorum_role_for(approach, [], sub="u-priya", roles=["planner"], initiator_sub=None) == "planner"
    assert quorum_role_for(approach, [], sub="u-x", roles=["viewer"], initiator_sub=None) is None
    assert QuorumView.build(approach, [_appr("u-priya", "planner")]).met
    # three people, both roles covered
    three = Requirement.model_validate({"roles": ["approver", "compliance"], "count": 3, "exclude_initiator": False})
    approvals = [_appr("u-a", "approver")]
    assert quorum_remaining(three, ["approver"]) == 2
    assert quorum_role_for(three, approvals, sub="u-b", roles=["approver"], initiator_sub=None) == "approver"
    approvals.append(_appr("u-b", "approver"))
    assert quorum_role_for(three, approvals, sub="u-c", roles=["approver"], initiator_sub=None) is None
    assert quorum_role_for(three, approvals, sub="u-c", roles=["compliance"], initiator_sub=None) == "compliance"
    # exclude_initiator without a known initiator does not block anyone
    assert quorum_role_for(Requirement.model_validate(FOUR_EYES), [], sub="u-priya", roles=["approver"], initiator_sub=None) == "approver"


@pytest.mark.parametrize(
    ("why", "raw"),
    [
        ("remaining wrong", {"required": FOUR_EYES, "approvals": [], "remaining": 1,
                             "missing_roles": ["approver", "compliance"]}),
        ("missing_roles wrong", {"required": FOUR_EYES, "approvals": [], "remaining": 2,
                                 "missing_roles": ["compliance"]}),
        ("duplicate sub", {"required": FOUR_EYES, "remaining": 0, "missing_roles": [],
                           "approvals": [{"sub": "u", "name": "U", "role": "approver", "at": "2026-10-07T10:00:00Z"},
                                         {"sub": "u", "name": "U", "role": "compliance",
                                          "at": "2026-10-07T10:00:00Z"}]}),
        ("role outside requirement", {"required": ONE_APPROVER, "remaining": 0, "missing_roles": [],
                                      "approvals": [{"sub": "u", "name": "U", "role": "planner",
                                                     "at": "2026-10-07T10:00:00Z"}]}),
        ("naive timestamp", {"required": ONE_APPROVER, "remaining": 0, "missing_roles": [],
                             "approvals": [{"sub": "u", "name": "U", "role": "approver",
                                            "at": "2026-10-07T10:00:00"}]}),
    ],
)
def test_quorum_view_rejects_inconsistent(why: str, raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        QuorumView.model_validate(raw)


def test_quorum_view_wire_round_trip() -> None:
    view = QuorumView.build(FOUR_EYES, [{"sub": "u-marco", "name": "Marco Rossi", "role": "approver",
                                         "at": "2026-10-07T10:00:00Z"}])
    wire = view.model_dump(mode="json")
    assert wire == {
        "required": FOUR_EYES,
        "approvals": [{"sub": "u-marco", "name": "Marco Rossi", "role": "approver", "at": "2026-10-07T10:00:00Z"}],
        "remaining": 1,
        "missing_roles": ["compliance"],
    }
    assert QuorumView.model_validate(wire) == view


# ─────────────────────────────────── receipts ────────────────────────────────


def _claims(**kw: Any) -> ApprovalReceiptClaims:
    data: dict[str, Any] = {
        "approval_id": "APR-0002", "change_set_ref": "cs-ext-1", "change_set_hash": HASH,
        "tenant": "demo-retail", "keelson_sub": "u-marco", "ext_sub": "m-marco",
        "approved_by": approved_by_user("marco"), "approver_name": "Marco Rossi",
        "initiated_by": "Priya Shah", "policy_version": "p-2", "now": 1_791_190_800,
        "jti": "4f9f8a8e-0c7e-4c39-9f43-6c1b7f1f8e12",
    }
    data.update(kw)
    return ApprovalReceiptClaims.issue(**data)


M1_CLAIM_KEYS = {
    "iss", "aud", "iat", "exp", "jti", "approval_id", "change_set_ref", "change_set_hash", "tenant",
    "keelson_sub", "ext_iss", "ext_sub", "approved_by", "approver_name", "initiated_by", "policy_version",
}
PRE_M5_RECEIPT_HASH = "7828b3d51ea579d9b82036c2ed21c504fa6de9f4ada36f8d06ceb83d90aaf146"


def test_receipt_without_co_approvers_is_the_m1_claim_set() -> None:
    claims = _claims().to_claims()
    assert set(claims) == M1_CLAIM_KEYS
    assert claims["approved_by"] == "user:marco" and claims["approver_name"] == "Marco Rossi"
    policy = _claims(approved_by=approved_by_policy("auto-small-transfer", "p-2"), approver_name=None,
                     keelson_sub="keelson-autocommit").to_claims()
    assert set(policy) == M1_CLAIM_KEYS


def test_receipt_co_approved_by() -> None:
    claims = _claims(co_approved_by=["user:lena"]).to_claims()
    assert claims["co_approved_by"] == ["user:lena"]
    assert set(claims) == M1_CLAIM_KEYS | {"co_approved_by"}
    again = ApprovalReceiptClaims.model_validate(claims)
    assert again.co_approved_by == ["user:lena"] and again.to_claims() == claims
    # the M1 consumer view (Merxa ignores unknown claims): every M1 claim is unchanged
    base = _claims().to_claims()
    assert {k: v for k, v in claims.items() if k != "co_approved_by"} == base


@pytest.mark.parametrize(
    "kw",
    [
        {"co_approved_by": ["user:marco"]},  # repeats approved_by
        {"co_approved_by": ["user:lena", "user:lena"]},
        {"co_approved_by": ["lena"]},
        {"co_approved_by": ["policy:x@p-2"]},
        {"co_approved_by": ["user:lena vogel"]},
        {"co_approved_by": [f"user:u{i}" for i in range(10)]},
        {"co_approved_by": ["user:lena"], "approved_by": "policy:auto-small-transfer@p-2", "approver_name": None},
    ],
)
def test_receipt_co_approved_by_rejects(kw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _claims(**kw)


# ─────────────────────────────── remediation checklist ───────────────────────


def test_checklist_block_round_trip_and_completion() -> None:
    block = parse_block(envelope("remediation_checklist@1"))
    assert isinstance(block, RemediationChecklistBlock) and block.placement == "inline"
    props = block.props
    assert props.progress.done == 2 and props.progress.total == 3 and not props.completed
    assert block_to_wire(block)["props"] == GOOD_PROPS["remediation_checklist@1"]
    done = good_props("remediation_checklist@1")
    done["steps"][1].update(status="skipped")
    done["progress"]["done"] = 3
    finished = RemediationChecklistProps.model_validate(done)
    assert finished.completed
    assert RemediationChecklistProps.progress_of(finished.steps).model_dump() == {"done": 3, "total": 3}
    manual = good_props("remediation_checklist@1")
    manual["steps"][1].update(status="done")
    manual["progress"]["done"] = 3
    assert not RemediationChecklistProps.model_validate(manual).completed  # done ≠ verified


def test_checklist_via_make_block() -> None:
    block = make_block("remediation_checklist", good_props("remediation_checklist@1"),
                       title="Guide me through 3 changes", provenance={"kind": "inference",
                                                                   "engine": "keelson-guide@0.1",
                                                                   "as_of": "2026-10-07"})
    assert block.data_ref is None and block.placement == "inline"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1&lines=SKU-1:120", True),
        ("https://erp.example.com:8443/po/PO-1#line-2", True),
        ("/api/cases/c1", True),
        ("http://merxa.localhost", True),
        ("javascript:alert(1)", False),
        ("data:text/html,x", False),
        ("//evil.example/x", False),
        ("http://user:pw@merxa.localhost/x", False),
        ("http://merxa.localhost/a b", False),
        ("http://merxa.localhost/a\\b", False),
        ("http://merxa.localhost/\nx", False),
        ("ftp://merxa.localhost/x", False),
        ("", False),
        ("http://" + "a" * 2000, False),
    ],
)
def test_deep_links(url: str, ok: bool) -> None:
    assert is_safe_deep_link(url) is ok


# ───────────────────────────── commit tracker / undo ─────────────────────────

UNDO = {"type": "undo", "label": "Undo changes", "enabled": True}


def test_commit_tracker_undo_and_rolled_back() -> None:
    props = CommitTrackerProps.model_validate({**good_props("commit_tracker@1"), "actions": [UNDO]})
    assert props.actions[0].type == "undo"
    rolled = CommitTrackerProps.model_validate(
        {**good_props("commit_tracker@1"), "status": "rolled_back",
         "actions": [{**UNDO, "enabled": False, "disabled_reason": "Already rolled back"}]}
    )
    assert rolled.status == "rolled_back"
    refused = CommitTrackerProps.model_validate(
        {**good_props("commit_tracker@1"),
         "actions": [{**UNDO, "enabled": False, "disabled_reason": "Later changes in Merxa"}],
         "conflicts": [{"entity_ref": "TO-0042", "detail": "Changed by Anna after this apply"}]}
    )
    assert refused.conflicts[0].entity_ref == "TO-0042"


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "applying", "actions": [UNDO]},
        {"status": "rolled_back", "actions": [UNDO]},
        {"status": "conflict", "actions": [UNDO]},
        {"actions": [UNDO, UNDO]},
        {"actions": [{**UNDO, "enabled": False}]},
        {"actions": [{**UNDO, "disabled_reason": "x"}]},
        {"actions": [{**UNDO, "type": "redo"}]},
        {"conflicts": [{"entity_ref": "", "detail": "x"}]},
    ],
)
def test_commit_tracker_rejects(mutation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CommitTrackerProps.model_validate({**good_props("commit_tracker@1"), **mutation})


# ───────────────────────── recommended option: apply/guide mix ───────────────


def test_recommended_option_mixed_apply_guide() -> None:
    props = good_props("recommended_option@1")
    props["actions"] = [
        {"type": "apply", "label": "Apply 3 changes for me", "enabled": True},
        {"type": "guide", "label": "Guide me through 2", "enabled": True},
    ]
    props["remediation_mix"] = {"apply_lines": 3, "guide_lines": 2}
    model = RecommendedOptionProps.model_validate(props)
    assert model.remediation_mix is not None and model.remediation_mix.guide_lines == 2
    assert model.model_dump(mode="json")["remediation_mix"] == {"apply_lines": 3, "guide_lines": 2}
    guide_only = {**props, "remediation_mix": {"apply_lines": 0, "guide_lines": 5},
                  "actions": [{"type": "apply", "label": "Apply", "enabled": False,
                               "disabled_reason": "Merxa cannot apply these changes"},
                              {"type": "guide", "label": "Guide me through 5", "enabled": True}]}
    RecommendedOptionProps.model_validate(guide_only)
    with pytest.raises(ValidationError, match="apply_lines"):
        RecommendedOptionProps.model_validate({**props, "remediation_mix": {"apply_lines": 0, "guide_lines": 5}})
    with pytest.raises(ValidationError):
        RecommendedOptionProps.model_validate({**props, "remediation_mix": {"apply_lines": 0, "guide_lines": 0}})
    with pytest.raises(ValidationError):
        RecommendedOptionProps.model_validate({**props, "remediation_mix": {"apply_lines": -1, "guide_lines": 2}})


# ───────────────────────────── narrative: re-plan banner ─────────────────────

BANNER = {"text": "New information: sea-air capacity on CGP-UK1-SA cut to 30% — plan updated (v2)",
          "plan_version": 2, "previous_plan_version": 1, "signal_id": "sig_01"}
VERSIONS = [{"version": 1, "label": "v1 · applied", "status": "committed", "current": False},
            {"version": 2, "label": "v2 · new", "status": "proposed", "current": True}]


def test_narrative_replan_banner() -> None:
    raw = {"markdown": "Sea-air capacity dropped; the plan moves 2 bookings to DC transfers.",
           "what_changed": "2 capacity bookings reduced, 1 transfer added", "banner": BANNER,
           "plan_versions": VERSIONS}
    model = NarrativeProps.model_validate(raw)
    assert model.model_dump(mode="json") == raw
    NarrativeProps.model_validate({"markdown": "x", "banner": {**BANNER, "signal_id": None}})


@pytest.mark.parametrize(
    "mutation",
    [
        {"banner": {**BANNER, "plan_version": 1}},
        {"banner": {**BANNER, "text": ""}},
        {"plan_versions": [VERSIONS[1], VERSIONS[0]]},
        {"plan_versions": [VERSIONS[0], {**VERSIONS[1], "current": False}]},
        {"plan_versions": [{**VERSIONS[0], "current": True}, VERSIONS[1]]},
        {"plan_versions": [VERSIONS[0], {**VERSIONS[1], "version": 1}]},
        {"plan_versions": [{**VERSIONS[0], "status": "draft"}, VERSIONS[1]]},
        {"banner": {**BANNER, "plan_version": 3, "previous_plan_version": 2}, "plan_versions": VERSIONS},
    ],
)
def test_narrative_rejects(mutation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        NarrativeProps.model_validate({"markdown": "x", **mutation})


# ────────────────────────── approval card: quorum + bundle ───────────────────


def test_approval_card_quorum_progress_and_rejection() -> None:
    quorum = QuorumView.build(FOUR_EYES, [_appr("u-marco", "approver", "Marco Rossi")]).model_dump(mode="json")
    props = {**good_props("approval_card@1"), "required_roles": ["approver", "compliance"], "quorum": quorum}
    model = ApprovalCardProps.model_validate(props)
    assert model.quorum is not None and model.quorum.missing_roles == ["compliance"]
    assert model.model_dump(mode="json") == props
    rejected = {**props, "rejection": {"sub": "u-lena", "name": "Lena Vogel", "role": "compliance",
                                       "at": "2026-10-07T10:05:00Z", "reason": "Supplier not approved"}}
    assert ApprovalCardProps.model_validate(rejected).rejection is not None
    with pytest.raises(ValidationError, match="required_roles"):
        ApprovalCardProps.model_validate({**props, "required_roles": ["approver"]})
    with pytest.raises(ValidationError):
        ApprovalCardProps.model_validate({**props, "quorum": {**quorum, "remaining": 0}})


def _cs(connector: str, cs_id: str, items: list[dict[str, Any]]) -> ChangeSet:
    return ChangeSet.model_validate({"id": cs_id, "tenant": "demo-retail", "connector": connector,
                                     "revision": 1, "items": items})


TRANSFER = {"seq": 1, "op": "transfer_order.create",
            "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-19",
                        "lines": [{"sku": "SKU-1", "qty": 120}]}}
PO_CREATE = {"seq": 1, "op": "po.create",
             "payload": {"supplier_code": "SUP-TR", "factory_code": "F-TR-01", "dest_dc": "UK1",
                         "eta_dc": "2026-11-02", "lines": [{"sku": "SKU-2", "qty": 300}]}}
CHANGE_QTY = {"seq": 2, "op": "po_line.change_qty", "target_ref": "PO-26-0101#1", "expected_version": 3,
              "payload": {"qty": 700}}
WMS_OP = {"seq": 1, "op": "wms.wave.release", "target_ref": "W-1", "payload": {"priority": 1}}


def _bundle(**over: Any) -> ChangeSetBundle:
    data: dict[str, Any] = {
        "id": "bnd_01",
        "tenant": "demo-retail",
        "change_sets": [_cs("merxa", "cs_m", [TRANSFER, {**PO_CREATE, "seq": 2}, {**CHANGE_QTY, "seq": 3}]),
                        _cs("fake-wms", "cs_w", [WMS_OP])],
        "atomic_groups": [{"group": "g-nearshore", "connector": "merxa", "seqs": [2, 3]}],
    }
    data.update(over)
    return ChangeSetBundle.model_validate(data)


def test_bundle_hash_binds_order_and_members() -> None:
    bundle = _bundle()
    m, w = bundle.change_sets
    assert bundle.connectors == ["merxa", "fake-wms"] and bundle.lines == 4
    assert bundle.ops == {"transfer_order.create", "po.create", "po_line.change_qty", "wms.wave.release"}
    expected = sha256_hex({"v": 1, "change_sets": [{"connector": "merxa", "hash": m.hash},
                                                  {"connector": "fake-wms", "hash": w.hash}]})
    assert bundle.hash == expected == bundle_hash([("merxa", m.hash), ("fake-wms", w.hash)])
    assert bundle_hash([("fake-wms", w.hash), ("merxa", m.hash)]) != bundle.hash  # order is bound
    # each member keeps its own H and idempotency key
    assert m.hash == change_set_hash(m.items) and m.idempotency_key != w.idempotency_key
    assert bundle.change_set("fake-wms") is w and bundle.change_set("sap") is None


def test_single_connector_bundle_keeps_the_m1_hash() -> None:
    single = _bundle(change_sets=[_cs("merxa", "cs_m", [TRANSFER])], atomic_groups=[])
    assert single.hash == single.change_sets[0].hash == change_set_hash([TRANSFER])


@pytest.mark.parametrize(
    ("why", "over"),
    [
        ("two change sets for one connector",
         {"change_sets": [_cs("merxa", "cs_1", [TRANSFER]), _cs("merxa", "cs_2", [TRANSFER])], "atomic_groups": []}),
        ("duplicate change set ids",
         {"change_sets": [_cs("merxa", "cs_1", [TRANSFER]), _cs("fake-wms", "cs_1", [WMS_OP])],
          "atomic_groups": []}),
        ("group split across connectors",
         {"atomic_groups": [{"group": "g", "connector": "merxa", "seqs": [2, 3]},
                            {"group": "g", "connector": "fake-wms", "seqs": [1]}]}),
        ("group on unknown connector", {"atomic_groups": [{"group": "g", "connector": "sap", "seqs": [1]}]}),
        ("group seq outside change set", {"atomic_groups": [{"group": "g", "connector": "fake-wms", "seqs": [1, 2]}]}),
        ("non-adjacent group", {"atomic_groups": [{"group": "g", "connector": "merxa", "seqs": [1, 3]}]}),
        ("overlapping groups", {"atomic_groups": [{"group": "a", "connector": "merxa", "seqs": [1, 2]},
                                                  {"group": "b", "connector": "merxa", "seqs": [2, 3]}]}),
        ("empty bundle", {"change_sets": [], "atomic_groups": []}),
        ("foreign tenant", {"tenant": "other-retail"}),
    ],
)
def test_bundle_rejects(why: str, over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _bundle(**over)


def test_bundle_hash_rejects_bad_members() -> None:
    with pytest.raises(ValueError):
        bundle_hash([])
    with pytest.raises(ValueError):
        bundle_hash([("merxa", HASH), ("merxa", HASH)])
    with pytest.raises(ValueError):
        bundle_hash([("merxa", "ABC")])
    with pytest.raises(ValueError):
        bundle_hash([("", HASH)])
    with pytest.raises(ValidationError):
        AtomicGroup.model_validate({"group": "g", "connector": "merxa", "seqs": []})


def test_approval_card_bundle_members() -> None:
    bundle = _bundle()
    members = [{"connector": cs.connector, "system": cs.connector.title(), "change_set_id": cs.id,
                "change_set_hash": cs.hash, "lines": len(cs.items)} for cs in bundle.change_sets]
    props = {**good_props("approval_card@1"), "change_set_id": bundle.id, "change_set_hash": bundle.hash,
             "summary": {"lines": 4, "units": 1_120, "value_cents": 2_000_000}, "bundle": members}
    assert ApprovalCardProps.model_validate(props).bundle[1].connector == "fake-wms"
    with pytest.raises(ValidationError, match="bundle hash"):
        ApprovalCardProps.model_validate({**props, "bundle": list(reversed(members))})
    with pytest.raises(ValidationError, match="summary.lines"):
        ApprovalCardProps.model_validate({**props, "summary": {"lines": 3, "units": 1, "value_cents": 1}})
    with pytest.raises(ValidationError):
        ApprovalCardProps.model_validate({**props, "bundle": [members[0], {**members[1], "connector": "merxa"}]})


# ─────────────────────────────── case actions / resume ───────────────────────


def _run(payload: dict[str, Any]) -> RunRequest:
    return RunRequest.model_validate(
        {"run_id": "run-1", "tenant": "demo-retail", "case_id": "case-1", "kind": "action",
         "actor": {"sub": "u-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]},
         "payload": payload}
    )


def test_action_types() -> None:
    assert set(ACTION_TYPES) == {"apply", "guide", "promote", "confirm_assumptions", "rollback", "checklist_step"}
    assert OPTION_ACTION_TYPES == {"apply", "guide", "promote", "confirm_assumptions"}


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "guide", "option_id": "opt-A", "plan_version": 2},
        {"type": "apply", "option_id": "opt-A", "plan_version": 2},
        {"type": "rollback", "change_set_id": "cs_01"},
        {"type": "checklist_step", "checklist_id": "chk_1", "seq": 2, "status": "done"},
        {"type": "checklist_step", "checklist_id": "chk_1", "seq": 1, "status": "skipped"},
    ],
)
def test_m5_actions_round_trip(raw: dict[str, Any]) -> None:
    run = _run(raw)
    assert isinstance(run.payload, ActionPayload)
    assert run.payload.model_dump(mode="json") == raw


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "rollback"},
        {"type": "rollback", "change_set_id": ""},
        {"type": "rollback", "change_set_id": "cs_01", "option_id": "opt-A"},
        {"type": "rollback", "change_set_id": "cs_01", "plan_version": 1},
        {"type": "checklist_step", "checklist_id": "chk_1", "seq": 1},
        {"type": "checklist_step", "checklist_id": "chk_1", "seq": 0, "status": "done"},
        {"type": "checklist_step", "checklist_id": "chk_1", "seq": 1, "status": "verified"},
        {"type": "checklist_step", "checklist_id": "chk_1", "seq": 1, "status": "pending"},
        {"type": "checklist_step", "checklist_id": "chk_1", "seq": 1, "status": "done", "option_id": "o"},
        {"type": "checklist_step", "checklist_id": "chk_1", "seq": 1, "status": "done", "assumption_keys": ["a"]},
        {"type": "apply", "option_id": "opt-A"},
        {"type": "guide", "plan_version": 1},
        {"type": "apply", "option_id": "opt-A", "plan_version": 1, "change_set_id": "cs_01"},
        {"type": "guide", "option_id": "opt-A", "plan_version": 1, "seq": 1},
        {"type": "undo", "change_set_id": "cs_01"},
    ],
)
def test_m5_actions_reject(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ActionPayload.model_validate(raw)


def test_resume_reason_only_on_reject_or_revise() -> None:
    base = {"interrupt_id": "approval-cs_01-1", "decided_by": []}
    rej = ResumePayload.model_validate({**base, "decision": "reject", "reason": "Supplier not approved"})
    assert rej.model_dump(mode="json")["reason"] == "Supplier not approved"
    approve = ResumePayload.model_validate({**base, "decision": "approve", "plan_version": 1, "change_set_hash": HASH})
    assert "reason" not in approve.model_dump(mode="json")
    with pytest.raises(ValidationError):
        ResumePayload.model_validate({**base, "decision": "approve", "plan_version": 1, "change_set_hash": HASH,
                                      "reason": "fine"})
    with pytest.raises(ValidationError):
        ResumePayload.model_validate({**base, "decision": "reject", "reason": "x" * (MAX_DECISION_REASON_CHARS + 1)})
    schema = approval_response_schema()
    assert schema["properties"]["reason"]["maxLength"] == MAX_DECISION_REASON_CHARS
    assert schema["required"] == ["decision"] and schema["additionalProperties"] is False


# ───────────────────────────────────── demo reset ────────────────────────────


def _steps(**status: str) -> list[dict[str, Any]]:
    return [{"step": s, "status": status.get(s, "ok"), "duration_ms": 120,
             **({"detail": "Keycloak unreachable"} if status.get(s) == "failed" else {})}
            for s in DEMO_RESET_STEPS]


def test_demo_reset_result() -> None:
    raw = {"ok": True, "keep_consents": False, "started_at": "2026-10-07T10:00:00Z", "duration_ms": 8_200,
           "steps": [{**_steps()[0], "counts": {"cases": 3, "run_event": 812}}, *_steps()[1:]]}
    result = DemoResetResult.model_validate(raw)
    assert result.within_target and result.failed_steps == []
    assert DemoResetResult.model_validate_json(result.model_dump_json()) == result
    rehearsal = DemoResetResult.model_validate(
        {**raw, "keep_consents": True, "steps": _steps(merxa_consents="skipped", token_broker="skipped")})
    assert rehearsal.ok
    failed = DemoResetResult.model_validate({**raw, "ok": False, "duration_ms": 31_000,
                                             "steps": _steps(merxa_consents="failed")})
    assert failed.failed_steps == ["merxa_consents"] and not failed.within_target
    assert DemoResetRequest.model_validate({}).keep_consents is False
    assert CONSENT_STEPS <= set(DEMO_RESET_STEPS)


@pytest.mark.parametrize(
    "mutation",
    [
        {"ok": True, "steps": _steps(scn_resync="failed")},
        {"ok": False, "steps": _steps()},
        {"keep_consents": True, "steps": _steps()},
        {"steps": [*_steps(), _steps()[0]]},
        {"steps": []},
        {"steps": [{"step": "x", "status": "failed", "duration_ms": 1}]},
        {"steps": [{"step": "Bad Step", "status": "ok", "duration_ms": 1}]},
        {"steps": [{"step": "x", "status": "ok", "duration_ms": -1}]},
        {"steps": [{"step": "x", "status": "ok", "duration_ms": 1, "counts": {"cases": -1}}]},
        {"steps": [{"step": "x", "status": "ok", "duration_ms": 1, "counts": {"Cases Table": 1}}]},
        {"steps": [{"step": "x", "status": "ok", "duration_ms": 1, "counts": {"c" * 65: 1}}]},
        {"started_at": "2026-10-07T10:00:00"},
    ],
)
def test_demo_reset_result_rejects(mutation: dict[str, Any]) -> None:
    raw = {"ok": True, "keep_consents": False, "started_at": "2026-10-07T10:00:00Z", "duration_ms": 1, "steps": _steps()}
    with pytest.raises(ValidationError):
        DemoResetResult.model_validate({**raw, **mutation})


# ──────────────────────────────── backward compatibility ─────────────────────

PRE_M5_KEYS = ("approval_card@1", "commit_tracker@1", "recommended_option@1", "narrative@1")
PINNED_BLOCK_HASHES = {  # pinned before M5 (test_m4.PRE_M4_BLOCK_HASHES)
    "recommended_option@1": "04f34390b3800b126153b7fd9a5ce47df2b2004024dd0a88f75f8635eed37147",
    "narrative@1": "559d80a6f19dc5eba3ff616b1337dbe3733a9f10fc585fa7f556700380d4ddc2",
}


@pytest.mark.parametrize("key", PRE_M5_KEYS)
def test_pre_m5_blocks_serialize_unchanged(key: str) -> None:
    wire = block_to_wire(parse_block(envelope(key)))
    assert set(wire["props"]) == set(GOOD_PROPS[key])  # no M5 key appears at its default
    if key in PINNED_BLOCK_HASHES:
        assert sha256_hex(wire) == PINNED_BLOCK_HASHES[key]
    else:  # these M1 examples spell out every field, so the wire equals the input exactly
        assert wire["props"] == GOOD_PROPS[key]


def test_pre_m5_actions_and_resumes_serialize_unchanged() -> None:
    for raw in ({"type": "apply", "option_id": "opt-A", "plan_version": 1},
                {"type": "confirm_assumptions", "option_id": "opt-A", "plan_version": 2,
                 "assumption_keys": ["delay_days"]}):
        assert ActionPayload.model_validate(raw).model_dump(mode="json") == raw
    resume = {"interrupt_id": "approval-cs_01-1", "decision": "approve", "plan_version": 1, "change_set_hash": HASH,
              "edits": None, "decided_by": [{"sub": "u", "username": "u", "name": "U", "role": "approver"}]}
    assert ResumePayload.model_validate(resume).model_dump(mode="json") == resume


def test_pre_m5_receipt_hash_pinned() -> None:
    assert sha256_hex(_claims().to_claims()) == PRE_M5_RECEIPT_HASH
