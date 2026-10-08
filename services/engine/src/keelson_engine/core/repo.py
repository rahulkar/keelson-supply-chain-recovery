"""Domain persistence helpers (all tenant-scoped through RLS).

Every function takes ``tenant=`` as a keyword; when omitted, the tenant bound with
``core.context.use_tenant()`` is used (the worker binds it per run). Positional parameters
follow ``55-engine-internals.md``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text as _sql
from sqlalchemy.ext.asyncio import AsyncSession

from keelson_contracts.common import is_block_id, new_evidence_id
from keelson_contracts.scenario import DisruptionScenario
from keelson_engine.core.context import resolve_tenant
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.sql import dumps, jsonable

__all__ = [
    "CaseNotFound",
    "PlanRow",
    "ScenarioRow",
    "CaseSnapshot",
    "add_message",
    "admin_audit",
    "blocks_in",
    "case_snapshot",
    "create_case",
    "create_change_set",
    "get_block",
    "get_block_data",
    "get_calc",
    "get_case",
    "get_agent_spec",
    "get_change_set",
    "get_evidence",
    "get_llm_replay",
    "latest_plan",
    "latest_scenario",
    "ledger_mark",
    "list_blocks",
    "list_cases",
    "list_evidence",
    "ledger_entries",
    "ledger_record",
    "LedgerRegression",
    "list_ledger",
    "list_messages",
    "list_plan_versions",
    "messages_in",
    "delete_llm_replays",
    "put_llm_replay",
    "save_block",
    "save_calc",
    "save_evidence",
    "save_finding",
    "save_plan",
    "save_plan_dependencies",
    "save_scenario",
    "set_case_mode",
    "set_case_status",
    "set_case_title",
    "set_plan_status",
    "touch_case",
    "update_change_set",
    "upsert_investigation_task",
]

log = get_logger(__name__)


class CaseNotFound(LookupError):
    pass


# ───────────────────────────── cases ─────────────────────────────

_CASE_COLS = (
    "id, tenant, title, mode, status, created_by, created_by_name, last_message_preview, created_at, updated_at"
)


async def create_case(
    title: str | None = None,
    *,
    created_by: dict[str, Any] | str,
    mode: str = "actual",
    tenant: str | None = None,
) -> str:
    t = resolve_tenant(tenant)
    if isinstance(created_by, dict):
        sub, name = str(created_by.get("sub")), str(created_by.get("name") or created_by.get("username") or "")
    else:
        sub, name = str(created_by), ""
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(
                'INSERT INTO "case" (tenant, title, mode, created_by, created_by_name) '
                "VALUES (:t, :title, :mode, :sub, :name) RETURNING id"
            ),
            {"t": t, "title": (title or "New case").strip()[:200] or "New case", "mode": mode, "sub": sub,
             "name": name},
        )
        return str(res.scalar_one())


async def get_case(case_id: str, *, tenant: str | None = None) -> dict[str, Any] | None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(_sql(f'SELECT {_CASE_COLS} FROM "case" WHERE id = :id'), {"id": case_id})
        m = res.mappings().first()
    return dict(m) if m is not None else None


async def list_cases(limit: int = 50, *, tenant: str | None = None) -> list[dict[str, Any]]:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        cols = ", ".join(f"c.{c.strip()}" for c in _CASE_COLS.split(","))
        res = await s.execute(
            _sql(
                f'SELECT {cols}, (SELECT max(g.created_at) FROM case_signal g WHERE g.case_id = c.id) '
                'AS last_signal_at FROM "case" c ORDER BY c.updated_at DESC, c.id DESC LIMIT :lim'
            ),
            {"lim": max(1, min(int(limit), 200))},
        )
        return [dict(m) for m in res.mappings().all()]


async def touch_case(case_id: str, *, tenant: str | None = None) -> None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        await s.execute(_sql('UPDATE "case" SET updated_at = now() WHERE id = :id'), {"id": case_id})


async def _update_case(case_id: str, column: str, value: Any, tenant: str | None) -> None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(f'UPDATE "case" SET {column} = :v, updated_at = now() WHERE id = :id RETURNING id'),
            {"v": value, "id": case_id},
        )
        if res.first() is None:
            raise CaseNotFound(case_id)


async def set_case_mode(case_id: str, mode: str, *, tenant: str | None = None) -> None:
    if mode not in ("actual", "what_if"):
        raise ValueError("mode must be actual|what_if")
    await _update_case(case_id, "mode", mode, tenant)


async def set_case_status(case_id: str, status: str, *, tenant: str | None = None) -> None:
    await _update_case(case_id, "status", status[:40], tenant)


async def set_case_title(case_id: str, title: str, *, tenant: str | None = None) -> None:
    await _update_case(case_id, "title", title.strip()[:200] or "New case", tenant)


async def add_message(
    case_id: str,
    role: str,
    text: str,
    run_id: str | None = None,
    *,
    tenant: str | None = None,
    author_sub: str | None = None,
) -> str:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(
                "INSERT INTO case_message (tenant, case_id, role, text, run_id, author_sub) "
                "VALUES (:t, :c, :role, :text, :r, :a) RETURNING id"
            ),
            {"t": t, "c": case_id, "role": role, "text": text, "r": run_id, "a": author_sub},
        )
        mid = str(res.scalar_one())
        await s.execute(
            _sql('UPDATE "case" SET updated_at = now(), last_message_preview = :p WHERE id = :c'),
            {"p": text[:200], "c": case_id},
        )
        return mid


async def list_messages(case_id: str, *, tenant: str | None = None) -> list[dict[str, Any]]:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        return await messages_in(s, case_id)


async def messages_in(session: AsyncSession, case_id: str) -> list[dict[str, Any]]:
    res = await session.execute(
        _sql("SELECT id, role, text, run_id, created_at FROM case_message WHERE case_id = :c ORDER BY created_at, id"),
        {"c": case_id},
    )
    return [dict(m) for m in res.mappings().all()]


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    last_event_seq: int
    open_interrupts: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    blocks: list[dict[str, Any]]


async def case_snapshot(case_id: str, *, tenant: str | None = None) -> CaseSnapshot:
    """Messages, blocks, open interrupts and ``last_event_seq`` of a case, mutually consistent.

    One ``REPEATABLE READ`` transaction, ``last_event_seq`` read **first**: whatever the
    snapshot does not contain (an interrupt created a moment later, its ``RUN_FINISHED``)
    has ``seq > last_event_seq`` and reaches the client through the SSE replay from that
    cursor. Reading the cursor after the other tables (or in separate transactions) could
    return a cursor past the ``RUN_FINISHED`` of an interrupt the snapshot missed, and the
    approval card would never show up.
    """
    from keelson_engine.core.events import last_seq_in  # noqa: PLC0415 - avoid import cycles
    from keelson_engine.core.interrupts import open_interrupts_in  # noqa: PLC0415

    t = resolve_tenant(tenant)
    async with tenant_session(t, snapshot=True) as s:
        last_seq = await last_seq_in(s, case_id)
        interrupts = await open_interrupts_in(s, case_id)
        messages = await messages_in(s, case_id)
        blocks = await blocks_in(s, case_id)
    return CaseSnapshot(last_seq, interrupts, messages, blocks)


# ───────────────────────────── scenarios ─────────────────────────────


@dataclass(frozen=True, slots=True)
class ScenarioRow:
    version: int
    scenario: DisruptionScenario
    fingerprint: str | None
    created_at: dt.datetime


async def save_scenario(
    case_id: str, scenario: DisruptionScenario | dict[str, Any], *, tenant: str | None = None
) -> int:
    """Store the next scenario version for the case; returns the version (1, 2, …)."""
    t = resolve_tenant(tenant)
    model = scenario if isinstance(scenario, DisruptionScenario) else DisruptionScenario.model_validate(scenario)
    try:
        fingerprint: str | None = model.fingerprint()
    except Exception:  # noqa: BLE001 - fingerprint is informative only
        fingerprint = None
    async with tenant_session(t) as s:
        await s.execute(_sql('SELECT id FROM "case" WHERE id = :c FOR UPDATE'), {"c": case_id})
        res = await s.execute(
            _sql(
                """
                INSERT INTO scenario (tenant, case_id, version, scenario, fingerprint)
                SELECT :t, :c, coalesce(max(version), 0) + 1, CAST(:s AS jsonb), :fp FROM scenario WHERE case_id = :c
                RETURNING version
                """
            ),
            {"t": t, "c": case_id, "s": dumps(model.model_dump(mode="json")), "fp": fingerprint},
        )
        return int(res.scalar_one())


async def latest_scenario(case_id: str, *, tenant: str | None = None) -> ScenarioRow | None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(
                "SELECT version, scenario, fingerprint, created_at FROM scenario WHERE case_id = :c "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"c": case_id},
        )
        m = res.mappings().first()
    if m is None:
        return None
    return ScenarioRow(int(m["version"]), DisruptionScenario.model_validate(m["scenario"]), m["fingerprint"],
                       m["created_at"])


# ───────────────────────────── evidence / calc / findings ─────────────────────────────


async def save_evidence(
    case_id: str,
    run_id: str | None,
    connector: str | None,
    tool: str,
    args: dict[str, Any],
    result: Any,
    prov: str = "fact",
    *,
    tenant: str | None = None,
    evidence_id: str | None = None,
) -> str:
    t = resolve_tenant(tenant)
    eid = evidence_id or new_evidence_id()
    async with tenant_session(t) as s:
        await s.execute(
            _sql(
                "INSERT INTO evidence (id, tenant, case_id, run_id, connector, tool, args, result, prov) "
                "VALUES (:id, :t, :c, :r, :conn, :tool, CAST(:args AS jsonb), CAST(:result AS jsonb), :prov)"
            ),
            {"id": eid, "t": t, "c": case_id, "r": run_id, "conn": connector, "tool": tool,
             "args": dumps(args or {}), "result": dumps(result), "prov": prov},
        )
    return eid


_EVIDENCE_COLS = "id, case_id, run_id, connector, tool, args, result, prov, created_at"


async def get_evidence(ids: list[str], *, tenant: str | None = None) -> list[dict[str, Any]]:
    t = resolve_tenant(tenant)
    if not ids:
        return []
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(f"SELECT {_EVIDENCE_COLS} FROM evidence WHERE id = ANY(:ids)"), {"ids": list(ids)}
        )
        rows = {m["id"]: dict(m) for m in res.mappings().all()}
    return [rows[i] for i in ids if i in rows]


async def list_evidence(case_id: str, *, run_id: str | None = None, tenant: str | None = None) -> list[dict[str, Any]]:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(
                f"SELECT {_EVIDENCE_COLS} FROM evidence WHERE case_id = :c "
                "AND (CAST(:r AS text) IS NULL OR run_id = :r) ORDER BY created_at, id"
            ),
            {"c": case_id, "r": run_id},
        )
        return [dict(m) for m in res.mappings().all()]


async def save_calc(case_id: str, run_id: str | None, calc_id: str, op: str, inputs: dict[str, Any], result: Any,
                    *, tenant: str | None = None) -> str:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        await s.execute(
            _sql(
                "INSERT INTO calc (id, tenant, case_id, run_id, op, inputs, result) "
                "VALUES (:id, :t, :c, :r, :op, CAST(:i AS jsonb), CAST(:res AS jsonb)) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": calc_id, "t": t, "c": case_id, "r": run_id, "op": op, "i": dumps(inputs), "res": dumps(result)},
        )
    return calc_id


async def get_calc(calc_id: str, *, tenant: str | None = None) -> dict[str, Any] | None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql("SELECT id, case_id, run_id, op, inputs, result, created_at FROM calc WHERE id = :id"), {"id": calc_id}
        )
        m = res.mappings().first()
    return dict(m) if m is not None else None


async def upsert_investigation_task(case_id: str, task_id: str, specialist: str, *, run_id: str | None = None,
                                    status: str = "pending", input_: dict[str, Any] | None = None,
                                    tenant: str | None = None) -> None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        await s.execute(
            _sql(
                """
                INSERT INTO investigation_task (id, tenant, case_id, run_id, specialist, status, input)
                VALUES (:id, :t, :c, :r, :sp, :st, CAST(:i AS jsonb))
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, run_id = EXCLUDED.run_id, updated_at = now()
                """
            ),
            {"id": task_id, "t": t, "c": case_id, "r": run_id, "sp": specialist, "st": status,
             "i": dumps(input_ or {})},
        )


async def save_finding(case_id: str, task_id: str, run_id: str | None, findings: Any, *,
                       tenant: str | None = None) -> int:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(
                "INSERT INTO finding (tenant, case_id, task_id, run_id, findings) "
                "VALUES (:t, :c, :task, :r, CAST(:f AS jsonb)) RETURNING id"
            ),
            {"t": t, "c": case_id, "task": task_id, "r": run_id, "f": dumps(findings)},
        )
        return int(res.scalar_one())


# ───────────────────────────── plans ─────────────────────────────


@dataclass(frozen=True, slots=True)
class PlanRow:
    id: int
    version: int
    options: list[dict[str, Any]]
    recommended_option_id: str | None
    kpis: dict[str, Any]
    status: str
    created_at: dt.datetime

    def option(self, option_id: str) -> dict[str, Any] | None:
        for opt in self.options:
            if opt.get("id") == option_id:
                return opt
        return None


async def save_plan(
    case_id: str,
    version: int,
    options: list[dict[str, Any]],
    recommended_option_id: str | None,
    kpis: dict[str, Any] | None = None,
    *,
    tenant: str | None = None,
    dependencies: Any = None,
) -> int:
    """Insert plan ``version`` (re-saving the same version overwrites it); returns the plan id.

    M5: the plan's dependencies (lanes, receipts, connectors referenced by its options; see
    ``core.replan``) are recorded in the same transaction, replacing those of an earlier save;
    ``dependencies`` (``core.replan.PlanDependencies``) adds refs the options do not name."""
    from keelson_engine.core import replan

    t = resolve_tenant(tenant)
    options_json = [jsonable(o) for o in options]
    deps = replan.extract_dependencies(options_json)
    if dependencies is not None:
        deps.update(dependencies)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(
                """
                INSERT INTO plan (tenant, case_id, version, options, recommended_option_id, kpis)
                VALUES (:t, :c, :v, CAST(:o AS jsonb), :rec, CAST(:k AS jsonb))
                ON CONFLICT (case_id, version) DO UPDATE SET options = EXCLUDED.options,
                  recommended_option_id = EXCLUDED.recommended_option_id, kpis = EXCLUDED.kpis, updated_at = now()
                RETURNING id
                """
            ),
            {"t": t, "c": case_id, "v": int(version), "o": dumps(options_json),
             "rec": recommended_option_id, "k": dumps(kpis or {})},
        )
        plan_id = int(res.scalar_one())
        await replan.record_dependencies(s, tenant=t, plan_id=plan_id, case_id=case_id, plan_version=int(version),
                                         deps=deps, source="plan")
        return plan_id


async def latest_plan(case_id: str, *, tenant: str | None = None) -> PlanRow | None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(
                "SELECT id, version, options, recommended_option_id, kpis, status, created_at FROM plan "
                "WHERE case_id = :c ORDER BY version DESC LIMIT 1"
            ),
            {"c": case_id},
        )
        m = res.mappings().first()
    if m is None:
        return None
    return PlanRow(int(m["id"]), int(m["version"]), list(m["options"] or []), m["recommended_option_id"],
                   dict(m["kpis"] or {}), m["status"], m["created_at"])


async def list_plan_versions(case_id: str, *, tenant: str | None = None) -> list[dict[str, Any]]:
    """``[{version, status, created_at}]`` ascending (plan version switcher, re-plan)."""
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql("SELECT version, status, created_at FROM plan WHERE case_id = :c ORDER BY version"), {"c": case_id}
        )
        return [{"version": int(r[0]), "status": str(r[1] or "proposed"), "created_at": r[2]} for r in res.all()]


async def save_plan_dependencies(case_id: str, plan_version: int, dependencies: dict[str, Any], *,
                                 tenant: str | None = None) -> int:
    """Index the runtime's plan fingerprint ``{lanes, dcs, po_lines, connectors, committed_lanes}``
    (M5, ``95`` §4) for the webhook re-plan router; replaces an earlier runtime fingerprint of the
    same plan version. Returns the number of rows (0 when the plan version does not exist)."""
    from keelson_engine.core import replan

    t = resolve_tenant(tenant)
    deps = replan.PlanDependencies.from_fingerprint(dependencies or {})
    async with tenant_session(t) as s:
        res = await s.execute(_sql("SELECT id FROM plan WHERE case_id = :c AND version = :v"),
                              {"c": case_id, "v": int(plan_version)})
        plan_id = res.scalar_one_or_none()
        if plan_id is None:
            return 0
        return await replan.record_dependencies(s, tenant=t, plan_id=int(plan_id), case_id=case_id,
                                                plan_version=int(plan_version), deps=deps, source="runtime")


async def set_plan_status(case_id: str, version: int, status: str, *, tenant: str | None = None) -> None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        await s.execute(
            _sql("UPDATE plan SET status = :st, updated_at = now() WHERE case_id = :c AND version = :v"),
            {"st": status, "c": case_id, "v": int(version)},
        )


# ───────────────────────────── blocks ─────────────────────────────


async def save_block(
    case_id: str,
    run_id: str | None,
    envelope: dict[str, Any] | Any,
    *,
    data: Any = None,
    tenant: str | None = None,
) -> str:
    """Insert or replace a render block by its envelope ``id`` (blocks are updated in place).

    ``data`` (optional) is the large payload served by ``GET /api/blocks/{id}/data``.
    """
    t = resolve_tenant(tenant)
    env = jsonable(envelope)
    if hasattr(envelope, "model_dump"):
        env = envelope.model_dump(mode="json", by_alias=True)
    block_id = str(env.get("id") or "")
    if not is_block_id(block_id):
        raise ValueError("envelope.id must be a blk_ id")
    async with tenant_session(t) as s:
        await s.execute(
            _sql(
                """
                INSERT INTO block (id, tenant, case_id, run_id, kind, v, envelope, data)
                VALUES (:id, :t, :c, :r, :kind, :v, CAST(:env AS jsonb), CAST(:data AS jsonb))
                ON CONFLICT (id) DO UPDATE SET envelope = EXCLUDED.envelope, run_id = EXCLUDED.run_id,
                  data = coalesce(EXCLUDED.data, block.data), kind = EXCLUDED.kind, v = EXCLUDED.v, updated_at = now()
                """
            ),
            {"id": block_id, "t": t, "c": case_id, "r": run_id, "kind": str(env.get("kind")),
             "v": int(env.get("v") or 1), "env": dumps(env), "data": dumps(data)},
        )
    return block_id


async def list_blocks(case_id: str, *, tenant: str | None = None) -> list[dict[str, Any]]:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        return await blocks_in(s, case_id)


async def blocks_in(session: AsyncSession, case_id: str) -> list[dict[str, Any]]:
    res = await session.execute(_sql("SELECT envelope FROM block WHERE case_id = :c ORDER BY created_at, id"),
                                {"c": case_id})
    return [dict(r[0]) for r in res.all()]


async def get_block(block_id: str, *, tenant: str | None = None) -> dict[str, Any] | None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql("SELECT id, case_id, run_id, kind, envelope, data IS NOT NULL AS has_data FROM block WHERE id = :id"),
            {"id": block_id},
        )
        m = res.mappings().first()
    return dict(m) if m is not None else None


async def get_block_data(block_id: str, *, tenant: str | None = None) -> Any:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(_sql("SELECT data FROM block WHERE id = :id"), {"id": block_id})
        return res.scalar_one_or_none()


# ───────────────────────────── change sets ─────────────────────────────

_CS_COLS = (
    "id, tenant, case_id, plan_id, plan_version, option_id, connector, revision, items, hash, status, "
    "ext_change_set_id, ext_code, preview, approval_id, receipt_jti, result, created_at, updated_at"
)
_CS_UPDATABLE = {
    "status": None,
    "ext_change_set_id": None,
    "ext_code": None,
    "preview": "jsonb",
    "approval_id": None,
    "receipt_jti": None,
    "result": "jsonb",
    "revision": None,
    "items": "jsonb",
    "hash": None,
}


async def create_change_set(
    case_id: str,
    plan_version: int,
    option_id: str,
    connector: str,
    items: list[dict[str, Any]],
    hash: str,  # noqa: A002 - contract name
    *,
    tenant: str | None = None,
) -> str:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        plan = await s.execute(
            _sql("SELECT id FROM plan WHERE case_id = :c AND version = :v"), {"c": case_id, "v": int(plan_version)}
        )
        plan_id = plan.scalar_one_or_none()
        items_json = [jsonable(i) for i in items]
        res = await s.execute(
            _sql(
                """
                INSERT INTO change_set (tenant, case_id, plan_id, plan_version, option_id, connector, items, hash)
                VALUES (:t, :c, :pid, :pv, :opt, :conn, CAST(:items AS jsonb), :h) RETURNING id
                """
            ),
            {"t": t, "c": case_id, "pid": plan_id, "pv": int(plan_version), "opt": option_id, "conn": connector,
             "items": dumps(items_json), "h": hash},
        )
        change_set_id = str(res.scalar_one())
        if plan_id is not None:  # M5: what the change set touches joins the plan's fingerprint
            from keelson_engine.core import replan

            await replan.record_dependencies(
                s, tenant=t, plan_id=int(plan_id), case_id=case_id, plan_version=int(plan_version),
                deps=replan.extract_dependencies(items_json, connectors=[connector]), source="change_set",
                replace=False,
            )
        return change_set_id


async def update_change_set(change_set_id: str, *, tenant: str | None = None, **fields: Any) -> None:
    t = resolve_tenant(tenant)
    unknown = set(fields) - set(_CS_UPDATABLE)
    if unknown:
        raise ValueError(f"unknown change_set fields: {sorted(unknown)}")
    if not fields:
        return
    sets = []
    params: dict[str, Any] = {"id": change_set_id}
    for name, value in fields.items():
        if _CS_UPDATABLE[name] == "jsonb":
            sets.append(f"{name} = CAST(:{name} AS jsonb)")
            params[name] = dumps(value)
        else:
            sets.append(f"{name} = :{name}")
            params[name] = value
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(f"UPDATE change_set SET {', '.join(sets)}, updated_at = now() WHERE id = :id RETURNING id"), params
        )
        if res.first() is None:
            raise LookupError(f"change_set {change_set_id} not found")


async def get_change_set(change_set_id: str, *, tenant: str | None = None) -> dict[str, Any] | None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(_sql(f"SELECT {_CS_COLS} FROM change_set WHERE id = :id"), {"id": change_set_id})
        m = res.mappings().first()
    return dict(m) if m is not None else None


async def ledger_mark(change_set_id: str, line_seq: int, state: str, ext_ref: str | None = None, *,
                      tenant: str | None = None) -> None:
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        await s.execute(
            _sql(
                """
                INSERT INTO commit_ledger (tenant, change_set_id, line_seq, state, ext_ref)
                VALUES (:t, :cs, :seq, :st, :ref)
                ON CONFLICT (change_set_id, line_seq) DO UPDATE SET state = EXCLUDED.state,
                  ext_ref = coalesce(EXCLUDED.ext_ref, commit_ledger.ext_ref), updated_at = now()
                """
            ),
            {"t": t, "cs": change_set_id, "seq": int(line_seq), "st": state, "ref": ext_ref},
        )


async def list_ledger(change_set_id: str, *, tenant: str | None = None) -> list[dict[str, Any]]:
    """Ledger rows of a change set (M7 adds the per-op columns: ``op``, ``connector``, ``tool``,
    ``idempotency``, ``attempts``, ``request_id``, ``readback``, ``error``; ``None`` for change-set
    commits)."""
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql("SELECT line_seq, state, ext_ref, updated_at, op, connector, tool, idempotency, attempts, "
                 "request_id, readback, error FROM commit_ledger WHERE change_set_id = :cs ORDER BY line_seq"),
            {"cs": change_set_id},
        )
        return [dict(m) for m in res.mappings().all()]


class LedgerRegression(RuntimeError):
    """A per-op ledger write that would move a settled line back (or lower its attempts)."""

    def __init__(self, change_set_id: str, line_seq: int, current_state: str | None) -> None:
        super().__init__(f"commit ledger line {change_set_id}#{line_seq} is {current_state}: refusing to move it back")
        self.change_set_id = change_set_id
        self.line_seq = line_seq
        self.current_state = current_state


async def ledger_record(entry: Any, *, tenant: str | None = None) -> dict[str, Any]:
    """M7 per-op commit (``110`` §4): upsert one ``keelson_contracts.commit.CommitLedgerEntry``
    (model or dict; validated: attempted|succeeded|in_doubt|failed|verified, retry rules,
    read-back evidence) as the line's ledger row. Returns the stored entry JSON."""
    from keelson_contracts.commit import CommitLedgerEntry

    e = entry if isinstance(entry, CommitLedgerEntry) else CommitLedgerEntry.model_validate(entry)
    t = resolve_tenant(tenant)
    data = e.model_dump(mode="json")
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql(
                """
                INSERT INTO commit_ledger (tenant, change_set_id, line_seq, state, ext_ref, op, connector, tool,
                                           idempotency, attempts, request_id, readback, error, updated_at)
                VALUES (:t, :cs, :seq, :st, :ref, :op, :conn, :tool, :idem, :att, :rid, CAST(:rb AS jsonb), :err,
                        :at)
                ON CONFLICT (change_set_id, line_seq) DO UPDATE SET state = EXCLUDED.state,
                  ext_ref = coalesce(EXCLUDED.ext_ref, commit_ledger.ext_ref), op = EXCLUDED.op,
                  connector = EXCLUDED.connector, tool = EXCLUDED.tool, idempotency = EXCLUDED.idempotency,
                  attempts = EXCLUDED.attempts, request_id = EXCLUDED.request_id, readback = EXCLUDED.readback,
                  error = EXCLUDED.error, updated_at = EXCLUDED.updated_at
                WHERE (commit_ledger.state <> 'verified' OR EXCLUDED.state = 'verified')
                  AND NOT (commit_ledger.state = 'succeeded' AND EXCLUDED.state NOT IN ('succeeded', 'verified'))
                  AND EXCLUDED.attempts >= coalesce(commit_ledger.attempts, 1)
                RETURNING line_seq
                """
            ),
            {"t": t, "cs": e.change_set_id, "seq": e.line_seq, "st": e.state, "ref": e.ext_ref, "op": e.op,
             "conn": e.connector, "tool": e.tool, "idem": e.idempotency.value, "att": e.attempts,
             "rid": e.request_id, "rb": dumps(data["readback"]), "err": e.error, "at": e.updated_at},
        )
        if res.first() is None:
            # a settled line (succeeded / verified) never moves back, attempts never decrease: a stale
            # writer (e.g. a second worker on a re-claimed run) cannot erase the evidence that the line
            # was applied, which is what keeps a per-op line from being sent twice
            log.warning("commit ledger regression refused", change_set_id=e.change_set_id, seq=e.line_seq,
                        state=e.state, attempts=e.attempts)
            current = [r for r in await list_ledger(e.change_set_id, tenant=t) if r["line_seq"] == e.line_seq]
            raise LedgerRegression(e.change_set_id, e.line_seq, current[0]["state"] if current else None)
    return data


async def ledger_entries(change_set_id: str, *, tenant: str | None = None) -> list[dict[str, Any]]:
    """The per-op ledger of a change set as ``CommitLedgerEntry`` JSON (rows of change-set commits,
    which carry no per-op columns, are skipped)."""
    from pydantic import ValidationError

    from keelson_contracts.commit import CommitLedgerEntry

    out: list[dict[str, Any]] = []
    for r in await list_ledger(change_set_id, tenant=tenant):
        if not r.get("op") or not r.get("tool") or not r.get("idempotency"):
            continue
        try:
            out.append(CommitLedgerEntry.model_validate({
                "change_set_id": change_set_id, "line_seq": r["line_seq"], "op": r["op"],
                "connector": r["connector"], "tool": r["tool"], "idempotency": r["idempotency"],
                "state": r["state"], "attempts": r["attempts"] or 1, "request_id": r["request_id"],
                "ext_ref": r["ext_ref"], "readback": r["readback"], "error": r["error"],
                "updated_at": r["updated_at"],
            }).model_dump(mode="json"))
        except ValidationError as exc:
            log.warning("commit ledger row invalid", change_set_id=change_set_id, seq=r["line_seq"],
                        error=str(exc)[:200])
    return out


# ───────────────────────────── audit ─────────────────────────────


async def admin_audit(
    actor: dict[str, Any] | str,
    action: str,
    target: str | None,
    before: Any = None,
    after: Any = None,
    *,
    tenant: str | None = None,
    platform: bool = False,
) -> None:
    """Append an ``admin_audit`` row. ``platform=True`` writes a cross-tenant row (tenant NULL)."""
    import structlog

    if isinstance(actor, dict):
        actor_sub, actor_username = actor.get("sub"), actor.get("username")
    else:
        actor_sub, actor_username = str(actor), str(actor)
    trace_id = structlog.contextvars.get_contextvars().get("trace_id")
    params = {
        "t": None if platform else resolve_tenant(tenant),
        "sub": actor_sub,
        "username": actor_username,
        "action": action,
        "target": target,
        "before": dumps(before),
        "after": dumps(after),
        "trace": trace_id,
    }
    sql = _sql(
        "INSERT INTO admin_audit (tenant, actor_sub, actor_username, action, target, before, after, trace_id) "
        "VALUES (:t, :sub, :username, :action, :target, CAST(:before AS jsonb), CAST(:after AS jsonb), :trace)"
    )
    if params["t"] is None:
        async with system_session() as s:
            await s.execute(sql, params)
    else:
        async with tenant_session(params["t"]) as s:
            await s.execute(sql, params)


# ───────────────────────────── agent specs ─────────────────────────────


async def get_agent_spec(version: str, *, tenant: str | None = None) -> dict[str, Any] | None:
    """``{version, spec, created_by, created_at}`` or None (``'default'`` = built-in spec)."""
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql("SELECT version, spec, created_by, created_at FROM agent_spec WHERE version = :v"), {"v": version}
        )
        m = res.mappings().first()
    return dict(m) if m is not None else None


# ───────────────────────────── LLM record/replay (0006, runtime/replay.py) ─────────────────────────────


async def get_llm_replay(key: str, *, tenant: str | None = None) -> dict[str, Any] | None:
    """The recorded responses for a normalised request key, or None (RLS: this tenant only)."""
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        res = await s.execute(
            _sql("SELECT key, alias, agent, spec_version, model, responses, usage, bytes, created_at "
                 "FROM llm_replay WHERE key = :k"),
            {"k": key},
        )
        m = res.mappings().first()
    return dict(m) if m is not None else None


async def put_llm_replay(
    key: str,
    *,
    alias: str,
    agent: str,
    spec_version: str,
    model: str,
    responses: list[dict[str, Any]],
    usage: dict[str, Any] | None,
    size: int,
    tenant: str | None = None,
) -> None:
    """Upsert one recording (``record`` mode re-records; concurrent identical misses converge)."""
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        await s.execute(
            _sql(
                "INSERT INTO llm_replay (tenant, key, alias, agent, spec_version, model, responses, usage, bytes) "
                "VALUES (:t, :k, :alias, :agent, :spec, :model, CAST(:responses AS jsonb), CAST(:usage AS jsonb), "
                ":bytes) ON CONFLICT (tenant, key) DO UPDATE SET alias = EXCLUDED.alias, agent = EXCLUDED.agent, "
                "spec_version = EXCLUDED.spec_version, model = EXCLUDED.model, responses = EXCLUDED.responses, "
                "usage = EXCLUDED.usage, bytes = EXCLUDED.bytes, created_at = now()"
            ),
            {"t": t, "k": key, "alias": alias, "agent": agent, "spec": spec_version, "model": model,
             "responses": dumps(responses), "usage": dumps(usage), "bytes": size},
        )


async def delete_llm_replays(*, older_than: dt.datetime | None = None, tenant: str | None = None) -> int:
    """Forget this tenant's recordings (all, or those created before ``older_than``)."""
    t = resolve_tenant(tenant)
    async with tenant_session(t) as s:
        if older_than is None:
            res = await s.execute(_sql("DELETE FROM llm_replay"))
        else:
            res = await s.execute(_sql("DELETE FROM llm_replay WHERE created_at < :ts"), {"ts": older_than})
    return int(res.rowcount or 0)
