"""M8 adapter sidecar contract additions (``docs/contracts/122-m8-adapter-sidecar.md`` §8):
receipt ``aud`` / ``ext_iss`` overrides (defaults byte-identical to M1–M7) and the optional
snapshot-manifest consistency label."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from keelson_contracts.receipts import (
    MERXA_ISSUER,
    RECEIPT_AUDIENCE,
    RECEIPT_ISSUER,
    ApprovalReceiptClaims,
    approved_by_policy,
    approved_by_user,
)
from keelson_contracts.scn import SCN_ENTITIES, SnapshotManifest

COMMON = dict(approval_id="APR-1", change_set_ref="cs-1", change_set_hash="a" * 64, tenant="demo-retail",
              keelson_sub="sub-marco", ext_sub="merxa-sub", approved_by=approved_by_user("marco"),
              approver_name="Marco Rossi", initiated_by="Priya Shah", policy_version="p-1", now=1_760_000_000,
              jti="jti-1")

M1_GOLDEN = {
    "iss": RECEIPT_ISSUER, "aud": "http://merxa.localhost/mcp", "iat": 1_760_000_000, "exp": 1_760_000_300,
    "jti": "jti-1", "approval_id": "APR-1", "change_set_ref": "cs-1", "change_set_hash": "a" * 64,
    "tenant": "demo-retail", "keelson_sub": "sub-marco", "ext_iss": "http://auth.localhost/realms/merxa",
    "ext_sub": "merxa-sub", "approved_by": "user:marco", "approver_name": "Marco Rossi", "initiated_by": "Priya Shah",
    "policy_version": "p-1",
}


def test_issue_defaults_are_the_m1_claims() -> None:
    claims = ApprovalReceiptClaims.issue(**COMMON).to_claims()  # type: ignore[arg-type]
    assert claims == M1_GOLDEN
    assert json.dumps(claims, sort_keys=True) == json.dumps(M1_GOLDEN, sort_keys=True)
    assert list(claims) == list(M1_GOLDEN)  # field order unchanged too
    assert RECEIPT_AUDIENCE == M1_GOLDEN["aud"] and MERXA_ISSUER == M1_GOLDEN["ext_iss"]


def test_issue_for_another_system() -> None:
    claims = ApprovalReceiptClaims.issue(  # type: ignore[arg-type]
        **{**COMMON, "approved_by": approved_by_policy("auto", "p-1"), "approver_name": None, "ext_sub": "connector:fh"},
        aud="http://fernhill.localhost/mcp", ext_iss="http://fernhill.localhost",
    ).to_claims()
    assert claims["aud"] == "http://fernhill.localhost/mcp" and claims["ext_iss"] == "http://fernhill.localhost"
    assert claims["iss"] == RECEIPT_ISSUER and claims["ext_sub"] == "connector:fh"
    with pytest.raises(ValidationError):
        ApprovalReceiptClaims.issue(**COMMON, aud="")  # type: ignore[arg-type]


def _manifest(**extra: object) -> dict[str, object]:
    return {"snapshot_id": "s-1", "as_of": "2026-10-08T10:00:00Z", "watermark": "j:4", "canonical_model": "scn/1.0",
            "expires_at": "2026-10-08T12:00:00Z",
            "entities": {e: {"rows": 0, "parts": 1, "sha256": ["0" * 64]} for e in SCN_ENTITIES}, **extra}


def test_manifest_consistency_defaults_and_rules() -> None:
    merxa = SnapshotManifest.model_validate({**_manifest(), "wal_lsn": "0/1"})  # Merxa's shape (extra keys ignored)
    assert merxa.consistency == "consistent" and merxa.as_of_range is None and not merxa.label.advisory
    crawled = SnapshotManifest.model_validate(_manifest(
        consistency="crawled", as_of_range={"from": "2026-10-08T09:59:00Z", "to": "2026-10-08T10:00:00Z"}))
    assert crawled.label.advisory and crawled.as_of_range is not None
    explicit = SnapshotManifest.model_validate(_manifest(consistency="consistent", as_of_range=None))
    assert explicit.consistency == "consistent"
    for bad in (_manifest(consistency="crawled"),
                _manifest(consistency="consistent", as_of_range={"from": "2026-10-08T09:59:00Z",
                                                                  "to": "2026-10-08T10:00:00Z"}),
                _manifest(consistency="crawled", as_of_range={"from": "2026-10-08T10:00:00Z",
                                                               "to": "2026-10-08T09:00:00Z"}),
                _manifest(consistency="eventual")):
        with pytest.raises(ValidationError):
            SnapshotManifest.model_validate(bad)
