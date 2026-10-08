"""Regression tests for defects found in review (each test names the failure it pins)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from examples import good_props, provenance
from keelson_contracts import agui, blocks, common, runs
from keelson_contracts.changeset import (
    CHANGE_SET_ITEM_ADAPTER,
    PoLineReschedulePayload,
    TransferOrderCreatePayload,
    change_set_hash,
    idempotency_key,
)
from keelson_contracts.receipts import ApprovalReceiptClaims
from keelson_contracts.scenario import DisruptionScenario, Perturbation, Window

HASH = "d1bbcbc4cb6b0706ba28795e350e70f90c2564efa920a0cdadbc1028667b83bc"
ACTOR = {"sub": "u-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}


def merxa_preview_hash(items: Iterable[Mapping[str, Any]]) -> str:
    """Independent re-statement of ``merxa_api.canonical.preview_hash`` (Merxa never imports Keelson)."""
    rows = sorted(items, key=lambda it: int(it["seq"]))
    data = [
        {
            "seq": int(it["seq"]),
            "op": it["op"],
            "target_ref": it.get("target_ref"),
            "expected_version": it.get("expected_version"),
            "payload": it.get("payload") or {},
        }
        for it in rows
    ]
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ──────────────── change-set hash must equal Merxa's preview_hash ────────────────


class TestHashMatchesMerxa:
    BASE = {"seq": 1, "op": "po_line.cancel", "target_ref": "PO-26-0101#1", "expected_version": 2}

    @pytest.mark.parametrize(
        "item",
        [
            {**BASE, "payload": None},  # was hashed as null; Merxa hashes {}
            dict(BASE),  # was rejected; Merxa hashes {}
            {**BASE, "payload": {}},
            {**BASE, "payload": {"reason": "late", "lines": [1, 2]}},
            {"seq": 2, "op": "transfer_order.create", "payload": {"from_dc": "DE1", "to_dc": "NL1"}},
        ],
    )
    def test_equal_for_merxa_edge_cases(self, item: dict[str, Any]) -> None:
        assert change_set_hash([item]) == merxa_preview_hash([item])

    def test_non_object_payload_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            change_set_hash([{**self.BASE, "payload": [1]}])


# ────────────────────────── ISO dates are strict ──────────────────────────


class TestIsoDate:
    @pytest.mark.parametrize("bad", [1760832000, 0, "2026-10-19T00:00:00Z", "2026-10-19T23:00:00", "20261019"])
    def test_change_set_dates_reject_timestamps_and_datetimes(self, bad: object) -> None:
        # Pydantic's lax date turned 1760832000 into 2025-10-19 and dropped the time of
        # datetime strings, silently changing what Merxa would apply.
        with pytest.raises(ValidationError):
            PoLineReschedulePayload.model_validate({"eta_dc": bad})
        with pytest.raises(ValidationError):
            TransferOrderCreatePayload.model_validate(
                {"from_dc": "DE1", "to_dc": "NL1", "ship_date": bad, "lines": [{"sku": "S", "qty": 1}]}
            )

    def test_datetime_object_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PoLineReschedulePayload(eta_dc=dt.datetime(2026, 11, 9, 12, 0))

    def test_accepts_iso_string_and_date(self) -> None:
        assert PoLineReschedulePayload.model_validate({"eta_dc": "2026-11-09"}).eta_dc == dt.date(2026, 11, 9)
        assert PoLineReschedulePayload(eta_dc=dt.date(2026, 11, 9)).model_dump(mode="json") == {"eta_dc": "2026-11-09"}
        with pytest.raises(ValidationError):
            PoLineReschedulePayload.model_validate({"eta_dc": "2026-02-30"})

    def test_item_via_union(self) -> None:
        with pytest.raises(ValidationError):
            CHANGE_SET_ITEM_ADAPTER.validate_python(
                {"seq": 1, "op": "po_line.reschedule", "target_ref": "PO-1#1", "expected_version": 1, "payload": {"eta_dc": 1}}
            )

    def test_window_and_block_provenance(self) -> None:
        with pytest.raises(ValidationError):
            Window.model_validate({"start": 0})
        with pytest.raises(ValidationError):
            blocks.BlockProvenance.model_validate({"kind": "fact", "engine": "e", "as_of": 0})

    def test_json_schema_still_format_date(self) -> None:
        class M(BaseModel):
            d: common.IsoDate

        assert M.model_json_schema()["properties"]["d"] == {"format": "date", "title": "D", "type": "string"}


# ────────────────────────── canonical JSON ──────────────────────────


def test_lone_surrogate_is_a_canonical_json_error() -> None:
    # Previously escaped as a bare UnicodeEncodeError from sha256_hex.
    with pytest.raises(common.CanonicalJsonError, match="surrogate"):
        common.canonical_json({"a": "\ud800"})
    with pytest.raises(common.CanonicalJsonError):
        common.sha256_hex(["ok", "\udfff"])


def test_idempotency_key_rejects_separator_collisions() -> None:
    # ("t", "a|1", 2) and ("t", "a", "1|2") would share the hashed text "t|a|1|2".
    with pytest.raises(ValueError, match="'\\|'"):
        idempotency_key("demo-retail", "a|1", 2)
    with pytest.raises(ValueError):
        idempotency_key("", "cs_1", 1)


# ────────────────────────── run requests ──────────────────────────


def resume(payload: dict[str, Any]) -> runs.RunRequest:
    return runs.RunRequest.model_validate(
        {"run_id": "r", "tenant": "demo-retail", "case_id": "c", "kind": "resume", "actor": ACTOR, "payload": payload}
    )


class TestResumePayload:
    def test_decided_by_from_record_decision_is_accepted(self) -> None:
        # 55-engine-internals: record_decision inserts payload={interrupt_id, decision, decided_by:[...]};
        # extra="forbid" used to reject every such resume run.
        r = resume(
            {
                "interrupt_id": "approval-cs_01-1",
                "decision": "approve",
                "plan_version": 1,
                "change_set_hash": HASH,
                "decided_by": [{"sub": "u-marco", "username": "marco", "name": "Marco Rossi", "role": "approver"}],
            }
        )
        assert isinstance(r.payload, runs.ResumePayload)
        assert r.payload.decided_by[0].username == "marco"
        assert runs.ResumePayload(interrupt_id="approach-c-1", decision="confirm").decided_by == []

    def test_duplicate_deciders_rejected(self) -> None:
        d = {"sub": "u-marco", "username": "marco", "name": "Marco Rossi"}
        with pytest.raises(ValidationError, match="same sub"):
            runs.ResumePayload(interrupt_id="approval-x-1", decision="reject", decided_by=[d, d])  # type: ignore[list-item]

    @pytest.mark.parametrize(
        ("interrupt_id", "decision"),
        [("approach-case-1-1", "approve"), ("approach-case-1-1", "reject"), ("approval-cs-1-1", "confirm"), ("approval-cs-1-1", "what_if")],
    )
    def test_decision_must_match_interrupt_kind(self, interrupt_id: str, decision: str) -> None:
        extra = {"plan_version": 1, "change_set_hash": HASH} if decision == "approve" else {}
        with pytest.raises(ValidationError, match="not valid for an"):
            resume({"interrupt_id": interrupt_id, "decision": decision, **extra})

    def test_cursor_beyond_bigint_rejected(self) -> None:
        assert runs.CaseCursor.from_request(tenant="demo-retail", case_id="c", after=str(2**63 - 1)).after_seq == 2**63 - 1
        for bad in (str(2**63), "9" * 30):
            with pytest.raises(ValueError):
                runs.CaseCursor.from_request(tenant="demo-retail", case_id="c", last_event_id=bad)
        with pytest.raises(ValidationError):
            runs.CaseCursor(tenant="demo-retail", case_id="c", after_seq=2**63)


# ────────────────────────── approval receipts ──────────────────────────


def claims(**kw: Any) -> ApprovalReceiptClaims:
    data: dict[str, Any] = {
        "approval_id": "APR-0001",
        "change_set_ref": "cs-1",
        "change_set_hash": HASH,
        "tenant": "demo-retail",
        "keelson_sub": "u-marco",
        "ext_sub": "m-marco",
        "approved_by": "user:marco",
        "approver_name": "Marco Rossi",
        "initiated_by": "Priya Shah",
        "policy_version": "p-1",
        "now": 1_791_190_800,
    }
    data.update(kw)
    return ApprovalReceiptClaims.issue(**data)


class TestReceiptConsistency:
    def test_policy_version_must_match_approved_by(self) -> None:
        assert claims(approved_by="policy:small-rebalance@p-1", approver_name=None).approval_kind == "policy"
        with pytest.raises(ValidationError, match="policy_version"):
            claims(approved_by="policy:small-rebalance@p-2", approver_name=None)

    @pytest.mark.parametrize("name", [None, ""])
    def test_user_approval_needs_approver_name(self, name: str | None) -> None:
        with pytest.raises(ValidationError, match="approver_name"):
            claims(approver_name=name)


# ────────────────────────── AG-UI builders ──────────────────────────


SC = agui.ScMetadata(tenant_id="demo-retail", case_id="c", stage="verified", prov="committed", label="Done")


class TestAgui:
    def test_custom_rejects_nan(self) -> None:
        # json.dumps(allow_nan=True) used to let NaN through; the AG-UI round trip then
        # failed with a misleading "does not round-trip" error (NaN != NaN).
        with pytest.raises(ValueError, match="JSON compliant"):
            agui.custom(SC, name="sc.x", value={"v": float("nan")})

    def test_validate_event_rejects_non_strict_json_even_without_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(agui, "_EVENT_ADAPTER", None)
        event = {"type": "CUSTOM", "name": "sc.x", "value": float("inf"), "metadata": {"sc": SC.wire()}}
        with pytest.raises(agui.AguiEventError, match="strict JSON"):
            agui.validate_event(event)

    def test_sse_frame_is_strict(self) -> None:
        with pytest.raises(ValueError):
            agui.sse_frame(1, {"type": "CUSTOM", "value": float("nan")})
        with pytest.raises(ValueError):
            agui.sse_frame(-1, {"type": "CUSTOM"})
        frame = agui.sse_frame(7, {"type": "CUSTOM", "value": "line1\nline2\r"})
        assert frame.count("\n") == 4 and "\r" not in frame  # one data line only

    def test_subagent_events_only_for_listed_roles(self) -> None:
        # A plumbing node could previously emit SUBAGENT_STARTED by passing a description.
        with pytest.raises(ValueError, match="only"):
            agui.subagent_started(SC, subagent_run_id="i:frame_case", name="frame_case", description="Framing")
        event = agui.subagent_started(SC, subagent_run_id="i:p", name="critic", description="Critic (round 2)")
        assert event["description"] == "Critic (round 2)"

    @pytest.mark.parametrize("ids", [("", "r"), ("c", "")])
    def test_run_finished_requires_ids(self, ids: tuple[str, str]) -> None:
        with pytest.raises(ValueError):
            agui.run_finished_success(SC, thread_id=ids[0], run_id=ids[1])
        with pytest.raises(ValueError):
            agui.run_finished_interrupt(
                SC,
                thread_id=ids[0],
                run_id=ids[1],
                interrupts=[{"id": "approach-c-1", "reason": "approach", "message": "m", "response_schema": {}}],
            )


# ────────────────────────── blocks ──────────────────────────


class TestBlocks:
    @pytest.mark.parametrize("value", ["yes", 1, "true"])
    def test_ui_flags_are_strict_booleans(self, value: object) -> None:
        props = good_props("kpi_strip@1")
        props["items"][0]["preliminary"] = value
        with pytest.raises(ValidationError):
            blocks.KpiStripProps.model_validate(props)
        rec = good_props("recommended_option@1")
        rec["actions"][0]["enabled"] = value
        with pytest.raises(ValidationError):
            blocks.RecommendedOptionProps.model_validate(rec)

    def test_make_block_defaults_evidence_pack_to_canvas(self) -> None:
        block = blocks.make_block(
            "evidence_pack", good_props("evidence_pack@1"), title="Evidence", provenance=provenance("fact")
        )
        assert block.placement == "canvas"
        assert blocks.make_block("narrative", {"markdown": "x"}, title="t", provenance=provenance()).placement == "inline"
        with pytest.raises(ValidationError, match="canvas-only"):
            blocks.make_block(
                "evidence_pack", good_props("evidence_pack@1"), title="E", provenance=provenance(), placement="inline"
            )


# ────────────────────────── scenario fingerprint ──────────────────────────


def scenario(perturbations: list[dict[str, Any]]) -> DisruptionScenario:
    return DisruptionScenario.model_validate(
        {
            "id": "scn-1",
            "tenant": "demo-retail",
            "mode": "actual",
            "event_family": "SUP",
            "event_type": "SUP.TRIM_DELAY",
            "description": "d",
            "perturbations": perturbations,
            "matched": [],
            "assumptions": [],
            "config_version": "cfg-1",
        }
    )


class TestFingerprint:
    def test_selector_and_week_order_do_not_matter(self) -> None:
        a = {"op": "demand_scale", "target": {"kinds": ["cluster", "store"], "ids": ["UK1-COLD-A", "NL1-MILD-A"]},
             "factor_bp": 11800, "weeks": ["2026-W45", "2026-W46"], "prov": "assumption"}
        b = {**a, "target": {"kinds": ["store", "cluster"], "ids": ["NL1-MILD-A", "UK1-COLD-A"]}, "weeks": ["2026-W46", "2026-W45"]}
        assert scenario([a]).fingerprint() == scenario([b]).fingerprint()
        c = {**a, "weeks": ["2026-W45"]}
        assert scenario([a]).fingerprint() != scenario([c]).fingerprint()

    @pytest.mark.parametrize("weeks", [[], ["2026-W45", "2026-W45"]])
    def test_demand_scale_weeks_must_be_a_non_empty_set(self, weeks: list[str]) -> None:
        from pydantic import TypeAdapter

        with pytest.raises(ValidationError):
            TypeAdapter(Perturbation).validate_python(
                {"op": "demand_scale", "target": {"kinds": ["cluster"], "ids": ["X"]}, "factor_bp": 1, "weeks": weeks, "prov": "fact"}
            )
