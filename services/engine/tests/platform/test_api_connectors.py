"""Connectors API: list, Connect-Merxa OAuth (private_key_jwt + PKCE + prompt=login + resource),
link status, tool snapshot refresh with classification + drift."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_browser import Browser
from e1_env import MERXA_MCP, TENANT_A
from e1_idp import FakeRealm
from keelson_engine.core import connectors
from keelson_engine.core.db import system_session, tenant_session
from test_tokens import MERXA_SPEC

pytestmark = pytest.mark.asyncio(loop_scope="session")

READ = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}

TOOLS: list[dict[str, Any]] = [
    {"name": "get_inventory", "description": "Inventory positions", "inputSchema": {"type": "object"},
     "annotations": READ},
    {"name": "list_supplier_messages", "description": "Supplier portal", "inputSchema": {"type": "object"},
     "annotations": READ},
    {"name": "create_change_set", "description": "Create", "inputSchema": {"type": "object"}, "annotations": WRITE},
    {"name": "add_change_set_items", "description": "Add", "inputSchema": {"type": "object"}, "annotations": WRITE},
    {"name": "validate_change_set", "description": "Validate", "inputSchema": {"type": "object"},
     "annotations": WRITE},
    {"name": "apply_change_set", "description": "Apply", "inputSchema": {"type": "object"},
     "annotations": DESTRUCTIVE},
]


@pytest_asyncio.fixture(loop_scope="session")
async def merxa(platform: None) -> None:
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)


@pytest.fixture
def tool_lister() -> Any:
    calls: list[tuple[str, str]] = []
    state = {"tools": copy.deepcopy(TOOLS)}

    async def lister(url: str, bearer: str) -> list[dict[str, Any]]:
        calls.append((url, bearer))
        return copy.deepcopy(state["tools"])

    connectors.set_tool_lister(lister)
    yield state, calls
    connectors.set_tool_lister(None)


async def _reset_tools() -> None:
    async with system_session() as s:
        await s.execute(text("DELETE FROM connector_tool WHERE connector = 'merxa'"))


async def test_list_connectors(merxa: None, browser_factory: Any) -> None:
    lena: Browser = await browser_factory("lena")
    items = (await lena.get("/api/connectors")).json()["items"]
    merxa_item = next(i for i in items if i["slug"] == "merxa")
    assert merxa_item["name"] == "Merxa Retail Suite" and merxa_item["url"] == MERXA_MCP
    assert merxa_item["link"] == {"status": "missing", "ext_username": None}
    nora: Browser = await browser_factory("nora")
    assert (await nora.get("/api/connectors")).json()["items"] == []
    assert (await nora.get("/api/connectors/merxa/link")).status_code == 404


async def test_connect_flow(merxa: None, browser_factory: Any, merxa_realm: FakeRealm) -> None:
    marco: Browser = await browser_factory("marco")
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("DELETE FROM connector_identity_link WHERE keelson_sub = 'sub-marco'"))
    async with system_session() as s:
        await s.execute(text("UPDATE bff_session SET merxa_username = 'marco.rossi' WHERE sub = 'sub-marco'"))
    resp = await marco.get("/api/connectors/merxa/connect", params={"return_to": "/cases/c1"})
    assert resp.status_code == 302
    loc = urlsplit(resp.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == merxa_realm.auth_endpoint
    params = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert params["client_id"] == "keelson-connect"
    assert params["scope"] == "openid offline_access changes:apply"
    assert params["prompt"] == "login" and params["login_hint"] == "marco.rossi"
    assert params["resource"] == MERXA_MCP and params["code_challenge_method"] == "S256"
    assert params["redirect_uri"] == "http://keelson.localhost/api/connectors/merxa/callback"
    code = merxa_realm.authorize(params, "marco.rossi")

    # a different Keelson session cannot complete marco's flow
    priya: Browser = await browser_factory("priya")
    stolen = await priya.get("/api/connectors/merxa/callback", params={"code": code, "state": params["state"]})
    assert stolen.status_code == 302 and "connect_error=invalid_state" in stolen.headers["location"]

    # restart for marco (state was consumed by the failed attempt)
    resp = await marco.get("/api/connectors/merxa/connect", params={"return_to": "/cases/c1"})
    params = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}
    code = merxa_realm.authorize(params, "marco.rossi")
    done = await marco.get("/api/connectors/merxa/callback", params={"code": code, "state": params["state"]})
    assert done.status_code == 302 and done.headers["location"] == "/cases/c1?connected=merxa"
    exchange = merxa_realm.grants("authorization_code")[-1]["form"]
    assert exchange["client_id"] == "keelson-connect" and "client_assertion" in exchange
    assert exchange["resource"] == MERXA_MCP and exchange["code_verifier"]
    link = (await marco.get("/api/connectors/merxa/link")).json()
    assert link["status"] == "active" and link["ext_username"] == "marco.rossi"
    assert "changes:apply" in link["scopes"] and "ops-director" in link["roles"]
    items = (await marco.get("/api/connectors")).json()["items"]
    assert next(i for i in items if i["slug"] == "merxa")["link"] == {"status": "active", "ext_username": "marco.rossi"}
    async with tenant_session(TENANT_A) as s:
        row = (await s.execute(text(
            "SELECT ext_sub, ext_iss FROM connector_identity_link WHERE keelson_sub = 'sub-marco'"))).one()
    assert row[0] == "sub-marco.rossi" and row[1] == "http://auth.localhost/realms/merxa"


async def test_connect_denied_and_unauthenticated(merxa: None, browser_factory: Any, api: Any) -> None:
    api.cookies.clear()
    resp = await api.get("/api/connectors/merxa/connect", params={"return_to": "/cases/x"})
    assert resp.status_code == 302 and resp.headers["location"].startswith("/api/auth/login?next=")
    marco: Browser = await browser_factory("marco")
    resp = await marco.get("/api/connectors/merxa/connect", params={"return_to": "https://evil.test"})
    params = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}
    denied = await marco.get("/api/connectors/merxa/callback",
                             params={"error": "access_denied", "state": params["state"]})
    assert denied.headers["location"] == "/?connect_error=denied"
    assert (await marco.get("/api/connectors/unknown/connect")).status_code == 404


async def test_refresh_tools_classifies_and_detects_tiers(merxa: None, browser_factory: Any,
                                                          tool_lister: Any) -> None:
    await _reset_tools()
    state, calls = tool_lister
    sam: Browser = await browser_factory("sam")
    marco: Browser = await browser_factory("marco")
    assert (await marco.post("/api/connectors/merxa/refresh-tools")).status_code == 403
    resp = await sam.post("/api/connectors/merxa/refresh-tools")
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["tools"] == 6 and summary["first_snapshot"] is True and summary["tiers"] == ["A", "W"]
    assert calls[-1][0] == MERXA_MCP  # reader token from the broker
    tools = {t.name: t for t in await connectors.list_tools(TENANT_A, "merxa")}
    assert tools["get_inventory"].status == "approved"
    assert tools["get_inventory"].classification.investigator_bindable
    assert tools["list_supplier_messages"].classification.content_trust.value == "untrusted_text"
    assert not tools["list_supplier_messages"].classification.investigator_bindable
    # M2: write tools of a first-party connector are approved classifications (never LLM-bindable)
    assert tools["create_change_set"].status == "approved"
    assert tools["create_change_set"].classification.access.value == "write"
    assert not tools["create_change_set"].classification.investigator_bindable
    assert tools["apply_change_set"].classification.access.value == "destructive"
    assert tools["create_change_set"].suggested is not None and tools["create_change_set"].drift is False
    assert [t.name for t in await connectors.list_approved_tools(TENANT_A, "merxa")] == [t["name"] for t in TOOLS]
    items = (await sam.get("/api/connectors")).json()["items"]
    merxa_item = next(i for i in items if i["slug"] == "merxa")
    assert merxa_item["tier"] == "W" and merxa_item["status"] == "active"

    # unchanged → nothing changes; drift on a first-party connector is re-approved + audited
    again = await connectors.refresh_tool_snapshot(TENANT_A, "merxa")
    assert again["changed"] == [] and again["added"] == []
    state["tools"][0]["description"] = "Inventory positions (v2)"
    drift = await connectors.refresh_tool_snapshot(TENANT_A, "merxa")
    assert drift["changed"] == ["get_inventory"] and drift["needs_review"] == []
    async with tenant_session(TENANT_A) as s:
        audit = (await s.execute(text(
            "SELECT after FROM admin_audit WHERE action = 'connector.tools.drift' ORDER BY id DESC LIMIT 1"
        ))).scalar_one()
    assert audit["changed"] == ["get_inventory"]

    # third-party drift → needs_review and hidden from agents; removed tools are marked
    state["tools"][0]["description"] = "Inventory positions (v3)"
    state["tools"].pop()  # apply_change_set disappears
    review = await connectors.refresh_tool_snapshot(TENANT_A, "merxa", auto_approve=False)
    assert review["needs_review"] == ["get_inventory"] and review["removed"] == ["apply_change_set"]
    names = [t.name for t in await connectors.list_approved_tools(TENANT_A, "merxa")]
    assert "get_inventory" not in names and "apply_change_set" not in names
    changed = next(t for t in await connectors.list_tools(TENANT_A, "merxa") if t.name == "get_inventory")
    assert changed.status == "needs_review" and changed.drift is True
    conn = await connectors.get_connector(TENANT_A, "merxa")
    # tool review state lives on the tools; the platform connector stays active
    assert conn.status == "active" and conn.tiers == ["A"]
    await _reset_tools()


async def test_connector_row(merxa: None) -> None:
    conn = await connectors.get_connector(TENANT_A, "merxa")
    assert conn.clients == {"reader": "keelson-reader", "preparer": "keelson-preparer",
                            "autocommit": "keelson-autocommit", "connect": "keelson-connect"}
    assert conn.lanes[0]["from_dc"] == "DE1" and conn.trust == "first_party"
    assert await connectors.list_tenants_with_connector("merxa") == [TENANT_A]
    with pytest.raises(connectors.ConnectorNotFound):
        await connectors.get_connector("northwind-test", "merxa")
