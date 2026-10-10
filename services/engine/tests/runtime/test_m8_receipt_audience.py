"""M8 (``docs/contracts/122-m8-adapter-sidecar.md`` §8): approval receipts are bound to the system
they are for. The platform Merxa connector keeps Merxa's ``aud`` / ``ext_iss`` (byte-identical
claims); a non-platform change-set connector (an adapter sidecar registered with a static header)
gets its own MCP URL as ``aud`` and its origin as ``ext_iss`` — for the apply and for the
rollback — so a receipt minted for one system cannot be replayed at another."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fakes.core import LANES, FakeCrypto
from fakes.harness import Harness
from fakes.m5 import ADA, ALL_M5_OPS, MARCO, PRIYA, TO, action_row, approve_row, decider, option, seed_plan
from fakes.merxa_stub import MerxaStub
from keelson_contracts.receipts import MERXA_ISSUER, RECEIPT_AUDIENCE

from keelson_engine.runtime.deps import ConnectorInfo
from keelson_engine.runtime.nodes import commit_nodes

SIDECAR_URL = "http://fernhill.localhost/mcp"


@pytest.fixture
async def platform_h(tmp_path: Path) -> AsyncIterator[Harness]:
    h = await Harness(tmp=tmp_path, merxa=MerxaStub(supported_ops=ALL_M5_OPS)).setup()
    try:
        yield h
    finally:
        await h.close()


@pytest.fixture
async def sidecar_h(tmp_path: Path) -> AsyncIterator[Harness]:
    """The planning connector ``merxa`` registered as a third-party static-header connector at an
    adapter sidecar's URL (the change-set stub stands in for the sidecar)."""
    h = await Harness(tmp=tmp_path, merxa=MerxaStub(supported_ops=ALL_M5_OPS)).setup()
    conns = h.core.connectors
    conns.registry["merxa"] = ConnectorInfo(slug="merxa", name="Fernhill ERP (MCP)", url=SIDECAR_URL, issuer="",
                                            lanes=LANES, auth_kind="static_header", vendor_name="Fernhill",
                                            config=dict(conns.merxa_config))
    conns.registry_tools["merxa"] = list(conns.tools)
    conns.secrets["merxa"] = {"X-Adapter-Key": "fh_" + "k" * 40}
    h.stubs[SIDECAR_URL] = h.merxa
    try:
        yield h
    finally:
        await h.close()


async def _apply_and_rollback(h: Harness) -> tuple[dict[str, Any], dict[str, Any]]:
    await seed_plan(h, [option("opt-A", [TO])])
    row = action_row(h, {"type": "apply", "option_id": "opt-A", "plan_version": 1}, ADA)
    assert await h.run(row) == "paused"
    assert await h.run(approve_row(h, row, [decider(MARCO, "approver")])) == "succeeded"
    [cs] = list(h.core.repo.change_sets.values())
    assert cs["status"] == "verified"
    [apply_call] = [args for tool, args in h.merxa.calls if tool == "apply_change_set"]
    rb = action_row(h, {"type": "rollback", "change_set_id": cs["id"]}, PRIYA)
    assert await h.run(rb) == "paused"
    assert await h.run(approve_row(h, rb, [decider(MARCO, "approver")])) == "succeeded"
    [rb_call] = [args for tool, args in h.merxa.calls if tool == "rollback_change_set"]
    return FakeCrypto.decode(apply_call["receipt"]), FakeCrypto.decode(rb_call["receipt"])


async def test_platform_receipts_keep_merxas_audience(platform_h: Harness) -> None:
    apply_claims, rollback_claims = await _apply_and_rollback(platform_h)
    for claims in (apply_claims, rollback_claims):
        assert claims["aud"] == RECEIPT_AUDIENCE == "http://merxa.localhost/mcp"
        assert claims["ext_iss"] == MERXA_ISSUER
        assert claims["ext_sub"] == f"merxa-{MARCO['sub']}"


async def test_sidecar_receipts_are_bound_to_the_connector(sidecar_h: Harness) -> None:
    apply_claims, rollback_claims = await _apply_and_rollback(sidecar_h)
    for claims in (apply_claims, rollback_claims):
        assert claims["aud"] == SIDECAR_URL
        assert claims["ext_iss"] == "http://fernhill.localhost"
        assert claims["ext_sub"] == "connector:merxa"
        assert claims["approved_by"] == "user:marco" and claims["tenant"] == "demo-retail"
    assert apply_claims["change_set_hash"] == rollback_claims["change_set_hash"]
    assert apply_claims["approval_id"] != rollback_claims["approval_id"]
    # the static header credential was used for every call; no delegated user token was minted
    assert not sidecar_h.core.tokens.issued


async def test_receipt_target(sidecar_h: Harness) -> None:
    class Run:
        tenant = "demo-retail"

    target = await commit_nodes._receipt_target(Run(), "merxa")  # type: ignore[arg-type]  # noqa: SLF001
    assert target == commit_nodes.ReceiptTarget(audience=SIDECAR_URL, ext_iss="http://fernhill.localhost")
    conns = sidecar_h.core.connectors
    conns.registry["erp"] = ConnectorInfo(slug="erp", name="ERP", url="https://erp.example.test/mcp/",
                                          issuer="https://idp.example.test/realms/erp/", lanes=(),
                                          auth_kind="oauth_client_credentials")
    sidecar_h.core.connectors.registry_tools["erp"] = []
    target = await commit_nodes._receipt_target(Run(), "erp")  # type: ignore[arg-type]  # noqa: SLF001
    assert target.claims() == {"aud": "https://erp.example.test/mcp", "ext_iss": "https://idp.example.test/realms/erp"}
    assert commit_nodes.ReceiptTarget().claims() == {}
