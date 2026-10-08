"""Wires the runtime to the fakes: FakeCore + Merxa MCP stub + optimizer stub + FakeLlm, with a
real ``DatabaseSessionService`` on a throwaway SQLite file (so a *fresh* Runner can resume)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from google.adk.sessions import DatabaseSessionService

from keelson_engine.runtime import mcp_client, optimizer_client
from keelson_engine.runtime.deps import ConnectorInfo, ToolInfo, set_deps
from keelson_engine.runtime.execute import run_case
from keelson_engine.runtime.llm import FakeModelFactory, FakeScript
from keelson_engine.runtime.mcp_client import CallRecord, McpGateway, proxied_transport_factory
from keelson_engine.runtime.optimizer_client import OptimizerClient
from keelson_engine.runtime.runner_cache import RunnerCache
from keelson_engine.runtime.spec import AgentSpec, PlanningDefaults
from keelson_engine.runtime.toolsets import definition_hash, wire_tool
from keelson_engine.runtime.workflow import WorkflowBindings

from .core import FakeCore, RunRow
from .merxa_stub import MerxaStub
from .optimizer_stub import SECRET, OptimizerStub
from .registry_server import API_KEY_HEADER, RegistryServer

TENANT = "demo-retail"
UNTRUSTED_TOOLS = frozenset({"list_supplier_messages", "list_events"})
PRIYA = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}
MARCO = {"sub": "sub-marco", "username": "marco", "name": "Marco Rossi", "roles": ["planner", "approver"]}


async def snapshot_from(stub: MerxaStub) -> list[ToolInfo]:
    """What E1's bootstrap stores: one tools/list, reads approved, writes approval_gate,
    supplier messages untrusted, plus the platform definition hash."""
    async with Client(FastMCPTransport(stub.mcp)) as client:
        tools = await client.list_tools()
    out: list[ToolInfo] = []
    for tool in tools:
        wire = wire_tool(tool)
        ann = wire.get("annotations") or {}
        read = bool(ann.get("readOnlyHint"))
        out.append(
            ToolInfo(
                name=tool.name,
                title=wire.get("title"),
                description=wire.get("description") or "",
                input_schema=dict(wire.get("inputSchema") or {}),
                output_schema=wire.get("outputSchema"),
                access="read" if read else ("destructive" if ann.get("destructiveHint") else "write"),
                content_trust="untrusted_text" if tool.name in UNTRUSTED_TOOLS else "trusted_data",
                status="approved" if read else "approval_gate",
                definition_hash=definition_hash(wire),
                annotations=dict(ann),
            )
        )
    return out


@dataclass
class Harness:
    tmp: Path
    core: FakeCore = field(default_factory=FakeCore)
    merxa: MerxaStub = field(default_factory=MerxaStub)
    optimizer: OptimizerStub = field(default_factory=OptimizerStub)
    script: FakeScript = field(default_factory=FakeScript)
    mcp_calls: list[CallRecord] = field(default_factory=list)
    case_id: str = "case-1"
    spec_overrides: dict[str, Any] = field(default_factory=dict)
    servers: dict[str, RegistryServer] = field(default_factory=dict)
    """Registry (Tier-A) MCP servers by URL."""
    stubs: dict[str, MerxaStub] = field(default_factory=dict)
    """M5: further W-tier connectors (in-memory stubs) by URL."""

    async def setup(self) -> Harness:
        self.core.connectors.tools = await snapshot_from(self.merxa)
        self.core.repo.add_case(self.case_id)
        set_deps(self.core.deps())
        mcp_client.set_gateway(
            McpGateway(
                lambda: self.core.deps(),
                url_for=self._url_for,
                transport_factory=self._transport,
                observer=self.mcp_calls.append,
            )
        )
        optimizer_client.set_optimizer(
            OptimizerClient(base_url="http://optimizer:8200", secret=SECRET, transport=self.optimizer.transport())
        )
        self.db_url = f"sqlite+aiosqlite:///{self.tmp / 'adk.db'}"
        self.cache = self.new_cache()
        return self

    def _url_for(self, tenant: str, connector: str) -> str | None:
        """The platform Merxa stub keeps its fixed URL; registry connectors use their row's URL."""
        return None if connector in self.core.connectors.registry else "http://merxa.localhost/mcp"

    def _transport(self, url: str, token: str, *, headers: Mapping[str, str] | None = None) -> Any:
        stub = self.stubs.get(url)
        if stub is not None:
            return FastMCPTransport(stub.mcp)
        server = self.servers.get(url)
        if server is not None:
            # the production factory (headers, no redirects, timeouts) over the server's ASGI app
            return proxied_transport_factory(None, client_transport=server.asgi_transport())(url, token, headers=headers)
        return FastMCPTransport(self.merxa.mcp)

    async def add_registry_connector(
        self,
        slug: str,
        server: RegistryServer,
        *,
        auth_kind: str = "static_header",
        tools: list[ToolInfo] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> RegistryServer:
        """Register a Tier-A connector like the admin API would (row + approved snapshot + secret)."""
        await server.start()
        self.servers[server.url] = server
        self.core.connectors.registry[slug] = ConnectorInfo(
            slug=slug, name=f"{server.vendor} (MCP)", url=server.url, issuer="", lanes=(),
            auth_kind=auth_kind, vendor_name=server.vendor,
        )
        self.core.connectors.registry_tools[slug] = tools if tools is not None else await server.snapshot()
        self.core.connectors.secrets[slug] = dict(headers if headers is not None else {API_KEY_HEADER: server.api_key})
        return server

    async def add_stub_connector(self, slug: str, stub: MerxaStub, *, vendor: str,
                                 url: str | None = None, config: Mapping[str, Any] | None = None) -> MerxaStub:
        """Register a second W-tier connector served by an in-memory change-set stub (platform
        principals, so delegated user tokens work like for Merxa)."""
        url = url or f"http://{slug}.example.test/mcp"
        self.stubs[url] = stub
        self.core.connectors.registry[slug] = ConnectorInfo(
            slug=slug, name=f"{vendor} (MCP)", url=url, issuer="", lanes=(), vendor_name=vendor,
            config=dict(config or {}),
        )
        self.core.connectors.registry_tools[slug] = await snapshot_from(stub)
        return stub

    def spec(self, tenant: str) -> AgentSpec:
        planning = PlanningDefaults(as_of=dt.date(2026, 10, 5))
        return AgentSpec(tenant=tenant, planning=planning, **self.spec_overrides)

    def new_cache(self) -> RunnerCache:
        """A brand-new runner cache (new Runner, new session service on the same DB file)."""

        async def spec_loader(tenant: str, version: str) -> AgentSpec:
            return self.spec(tenant)

        async def bindings(tenant: str, spec: AgentSpec) -> WorkflowBindings:
            return WorkflowBindings(models=FakeModelFactory(self.script))

        return RunnerCache(session_service=DatabaseSessionService(db_url=self.db_url), spec_loader=spec_loader, bindings=bindings)

    async def close(self) -> None:
        for server in self.servers.values():
            await server.stop()
        await self.cache.close()
        set_deps(None)
        mcp_client.set_gateway(None)
        optimizer_client.set_optimizer(None)

    def row(
        self,
        kind: str,
        payload: Mapping[str, Any],
        actor: Mapping[str, Any] = PRIYA,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
    ) -> RunRow:
        return RunRow(
            id=run_id or str(uuid.uuid4()), tenant=TENANT, case_id=self.case_id, kind=kind, payload=dict(payload),
            actor=dict(actor), session_id=session_id, invocation_id=invocation_id,
        )

    async def run(self, row: RunRow, *, cache: RunnerCache | None = None, controller: Any = None) -> str:
        lease = await (cache or self.cache).get(row.tenant, row.agent_spec_version)
        try:
            return await run_case(row, deps=self.core.deps(), lease=lease, controller=controller)
        finally:
            await lease.release()

    def resume_row(self, paused: RunRow, decision: str, decided_by: list[Mapping[str, Any]], **extra: Any) -> RunRow:
        created = self.core.interrupts.created[-1]
        deciders = [{k: v for k, v in d.items() if k in ("sub", "username", "name", "role")} for d in decided_by]
        payload = {"interrupt_id": created["interrupt_id"], "decision": decision, "decided_by": deciders, **extra}
        last = dict(decided_by[-1]) if decided_by else dict(PRIYA)
        actor = {"sub": last["sub"], "username": last["username"], "name": last.get("name", ""),
                 "roles": list(last.get("roles", []))}
        return self.row(
            "resume", payload, actor,
            session_id=created["payload"]["session_id"], invocation_id=created["invocation_id"],
        )

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return [e["event"] for e in self.core.bus.events if e["run_id"] == run_id]
