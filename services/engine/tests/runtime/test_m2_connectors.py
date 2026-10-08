"""M2 connector registry at runtime: per-connector auth (static header, client credentials,
none) through the production transport factory, toolset filtering by the M2 classification,
and the connector check in the PolicyPlugin."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from fakes.harness import PRIYA, Harness
from fakes.registry_server import API_KEY_HEADER, RegistryServer
from google.adk.models.llm_request import LlmRequest
from keelson_contracts.runs import Actor

from keelson_engine.runtime.context import RunContext, bind_run
from keelson_engine.runtime.deps import ConnectorInfo, ToolInfo
from keelson_engine.runtime.errors import McpTransportError, RuntimeConfigError
from keelson_engine.runtime.mcp_client import (
    ConnectorAuth,
    McpGateway,
    UserPrincipal,
    get_gateway,
    proxied_client_kwargs,
)
from keelson_engine.runtime.plugins import PolicyPlugin
from keelson_engine.runtime.spec import AgentSpec, SpecialistSpec
from keelson_engine.runtime.toolsets import RegistryTool, RegistryToolset, require_toolset_ok

TENANT = "demo-retail"


@dataclasses.dataclass
class Ctx:
    agent_name: str


def run_ctx(h: Harness, **spec: Any) -> RunContext:
    return RunContext(
        tenant=TENANT, case_id=h.case_id, run_id="run-m2", kind="chat", actor=Actor.model_validate(PRIYA),
        payload={}, spec=AgentSpec(tenant=TENANT, **spec), deps=h.core.deps(), invocation_id="e-run-m2",
    )


# ─────────────────────────────── auth kinds ───────────────────────────────


async def test_static_header_connector_calls_through_the_production_transport(harness: Harness) -> None:
    h = harness
    server = await h.add_registry_connector("northwind", RegistryServer())
    data = await get_gateway().call(
        tenant=TENANT, connector="northwind", principal="reader", tool="list_items",
        args={"type": "purchase_order", "filter_json": '{"status": "open"}'},
    )
    assert [r["code"] for r in data["items"]] == ["PO-BD-0001", "PO-BD-0002", "PO-BD-0003"]
    # every HTTP request carried the decrypted header from core.connectors.auth_headers
    assert server.seen_keys and set(server.seen_keys) == {server.api_key}
    assert h.core.connectors.auth_header_calls == [(TENANT, "northwind", "reader")]
    # no platform token was minted for a registry connector
    assert h.core.tokens.issued == []
    [record] = [c for c in h.mcp_calls if c.connector == "northwind"]
    assert record.principal == "reader" and record.ok


async def test_wrong_header_secret_is_a_transport_error(harness: Harness) -> None:
    h = harness
    server = await h.add_registry_connector("northwind", RegistryServer(), headers={API_KEY_HEADER: "mx_wrong"})
    server.seen_keys.clear()  # drop the admin snapshot's requests
    with pytest.raises(McpTransportError):
        await get_gateway().call(tenant=TENANT, connector="northwind", principal="reader", tool="list_items",
                                 args={"type": "sku"})
    assert server.seen_keys and set(server.seen_keys) == {"mx_wrong"} and not server.calls


async def test_concurrent_reads_share_one_streamable_http_session(harness: Harness) -> None:
    """``gather_problem_data`` overlaps independent reads on one session: the streamable HTTP
    client must keep the responses apart."""
    h = harness
    server = await h.add_registry_connector("northwind", RegistryServer())
    codes = [r["code"] for r in server.records["purchase_order"]]
    async with get_gateway().session(tenant=TENANT, connector="northwind", principal="reader") as session:
        results = await asyncio.gather(*(
            session.call("get_item", {"type": "purchase_order", "key": code}) for code in codes * 3
        ))
    assert [r["key"] for r in results] == codes * 3
    assert h.core.connectors.auth_header_calls == [(TENANT, "northwind", "reader")]  # one session, one lookup


async def test_client_credentials_connector_sends_the_bearer_from_auth_headers(harness: Harness) -> None:
    h = harness
    server = RegistryServer()
    await h.add_registry_connector(
        "erp-cc", server, auth_kind="oauth_client_credentials",
        headers={"Authorization": f"Bearer {server.api_key}"},
    )
    data = await get_gateway().call(tenant=TENANT, connector="erp-cc", principal="reader", tool="get_item",
                                    args={"type": "purchase_order", "key": "PO-BD-0002"})
    assert data["data"]["dest_dc"] == "NL1"
    assert server.seen_bearer >= 1 and h.core.connectors.auth_header_calls == [(TENANT, "erp-cc", "reader")]


async def test_header_connectors_refuse_delegated_users_and_unknown_kinds(harness: Harness) -> None:
    h = harness
    await h.add_registry_connector("northwind", RegistryServer())
    gw = get_gateway()
    with pytest.raises(RuntimeConfigError, match="service principals"):
        await gw.auth(tenant=TENANT, connector="northwind", principal=UserPrincipal("sub-marco"))
    h.core.connectors.registry["odd"] = ConnectorInfo(slug="odd", name="Odd", url="http://odd.test/mcp", issuer="",
                                                      lanes=(), auth_kind="kerberos")
    with pytest.raises(RuntimeConfigError, match="unsupported auth kind"):
        await gw.auth(tenant=TENANT, connector="odd", principal="reader")
    h.core.connectors.registry["open"] = ConnectorInfo(slug="open", name="Open", url="http://open.test/mcp", issuer="",
                                                       lanes=(), auth_kind="none")
    assert await gw.auth(tenant=TENANT, connector="open", principal="reader") == ConnectorAuth()
    # platform Merxa keeps the Token Broker
    platform = await gw.auth(tenant=TENANT, connector="merxa", principal="reader")
    assert platform.bearer and not platform.headers
    assert h.core.connectors.auth_header_calls == []
    # a disabled / unknown registry connector (auth_headers raises LookupError) is a config error
    del h.core.connectors.secrets["northwind"]
    with pytest.raises(RuntimeConfigError, match="not active"):
        await gw.auth(tenant=TENANT, connector="northwind", principal="reader")


async def test_registry_connector_url_wins_over_the_platform_override(harness: Harness) -> None:
    """A tenant (e.g. northwind-test) may register ``/mcp-lite`` under the slug ``merxa``: the
    ``MERXA_MCP_URL`` override applies to the platform auth kind only."""
    h = harness
    seen: list[tuple[str, dict[str, str]]] = []

    def factory(url: str, token: str, *, headers: dict[str, str] | None = None) -> Any:
        seen.append((url, dict(headers or {})))
        raise RuntimeError("stop before connecting")

    gw = McpGateway(lambda: h.core.deps(), url_for=lambda t, c: "http://merxa.localhost/mcp", transport_factory=factory)
    h.core.connectors.secrets["merxa"] = {API_KEY_HEADER: "mx_k"}
    for kind, url in (("oauth_platform_merxa", "http://merxa.localhost/mcp"),
                      ("static_header", "http://merxa.localhost/mcp-lite")):
        h.core.connectors.registry["merxa"] = ConnectorInfo(
            slug="merxa", name="Merxa", url="http://merxa.localhost/mcp-lite", issuer="", lanes=(), auth_kind=kind)
        gw.forget()
        with pytest.raises(RuntimeError, match="stop before connecting"):
            await gw.client(tenant=TENANT, connector="merxa", principal="reader")
        assert seen[-1][0] == url
    assert seen[-1][1] == {API_KEY_HEADER: "mx_k"} and seen[0][1] == {}


def test_connector_auth_never_shows_credentials() -> None:
    auth = ConnectorAuth(bearer="tok-secret", headers={API_KEY_HEADER: "mx_live_secret"})
    assert "secret" not in repr(auth) and API_KEY_HEADER in repr(auth)


def test_connector_http_client_uses_the_egress_proxy_without_redirects() -> None:
    kwargs = proxied_client_kwargs("http://egress:3128")
    assert kwargs["proxy"] == "http://egress:3128"
    assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
    assert kwargs["timeout"].connect == 15.0 and kwargs["timeout"].read == 120.0


async def test_connector_rows_are_cached_briefly(harness: Harness) -> None:
    h = harness
    calls = 0
    original = h.core.connectors.get_connector

    async def counting(**kw: Any) -> ConnectorInfo:
        nonlocal calls
        calls += 1
        return await original(**kw)

    h.core.connectors.get_connector = counting  # type: ignore[method-assign]
    gw = get_gateway()
    for _ in range(3):
        await gw.connector_info(tenant=TENANT, connector="merxa")
    assert calls == 1
    gw.forget(tenant=TENANT)
    await gw.connector_info(tenant=TENANT, connector="merxa")
    assert calls == 2


# ─────────────────────────── toolset classification ───────────────────────────


def test_tool_classification_precedence() -> None:
    base = {"name": "t", "description": "", "input_schema": {}, "status": "approved",
            "annotations": {"readOnlyHint": True}}
    # no M2 columns, no classification: an approved read is trusted
    assert ToolInfo.from_row(base).investigator_bindable
    # M1 classification JSON still marks untrusted text
    m1 = ToolInfo.from_row(base | {"classification": {"access": "read", "content_trust": "untrusted_text"}})
    assert m1.content_trust == "untrusted_text" and not m1.investigator_bindable
    # M2 columns win over the JSON and the annotations (top level or in ``extra``)
    m2 = ToolInfo.from_row(base | {"classification": {"access": "read", "content_trust": "trusted_data"},
                                   "access": "write"})
    assert m2.access == "write" and not m2.investigator_bindable
    extra = ToolInfo.from_row(base | {"extra": {"content_trust": "untrusted_text"}})
    assert not extra.investigator_bindable
    for status in ("needs_review", "disabled", "approval_gate"):
        assert not ToolInfo.from_row(base | {"status": status}).investigator_bindable
    # no annotation and no columns: unknown tools are writes
    assert ToolInfo.from_row(base | {"annotations": {}}).access == "write"


def test_real_core_rows_map_through_the_adapter_types() -> None:
    """The E1 ``core.connectors`` row types (``ToolRow`` with a ``ToolClassification``,
    ``ConnectorRow``) map onto the runtime types without losing the M2 classification/status."""
    from keelson_contracts.connectors import ContentTrust, Egress, Idempotency, ToolAccess, ToolClassification

    from keelson_engine.core.connectors import ConnectorRow, ToolRow

    def tool(name: str, access: ToolAccess, trust: ContentTrust, status: str = "approved") -> ToolRow:
        cls = ToolClassification(access=access, content_trust=trust, egress=Egress.INTERNAL, idempotency=Idempotency.NONE)
        return ToolRow(name=name, title=None, description="", input_schema={"type": "object"}, output_schema=None,
                       annotations={"readOnlyHint": True}, classification=cls, definition_hash="h", status=status)

    read = ToolInfo.from_row(tool("list_items", ToolAccess.READ, ContentTrust.TRUSTED_DATA))
    assert (read.access, read.content_trust, read.status, read.investigator_bindable) == (
        "read", "trusted_data", "approved", True)
    # an admin reclassification wins over the readOnlyHint annotation
    assert not ToolInfo.from_row(tool("create_item", ToolAccess.WRITE, ContentTrust.TRUSTED_DATA)).investigator_bindable
    assert not ToolInfo.from_row(tool("list_notes", ToolAccess.READ, ContentTrust.UNTRUSTED_TEXT)).investigator_bindable
    assert not ToolInfo.from_row(tool("list_items", ToolAccess.READ, ContentTrust.TRUSTED_DATA,
                                      "needs_review")).investigator_bindable

    def conn(status: str, auth_kind: str) -> ConnectorRow:
        return ConnectorRow(tenant=TENANT, slug="lite", name="Merxa lite", url="http://merxa.localhost/mcp-lite",
                            issuer="", clients={}, lanes=[], config={"vendor_name": "Merxa"}, tier="A", tiers=["A"],
                            trust="third_party", status=status, auth_kind=auth_kind)

    info = ConnectorInfo.from_row(conn("active", "static_header"))
    assert (info.auth_kind, info.url, info.label, info.usable) == (
        "static_header", "http://merxa.localhost/mcp-lite", "Merxa", True)
    for status in ("draft", "tested", "disabled"):
        assert not ConnectorInfo.from_row(conn(status, "static_header")).usable


async def test_registry_toolset_serves_only_approved_trusted_reads(harness: Harness) -> None:
    h = harness
    server = RegistryServer()
    tools = await server.snapshot(status={"get_item": "needs_review"})
    # an admin approved the write tools (access=write) and a read marked untrusted: none may reach an agent
    tools.append(dataclasses.replace(tools[0], name="list_notes", content_trust="untrusted_text"))
    await h.add_registry_connector("northwind", server, tools=tools)
    ctx = run_ctx(h)
    toolset = RegistryToolset(
        connector="northwind",
        allowlist=["list_items", "get_item", "update_item", "create_transfer_order", "list_notes"],
        owner="answer_investigator",
    )
    with bind_run(ctx):
        bound = await toolset.get_tools()
        assert [t.name for t in bound] == ["list_items"]
        meta = bound[0].custom_metadata["keelson"]
        assert meta == {"kind": "registry", "connector": "northwind", "access": "read", "content_trust": "trusted_data"}
        with pytest.raises(RuntimeConfigError, match="create_transfer_order.*get_item.*list_notes.*update_item"):
            await require_toolset_ok(Ctx("answer_investigator"), LlmRequest())  # type: ignore[arg-type]
    # the live definition check ran over the header-authenticated session
    assert server.seen_keys and set(server.seen_keys) == {server.api_key}
    assert server.writes == 0


async def test_empty_allowlist_or_snapshot_never_opens_a_session(harness: Harness) -> None:
    h = harness
    ctx = run_ctx(h)
    with bind_run(ctx):
        assert await RegistryToolset(connector="nowhere", allowlist=[], owner="answer_investigator").get_tools() == []
        h.core.connectors.registry_tools["empty"] = []
        h.core.connectors.registry["empty"] = ConnectorInfo(slug="empty", name="E", url="http://e.test/mcp",
                                                            issuer="", lanes=(), auth_kind="static_header")
        assert await RegistryToolset(connector="empty", allowlist=["list_items"], owner="x_investigator").get_tools() == []
    assert ctx.toolset_errors["x_investigator"] and not h.mcp_calls


async def test_policy_denies_a_registry_tool_from_another_connector(harness: Harness) -> None:
    h = harness
    server = await h.add_registry_connector("northwind", RegistryServer())
    info = next(t for t in h.core.connectors.registry_tools["northwind"] if t.name == "list_items")
    ctx = run_ctx(
        h,
        specialists=(SpecialistSpec(slug="inventory_demand", tools=("get_inventory", "list_items")),),
        answer_connector="northwind", answer_tools=("list_items",),
    )
    plugin = PolicyPlugin()
    with bind_run(ctx):
        wrong = await plugin.before_tool_callback(
            tool=RegistryTool(info, connector="northwind"), tool_args={},
            tool_context=Ctx("inventory_demand_investigator"),  # type: ignore[arg-type]
        )
        right = await plugin.before_tool_callback(
            tool=RegistryTool(info, connector="northwind"), tool_args={},
            tool_context=Ctx("answer_investigator"),  # type: ignore[arg-type]
        )
    assert wrong and "not one bound to inventory_demand_investigator" in wrong["error"]  # M7: up to three connectors
    assert right is None
    assert not server.calls


# ─────────────────────────── connector status / URL guard ───────────────────────────


@pytest.mark.parametrize("status", ["disabled", "draft", "tested"])
@pytest.mark.parametrize("auth_kind", ["oauth_platform_merxa", "none", "static_header"])
async def test_inactive_connectors_are_never_called(harness: Harness, status: str, auth_kind: str) -> None:
    """Regression: the gateway only refused non-active connectors through
    ``core.connectors.auth_headers``; the Token Broker (platform Merxa) and ``none`` auth called a
    disabled connector anyway (problem gathering, snapshot check, commits)."""
    h = harness
    server = await h.add_registry_connector("northwind", RegistryServer(), auth_kind=auth_kind)
    h.core.connectors.registry["northwind"] = dataclasses.replace(
        h.core.connectors.registry["northwind"], status=status
    )
    server.calls.clear()
    gw = get_gateway()
    gw.forget()
    with pytest.raises(RuntimeConfigError, match=f"is {status}, not active"):
        await gw.call(tenant=TENANT, connector="northwind", principal="reader", tool="list_items",
                      args={"type": "sku"})
    with pytest.raises(RuntimeConfigError):  # a caller-supplied token does not bypass it either
        await gw.client(tenant=TENANT, connector="northwind", principal=UserPrincipal("sub-x"), token="t")
    assert not server.calls and h.core.tokens.issued == []
    # labels for old evidence still resolve
    assert (await gw.connector_info(tenant=TENANT, connector="northwind")).label == server.vendor


def test_connector_info_status_from_core_rows() -> None:
    row = {"slug": "x", "name": "X", "mcp_url": "http://x.test/mcp", "status": "disabled", "auth_kind": "none"}
    assert not ConnectorInfo.from_row(row).usable
    assert ConnectorInfo.from_row({**row, "status": "active"}).usable
    assert ConnectorInfo.from_row({k: v for k, v in row.items() if k != "status"}).usable  # M1 row


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "gopher://merxa.localhost/", "http://user:pw@merxa.localhost/mcp", "merxa.localhost/mcp"]
)
async def test_only_plain_http_urls_are_dialled(harness: Harness, url: str) -> None:
    h = harness
    h.core.connectors.registry["odd"] = ConnectorInfo(slug="odd", name="Odd", url=url, issuer="", lanes=(),
                                                      auth_kind="none")
    with pytest.raises(RuntimeConfigError, match="invalid MCP URL"):
        await get_gateway().url(tenant=TENANT, connector="odd")
