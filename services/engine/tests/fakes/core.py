"""In-memory implementations of the E1 core ports used by the runtime (``runtime/deps.py``)."""

from __future__ import annotations

import base64
import datetime as dt
import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from keelson_contracts.common import new_evidence_id
from keelson_contracts.receipts import ApprovalReceiptClaims
from keelson_contracts.scenario import DisruptionScenario

from keelson_engine.runtime.deps import (
    ConnectorInfo,
    RuntimeDeps,
    ScenarioRecord,
    ToolInfo,
)
from keelson_engine.runtime.errors import ConnectRequiredError
from keelson_engine.runtime.scn_store import DueSync, SnapshotRef

LANES = (
    {"from_dc": "DE1", "to_dc": "NL1", "transit_days": 2, "cost_per_unit_cents": 35, "max_units_wk": 20000},
    {"from_dc": "DE1", "to_dc": "UK1", "transit_days": 4, "cost_per_unit_cents": 60, "max_units_wk": 12000},
    {"from_dc": "PL1", "to_dc": "NL1", "transit_days": 3, "cost_per_unit_cents": 45, "max_units_wk": 15000},
)


class SimulatedCrash(BaseException):
    """Raised by a fake to simulate the worker process dying mid-run."""


def fake_jwt(claims: dict[str, Any]) -> str:
    def b64(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


class FakeRepo:
    def __init__(self) -> None:
        self.cases: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.scenarios: dict[str, list[tuple[int, DisruptionScenario]]] = {}
        self.evidence: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, list[dict[str, Any]]] = {}
        self.blocks: list[dict[str, Any]] = []
        self.change_sets: dict[str, dict[str, Any]] = {}
        self.ledger: list[dict[str, Any]] = []
        self.ledger_entries: list[dict[str, Any]] = []
        """M7 per-op ``ledger_record`` entries (``CommitLedgerEntry`` JSON), in write order."""
        self.audit: list[dict[str, Any]] = []
        self.plan_status: dict[tuple[str, int], str] = {}
        self.findings: list[dict[str, Any]] = []
        self._ids = itertools.count(1)
        self.crash_on_update: Any = None
        """``callable(change_set_id, fields) -> bool``: raise SimulatedCrash once when true."""

    def add_case(self, case_id: str, *, title: str = "Trim delay", mode: str = "actual") -> None:
        self.cases[case_id] = {"id": case_id, "title": title, "mode": mode, "status": "open"}

    async def get_case(self, *, tenant: str, case_id: str) -> dict[str, Any] | None:
        return dict(self.cases.get(case_id) or {}) or None

    async def recent_messages(self, *, tenant: str, case_id: str, limit: int) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["case_id"] == case_id][-limit:]

    async def add_message(self, *, tenant: str, case_id: str, role: str, text: str, run_id: str | None) -> Any:
        self.messages.append({"case_id": case_id, "role": role, "text": text, "run_id": run_id})
        return len(self.messages)

    async def set_case_mode(self, *, tenant: str, case_id: str, mode: str) -> None:
        self.cases.setdefault(case_id, {"id": case_id, "title": "", "status": "open"})["mode"] = mode

    async def set_case_status(self, *, tenant: str, case_id: str, status: str) -> None:
        self.cases.setdefault(case_id, {"id": case_id, "title": "", "mode": "actual"})["status"] = status

    async def save_scenario(self, *, tenant: str, case_id: str, scenario: DisruptionScenario) -> int:
        rows = self.scenarios.setdefault(case_id, [])
        version = len(rows) + 1
        rows.append((version, scenario.model_copy(deep=True)))
        return version

    async def latest_scenario(self, *, tenant: str, case_id: str) -> ScenarioRecord | None:
        rows = self.scenarios.get(case_id)
        if not rows:
            return None
        version, scenario = rows[-1]
        return ScenarioRecord(version=version, scenario=scenario)

    async def save_evidence(self, *, tenant: str, case_id: str, run_id: str, connector: str, tool: str,
                            args: dict[str, Any], result: Any, prov: str) -> str:
        evidence_id = new_evidence_id()
        self.evidence[evidence_id] = {
            "id": evidence_id, "case_id": case_id, "run_id": run_id, "connector": connector, "tool": tool,
            "args": json.loads(json.dumps(args, default=str)), "result": json.loads(json.dumps(result, default=str)),
            "prov": prov, "created_at": dt.datetime(2026, 10, 5, 9, 0, tzinfo=dt.UTC),
        }
        return evidence_id

    async def get_evidence(self, *, tenant: str, ids: Sequence[str]) -> list[dict[str, Any]]:
        return [dict(self.evidence[i]) for i in ids if i in self.evidence]

    def evidence_by_tool(self, tool: str) -> list[dict[str, Any]]:
        return [e for e in self.evidence.values() if e["tool"] == tool]

    async def latest_evidence(self, *, tenant: str, case_id: str, tool: str) -> dict[str, Any] | None:
        rows = [e for e in self.evidence.values() if e["case_id"] == case_id and e["tool"] == tool]
        return dict(rows[-1]) if rows else None

    async def save_plan(self, *, tenant: str, case_id: str, version: int, options: list[dict[str, Any]],
                        recommended_option_id: str, kpis: dict[str, Any]) -> int:
        plan_id = next(self._ids)
        self.plans.setdefault(case_id, []).append(
            {"id": plan_id, "version": version, "options": json.loads(json.dumps(options)),
             "recommended_option_id": recommended_option_id, "kpis": json.loads(json.dumps(kpis)), "status": "proposed"}
        )
        return plan_id

    async def latest_plan(self, *, tenant: str, case_id: str) -> dict[str, Any] | None:
        rows = self.plans.get(case_id)
        return dict(rows[-1]) if rows else None

    async def mark_plan_status(self, *, tenant: str, case_id: str, version: int, status: str) -> None:
        self.plan_status[(case_id, version)] = status
        for plan in self.plans.get(case_id, []):
            if plan["version"] == version:
                plan["status"] = status

    async def save_block(self, *, tenant: str, case_id: str, run_id: str, envelope: dict[str, Any],
                         data: Any = None) -> str:
        self.blocks.append({"case_id": case_id, "run_id": run_id, "envelope": json.loads(json.dumps(envelope)),
                            "data": None if data is None else json.loads(json.dumps(data))})
        return str(envelope["id"])

    def block_data(self, block_id: str) -> Any:
        """What ``GET /api/blocks/{id}/data`` would serve (the latest save of the block)."""
        rows = [b for b in self.blocks if b["envelope"]["id"] == block_id]
        return rows[-1]["data"] if rows else None

    def blocks_of(self, kind: str) -> list[dict[str, Any]]:
        return [b["envelope"] for b in self.blocks if b["envelope"]["kind"] == kind]

    async def create_change_set(self, *, tenant: str, case_id: str, plan_version: int, option_id: str, connector: str,
                                items: list[dict[str, Any]], hash: str) -> str:
        cs_id = f"cs-{next(self._ids)}"
        self.change_sets[cs_id] = {
            "id": cs_id, "case_id": case_id, "plan_version": plan_version, "option_id": option_id,
            "connector": connector, "revision": 1, "items": json.loads(json.dumps(items)), "hash": hash,
            "status": "draft", "ext_change_set_id": None, "ext_code": None, "preview": None,
            "approval_id": None, "receipt_jti": None, "result": None,
        }
        return cs_id

    async def update_change_set(self, *, tenant: str, change_set_id: str, **fields: Any) -> None:
        if self.crash_on_update is not None and self.crash_on_update(change_set_id, fields):
            self.crash_on_update = None
            raise SimulatedCrash(f"worker died while recording {fields}")
        self.change_sets[change_set_id].update(json.loads(json.dumps(fields, default=str)))

    async def get_change_set(self, *, tenant: str, change_set_id: str) -> dict[str, Any] | None:
        row = self.change_sets.get(change_set_id)
        return json.loads(json.dumps(row)) if row else None

    async def list_change_sets(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(cs)) for cs in self.change_sets.values() if cs["case_id"] == case_id]

    async def ledger_mark(self, *, tenant: str, change_set_id: str, line_seq: int, state: str,
                          ext_ref: str | None = None) -> None:
        self.ledger.append({"change_set_id": change_set_id, "seq": line_seq, "state": state, "ext_ref": ext_ref})

    async def ledger_record(self, *, tenant: str, entry: Any) -> None:
        """M7 per-op: the full ledger entry (also listed in :attr:`ledger` like ``ledger_mark``)."""
        data = entry.model_dump(mode="json")
        self.ledger_entries.append(data)
        self.ledger.append({"change_set_id": data["change_set_id"], "seq": data["line_seq"], "state": data["state"],
                            "ext_ref": data["ext_ref"]})

    async def admin_audit(self, *, tenant: str, actor: str, action: str, target: str, before: Any = None,
                          after: Any = None) -> None:
        self.audit.append({"actor": actor, "action": action, "target": target, "before": before, "after": after})

    async def get_agent_spec(self, *, tenant: str, version: str) -> dict[str, Any] | None:
        return None

    async def save_findings(self, *, tenant: str, case_id: str, task_id: str, specialist: str, run_id: str,
                            findings: dict[str, Any], status: str, task_input: dict[str, Any] | None = None) -> None:
        self.findings.append({"case_id": case_id, "task_id": task_id, "specialist": specialist, "run_id": run_id,
                              "findings": findings, "status": status, "input": dict(task_input or {})})

    async def list_investigation_tasks(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        from keelson_engine.runtime.deps import task_record  # noqa: PLC0415

        return [task_record({"id": f["task_id"], "specialist": f["specialist"], "status": f["status"],
                             "input": f["input"], "findings": f["findings"]})
                for f in self.findings if f["case_id"] == case_id]


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._seq = itertools.count(1)

    async def append(self, *, tenant: str, case_id: str, run_id: str | None, event: dict[str, Any],
                     persist: bool = True, live: bool = True) -> int:
        from keelson_contracts.agui import validate_event  # noqa: PLC0415

        validate_event(event)
        sc = event["metadata"]["sc"]
        assert sc["tenantId"] == tenant and sc["caseId"] == case_id
        seq = next(self._seq) if persist else 0
        self.events.append({"seq": seq, "run_id": run_id, "event": event, "persist": persist, "live": live})
        return seq

    def types(self, run_id: str | None = None, *, persisted_only: bool = False) -> list[str]:
        return [
            e["event"]["type"] for e in self.events
            if (run_id is None or e["run_id"] == run_id) and (e["persist"] or not persisted_only)
        ]

    def of_type(self, etype: str, run_id: str | None = None) -> list[dict[str, Any]]:
        return [e["event"] for e in self.events if e["event"]["type"] == etype and (run_id is None or e["run_id"] == run_id)]


class FakeTokens:
    def __init__(self) -> None:
        self.issued: list[tuple[str, str]] = []
        self.unlinked: set[str] = set()

    async def service_token(self, *, tenant: str, connector: str, principal: str) -> str:
        self.issued.append(("service", principal))
        return fake_jwt({"sub": f"svc-{principal}", "azp": f"keelson-{principal}"})

    async def user_token(self, *, tenant: str, connector: str, keelson_sub: str) -> str:
        if keelson_sub in self.unlinked:
            raise ConnectRequiredError("http://keelson.localhost/api/connectors/merxa/connect")
        self.issued.append(("user", keelson_sub))
        return fake_jwt({"sub": f"merxa-{keelson_sub}", "azp": "keelson-connect"})

    async def link_status(self, *, tenant: str, connector: str, keelson_sub: str) -> dict[str, Any]:
        return {"status": "missing" if keelson_sub in self.unlinked else "active"}


class FakeCrypto:
    def __init__(self) -> None:
        self.signed: list[ApprovalReceiptClaims] = []

    def sign_approval_receipt(self, claims: ApprovalReceiptClaims) -> str:
        self.signed.append(claims)
        return "receipt." + base64.urlsafe_b64encode(json.dumps(claims.to_claims()).encode()).decode()

    @staticmethod
    def decode(receipt: str) -> dict[str, Any]:
        return json.loads(base64.urlsafe_b64decode(receipt.split(".", 1)[1]))


@dataclass
class FakeConnectors:
    tools: list[ToolInfo] = field(default_factory=list)
    """The platform (``merxa``) snapshot."""
    refreshes: int = 0
    refresh_failures: int = 0
    registry: dict[str, ConnectorInfo] = field(default_factory=dict)
    """M2 registry connectors by slug (anything else is the platform Merxa connector)."""
    registry_tools: dict[str, list[ToolInfo]] = field(default_factory=dict)
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    """``core.connectors.auth_headers`` fake: connector slug → headers."""
    auth_header_calls: list[tuple[str, str, str]] = field(default_factory=list)
    llm_keys: dict[str, str] = field(default_factory=dict)
    """Per-tenant virtual keys (default ``sk-test-tenant``)."""
    llm_key_versions: dict[str, str] = field(default_factory=dict)
    """Per-tenant key versions (default ``k-1``); bump one to simulate a rotation."""
    key_version_reads: int = 0
    key_version_error: bool = False
    forgotten: list[str] = field(default_factory=list)
    merxa_config: dict[str, Any] = field(default_factory=dict)
    """Non-secret config of the platform connector (e.g. ``supported_ops``, ``alt_sources``)."""
    action_mappings: dict[str, Any] = field(default_factory=dict)
    """M7: connector slug → action mapping (``ActionMapping`` or its JSON); absent = none."""
    mapping_reads: int = 0

    async def get_action_mapping(self, *, tenant: str, connector: str) -> Any:
        self.mapping_reads += 1
        return self.action_mappings.get(connector)

    async def get_connector(self, *, tenant: str, slug: str) -> ConnectorInfo:
        if slug in self.registry:
            return self.registry[slug]
        return ConnectorInfo(slug=slug, name="Merxa Retail Suite", url="http://merxa.localhost/mcp",
                             issuer="http://auth.localhost/realms/merxa", lanes=LANES, config=dict(self.merxa_config))

    async def list_approved_tools(self, *, tenant: str, connector: str) -> list[ToolInfo]:
        """Like ``core.connectors.list_approved_tools``: approved reads and gated writes (the
        toolset filters on ``investigator_bindable``)."""
        tools = self.registry_tools[connector] if connector in self.registry_tools else self.tools
        return [t for t in tools if t.status in ("approved", "approval_gate")]

    async def get_tenant_llm_key(self, *, tenant: str) -> str:
        return self.llm_keys.get(tenant, "sk-test-tenant")

    async def auth_headers(self, *, tenant: str, connector: str, principal: str) -> dict[str, str]:
        self.auth_header_calls.append((tenant, connector, principal))
        if connector not in self.secrets:
            raise LookupError(f"no header secret for {connector}")
        return dict(self.secrets[connector])

    async def tenant_llm_key_version(self, *, tenant: str) -> str | None:
        self.key_version_reads += 1
        if self.key_version_error:
            raise ConnectionError("db unavailable")
        return self.llm_key_versions.get(tenant, "k-1")

    def forget_tenant_llm_key(self, *, tenant: str) -> None:
        self.forgotten.append(tenant)

    async def refresh_tool_snapshots(self) -> None:
        if self.refresh_failures > 0:
            self.refresh_failures -= 1
            raise RuntimeError("engine-api JWKS not reachable yet")
        self.refreshes += 1


@dataclass
class FakeRuns:
    queue: list[Any] = field(default_factory=list)
    finished: list[dict[str, Any]] = field(default_factory=list)
    heartbeats: int = 0
    lease_seconds: list[int] = field(default_factory=list)
    cancel: set[str] = field(default_factory=set)
    lost_lease: set[str] = field(default_factory=set)
    reaped: int = 0
    invocations: dict[str, str] = field(default_factory=dict)

    async def claim_next_run(self, worker_id: str, lease_seconds: int = 60) -> Any | None:
        return self.queue.pop(0) if self.queue else None

    async def heartbeat(self, run_id: str, worker_id: str, lease_seconds: int = 60) -> bool:
        self.heartbeats += 1
        self.lease_seconds.append(lease_seconds)
        return run_id not in self.lost_lease

    async def finish_run(self, run_id: str, status: str, *, invocation_id: str | None = None,
                         error: str | None = None, worker_id: str | None = None) -> None:
        self.finished.append({"run_id": run_id, "status": status, "invocation_id": invocation_id, "error": error,
                              "worker_id": worker_id})

    async def reap_expired(self, max_attempts: int = 3) -> list[str]:
        self.reaped += 1
        return []

    async def is_cancel_requested(self, run_id: str) -> bool:
        return run_id in self.cancel

    async def record_invocation(self, run_id: str, invocation_id: str) -> None:
        self.invocations[run_id] = invocation_id

    def status_of(self, run_id: str) -> str | None:
        rows = [f for f in self.finished if f["run_id"] == run_id]
        return rows[-1]["status"] if rows else None


@dataclass
class FakeInterrupts:
    created: list[dict[str, Any]] = field(default_factory=list)
    superseded: int = 0

    async def create_interrupt(self, **kwargs: Any) -> None:
        self.created.append(kwargs)

    async def supersede_open(self, *, tenant: str, case_id: str, reason: str) -> int:
        self.superseded += 1
        return 0


@dataclass
class FakeScn:
    """In-memory :class:`~keelson_engine.runtime.scn_store.ScnPort`. ``reader_factory`` (e.g.
    :meth:`fakes.scn_fixture.ScnDatabase.reader`) serves SQL reads from a real snapshot."""

    snapshot: SnapshotRef | None = None
    reader_factory: Any = None
    current_error: BaseException | None = None
    pins: list[tuple[str, str, Any]] = field(default_factory=list)
    unpins: list[tuple[str, Any]] = field(default_factory=list)
    stale: list[tuple[str, str, str]] = field(default_factory=list)
    syncs: list[tuple[str, str, str]] = field(default_factory=list)
    due: list[list[DueSync]] = field(default_factory=list)
    """Successive ``due_syncs()`` answers (then empty)."""
    due_calls: int = 0
    targets: list[tuple[str, str]] = field(default_factory=list)
    sync_errors: dict[tuple[str, str], BaseException] = field(default_factory=dict)
    sync_gate: Any = None
    """An ``asyncio.Event`` every sync waits for (simulates a long sync)."""

    async def current_snapshot(self, *, tenant: str, connector: str) -> SnapshotRef | None:
        if self.current_error is not None:
            raise self.current_error
        return self.snapshot

    async def pin_snapshot(self, *, tenant: str, snapshot_id: str, plan_id: Any) -> None:
        self.pins.append((tenant, snapshot_id, plan_id))

    async def unpin_plan(self, *, tenant: str, plan_id: Any) -> None:
        self.unpins.append((tenant, plan_id))

    async def mark_stale(self, *, tenant: str, connector: str, reason: str) -> None:
        self.stale.append((tenant, connector, reason))

    async def sync_connector(self, *, tenant: str, connector: str, reason: str) -> Any:
        self.syncs.append((tenant, connector, reason))
        if self.sync_gate is not None:
            await self.sync_gate.wait()
        error = self.sync_errors.get((tenant, connector))
        if error is not None:
            raise error
        return {"id": "snap-new", "status": "ready"}

    pending: list[DueSync] = field(default_factory=list)
    """Requested / poll-marked syncs: part of the next ``due_syncs()`` answer (like the platform)."""

    async def due_syncs(self) -> list[DueSync]:
        self.due_calls += 1
        out = [*self.pending, *(self.due.pop(0) if self.due else [])]
        self.pending.clear()
        return out

    polls: list[tuple[str, str]] = field(default_factory=list)
    poll_errors: dict[tuple[str, str], BaseException] = field(default_factory=dict)
    changed: set[tuple[str, str]] = field(default_factory=set)
    """Connectors whose next poll reports changes (the poll then marks them stale and due)."""
    requests: list[tuple[str, str, str]] = field(default_factory=list)

    async def poll_changes(self, *, tenant: str, connector: str) -> bool:
        self.polls.append((tenant, connector))
        error = self.poll_errors.get((tenant, connector))
        if error is not None:
            raise error
        if (tenant, connector) in self.changed:
            self.changed.discard((tenant, connector))
            self.stale.append((tenant, connector, "poll"))
            self.pending.append(DueSync(tenant, connector, "poll"))
            return True
        return False

    async def request_sync(self, *, tenant: str, connector: str, reason: str) -> None:
        self.requests.append((tenant, connector, reason))
        self.pending.append(DueSync(tenant, connector, reason))

    async def startup_targets(self) -> list[tuple[str, str]]:
        return list(self.targets)

    # ── M7 ──
    snapshots: dict[str, SnapshotRef] = field(default_factory=dict)
    """``get_snapshot`` answers by id (plans pinned to a snapshot, M7 labels)."""

    async def get_snapshot(self, *, tenant: str, snapshot_id: str) -> SnapshotRef | None:
        if snapshot_id in self.snapshots:
            return self.snapshots[snapshot_id]
        if self.snapshot is not None and self.snapshot.id == snapshot_id:
            return self.snapshot
        return None

    def reader(self, tenant: str) -> Any:
        if self.reader_factory is None:
            raise LookupError("no snapshot reader configured")
        return self.reader_factory(tenant)


class FakeChecklists:
    """In-memory ``ChecklistsPort`` (M5 Guide mode)."""

    def __init__(self, tenant: str = "demo-retail") -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.updates: list[dict[str, Any]] = []
        self.tenant = tenant

    async def create_checklist(self, *, tenant: str, case_id: str, checklist_id: str, connector: str, plan_version: int,
                               option_id: str, steps: list[dict[str, Any]], meta: dict[str, Any]) -> None:
        self.rows.setdefault(checklist_id, json.loads(json.dumps({
            "id": checklist_id, "tenant": tenant, "case_id": case_id, "connector": connector,
            "plan_version": plan_version, "option_id": option_id, "status": "open", "steps": steps, "meta": meta,
        })))

    async def get_checklist(self, *, tenant: str, checklist_id: str) -> dict[str, Any] | None:
        row = self.rows.get(checklist_id)
        return json.loads(json.dumps(row)) if row and row["tenant"] == tenant else None

    async def list_open_checklists(self, *, tenant: str | None = None) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(r)) for r in self.rows.values()
                if r["status"] == "open" and tenant in (None, r["tenant"])]

    async def set_step(self, *, tenant: str, checklist_id: str, seq: int, status: str, verified_at: str | None = None,
                       evidence_id: str | None = None, actor: str | None = None) -> dict[str, Any] | None:
        row = self.rows.get(checklist_id)
        if row is None or row["tenant"] != tenant:
            return None
        for step in row["steps"]:
            if int(step["seq"]) == int(seq):
                step["status"] = status
                step["verified_at"] = verified_at if status == "verified" else None
                step["evidence_id"] = evidence_id if status == "verified" else None
        self.updates.append({"checklist_id": checklist_id, "seq": seq, "status": status, "actor": actor})
        return json.loads(json.dumps(row))

    async def set_status(self, *, tenant: str, checklist_id: str, status: str) -> None:
        self.rows[checklist_id]["status"] = status


class FakeSignals:
    """In-memory ``SignalsPort``: stored webhook events by id."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)

    def add(self, type_: str, data: dict[str, Any], *, connector: str = "merxa") -> str:
        signal_id = str(next(self._ids))
        self.events[signal_id] = {"id": signal_id, "connector": connector, "type": type_, "data": dict(data),
                                  "received_at": "2026-10-07T09:00:00Z"}
        return signal_id

    async def get_signal(self, *, tenant: str, signal_id: str) -> dict[str, Any] | None:
        row = self.events.get(signal_id)
        return dict(row) if row else None

    async def latest_event_id(self, *, tenant: str, connector: str) -> int | None:
        ids = [int(k) for k, v in self.events.items() if v["connector"] == connector]
        return max(ids) if ids else None


class FakePlans:
    """In-memory ``PlansPort`` over :class:`FakeRepo`."""

    def __init__(self, repo: FakeRepo) -> None:
        self.repo = repo
        self.dependencies: dict[tuple[str, int], dict[str, Any]] = {}

    async def list_plan_versions(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return [{"version": p["version"], "status": self.repo.plan_status.get((case_id, p["version"]), p["status"])}
                for p in self.repo.plans.get(case_id, [])]

    async def save_plan_dependencies(self, *, tenant: str, case_id: str, plan_version: int,
                                     dependencies: dict[str, Any]) -> None:
        self.dependencies[(case_id, plan_version)] = json.loads(json.dumps(dependencies))


@dataclass
class FakeCore:
    repo: FakeRepo = field(default_factory=FakeRepo)
    bus: FakeBus = field(default_factory=FakeBus)
    tokens: FakeTokens = field(default_factory=FakeTokens)
    crypto: FakeCrypto = field(default_factory=FakeCrypto)
    connectors: FakeConnectors = field(default_factory=FakeConnectors)
    runs: FakeRuns = field(default_factory=FakeRuns)
    interrupts: FakeInterrupts = field(default_factory=FakeInterrupts)
    scn: FakeScn = field(default_factory=FakeScn)
    kb: FakeKb = field(default_factory=lambda: FakeKb())
    checklists: FakeChecklists | None = field(default_factory=FakeChecklists)
    signals: FakeSignals = field(default_factory=FakeSignals)
    plans: FakePlans | None = None
    hub: Any = None
    """M6 ``SignalHubPort`` (``fakes.m6.FakeSignalHub``); ``None`` = no Signal Hub."""

    def deps(self) -> RuntimeDeps:
        if self.plans is None:
            self.plans = FakePlans(self.repo)
        return RuntimeDeps(
            repo=self.repo, bus=self.bus, tokens=self.tokens, crypto=self.crypto,  # type: ignore[arg-type]
            connectors=self.connectors, runs=self.runs, interrupts=self.interrupts,  # type: ignore[arg-type]
            scn=self.scn, kb=self.kb, checklists=self.checklists, signals=self.signals,  # type: ignore[arg-type]
            plans=self.plans, hub=self.hub,  # type: ignore[arg-type]
        )


class FakeKb:
    """``core.kb.search`` stand-in: chunks returned best-first for every query (recorded)."""

    def __init__(self) -> None:
        self.chunks: list[dict[str, Any]] = [
            {"chunk_id": "chk-1", "document_title": "Freight & expedite policy", "heading": "Sea-air premium caps",
             "text": "Sea-air may be used for peak-season styles when the premium stays within the cap agreed "
                     "with finance.", "score": 0.91},
        ]
        self.queries: list[dict[str, Any]] = []

    async def search(self, *, tenant: str, query: str, kb_ids: Sequence[str] | None = None,
                     top_k: int = 6) -> list[dict[str, Any]]:
        self.queries.append({"tenant": tenant, "query": query, "kb_ids": list(kb_ids or []), "top_k": top_k})
        return [dict(c) for c in self.chunks[:top_k]]


@dataclass
class RunRow:
    """Shape of E1's ``core.runs.RunRow``."""

    id: str
    tenant: str
    case_id: str
    kind: str
    payload: dict[str, Any]
    actor: dict[str, Any]
    attempt: int = 1
    invocation_id: str | None = None
    session_id: str | None = None
    agent_spec_version: str | None = None
    created_by: str | None = None
    origin_kind: str | None = None
    lease_owner: str | None = None
