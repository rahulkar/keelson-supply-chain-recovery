"""Ports between the runtime (E2) and the platform core (E1).

The runtime never imports ``keelson_engine.core`` at module import time: every call goes
through the small protocols below. :class:`CoreAdapter` implements them on top of the E1
modules (``core.repo``, ``core.events``, ``core.tokens``, ``core.crypto``,
``core.connectors``, ``core.runs``, ``core.interrupts``) with the signatures of
``docs/contracts/55-engine-internals.md``; tests install in-memory fakes with
:func:`set_deps`.

Repo calls pass ``tenant=`` explicitly (E1's ``core.repo`` convention).
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from keelson_contracts.receipts import ApprovalReceiptClaims
from keelson_contracts.scenario import DisruptionScenario

from .errors import ConnectRequiredError, RuntimeConfigError

__all__ = [
    "BusPort",
    "ChecklistsPort",
    "ConnectorInfo",
    "ConnectorsPort",
    "CoreAdapter",
    "CryptoPort",
    "HEADER_AUTH_KINDS",
    "INACTIVE_CONNECTOR_STATUSES",
    "InterruptsPort",
    "KbPort",
    "MappingsPort",
    "PLATFORM_AUTH_KIND",
    "PlansPort",
    "RepoPort",
    "RunRowLike",
    "RunsPort",
    "RuntimeDeps",
    "ScenarioRecord",
    "SignalHubPort",
    "SignalsPort",
    "TokensPort",
    "ToolInfo",
    "field_of",
    "get_deps",
    "set_deps",
]

log = structlog.get_logger(__name__)

Principal = Literal["reader", "preparer", "autocommit"]
RunStatus = Literal["succeeded", "failed", "paused", "cancelled"]


def field_of(row: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an attribute object (DB rows, dataclasses, models)."""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


# ──────────────────────────────── value types ────────────────────────────────


@dataclass(frozen=True)
class ToolInfo:
    """An admin-approved connector tool from the ``connector_tool`` snapshot."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    title: str | None = None
    access: str = "read"
    content_trust: str = "trusted_data"
    status: str = "approved"
    definition_hash: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    idempotency: str | None = None
    """Admin-confirmed ``enforced | best_effort | none`` (``connector_tool.idempotency``); ``None`` =
    not classified (then the MCP ``idempotentHint`` decides, see :attr:`idempotency_enforced`)."""

    @property
    def idempotency_enforced(self) -> bool:
        """M5 capability table: a write tool may apply lines only when replays are safe —
        ``idempotency = enforced``, or (unclassified rows) no ``idempotentHint: false``."""
        if self.idempotency is not None:
            return self.idempotency == "enforced"
        hint = self.annotations.get("idempotentHint", self.annotations.get("idempotent_hint"))
        return hint is not False

    @property
    def investigator_bindable(self) -> bool:
        return (
            self.status == "approved"
            and self.access == "read"
            and self.content_trust == "trusted_data"
        )

    @classmethod
    def from_row(cls, row: Any) -> ToolInfo:
        """Classification precedence: the M2 ``connector_tool`` columns (``access``,
        ``content_trust``; top-level or in ``extra``) → the M1 ``classification`` JSON → the MCP
        annotations. Without the M2 columns an approved read is ``trusted_data`` unless its M1
        classification says ``untrusted_text``."""
        classification = field_of(row, "classification")
        extra = field_of(row, "extra") or {}
        access = _str(field_of(row, "access")) or _str(field_of(extra, "access"))
        trust = _str(field_of(row, "content_trust")) or _str(field_of(extra, "content_trust"))
        if classification is not None:
            if isinstance(classification, str):
                if classification == "untrusted_text":
                    trust = trust or "untrusted_text"
                elif classification in ("read", "write", "destructive"):
                    access = access or classification
            else:
                access = access or _str(field_of(classification, "access"))
                trust = trust or _str(field_of(classification, "content_trust"))
        annotations = dict(field_of(row, "annotations") or {})
        if access is None:
            ro = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
            destructive = annotations.get("destructiveHint", annotations.get("destructive_hint"))
            access = "read" if ro else ("destructive" if destructive else "write")
        status = _str(field_of(row, "status")) or "approved"
        return cls(
            name=str(field_of(row, "name")),
            description=str(field_of(row, "description") or ""),
            input_schema=dict(field_of(row, "input_schema") or {"type": "object", "properties": {}}),
            output_schema=field_of(row, "output_schema"),
            title=field_of(row, "title"),
            access=str(access),
            content_trust=str(trust or "trusted_data"),
            status=status,
            definition_hash=field_of(row, "definition_hash"),
            annotations=annotations,
            idempotency=_str(field_of(row, "idempotency")) or _str(field_of(extra, "idempotency"))
            or (_str(field_of(classification, "idempotency")) if isinstance(classification, Mapping) else None),
        )


def _str(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


PLATFORM_AUTH_KIND = "oauth_platform_merxa"
"""Merxa platform principals (reader/preparer/autocommit service accounts + delegated users)
through the Token Broker; the M1 default for rows without an ``auth_kind``."""

HEADER_AUTH_KINDS = frozenset({"none", "static_header", "oauth_client_credentials", "service_jwt"})
"""Auth kinds resolved by ``core.connectors.auth_headers`` (M2 connector registry). M6
``service_jwt``: an internal first-party service (the Signal Hub, ``100`` §1, §6) whose headers are
a short-lived engine service JWT (``Authorization: Bearer …``, tenant claim) minted by the platform."""

INACTIVE_CONNECTOR_STATUSES = frozenset({"draft", "tested", "disabled"})
"""Connector statuses the runtime never calls (``core.connectors.ConnectorDisabled``)."""


@dataclass(frozen=True)
class ConnectorInfo:
    slug: str
    name: str
    url: str
    issuer: str
    lanes: tuple[dict[str, Any], ...]
    auth_kind: str = PLATFORM_AUTH_KIND
    """``none | static_header | oauth_client_credentials | oauth_platform_merxa`` (``70`` §3)."""
    vendor_name: str | None = None
    """Short vendor name for user-facing labels ("Read 12 purchase orders from <vendor>")."""
    status: str = "active"
    """Connector lifecycle (``draft | tested | active | disabled``, ``70`` §3); M1 rows without
    a status count as active."""
    config: dict[str, Any] = field(default_factory=dict)
    """Non-secret connector config (M3: optional ``supported_ops``, ``alt_sources``)."""
    tiers: tuple[str, ...] = ()
    """Detected capability tiers (``A``/``B``/``C``/``W``)."""

    @property
    def label(self) -> str:
        return self.vendor_name or self.name or self.slug

    @property
    def usable(self) -> bool:
        """The runtime may call this connector: every status except the non-active M2 ones
        (M1 statuses such as ``pending_snapshot`` were migrated to ``active`` by ``0003``)."""
        return self.status not in INACTIVE_CONNECTOR_STATUSES

    @classmethod
    def from_row(cls, row: Any) -> ConnectorInfo:
        config = field_of(row, "config") or {}
        lanes = field_of(row, "lanes")
        if lanes is None:
            lanes = field_of(config, "lanes") or []
        auth_kind = _str(field_of(row, "auth_kind")) or _str(field_of(config, "auth_kind")) or PLATFORM_AUTH_KIND
        vendor = field_of(row, "vendor_name") or field_of(config, "vendor_name") or field_of(config, "vendor")
        return cls(
            slug=str(field_of(row, "slug")),
            name=str(field_of(row, "name") or field_of(row, "slug")),
            url=str(field_of(row, "url") or field_of(row, "mcp_url") or field_of(config, "mcp_url") or ""),
            issuer=str(field_of(row, "issuer") or field_of(config, "issuer") or ""),
            lanes=tuple(dict(lane) for lane in lanes),
            auth_kind=auth_kind,
            vendor_name=str(vendor) if vendor else None,
            status=_str(field_of(row, "status")) or "active",
            config=dict(config) if isinstance(config, Mapping) else {},
            tiers=tuple(str(t) for t in (field_of(row, "tiers") or ())),
        )


@dataclass(frozen=True)
class ScenarioRecord:
    version: int
    scenario: DisruptionScenario


class RunRowLike(Protocol):
    id: str
    tenant: str
    case_id: str
    kind: str
    payload: Any
    actor: Any
    attempt: int
    invocation_id: str | None
    session_id: str | None
    agent_spec_version: str | None


# ─────────────────────────────────── ports ───────────────────────────────────


@runtime_checkable
class RepoPort(Protocol):
    async def get_case(self, *, tenant: str, case_id: str) -> dict[str, Any] | None: ...
    async def recent_messages(self, *, tenant: str, case_id: str, limit: int) -> list[dict[str, Any]]: ...
    async def add_message(self, *, tenant: str, case_id: str, role: str, text: str, run_id: str | None) -> Any: ...
    async def set_case_mode(self, *, tenant: str, case_id: str, mode: str) -> None: ...
    async def set_case_status(self, *, tenant: str, case_id: str, status: str) -> None: ...
    async def save_scenario(self, *, tenant: str, case_id: str, scenario: DisruptionScenario) -> int: ...
    async def latest_scenario(self, *, tenant: str, case_id: str) -> ScenarioRecord | None: ...
    async def save_evidence(
        self, *, tenant: str, case_id: str, run_id: str, connector: str, tool: str,
        args: dict[str, Any], result: Any, prov: str,
    ) -> str: ...
    async def get_evidence(self, *, tenant: str, ids: Sequence[str]) -> list[dict[str, Any]]: ...
    async def latest_evidence(self, *, tenant: str, case_id: str, tool: str) -> dict[str, Any] | None:
        """The case's newest evidence row written by ``tool`` (M4: restores the investigation
        plan / problem data in a later run of the case; ``None`` = none)."""
        ...
    async def save_plan(
        self, *, tenant: str, case_id: str, version: int, options: list[dict[str, Any]],
        recommended_option_id: str, kpis: dict[str, Any],
    ) -> Any:
        """Returns the plan row id (``core.repo.save_plan``) — the ``plan_id`` of ``scn_pin``."""
        ...
    async def latest_plan(self, *, tenant: str, case_id: str) -> dict[str, Any] | None: ...
    async def mark_plan_status(self, *, tenant: str, case_id: str, version: int, status: str) -> None: ...
    async def save_block(
        self, *, tenant: str, case_id: str, run_id: str, envelope: dict[str, Any], data: Any = None
    ) -> str:
        """``data`` = the large payload served at the block's ``data_ref`` (``GET /api/blocks/{id}/data``)."""
        ...
    async def create_change_set(
        self, *, tenant: str, case_id: str, plan_version: int, option_id: str, connector: str,
        items: list[dict[str, Any]], hash: str,
    ) -> str: ...
    async def update_change_set(self, *, tenant: str, change_set_id: str, **fields: Any) -> None: ...
    async def get_change_set(self, *, tenant: str, change_set_id: str) -> dict[str, Any] | None: ...
    async def list_change_sets(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        """Every engine change set of the case (``id, plan_version, option_id, status, ext_*``),
        oldest first — used to keep one plan option from being applied twice."""
        ...
    async def ledger_mark(
        self, *, tenant: str, change_set_id: str, line_seq: int, state: str, ext_ref: str | None = None
    ) -> None: ...
    async def ledger_record(self, *, tenant: str, entry: Any) -> None:
        """M7 per-op: upsert one full ``CommitLedgerEntry`` (state, attempts, request id, read-back,
        error) as the line's ``commit_ledger`` row."""
        ...
    async def admin_audit(
        self, *, tenant: str, actor: str, action: str, target: str, before: Any = None, after: Any = None
    ) -> None: ...
    async def get_agent_spec(self, *, tenant: str, version: str) -> dict[str, Any] | None: ...
    async def save_findings(
        self, *, tenant: str, case_id: str, task_id: str, specialist: str, run_id: str, findings: dict[str, Any],
        status: str, task_input: dict[str, Any] | None = None,
    ) -> None:
        """Upserts ``investigation_task`` (``task_input`` → its ``input``: plan task id, rev,
        depends_on, assumption_keys) and appends the ``finding`` row."""
        ...
    async def list_investigation_tasks(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        """The case's task runs, oldest first: ``{task_id: "<plan task id>-r<rev>", id, specialist,
        status, input, findings}`` (latest finding per row) — the follow-up dirty set reads them."""
        ...


class BusPort(Protocol):
    async def append(
        self, *, tenant: str, case_id: str, run_id: str | None, event: dict[str, Any],
        persist: bool = True, live: bool = True,
    ) -> int: ...


class TokensPort(Protocol):
    async def service_token(self, *, tenant: str, connector: str, principal: Principal) -> str: ...
    async def user_token(self, *, tenant: str, connector: str, keelson_sub: str) -> str: ...
    async def link_status(self, *, tenant: str, connector: str, keelson_sub: str) -> dict[str, Any]: ...


class CryptoPort(Protocol):
    def sign_approval_receipt(self, claims: ApprovalReceiptClaims) -> str: ...


class ConnectorsPort(Protocol):
    async def get_connector(self, *, tenant: str, slug: str) -> ConnectorInfo: ...
    async def list_approved_tools(self, *, tenant: str, connector: str) -> list[ToolInfo]: ...
    async def get_tenant_llm_key(self, *, tenant: str) -> str: ...
    async def refresh_tool_snapshots(self) -> None: ...
    async def auth_headers(self, *, tenant: str, connector: str, principal: str) -> dict[str, str]:
        """HTTP headers authenticating ``principal`` to a registry connector whose auth kind is
        ``static_header`` / ``oauth_client_credentials`` / ``none`` (secrets decrypted by E1;
        never logged). Backed by ``core.connectors.auth_headers(tenant, connector_slug, principal)``."""
        ...
    async def tenant_llm_key_version(self, *, tenant: str) -> str | None:
        """Opaque version of the tenant's LiteLLM virtual key; changes on every rotation
        (``None`` = no key). The runner cache rebuilds a tenant's runners when it changes."""
        ...
    def forget_tenant_llm_key(self, *, tenant: str) -> None:
        """Drop any in-process copy of the tenant's (old) virtual key."""
        ...


class MappingsPort(Protocol):
    """M7 (``110`` §4), optional on :class:`ConnectorsPort`: the connector's current action
    mapping (``connector_mapping`` kind ``action``) — an ``ActionMapping`` or its JSON, ``None``
    when the connector has none."""

    async def get_action_mapping(self, *, tenant: str, connector: str) -> Any: ...


class RunsPort(Protocol):
    async def claim_next_run(self, worker_id: str, lease_seconds: int = 60) -> Any | None: ...
    async def heartbeat(self, run_id: str, worker_id: str, lease_seconds: int = 60) -> bool: ...
    async def finish_run(
        self, run_id: str, status: RunStatus, *, invocation_id: str | None = None, error: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Record the final status. With ``worker_id`` (the lease owner that claimed the run) a
        run whose lease now belongs to another worker, or that is already final, is left alone."""
        ...
    async def reap_expired(self, max_attempts: int = 3) -> list[str]: ...
    async def is_cancel_requested(self, run_id: str) -> bool: ...
    async def record_invocation(self, run_id: str, invocation_id: str) -> None: ...


class KbPort(Protocol):
    """Knowledge-base retrieval (``90`` §7, ``core.kb.search``). Results are **untrusted_text**:
    only the quarantine wrapper (``runtime.quarantine``) may call it."""

    async def search(
        self, *, tenant: str, query: str, kb_ids: Sequence[str] | None = None, top_k: int = 6
    ) -> list[dict[str, Any]]:
        """``[{chunk_id, document_title, heading, text, score}]`` best first."""
        ...


class InterruptsPort(Protocol):
    async def create_interrupt(
        self, *, tenant: str, case_id: str, run_id: str, kind: Literal["approach", "approval"],
        interrupt_id: str, payload: dict[str, Any], quorum_spec: dict[str, Any],
        plan_version: int | None, change_set_hash: str | None, invocation_id: str,
        session_id: str | None = None, message: str | None = None,
    ) -> None: ...
    async def supersede_open(self, *, tenant: str, case_id: str, reason: str) -> int: ...


class ChecklistsPort(Protocol):
    """Guide-mode checklists (M5, ``95`` §2). One row per checklist: ``{id, tenant, case_id,
    connector, plan_version, option_id, status: open|completed|cancelled, steps: [ChecklistStep
    JSON], meta: {...}, created_at, updated_at}``. ``steps`` follow ``remediation_checklist@1``."""

    async def create_checklist(
        self, *, tenant: str, case_id: str, checklist_id: str, connector: str, plan_version: int,
        option_id: str, steps: list[dict[str, Any]], meta: dict[str, Any],
    ) -> None:
        """Idempotent on ``checklist_id`` (a crash-retried node re-creates the same id)."""
        ...

    async def get_checklist(self, *, tenant: str, checklist_id: str) -> dict[str, Any] | None: ...

    async def list_open_checklists(self, *, tenant: str | None = None) -> list[dict[str, Any]]:
        """Open checklists (all tenants when ``tenant`` is ``None``: the worker's verify loop)."""
        ...

    async def set_step(
        self, *, tenant: str, checklist_id: str, seq: int, status: str, verified_at: str | None = None,
        evidence_id: str | None = None, actor: str | None = None,
    ) -> dict[str, Any] | None:
        """Update one step under a row lock; returns the updated checklist (``None`` = unknown)."""
        ...

    async def set_status(self, *, tenant: str, checklist_id: str, status: str) -> None: ...

    # optional: ``async def poll() -> int`` — the platform's own read-back loop (``core.checklists.
    # poll_checklists``); the worker prefers it to the runtime's ``guide.GuideVerifier``.


class SignalsPort(Protocol):
    """Signals the runtime reads (M5, ``95`` §4): a ``signal`` run's ``signal_id`` names a stored
    connector event (webhook) — ``{id, connector, type, data, received_at}``. ``data`` holds the
    event's structured fields (facts); free text in it is untrusted."""

    async def get_signal(self, *, tenant: str, signal_id: str) -> dict[str, Any] | None: ...

    async def latest_event_id(self, *, tenant: str, connector: str) -> int | None:
        """Newest stored webhook event of the connector (the guide verify loop's wake-up)."""
        ...


class SignalHubPort(Protocol):
    """Signal Hub reads of a ``signal`` run ``{event_id, alert_id}`` (M6, ``100`` §5, §6), called
    with the run's tenant (the platform mints the service JWT; the tenant claim scopes the data).
    Results are JSON (validated by ``runtime.signal_hub.parse_event_frame``); ``None`` = not found."""

    async def get_alert(self, *, tenant: str, alert_id: str) -> dict[str, Any] | None:
        """``GET /v1/alerts/{id}``: ``AlertDetail`` JSON ``{alert, event, signals, exposures}``."""
        ...

    async def get_event(self, *, tenant: str, event_id: str) -> dict[str, Any] | None:
        """``GET /v1/events/{id}``: ``{event, signals?, exposures?}`` (or the bare event)."""
        ...


class PlansPort(Protocol):
    """Plan versions and dependency fingerprints (M5, ``95`` §4)."""

    async def list_plan_versions(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        """``[{version, status}]`` ascending."""
        ...

    async def save_plan_dependencies(
        self, *, tenant: str, case_id: str, plan_version: int, dependencies: dict[str, Any]
    ) -> None:
        """Index what a plan depends on (``{lanes, dcs, po_lines, connectors, committed_lanes}``) so
        the webhook receiver can find the plans a ``lane.capacity_changed`` affects. The runtime
        also stores the fingerprint in ``plan.kpis.dependencies``."""
        ...


@dataclass
class RuntimeDeps:
    repo: RepoPort
    bus: BusPort
    tokens: TokensPort
    crypto: CryptoPort
    connectors: ConnectorsPort
    runs: RunsPort
    interrupts: InterruptsPort
    reasoning_sink: Callable[[], Any] | None = None
    """Factory of a reasoning coalescer (``EventBus.coalescer``); ``None`` = route by hand."""
    scn: Any = None
    """:class:`~keelson_engine.runtime.scn_store.ScnPort` (canonical snapshot store, M3);
    ``None`` = no snapshots (ProblemData from the MCP gatherer, advisory)."""
    kb: KbPort | None = None
    """Knowledge-base search (M4); ``None`` = no knowledge base (the playbook analyst reports a gap)."""
    checklists: ChecklistsPort | None = None
    """Guide-mode checklists (M5); ``None`` = Guide mode unavailable (the action is refused)."""
    signals: SignalsPort | None = None
    """Stored signals / webhook events (M5); ``None`` = signal runs are rejected."""
    plans: PlansPort | None = None
    """Plan versions + dependency index (M5); ``None`` = only ``plan.kpis.dependencies``."""
    hub: SignalHubPort | None = None
    """Signal Hub reads (M6); ``None`` = signal runs ``{event_id}`` fail visibly (nothing framed)."""


# ───────────────────────────── E1 core adapter ──────────────────────────────


async def _call(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Call an E1 function with only the keyword arguments its signature accepts.

    Lets the adapter follow the documented names while tolerating optional extras (e.g. a
    function that binds the tenant from ``core.context.use_tenant`` instead of ``tenant=``).
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None
    if sig is not None and not any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def task_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """An ``investigation_task`` row as a follow-up task record (``task_id = <plan id>-r<rev>``)."""
    raw = row.get("input")
    payload: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    plan_id, rev = payload.get("task_id"), payload.get("rev")
    task_id = f"{plan_id}-r{rev}" if plan_id and rev else str(row.get("id"))
    return {"task_id": task_id, "id": row.get("id"), "specialist": row.get("specialist"),
            "status": row.get("status"), "input": dict(payload), "findings": row.get("findings")}


def _maybe(module: Any, name: str) -> Callable[..., Awaitable[Any]] | None:
    fn = getattr(module, name, None)
    return fn if callable(fn) else None


class CoreAdapter:
    """Implements every port on top of the E1 core modules (lazy imports)."""

    def __init__(self) -> None:
        from keelson_engine.core import (  # noqa: PLC0415 - lazy: E1 modules need settings/DB
            connectors,
            crypto,
            events,
            interrupts,
            repo,
            runs,
            tokens,
        )

        self._repo = repo
        self._events = events
        self._tokens_mod = tokens
        self._crypto = crypto
        self._connectors = connectors
        self._runs = runs
        self._interrupts = interrupts
        self.repo: RepoPort = _RepoAdapter(repo)  # type: ignore[assignment]

    # bus
    def bus(self) -> BusPort:
        return self._events.get_event_bus()  # type: ignore[no-any-return]

    def deps(self) -> RuntimeDeps:
        bus = self.bus()
        return RuntimeDeps(
            repo=self.repo,
            bus=bus,
            tokens=_TokensAdapter(self._tokens_mod),
            crypto=_CryptoAdapter(self._crypto),
            connectors=_ConnectorsAdapter(self._connectors),
            runs=_RunsAdapter(self._runs),
            interrupts=_InterruptsAdapter(self._interrupts),
            reasoning_sink=getattr(bus, "coalescer", None),
            scn=_scn_adapter(),
            kb=_KbAdapter(),
            checklists=_checklists_adapter(),
            signals=_SignalsAdapter(),
            plans=_PlansAdapter(self._repo),
            hub=_SignalHubAdapter(),
        )


# ─────────────────────────────── M5 adapters ────────────────────────────────


def _core_module(name: str) -> Any | None:
    try:
        import importlib  # noqa: PLC0415

        return importlib.import_module(f"keelson_engine.core.{name}")
    except ImportError:
        return None


_CHECKLIST_FUNCS = ("create_checklist", "get_checklist", "list_open_checklists", "set_checklist_step",
                    "set_checklist_status")


def _checklists_adapter() -> ChecklistsPort | None:
    """``core.checklists`` (M5 platform): ``create_checklist``, ``get_checklist``,
    ``list_open_checklists``, ``set_checklist_step``, ``set_checklist_status``. ``None`` while the
    platform module is missing (Guide mode then refuses politely)."""
    mod = _core_module("checklists")
    if mod is None or not all(callable(getattr(mod, f, None)) for f in _CHECKLIST_FUNCS):
        log.info("guide checklists unavailable (core.checklists missing)")
        return None
    return _ChecklistsAdapter(mod)


class _ChecklistsAdapter:
    def __init__(self, mod: Any) -> None:
        self._m = mod
        if callable(getattr(mod, "poll_checklists", None)):
            self.poll = self._poll  # the platform's own read-back loop (webhooks + 60 s poll)

    async def create_checklist(self, **kwargs: Any) -> None:
        # block_id: the platform refreshes the remediation_checklist@1 block in place on read-back
        from keelson_contracts.common import stable_block_id  # noqa: PLC0415

        kwargs.setdefault("block_id", stable_block_id(kwargs["case_id"], "remediation_checklist",
                                                      kwargs["checklist_id"]))
        await _call(self._m.create_checklist, **kwargs)

    async def _poll(self) -> int:
        """``core.checklists.poll_checklists()``: verifies every (tenant, connector) with an open
        checklist that got a webhook event or was not checked for 60 s; returns steps verified."""
        results = await _call(self._m.poll_checklists)
        return sum(len(field_of(r, "verified") or []) for r in results or [])

    async def get_checklist(self, *, tenant: str, checklist_id: str) -> dict[str, Any] | None:
        row = await _call(self._m.get_checklist, tenant=tenant, checklist_id=checklist_id)
        return None if row is None else dict(row)

    async def list_open_checklists(self, *, tenant: str | None = None) -> list[dict[str, Any]]:
        return [dict(r) for r in await _call(self._m.list_open_checklists, tenant=tenant) or []]

    async def set_step(self, **kwargs: Any) -> dict[str, Any] | None:
        row = await _call(self._m.set_checklist_step, **kwargs)
        return None if row is None else dict(row)

    async def set_status(self, *, tenant: str, checklist_id: str, status: str) -> None:
        await _call(self._m.set_checklist_status, tenant=tenant, checklist_id=checklist_id, status=status)


class _SignalsAdapter:
    """``core.signals.get_signal`` when the platform provides it, else one tenant-scoped read of
    ``connector_event`` (M2 table): ``signal_id`` = the row id or the event's ``webhook-id``."""

    async def get_signal(self, *, tenant: str, signal_id: str) -> dict[str, Any] | None:
        mod = _core_module("signals")
        fn = _maybe(mod, "get_signal") if mod is not None else None
        if fn is not None:
            row = await _call(fn, tenant=tenant, signal_id=signal_id)
            return None if row is None else dict(row)
        from sqlalchemy import text  # noqa: PLC0415

        from keelson_engine.core.db import tenant_session  # noqa: PLC0415

        async with tenant_session(tenant) as s:
            res = await s.execute(
                text(
                    "SELECT id, connector, event_id, type, payload, received_at FROM connector_event "
                    "WHERE tenant = :t AND (id::text = :s OR event_id = :s) ORDER BY id DESC LIMIT 1"
                ),
                {"t": tenant, "s": str(signal_id)},
            )
            row = res.mappings().first()
        if row is None:
            return None
        payload = row["payload"] if isinstance(row["payload"], Mapping) else {}
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        return {"id": str(row["id"]), "connector": row["connector"], "event_id": row["event_id"],
                "type": row["type"], "data": dict(data), "received_at": row["received_at"]}

    async def latest_event_id(self, *, tenant: str, connector: str) -> int | None:
        from sqlalchemy import text  # noqa: PLC0415

        from keelson_engine.core.db import tenant_session  # noqa: PLC0415

        async with tenant_session(tenant) as s:
            res = await s.execute(
                text("SELECT max(id) FROM connector_event WHERE tenant = :t AND connector = :c"),
                {"t": tenant, "c": connector},
            )
            value = res.scalar_one_or_none()
        return None if value is None else int(value)


class _SignalHubAdapter:
    """``core.signal_hub`` (M6 platform: the hub REST client with the service JWT):
    ``get_alert(tenant, alert_id)`` and ``get_event(tenant, event_id)``. Without the platform
    module, events are read with the Signal Hub connector's MCP tools (``get_event`` +
    ``assess_exposure``, reader principal); alert details need the platform module."""

    async def get_alert(self, *, tenant: str, alert_id: str) -> dict[str, Any] | None:
        mod = _core_module("signal_hub")
        fn = (_maybe(mod, "get_alert") or _maybe(mod, "get_alert_detail")) if mod is not None else None
        if fn is None:
            raise LookupError("alert details need core.signal_hub.get_alert (M6 platform)")
        row = await _call(fn, tenant=tenant, alert_id=alert_id)
        return None if row is None else _jsonable(row)

    async def get_event(self, *, tenant: str, event_id: str) -> dict[str, Any] | None:
        """The event (``core.signal_hub.get_event``, else the hub's MCP ``get_event``) plus its
        exposures from the MCP ``assess_exposure`` when the read carries none (the REST event read
        is the bare ``DisruptionEvent``)."""
        from .mcp_client import get_gateway  # noqa: PLC0415
        from .spec import SIGNAL_HUB_CONNECTOR  # noqa: PLC0415

        mod = _core_module("signal_hub")
        fn = _maybe(mod, "get_event") if mod is not None else None
        gateway = get_gateway()
        out: dict[str, Any]
        if fn is not None:
            row = await _call(fn, tenant=tenant, event_id=event_id)
            if row is None:
                return None
            data = _jsonable(row)
            out = data if isinstance(data.get("event"), Mapping) else {"event": data}
        else:
            event = await gateway.call(tenant=tenant, connector=SIGNAL_HUB_CONNECTOR, principal="reader",
                                       tool="get_event", args={"event_id": event_id})
            out = dict(event) if isinstance(event, Mapping) else {}
        if out and not isinstance(out.get("exposures"), list):
            try:
                exposure = await gateway.call(tenant=tenant, connector=SIGNAL_HUB_CONNECTOR, principal="reader",
                                              tool="assess_exposure", args={"event_id": event_id})
            except Exception as exc:  # the frame then has no exposed nodes (a visible inference note)
                log.info("event exposure unavailable", error=type(exc).__name__)
                exposure = None
            if isinstance(exposure, Mapping) and isinstance(exposure.get("exposures"), list):
                out["exposures"] = list(exposure["exposures"])
        return out or None


def _jsonable(row: Any) -> dict[str, Any]:
    if hasattr(row, "model_dump"):
        return dict(row.model_dump(mode="json"))
    return dict(row)


class _PlansAdapter:
    def __init__(self, repo: Any) -> None:
        self._r = repo

    async def list_plan_versions(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        fn = _maybe(self._r, "list_plan_versions") or _maybe(self._r, "list_plans")
        if fn is not None:
            rows = await _call(fn, tenant=tenant, case_id=case_id)
            return sorted(({"version": int(field_of(r, "version")), "status": str(field_of(r, "status") or "proposed")}
                           for r in rows or []), key=lambda r: r["version"])
        from sqlalchemy import text  # noqa: PLC0415

        from keelson_engine.core.db import tenant_session  # noqa: PLC0415

        async with tenant_session(tenant) as s:
            res = await s.execute(
                text("SELECT version, status FROM plan WHERE tenant = :t AND case_id = :c ORDER BY version"),
                {"t": tenant, "c": case_id},
            )
            return [{"version": int(r[0]), "status": str(r[1] or "proposed")} for r in res.all()]

    async def save_plan_dependencies(
        self, *, tenant: str, case_id: str, plan_version: int, dependencies: dict[str, Any]
    ) -> None:
        fn = _maybe(self._r, "save_plan_dependencies")
        if fn is not None:
            await _call(fn, tenant=tenant, case_id=case_id, plan_version=plan_version, dependencies=dependencies)
            return
        # no index yet: the fingerprint lives in plan.kpis.dependencies (written with the plan)
        from sqlalchemy import text  # noqa: PLC0415

        from keelson_engine.core.db import tenant_session  # noqa: PLC0415
        from keelson_engine.core.sql import dumps  # noqa: PLC0415

        async with tenant_session(tenant) as s:
            await s.execute(
                text(
                    "UPDATE plan SET kpis = jsonb_set(kpis, '{dependencies}', CAST(:d AS jsonb), true) "
                    "WHERE tenant = :t AND case_id = :c AND version = :v"
                ),
                {"t": tenant, "c": case_id, "v": plan_version, "d": dumps(dependencies)},
            )


def _scn_adapter() -> Any:
    from .scn_store import CoreScnAdapter  # noqa: PLC0415

    return CoreScnAdapter()


class _KbAdapter:
    """``core.kb.search(tenant, query, kb_ids, top_k)`` (M4 platform). Until the platform module
    exists the knowledge base is empty (the playbook analyst reports a gap, never fails)."""

    async def search(
        self, *, tenant: str, query: str, kb_ids: Sequence[str] | None = None, top_k: int = 6
    ) -> list[dict[str, Any]]:
        try:
            from keelson_engine.core import kb  # noqa: PLC0415 - optional until the platform ships it
        except ImportError:
            log.warning("knowledge base search unavailable (core.kb missing)")
            return []
        try:
            # ``None`` = all of the tenant's knowledge bases; an empty list = none (same as core.kb.search)
            ids = list(kb_ids) if kb_ids is not None else None
            rows = await _call(kb.search, tenant, query, kb_ids=ids, top_k=top_k)
        except kb.KbError as exc:  # no profile / embedding outage: the playbook analyst reports a gap
            raise LookupError(f"knowledge base unavailable ({exc.code})") from exc
        keys = ("chunk_id", "document_title", "heading", "text", "score")
        return [{k: field_of(r, k) for k in keys} for r in rows or []]


class _RepoAdapter:
    def __init__(self, repo: Any) -> None:
        self._r = repo

    async def get_case(self, *, tenant: str, case_id: str) -> dict[str, Any] | None:
        row = await _call(self._r.get_case, tenant=tenant, case_id=case_id)
        if row is None:
            return None
        if isinstance(row, Mapping):
            return dict(row.get("case", row)) | ({"messages": row["messages"]} if "messages" in row else {})
        return {k: field_of(row, k) for k in ("id", "title", "mode", "status", "created_by")}

    async def recent_messages(self, *, tenant: str, case_id: str, limit: int) -> list[dict[str, Any]]:
        fn = _maybe(self._r, "list_messages") or _maybe(self._r, "recent_messages")
        if fn is not None:
            rows = await _call(fn, tenant=tenant, case_id=case_id, limit=limit)
        else:
            case = await self.get_case(tenant=tenant, case_id=case_id)
            rows = (case or {}).get("messages") or []
        out = [
            {"role": str(field_of(r, "role")), "text": str(field_of(r, "text") or "")}
            for r in list(rows)[-limit:]
        ]
        return out

    async def add_message(self, *, tenant: str, case_id: str, role: str, text: str, run_id: str | None) -> Any:
        return await _call(self._r.add_message, tenant=tenant, case_id=case_id, role=role, text=text, run_id=run_id)

    async def set_case_mode(self, *, tenant: str, case_id: str, mode: str) -> None:
        await _call(self._r.set_case_mode, tenant=tenant, case_id=case_id, mode=mode)

    async def set_case_status(self, *, tenant: str, case_id: str, status: str) -> None:
        await _call(self._r.set_case_status, tenant=tenant, case_id=case_id, status=status)

    async def save_scenario(self, *, tenant: str, case_id: str, scenario: DisruptionScenario) -> int:
        return int(await _call(self._r.save_scenario, tenant=tenant, case_id=case_id, scenario=scenario))

    async def latest_scenario(self, *, tenant: str, case_id: str) -> ScenarioRecord | None:
        row = await _call(self._r.latest_scenario, tenant=tenant, case_id=case_id)
        if row is None:
            return None
        if isinstance(row, tuple):
            version, data = row
        else:
            version = field_of(row, "version")
            data = field_of(row, "scenario", field_of(row, "spec", row))
        scenario = data if isinstance(data, DisruptionScenario) else DisruptionScenario.model_validate(data)
        return ScenarioRecord(version=int(version), scenario=scenario)

    async def save_evidence(
        self, *, tenant: str, case_id: str, run_id: str, connector: str, tool: str,
        args: dict[str, Any], result: Any, prov: str,
    ) -> str:
        return str(
            await _call(
                self._r.save_evidence, tenant=tenant, case_id=case_id, run_id=run_id, connector=connector,
                tool=tool, args=args, result=result, prov=prov,
            )
        )

    async def get_evidence(self, *, tenant: str, ids: Sequence[str]) -> list[dict[str, Any]]:
        rows = await _call(self._r.get_evidence, tenant=tenant, ids=list(ids))
        return [
            {k: field_of(r, k) for k in ("id", "case_id", "run_id", "connector", "tool", "args", "result", "prov",
                                          "created_at")}
            for r in rows or []
        ]

    async def latest_evidence(self, *, tenant: str, case_id: str, tool: str) -> dict[str, Any] | None:
        keys = ("id", "connector", "tool", "args", "result", "prov", "created_at")
        fn = _maybe(self._r, "latest_evidence")
        if fn is not None:
            row = await _call(fn, tenant=tenant, case_id=case_id, tool=tool)
            return None if row is None else {k: field_of(row, k) for k in keys}
        # core.repo has no "latest by tool" helper: one indexed read through the tenant-scoped
        # session (RLS) with an explicit tenant predicate (never the whole case's evidence).
        from sqlalchemy import text  # noqa: PLC0415

        from keelson_engine.core.db import tenant_session  # noqa: PLC0415

        async with tenant_session(tenant) as s:
            res = await s.execute(
                text(
                    f"SELECT {', '.join(keys)} FROM evidence WHERE tenant = :t AND case_id = :c AND tool = :tool "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"t": tenant, "c": case_id, "tool": tool},
            )
            row = res.mappings().first()
        return dict(row) if row is not None else None

    async def save_plan(
        self, *, tenant: str, case_id: str, version: int, options: list[dict[str, Any]],
        recommended_option_id: str, kpis: dict[str, Any],
    ) -> Any:
        return await _call(
            self._r.save_plan, tenant=tenant, case_id=case_id, version=version, options=options,
            recommended_option_id=recommended_option_id, kpis=kpis,
        )

    async def latest_plan(self, *, tenant: str, case_id: str) -> dict[str, Any] | None:
        row = await _call(self._r.latest_plan, tenant=tenant, case_id=case_id)
        if row is None:
            return None
        return {
            k: field_of(row, k)
            for k in ("id", "version", "options", "recommended_option_id", "kpis", "status")
        }

    async def mark_plan_status(self, *, tenant: str, case_id: str, version: int, status: str) -> None:
        fn = _maybe(self._r, "set_plan_status") or _maybe(self._r, "update_plan")
        if fn is not None:
            await _call(fn, tenant=tenant, case_id=case_id, version=version, status=status)

    async def save_block(
        self, *, tenant: str, case_id: str, run_id: str, envelope: dict[str, Any], data: Any = None
    ) -> str:
        if data is None:
            return str(await _call(self._r.save_block, tenant=tenant, case_id=case_id, run_id=run_id, envelope=envelope))
        return str(
            await _call(self._r.save_block, tenant=tenant, case_id=case_id, run_id=run_id, envelope=envelope, data=data)
        )

    async def create_change_set(
        self, *, tenant: str, case_id: str, plan_version: int, option_id: str, connector: str,
        items: list[dict[str, Any]], hash: str,
    ) -> str:
        return str(
            await _call(
                self._r.create_change_set, tenant=tenant, case_id=case_id, plan_version=plan_version,
                option_id=option_id, connector=connector, items=items, hash=hash,
            )
        )

    async def update_change_set(self, *, tenant: str, change_set_id: str, **fields: Any) -> None:
        fn = self._r.update_change_set
        sig = inspect.signature(fn)
        id_name = "change_set_id" if "change_set_id" in sig.parameters else "id"
        await fn(**{id_name: change_set_id}, tenant=tenant, **fields)

    async def get_change_set(self, *, tenant: str, change_set_id: str) -> dict[str, Any] | None:
        fn = self._r.get_change_set
        sig = inspect.signature(fn)
        id_name = "change_set_id" if "change_set_id" in sig.parameters else "id"
        row = await fn(**{id_name: change_set_id}, tenant=tenant)
        if row is None:
            return None
        keys = (
            "id", "case_id", "plan_id", "plan_version", "option_id", "connector", "revision", "items",
            "hash", "status", "ext_change_set_id", "ext_code", "preview", "approval_id", "receipt_jti",
            "result",
        )
        return {k: field_of(row, k) for k in keys}

    async def list_change_sets(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        keys = ("id", "plan_version", "option_id", "status", "ext_change_set_id", "ext_code", "hash", "revision")
        fn = _maybe(self._r, "list_change_sets")
        if fn is not None:
            rows = await _call(fn, tenant=tenant, case_id=case_id)
            return [{k: field_of(r, k) for k in keys} for r in rows or []]
        # core.repo has no listing helper (55-engine-internals.md): read through the
        # tenant-scoped session (RLS) with an explicit tenant predicate as well.
        from sqlalchemy import text  # noqa: PLC0415

        from keelson_engine.core.db import tenant_session  # noqa: PLC0415

        async with tenant_session(tenant) as s:
            res = await s.execute(
                text(
                    f"SELECT {', '.join(keys)} FROM change_set WHERE tenant = :t AND case_id = :c "
                    "ORDER BY created_at, id"
                ),
                {"t": tenant, "c": case_id},
            )
            return [dict(m) for m in res.mappings().all()]

    async def ledger_mark(
        self, *, tenant: str, change_set_id: str, line_seq: int, state: str, ext_ref: str | None = None
    ) -> None:
        await _call(
            self._r.ledger_mark, tenant=tenant, change_set_id=change_set_id, line_seq=line_seq,
            state=state, ext_ref=ext_ref,
        )

    async def ledger_record(self, *, tenant: str, entry: Any) -> None:
        fn = _maybe(self._r, "ledger_record")
        if fn is None:  # a pre-M7 platform: state + external ref only
            await self.ledger_mark(tenant=tenant, change_set_id=entry.change_set_id, line_seq=entry.line_seq,
                                   state=entry.state, ext_ref=entry.ext_ref)
            return
        await _call(fn, entry, tenant=tenant)

    async def admin_audit(
        self, *, tenant: str, actor: str, action: str, target: str, before: Any = None, after: Any = None
    ) -> None:
        await _call(
            self._r.admin_audit, tenant=tenant, actor=actor, action=action, target=target,
            before=before, after=after,
        )

    async def save_findings(
        self, *, tenant: str, case_id: str, task_id: str, specialist: str, run_id: str, findings: dict[str, Any],
        status: str, task_input: dict[str, Any] | None = None,
    ) -> None:
        upsert = _maybe(self._r, "upsert_investigation_task")
        if upsert is not None:
            await _call(upsert, tenant=tenant, case_id=case_id, task_id=task_id, specialist=specialist,
                        run_id=run_id, status=status, input_=task_input or {})
        save = _maybe(self._r, "save_finding")
        if save is not None:
            await _call(save, tenant=tenant, case_id=case_id, task_id=task_id, run_id=run_id, findings=findings)

    async def list_investigation_tasks(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        fn = _maybe(self._r, "list_investigation_tasks")
        if fn is not None:
            return [dict(r) for r in await _call(fn, tenant=tenant, case_id=case_id) or []]
        from sqlalchemy import text  # noqa: PLC0415

        from keelson_engine.core.db import tenant_session  # noqa: PLC0415

        async with tenant_session(tenant) as s:
            res = await s.execute(
                text(
                    "SELECT t.id, t.specialist, t.status, t.input, f.findings FROM investigation_task t "
                    "LEFT JOIN LATERAL (SELECT findings FROM finding WHERE finding.tenant = t.tenant AND "
                    "finding.task_id = t.id ORDER BY finding.id DESC LIMIT 1) f ON true "
                    "WHERE t.tenant = :t AND t.case_id = :c ORDER BY t.created_at, t.id"
                ),
                {"t": tenant, "c": case_id},
            )
            rows = [dict(m) for m in res.mappings().all()]
        return [task_record(r) for r in rows]

    async def get_agent_spec(self, *, tenant: str, version: str) -> dict[str, Any] | None:
        fn = _maybe(self._r, "get_agent_spec")
        if fn is None:
            return None
        row = await _call(fn, tenant=tenant, version=version)
        if row is None:
            return None
        spec = field_of(row, "spec", row)
        return dict(spec) if isinstance(spec, Mapping) else None


class _TokensAdapter:
    def __init__(self, mod: Any) -> None:
        self._mod = mod

    def _broker(self) -> Any:
        return self._mod.get_token_broker()

    async def service_token(self, *, tenant: str, connector: str, principal: Principal) -> str:
        return str(await self._broker().service_token(tenant=tenant, connector=connector, principal=principal))

    async def user_token(self, *, tenant: str, connector: str, keelson_sub: str) -> str:
        connect_required = getattr(self._mod, "ConnectRequired", None)
        try:
            return str(await self._broker().user_token(tenant=tenant, connector=connector, keelson_sub=keelson_sub))
        except Exception as exc:
            if connect_required is not None and isinstance(exc, connect_required):
                raise ConnectRequiredError(getattr(exc, "connect_url", None)) from exc
            raise

    async def link_status(self, *, tenant: str, connector: str, keelson_sub: str) -> dict[str, Any]:
        res = await self._broker().link_status(tenant=tenant, connector=connector, keelson_sub=keelson_sub)
        return dict(res) if isinstance(res, Mapping) else {"status": str(field_of(res, "status", "missing"))}


class _CryptoAdapter:
    def __init__(self, mod: Any) -> None:
        self._mod = mod

    def sign_approval_receipt(self, claims: ApprovalReceiptClaims) -> str:
        return str(self._mod.sign_approval_receipt(claims))


class _ConnectorsAdapter:
    def __init__(self, mod: Any) -> None:
        self._mod = mod
        self._snapshots_done: set[str] = set()
        """Tenants whose start-up tool snapshot refresh already succeeded (worker retries)."""

    async def get_connector(self, *, tenant: str, slug: str) -> ConnectorInfo:
        return ConnectorInfo.from_row(await _call(self._mod.get_connector, tenant, slug))

    async def list_approved_tools(self, *, tenant: str, connector: str) -> list[ToolInfo]:
        rows = await _call(self._mod.list_approved_tools, tenant, connector)
        return [ToolInfo.from_row(r) for r in rows or []]

    async def get_tenant_llm_key(self, *, tenant: str) -> str:
        return str(await _call(self._mod.get_tenant_llm_key, tenant))

    async def get_action_mapping(self, *, tenant: str, connector: str) -> Any:
        """M7: the connector's active action mapping (``core.connector_mappings.get_action_mapping``
        → ``ActionMapping | None``; ``None`` while the platform module is missing)."""
        mod = _core_module("connector_mappings")
        fn = _maybe(mod, "get_action_mapping") if mod is not None else None
        if fn is None:
            return None
        return await _call(fn, tenant, connector)

    async def auth_headers(self, *, tenant: str, connector: str, principal: str) -> dict[str, str]:
        fn = _maybe(self._mod, "auth_headers")
        if fn is None:
            raise RuntimeConfigError(
                f"connector {connector!r} needs core.connectors.auth_headers (M2 connector registry), "
                "which is not available"
            )
        headers = await _call(fn, tenant, connector, principal)
        return {str(k): str(v) for k, v in dict(headers or {}).items()}

    async def tenant_llm_key_version(self, *, tenant: str) -> str | None:
        """What is watched (in order):

        1. ``core.connectors.get_tenant_llm_key_version(tenant)`` (M2 admin side; also accepted
           as ``tenant_llm_key_version``): the ``tenant_llm_key.key_version`` column, bumped by
           ``store_tenant_llm_key`` whenever the stored key changes (rotate-key); ``0`` = no key;
        2. otherwise (pre-M2 core) a fingerprint of the stored key row (``tenant_llm_key``: ``key_enc``,
           ``key_id``, ``litellm_key_alias``, ``updated_at`` via
           ``core.connectors.read_tenant_llm_key_row``). ``key_enc`` is a fresh AES-GCM
           ciphertext (random nonce) on every ``store_tenant_llm_key``, so a rotation always
           changes it; the key itself is never decrypted for this."""
        fn = _maybe(self._mod, "get_tenant_llm_key_version") or _maybe(self._mod, "tenant_llm_key_version")
        if fn is not None:
            version = await _call(fn, tenant)
            return None if version is None else str(version)
        reader = _maybe(self._mod, "read_tenant_llm_key_row")
        if reader is None:
            return None
        row = await _call(reader, tenant)
        if row is None:
            return None
        material = "|".join(
            str(field_of(row, k) or "") for k in ("key_enc", "key_id", "litellm_key_alias", "updated_at")
        )
        return "fp-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def forget_tenant_llm_key(self, *, tenant: str) -> None:
        """``core.connectors`` caches the decrypted key in-process (10 s): drop it so the rebuilt
        runner reads the rotated key (``clear_llm_key_cache`` when no per-tenant helper exists)."""
        for name in ("forget_tenant_llm_key", "invalidate_tenant_llm_key"):
            fn = getattr(self._mod, name, None)
            if callable(fn):
                fn(tenant)
                return
        clear = getattr(self._mod, "clear_llm_key_cache", None)
        if callable(clear):
            clear()

    async def refresh_tool_snapshots(self) -> None:
        """Refresh the ``merxa`` tool snapshot of every tenant that has it active.

        Tenants are independent: one tenant's failing connector (revoked API key, unreachable
        ``/mcp-lite``) neither stops the tenants after it nor makes the worker's back-off loop
        refresh the healthy ones again — tenants already refreshed by this adapter are skipped on
        the retry, and the error names only the failing tenants (no exception text)."""
        fn = _maybe(self._mod, "refresh_tool_snapshot")
        if fn is None:
            raise RuntimeError("core.connectors.refresh_tool_snapshot is not available")
        params = inspect.signature(fn).parameters
        tenant_param = params.get("tenant")
        if tenant_param is None or tenant_param.default is not inspect.Parameter.empty:
            await _call(fn, connector="merxa", slug="merxa")
            return
        lister = _maybe(self._mod, "list_tenants_with_connector") or _maybe(self._mod, "list_tenants")
        if lister is None:
            raise RuntimeError("core.connectors cannot enumerate tenants for the snapshot refresh")
        failed: list[str] = []
        for row in await _call(lister, connector="merxa", slug="merxa"):
            tenant = str(field_of(row, "slug", row))
            if tenant in self._snapshots_done:
                continue
            try:
                await _call(fn, tenant=tenant, connector="merxa", slug="merxa")
            except Exception as exc:  # isolate tenants; the worker retries the failed ones
                log.warning("tool snapshot refresh failed", tenant=tenant, error=type(exc).__name__)
                failed.append(tenant)
                continue
            self._snapshots_done.add(tenant)
        if failed:
            raise RuntimeError(f"tool snapshot refresh failed for tenant(s) {sorted(failed)}")


class _RunsAdapter:
    def __init__(self, mod: Any) -> None:
        self._m = mod

    async def claim_next_run(self, worker_id: str, lease_seconds: int = 60) -> Any | None:
        return await self._m.claim_next_run(worker_id, lease_seconds)

    async def heartbeat(self, run_id: str, worker_id: str, lease_seconds: int = 60) -> bool:
        # Renew with the same lease length the run was claimed with (E1 defaults to 60 s).
        return bool(await _call(self._m.heartbeat, run_id, worker_id, lease_seconds=lease_seconds))

    async def finish_run(
        self, run_id: str, status: RunStatus, *, invocation_id: str | None = None, error: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        finished = await self._m.finish_run(
            run_id, status, invocation_id=invocation_id, error=error, worker_id=worker_id
        )
        if finished is False:
            log.warning("run outcome not recorded: already final or leased by another worker",
                        run_id=run_id, status=status)

    async def reap_expired(self, max_attempts: int = 3) -> list[str]:
        return list(await self._m.reap_expired(max_attempts))

    async def is_cancel_requested(self, run_id: str) -> bool:
        return bool(await self._m.is_cancel_requested(run_id))

    async def record_invocation(self, run_id: str, invocation_id: str) -> None:
        fn = _maybe(self._m, "set_run_invocation")
        if fn is not None:
            await fn(run_id, invocation_id)


class _InterruptsAdapter:
    def __init__(self, mod: Any) -> None:
        self._m = mod

    async def create_interrupt(self, **kwargs: Any) -> None:
        await _call(self._m.create_interrupt, **kwargs)

    async def supersede_open(self, *, tenant: str, case_id: str, reason: str) -> int:
        return int(await _call(self._m.supersede_open, tenant, case_id, reason) or 0)


_deps: RuntimeDeps | None = None


def get_deps() -> RuntimeDeps:
    """Process-wide runtime dependencies (E1 core unless a test installed fakes)."""
    global _deps
    if _deps is None:
        _deps = CoreAdapter().deps()
    return _deps


def set_deps(deps: RuntimeDeps | None) -> None:
    global _deps
    _deps = deps
