"""RunRequest envelope, CaseCursor, approval receipt claims, connector classification."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from keelson_contracts.connectors import (
    ConnectorTier,
    ContentTrust,
    Egress,
    Idempotency,
    ToolAccess,
    ToolClassification,
    detect_tiers,
)
from keelson_contracts.receipts import (
    RECEIPT_KID,
    RECEIPT_TYP,
    ApprovalReceiptClaims,
    approved_by_policy,
    approved_by_user,
    receipt_header,
)
from keelson_contracts.runs import (
    ActionPayload,
    CaseCursor,
    ChatPayload,
    ResumePayload,
    RunRequest,
    adk_app_name,
)

HASH = "d1bbcbc4cb6b0706ba28795e350e70f90c2564efa920a0cdadbc1028667b83bc"
ACTOR = {"sub": "u-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}


def req(kind: str, payload: dict[str, Any]) -> RunRequest:
    return RunRequest.model_validate(
        {"run_id": "run-1", "tenant": "demo-retail", "case_id": "case-1", "kind": kind, "actor": ACTOR, "payload": payload}
    )


class TestRunRequest:
    def test_payload_typed_by_kind(self) -> None:
        assert isinstance(req("chat", {"text": "Meghna Trims is 7 days late"}).payload, ChatPayload)
        action = req("action", {"type": "apply", "option_id": "opt-A", "plan_version": 1})
        assert isinstance(action.payload, ActionPayload)
        resume = req("resume", {"interrupt_id": "approval-cs-1", "decision": "approve", "plan_version": 1, "change_set_hash": HASH})
        assert isinstance(resume.payload, ResumePayload) and resume.payload.interrupt_kind == "approval"
        assert req("signal", {"signal_id": "sig-1"}).kind == "signal"
        assert action.app_name == "t_demo_retail" == adk_app_name("demo-retail")

    def test_json_round_trip(self) -> None:
        r = req("action", {"type": "promote", "option_id": "opt-A", "plan_version": 2})
        assert RunRequest.model_validate_json(r.model_dump_json()) == r

    @pytest.mark.parametrize(
        ("kind", "payload"),
        [
            ("chat", {"option_id": "x"}),  # payload of another kind
            ("chat", {"text": ""}),
            ("action", {"type": "delete", "option_id": "o", "plan_version": 1}),
            ("action", {"type": "apply", "option_id": "o", "plan_version": "1"}),
            ("resume", {"interrupt_id": "i", "decision": "approve"}),  # approve needs hash + plan
            ("resume", {"interrupt_id": "i", "decision": "edit"}),  # edit needs text
            ("resume", {"interrupt_id": "i", "decision": "maybe"}),
            ("telepathy", {"text": "hi"}),
        ],
    )
    def test_rejects_bad(self, kind: str, payload: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            req(kind, payload)

    def test_model_instance_payload_must_match_kind(self) -> None:
        with pytest.raises(ValidationError, match="requires payload ChatPayload"):
            RunRequest(
                run_id="r", tenant="demo-retail", case_id="c", kind="chat", actor=ACTOR,  # type: ignore[arg-type]
                payload=ActionPayload(type="apply", option_id="o", plan_version=1),
            )

    def test_bad_tenant(self) -> None:
        with pytest.raises(ValidationError):
            RunRequest.model_validate(
                {"run_id": "r", "tenant": "Demo Retail", "case_id": "c", "kind": "chat", "actor": ACTOR, "payload": {"text": "x"}}
            )


class TestCaseCursor:
    def test_header_wins_over_query(self) -> None:
        c = CaseCursor.from_request(tenant="demo-retail", case_id="case-1", last_event_id="42", after="7")
        assert c.after_seq == 42 and c.stream_key == "keelson:case:case-1"

    def test_query_and_default(self) -> None:
        assert CaseCursor.from_request(tenant="demo-retail", case_id="c", after="7").after_seq == 7
        assert CaseCursor.from_request(tenant="demo-retail", case_id="c").after_seq == 0
        assert CaseCursor.from_request(tenant="demo-retail", case_id="c", last_event_id="").after_seq == 0

    @pytest.mark.parametrize("bad", ["-1", "abc", "1.5", "²"])
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError):
            CaseCursor.from_request(tenant="demo-retail", case_id="c", last_event_id=bad)


class TestReceipt:
    def claims(self, **kw: Any) -> ApprovalReceiptClaims:
        data: dict[str, Any] = {
            "approval_id": "APR-0001",
            "change_set_ref": "0192f0c0-0000-7000-8000-000000000001",
            "change_set_hash": HASH,
            "tenant": "demo-retail",
            "keelson_sub": "u-marco",
            "ext_sub": "m-marco",
            "approved_by": approved_by_user("marco"),
            "approver_name": "Marco Rossi",
            "initiated_by": "Priya Shah",
            "policy_version": "p-1",
            "now": 1_791_190_800,
            "jti": "4f9f8a8e-0c7e-4c39-9f43-6c1b7f1f8e11",
        }
        data.update(kw)
        return ApprovalReceiptClaims.issue(**data)

    def test_exact_claim_set(self) -> None:
        claims = self.claims().to_claims()
        assert claims == {
            "iss": "http://keelson.localhost",
            "aud": "http://merxa.localhost/mcp",
            "iat": 1_791_190_800,
            "exp": 1_791_191_100,
            "jti": "4f9f8a8e-0c7e-4c39-9f43-6c1b7f1f8e11",
            "approval_id": "APR-0001",
            "change_set_ref": "0192f0c0-0000-7000-8000-000000000001",
            "change_set_hash": HASH,
            "tenant": "demo-retail",
            "keelson_sub": "u-marco",
            "ext_iss": "http://auth.localhost/realms/merxa",
            "ext_sub": "m-marco",
            "approved_by": "user:marco",
            "approver_name": "Marco Rossi",
            "initiated_by": "Priya Shah",
            "policy_version": "p-1",
        }

    def test_header(self) -> None:
        assert receipt_header() == {"alg": "ES256", "kid": "ar-1", "typ": "approval-receipt+jwt"}
        assert (RECEIPT_KID, RECEIPT_TYP) == ("ar-1", "approval-receipt+jwt")

    def test_policy_approval(self) -> None:
        c = self.claims(approved_by=approved_by_policy("small-rebalance", "p-1"), approver_name=None)
        assert c.approval_kind == "policy" and c.approved_by == "policy:small-rebalance@p-1"
        assert self.claims().approval_kind == "user"

    @pytest.mark.parametrize(
        "change",
        [
            {"approved_by": "marco"},
            {"approved_by": "policy:no-version"},
            {"change_set_hash": "abc"},
            {"ttl_s": 0},
            {"tenant": "Demo"},
        ],
    )
    def test_rejects_bad(self, change: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            self.claims(**change)

    def test_fresh_jti_and_time(self) -> None:
        a = ApprovalReceiptClaims.issue(
            approval_id="A", change_set_ref="c", change_set_hash=HASH, tenant="demo-retail",
            keelson_sub="s", ext_sub="e", approved_by="user:marco", approver_name="M",
            initiated_by="P", policy_version="p-1",
        )
        assert a.exp - a.iat == 300 and len(a.jti) == 36


class TestConnectors:
    def test_classification_rules(self) -> None:
        read = ToolClassification(
            access=ToolAccess.READ, content_trust=ContentTrust.TRUSTED_DATA,
            egress=Egress.INTERNAL, idempotency=Idempotency.ENFORCED,
        )
        assert read.investigator_bindable and not read.requires_approval
        messages = read.model_copy(update={"content_trust": ContentTrust.UNTRUSTED_TEXT})
        assert not messages.investigator_bindable
        apply = ToolClassification.model_validate(
            {"access": "destructive", "content_trust": "trusted_data", "egress": "external", "idempotency": "none"}
        )
        assert apply.requires_approval and not apply.auto_executable
        with pytest.raises(ValidationError):
            ToolClassification.model_validate({"access": "admin", "content_trust": "trusted_data", "egress": "internal", "idempotency": "none"})

    def test_detect_tiers(self) -> None:
        merxa = {"get_inventory", "create_change_set", "add_change_set_items", "validate_change_set", "apply_change_set"}
        assert detect_tiers(merxa) == [ConnectorTier.A, ConnectorTier.W]
        assert detect_tiers({"list_items"}) == [ConnectorTier.A]
        full = merxa | {"scn.snapshot_open", "scn.snapshot_export", "scn.changes_since"}
        assert detect_tiers(full) == [ConnectorTier.A, ConnectorTier.B, ConnectorTier.C, ConnectorTier.W]
        assert detect_tiers(set()) == []
