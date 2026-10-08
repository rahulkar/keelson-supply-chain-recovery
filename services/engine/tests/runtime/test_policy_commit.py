"""Approval policy, change-set compilation, receipts (claims from the Decider) and reconcile."""

from __future__ import annotations

import pytest
from fakes.core import FakeCrypto, fake_jwt
from keelson_contracts.changeset import ChangeSet, change_set_hash, idempotency_key
from keelson_contracts.runs import Actor, Decider
from keelson_contracts.scenario import Assumption, DisruptionScenario

from keelson_engine.runtime.commit import (
    approval_id_for,
    compile_changeset,
    mint_receipt,
    token_subject,
)
from keelson_engine.runtime.policy import decide_approval
from keelson_engine.runtime.spec import PolicyConfig

TO = {
    "seq": 1, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
    "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-12", "lines": [{"sku": "PUF-W-BLK-M", "qty": 300}]},
}


def scenario(*, mode: str = "actual", prov: str = "decision") -> DisruptionScenario:
    return DisruptionScenario(
        id="scn-1", tenant="demo-retail", mode=mode, event_family="SUP", event_type="trim_delay", description="d",
        perturbations=[], matched=[],
        assumptions=[Assumption(key="delay_days", label="Delay", value_text="7 days", prov=prov)],
        config_version="default/p-1",
    )


PRIYA = Actor(sub="s-p", username="priya", name="Priya Shah", roles=["planner"])
MARCO = Actor(sub="s-m", username="marco", name="Marco Rossi", roles=["planner", "approver"])


def cs(actions: list[dict[str, object]] | None = None) -> ChangeSet:
    return compile_changeset(tenant="demo-retail", change_set_id="cs-1", connector="merxa", actions=actions or [TO])


def test_compile_assigns_seq_and_hash_matches_merxa() -> None:
    a2 = {**TO, "seq": 7, "payload": {**TO["payload"], "to_dc": "UK1"}}  # type: ignore[dict-item]
    change_set = cs([a2, {**TO, "seq": 3}])
    assert [i.seq for i in change_set.items] == [1, 2]
    assert change_set.items[0].payload.to_dc == "NL1"
    assert change_set.hash == change_set_hash([i.model_dump(mode="json") for i in change_set.items])
    assert change_set.idempotency_key == idempotency_key("demo-retail", "cs-1", 1)
    assert approval_id_for(change_set) == f"APR-{change_set.idempotency_key[:12].upper()}"


def test_policy_refuses_what_if_and_unconfirmed() -> None:
    config = PolicyConfig()
    d = decide_approval(change_set=cs(), scenario=scenario(mode="what_if"), case_mode="actual", initiator=MARCO, config=config)
    assert d.outcome == "refuse" and "what-if" in d.reason
    d = decide_approval(change_set=cs(), scenario=scenario(), case_mode="what_if", initiator=MARCO, config=config)
    assert d.outcome == "refuse"
    d = decide_approval(change_set=cs(), scenario=scenario(prov="assumption"), case_mode="actual", initiator=MARCO, config=config)
    assert d.outcome == "refuse" and "unconfirmed" in d.reason


def test_policy_auto_only_for_small_sets_initiated_by_a_planner() -> None:
    # M5 (95 §1 rule 4): small transfer/allocation-only change sets started by a planner are auto
    config = PolicyConfig()
    auto = decide_approval(change_set=cs(), scenario=scenario(), case_mode="actual", initiator=MARCO, config=config)
    assert auto.outcome == "auto" and auto.approved_by == "policy:auto-small-transfer@p-1"
    assert decide_approval(change_set=cs(), scenario=scenario(), case_mode="actual", initiator=PRIYA,
                           config=config).outcome == "auto"
    admin = Actor(sub="s-a", username="ada", name="Ada Admin", roles=["tenant-admin"])
    human = decide_approval(change_set=cs(), scenario=scenario(), case_mode="actual", initiator=admin, config=config)
    assert human.outcome == "human" and human.quorum == {"roles": ["approver"], "count": 1, "exclude_initiator": False}
    big = {**TO, "payload": {**TO["payload"], "lines": [{"sku": "PUF-W-BLK-M", "qty": 501}]}}  # type: ignore[dict-item]
    assert decide_approval(change_set=cs([big]), scenario=scenario(), case_mode="actual", initiator=MARCO, config=config).outcome == "human"
    many = [{**TO, "seq": i, "payload": {**TO["payload"], "lines": [{"sku": f"S{i}", "qty": 1}]}} for i in range(1, 12)]  # type: ignore[dict-item]
    assert decide_approval(change_set=cs(many), scenario=scenario(), case_mode="actual", initiator=MARCO, config=config).outcome == "human"
    po = {"seq": 1, "op": "po_line.reschedule", "target_ref": "PO-BD-0001#1", "expected_version": 2, "payload": {"eta_dc": "2026-11-05"}}
    assert decide_approval(change_set=cs([po]), scenario=scenario(), case_mode="actual", initiator=MARCO, config=config).outcome == "human"


def test_receipt_claims_from_decider() -> None:
    crypto = FakeCrypto()
    change_set = cs()
    decider = Decider(sub="s-m", username="marco", name="Marco Rossi", role="approver")
    token = fake_jwt({"sub": "merxa-marco-sub"})
    receipt, claims = mint_receipt(
        crypto, change_set=change_set, ext_change_set_id="uuid-ext", keelson_sub=decider.sub, ext_sub=token_subject(token),
        approved_by=f"user:{decider.username}", approver_name=decider.name, initiated_by="Priya Shah", policy_version="p-1",
    )
    data = FakeCrypto.decode(receipt)
    assert data == claims.to_claims()
    assert claims.iss == "http://keelson.localhost" and claims.aud == "http://merxa.localhost/mcp"
    assert claims.change_set_ref == "uuid-ext" and claims.change_set_hash == change_set.hash
    assert claims.approved_by == "user:marco" and claims.approver_name == "Marco Rossi"
    assert claims.ext_sub == "merxa-marco-sub" and claims.keelson_sub == "s-m"
    assert claims.ext_iss == "http://auth.localhost/realms/merxa" and claims.exp - claims.iat == 300
    assert claims.approval_id == approval_id_for(change_set)


def test_token_subject_rejects_non_jwt() -> None:
    with pytest.raises(ValueError):
        token_subject("opaque-token")
    with pytest.raises(ValueError):
        token_subject(fake_jwt({"azp": "x"}))
