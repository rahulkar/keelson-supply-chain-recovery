"""``RegistryToolset``: connector read tools for LLM investigators, served from the
admin-approved ``connector_tool`` snapshot (never from a live ``tools/list``).

* Only tools that are ``approved`` + ``access=read`` + ``content_trust=trusted_data`` are ever
  bound (``ToolInfo.investigator_bindable``; M2 classification columns of ``connector_tool``,
  falling back to the M1 ``classification`` JSON / annotations when the columns are absent).
  ``needs_review`` / ``disabled`` tools, write/destructive tools and ``untrusted_text`` tools
  (e.g. ``list_supplier_messages``, ``list_events``) never reach an LLM.
* Each toolset serves one connector (a specialist's ``connector`` or the ``answer_connector``);
  calls run through :mod:`runtime.mcp_client` with the **reader** principal only, using that
  connector's auth kind (platform token or registry headers).
* Once per run the snapshot is compared with the server's live definitions
  (:func:`definition_hash`); a changed or vanished tool is dropped and audited.
* An allowlisted tool that is missing from the approved snapshot fails the agent loudly
  (:func:`require_toolset_ok`, installed as the agent's ``before_model_callback``).

``CalcTool`` is the deterministic ``calc`` tool (count/sum/min/max/date_diff over evidence).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

import structlog
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .context import RunContext, current_run
from .deps import ToolInfo
from .errors import McpToolError, RuntimeConfigError
from .jsonptr import JsonPointerError, resolve_pointer

__all__ = [
    "CalcTool",
    "KEELSON_META",
    "RESERVED_TOOL_NAMES",
    "UNTRUSTED_CONTENT",
    "UNTRUSTED_SOURCE_TOOLS",
    "RegistryTool",
    "RegistryToolset",
    "definition_hash",
    "require_toolset_ok",
    "stored_wire",
    "verified_snapshot",
    "wire_tool",
]

log = structlog.get_logger(__name__)

KEELSON_META = "keelson"
"""Key in ``BaseTool.custom_metadata`` describing a Keelson tool (kind, access, trust)."""

UNTRUSTED_SOURCE_TOOLS = frozenset({
    "list_supplier_messages", "list_events",
    # M6 Signal Hub MCP tools (100 §5): news headlines / event titles ride along in their results
    "search_signals", "get_event", "hazards_near_node", "assess_exposure",
})
"""Connector tools that return third-party free text (``90`` §4). They are read only through the
quarantine wrappers (``runtime.quarantine``), whatever the connector snapshot classifies them as
(the platform default lists only ``list_supplier_messages`` as ``untrusted_text``): never bound to
an LLM agent, and ``PolicyPlugin`` denies them as a second line. M6 adds the Signal Hub read tools
(``SignalHubTool`` wrappers pass their structured fields and quarantine their text)."""

UNTRUSTED_CONTENT = frozenset({"untrusted_text", "untrusted_extraction"})
"""``evidence.args.content_trust`` of rows written by the quarantine wrappers
(``runtime.quarantine``: raw untrusted text and its extraction); ``calc`` refuses them."""

RESERVED_TOOL_NAMES = frozenset({"calc", "adk_request_input", "transfer_to_agent", "set_model_response"})
"""Function names owned by Keelson (``calc``) or ADK; a connector tool with one of these names is
never bound to an LLM agent (it would shadow the built-in or duplicate its declaration)."""


def definition_hash(tool: Mapping[str, Any]) -> str:
    """sha256 over a tool's wire definition (``tools/list`` dict, camelCase keys).

    Same material and encoding as the platform snapshot (``core.connectors.definition_hash``):
    name, title, description, inputSchema, outputSchema, annotations; sorted-key JSON."""
    material = {
        "name": tool.get("name"),
        "title": tool.get("title"),
        "description": tool.get("description") or "",
        "inputSchema": tool.get("inputSchema") or {},
        "outputSchema": tool.get("outputSchema"),
        "annotations": tool.get("annotations") or {},
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def wire_tool(tool: Any) -> dict[str, Any]:
    """An MCP ``Tool`` as its wire dict (camelCase, nulls dropped)."""
    if hasattr(tool, "model_dump"):
        return dict(tool.model_dump(mode="json", by_alias=True, exclude_none=True))
    return dict(tool)


def stored_wire(info: ToolInfo) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": info.name,
        "description": info.description,
        "inputSchema": info.input_schema,
        "annotations": info.annotations,
    }
    if info.title is not None:
        data["title"] = info.title
    if info.output_schema is not None:
        data["outputSchema"] = info.output_schema
    return data


async def verified_snapshot(run: RunContext, connector: str) -> list[ToolInfo]:
    """Approved snapshot tools whose definitions still match the live server (cached per run;
    single-flight per connector: M4 runs several specialists of one connector in parallel)."""
    cached = run.verified_tools.get(connector)
    if cached is not None:
        return cached
    async with run.lock(f"verified_tools:{connector}"):
        cached = run.verified_tools.get(connector)
        if cached is not None:
            return cached
        return await _verify_snapshot(run, connector)


async def _verify_snapshot(run: RunContext, connector: str) -> list[ToolInfo]:
    from .mcp_client import get_gateway  # noqa: PLC0415 - avoid import cycle

    deps = run.deps
    snapshot = [
        t for t in await deps.connectors.list_approved_tools(tenant=run.tenant, connector=connector)
        if t.investigator_bindable
    ]
    if not snapshot:
        run.verified_tools[connector] = []
        return []
    async with get_gateway().session(tenant=run.tenant, connector=connector, principal="reader") as session:
        live = {t.name: t for t in await session.list_tools()}
    kept: list[ToolInfo] = []
    for tool in snapshot:
        live_tool = live.get(tool.name)
        reason: str | None = None
        if live_tool is None:
            reason = "tool no longer offered by the connector"
        else:
            stored = tool.definition_hash or definition_hash(stored_wire(tool))
            current = definition_hash(wire_tool(live_tool))
            if stored != current:
                reason = "definition_hash mismatch (tool changed since admin approval)"
        if reason is None:
            kept.append(tool)
            continue
        log.warning("connector tool dropped", tool=tool.name, connector=connector, reason=reason)
        await deps.repo.admin_audit(
            tenant=run.tenant,
            actor="engine-worker",
            action="connector_tool.dropped",
            target=f"{connector}:{tool.name}",
            before={"definition_hash": tool.definition_hash},
            after={"reason": reason},
        )
    run.verified_tools[connector] = kept
    return kept


class RegistryTool(BaseTool):
    """One approved read tool, executed with the reader principal."""

    def __init__(self, info: ToolInfo, *, connector: str) -> None:
        super().__init__(
            name=info.name,
            description=info.description,
            custom_metadata={
                KEELSON_META: {
                    "kind": "registry",
                    "connector": connector,
                    "access": info.access,
                    "content_trust": info.content_trust,
                }
            },
        )
        self._info = info
        self._connector = connector

    @property
    def connector(self) -> str:
        return self._connector

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=self._info.input_schema or {"type": "object", "properties": {}},
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        from .mcp_client import get_gateway  # noqa: PLC0415

        run = current_run()
        try:
            return await get_gateway().call(
                tenant=run.tenant, connector=self._connector, principal="reader", tool=self.name, args=args
            )
        except McpToolError as exc:
            # A tool-level error (bad argument, unknown code) is information for the model;
            # transport failures propagate and fail the specialist.
            return {"error": f"{exc.code}: {exc.detail}"}


class RegistryToolset(BaseToolset):
    """Snapshot-backed toolset filtered to one agent's allowlist."""

    def __init__(self, *, connector: str, allowlist: Iterable[str], owner: str) -> None:
        super().__init__()
        self._connector = connector
        self._allowlist = frozenset(allowlist)
        self._owner = owner
        # ADK's cache is keyed by invocation id, but a resume or crash-retry run continues the
        # paused invocation on the shared (cached) toolset: resolve per call instead, so each
        # run checks its own snapshot (``verified_snapshot`` caches per run) and records its own
        # binding errors.
        self._use_invocation_cache = False

    @property
    def allowlist(self) -> frozenset[str]:
        return self._allowlist

    @property
    def connector(self) -> str:
        return self._connector

    async def get_tools(self, readonly_context: ReadonlyContext | None = None) -> list[BaseTool]:
        if not self._allowlist:
            return []  # e.g. a tenant without connectors: the answer path keeps only calc
        run = current_run()
        tools = await verified_snapshot(run, self._connector)
        refused = RESERVED_TOOL_NAMES | UNTRUSTED_SOURCE_TOOLS
        bindable = {t.name: t for t in tools if t.investigator_bindable and t.name not in refused}
        clashing = sorted(self._allowlist & RESERVED_TOOL_NAMES)
        untrusted = sorted(self._allowlist & UNTRUSTED_SOURCE_TOOLS)
        missing = sorted(self._allowlist - set(bindable) - refused)
        if untrusted:
            # e.g. a spec that lists ``list_events`` on a connector whose snapshot calls it trusted
            run.toolset_errors[self._owner] = (
                f"tools {untrusted} return untrusted text and are only read through quarantine wrappers; "
                f"remove them from the tools of {self._owner}"
            )
        elif clashing:
            # A connector tool named like a Keelson/ADK tool (e.g. a third-party ``calc``) would
            # shadow it or produce duplicate function declarations: never serve it.
            run.toolset_errors[self._owner] = (
                f"tools {clashing} bound to {self._owner} on {self._connector} clash with reserved Keelson tool names"
            )
        elif missing:
            run.toolset_errors[self._owner] = (
                f"tools {missing} are bound to {self._owner} but not approved/available on {self._connector}"
            )
        return [RegistryTool(bindable[name], connector=self._connector) for name in sorted(self._allowlist & set(bindable))]


async def require_toolset_ok(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """``before_model_callback`` for tool-using agents: fail loudly on a broken binding."""
    run = current_run()
    problem = run.toolset_errors.get(callback_context.agent_name)
    if problem:
        raise RuntimeConfigError(problem)
    return None


# ─────────────────────────────────── calc ────────────────────────────────────

CALC_OPS = ("count", "sum", "min", "max", "date_diff")


class CalcTool(BaseTool):
    """Deterministic arithmetic over stored evidence; the result is itself evidence (``calc_id``)."""

    def __init__(self) -> None:
        super().__init__(
            name="calc",
            description=(
                "Deterministic calculation over evidence you already retrieved. op: count (items of an "
                "array), sum/min/max (numbers, or a numeric `field` of each object in an array), "
                "date_diff (days from the date at json_pointer to the date at json_pointer_end). "
                "Cite the returned calc_id as {{calc:<calc_id>}} instead of writing the number."
            ),
            custom_metadata={KEELSON_META: {"kind": "calc", "access": "read", "evidence": "self"}},
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(CALC_OPS)},
                    "evidence_id": {"type": "string", "description": "an _evidence_id from a tool result"},
                    "json_pointer": {"type": "string", "description": "RFC 6901 pointer, e.g. /items"},
                    "field": {"type": "string", "description": "object field for sum/min/max over an array"},
                    "json_pointer_end": {"type": "string", "description": "second date for date_diff"},
                },
                "required": ["op", "evidence_id", "json_pointer"],
            },
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        run = current_run()
        try:
            value = await compute_calc(run, args)
        except CalcError as exc:
            return {"error": f"calc_error: {exc}"}
        calc_id = await run.deps.repo.save_evidence(
            tenant=run.tenant,
            case_id=run.case_id,
            run_id=run.run_id,
            connector="keelson",
            tool="calc",
            args={k: args.get(k) for k in ("op", "evidence_id", "json_pointer", "field", "json_pointer_end")},
            result={"value": value},
            prov="fact",
        )
        run.calc_values[calc_id] = value
        run.evidence_log.append({"id": calc_id, "agent": tool_context.agent_name, "tool": "calc", "args": dict(args)})
        return {"calc_id": calc_id, "value": value}


class CalcError(ValueError):
    pass


def _number(value: Any) -> int:
    # Errors name the type only, never the value: the value is data the model must cite, not read.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalcError(f"not an integer (got a {type(value).__name__})")
    return value


def _date(value: Any) -> dt.date:
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError as exc:
            raise CalcError("not an ISO date (got a string)") from exc
    raise CalcError(f"not an ISO date (got a {type(value).__name__})")


async def compute_calc(run: RunContext, args: Mapping[str, Any]) -> int:
    op = args.get("op")
    if op not in CALC_OPS:
        raise CalcError(f"unknown op {op!r}")
    evidence_id = str(args.get("evidence_id") or "")
    rows = await run.deps.repo.get_evidence(tenant=run.tenant, ids=[evidence_id])
    if not rows or rows[0].get("case_id") not in (None, run.case_id):
        raise CalcError(f"unknown evidence {evidence_id[:80]!r}")
    row_args = rows[0].get("args") or {}
    if isinstance(row_args, Mapping) and row_args.get("content_trust") in UNTRUSTED_CONTENT:
        # A calc result is a fact: never derive one from untrusted text (and never read it back).
        raise CalcError("this evidence holds untrusted text; calc only works on system data")
    doc = rows[0].get("result")
    try:
        target = resolve_pointer(doc, str(args.get("json_pointer") or ""))
    except JsonPointerError as exc:
        raise CalcError(str(exc)) from exc
    if op == "date_diff":
        end_ptr = args.get("json_pointer_end")
        if not end_ptr:
            raise CalcError("date_diff needs json_pointer_end")
        try:
            end = resolve_pointer(doc, str(end_ptr))
        except JsonPointerError as exc:
            raise CalcError(str(exc)) from exc
        return (_date(end) - _date(target)).days
    if not isinstance(target, list):
        raise CalcError("json_pointer must point at an array")
    if op == "count":
        return len(target)
    field = args.get("field")
    values = [_number(item.get(field) if field and isinstance(item, Mapping) else item) for item in target]
    if not values:
        raise CalcError("empty array")
    if op == "sum":
        return sum(values)
    return min(values) if op == "min" else max(values)
