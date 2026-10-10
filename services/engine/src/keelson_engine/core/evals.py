"""M8 Evals UI store (``docs/contracts/128-m8-evals-ui.md`` §3–§4).

Eval runs (in-app or imported), their case results and progress events, the in-app runner's
heartbeat / claim / lease protocol, the Keelson-case links the runtime reads, imports from JSON
documents and from the mounted results folder, exports, comparisons and trends.

Eval history is platform-wide and only platform admins read it, so the API side uses
``system_session``; the tables are still tenant tables (RLS ``FORCE``d) because their rows belong to
the tenant the cases ran in. The runtime port (:func:`get_eval_link`, :func:`add_eval_usage`) runs
in the run's ``tenant_session``.

Rules:

* **One active run platform-wide** (unique index): the runner resets Merxa's demo data per case.
* **Leases.** A claimed run is ``running`` with ``lease_expires_at``; every runner call renews it.
  An expired lease is reaped lazily (on claim, status and listing reads): the run ends ``failed``
  (``runner_lost``; ``cancelled`` when a cancel was requested) and keeps the case results it has.
* **The engine is authoritative**: the summary and threshold outcomes are computed from the stored
  case results when a run finishes (``core.evals_summary``), whatever a runner or a file claimed,
  and a case result is merged with what the engine metered for its linked Keelson case.
* **Untrusted input**: runner reports and imported files go through
  ``keelson_contracts.evals.case_result_from_doc`` (caps, sanitising); a reported Keelson case id
  is kept only when that case is linked to the run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keelson_contracts.evals import (
    DEFAULT_EVAL_THRESHOLDS,
    EVAL_LIVE_MODES,
    EVAL_MODES,
    EVALS_SCHEMA_V2,
    EvalActor,
    EvalBudget,
    EvalCaseResult,
    EvalCaseRow,
    EvalCompare,
    EvalCounters,
    EvalDocError,
    EvalDocHeader,
    EvalEngineAccounting,
    EvalModeAvailability,
    EvalProgress,
    EvalRecordings,
    EvalRun,
    EvalRunDetail,
    EvalRunEvent,
    EvalRunEventIn,
    EvalRunnerInfo,
    EvalRunnerJob,
    EvalRunSummary,
    EvalSuiteInfo,
    EvalThreshold,
    EvalThresholdResult,
    EvalTrends,
    breach_count,
    case_result_from_doc,
    case_result_to_doc,
    clean_text,
    evaluate_thresholds,
    results_doc_sha256,
    results_doc_to_case_results,
)
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from keelson_engine.config import get_settings
from keelson_engine.core import evals_summary as S
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = [
    "RUNNER_ACTOR",
    "EvalLink",
    "active_run",
    "add_eval_usage",
    "attach_case",
    "cancel_run",
    "case_result",
    "claim",
    "compare",
    "create_run",
    "export_doc",
    "finish_run",
    "get_eval_link",
    "gateway_budget_spent",
    "get_run",
    "import_doc",
    "import_folder",
    "list_files",
    "list_runs",
    "mode_availability",
    "put_case_result",
    "reap_expired",
    "recordings",
    "run_detail",
    "run_events",
    "runner_events",
    "runner_heartbeat",
    "runner_info",
    "trends",
]

log = get_logger(__name__)

RUNNER_ACTOR = {"sub": "evals-runner", "username": "evals-runner"}
"""The audit actor of what the runner does (finishing a run)."""
MAX_EVENTS_PER_POST = 50
MAX_EVENTS_PER_RUN = 5000
EVENT_DATA_MAX_BYTES = 16384
"""``eval_run_event.data``'s size check (``octet_length(data::text)``)."""
SUITE_MAX_BYTES = 262144
"""``eval_runner.suite``'s size check."""
EVENTS_PAGE = 200
RUNNER_ROWS_KEPT_DAYS = 7
TERMINAL = ("succeeded", "failed", "cancelled")
_EVAL_FILE_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z\.json")
"""``evals/results/<UTC stamp>.json`` (the M7 listing's file names)."""
_RUN_COLS = (
    "r.id, r.tenant, r.source, r.mode, r.status, r.case_ids, r.requested_by, r.note, r.budget_max_tokens, "
    "r.budget_max_cost_microusd, r.tokens_live, r.tokens_replayed, r.llm_calls_live, r.llm_calls_replayed, "
    "r.replay_misses, r.cost_microusd, r.runner_id, r.lease_expires_at, r.cancel_requested, r.suite_hash, "
    "r.thresholds, r.summary, r.threshold_results, r.breaches, r.doc_schema, r.doc_sha256, r.file_name, r.target, "
    "r.started_at, r.finished_at, r.error, r.created_at"
)
_RUN_SELECT = (
    f"SELECT {_RUN_COLS}, "
    "(SELECT count(*) FROM eval_case_result c WHERE c.run_id = r.id) AS cases_done, "
    "(SELECT row_to_json(e) FROM (SELECT type, case_id, turn_id FROM eval_run_event x WHERE x.run_id = r.id "
    "ORDER BY x.seq DESC LIMIT 1) e) AS last_event "
    "FROM eval_run r"
)


def _j(value: Any) -> Any:
    """A json / jsonb value as Python (the driver may hand back text)."""
    return json.loads(value) if isinstance(value, str) else value


def _jsonb_text_bytes(value: Any) -> int:
    """UTF-8 size of ``value`` as Postgres prints a jsonb (``", "`` / ``": "`` separators)."""
    return len(json.dumps(value, ensure_ascii=False, separators=(", ", ": ")).encode("utf-8"))


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _not_found() -> AdminError:
    return AdminError(404, "not_found", "eval run not found")


def _uuid(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value or ""):
        raise _not_found()
    return value


def _live_budget_exhausted(row: Mapping[str, Any]) -> bool:
    if row["mode"] not in EVAL_LIVE_MODES or row["budget_max_tokens"] is None:
        return False
    return (int(row["tokens_live"]) >= int(row["budget_max_tokens"])
            or int(row["cost_microusd"]) >= int(row["budget_max_cost_microusd"]))


# ───────────────────────────── views ─────────────────────────────


def _progress(row: Mapping[str, Any]) -> EvalProgress:
    last = _j(row.get("last_event")) or {}
    current_case = current_turn = None
    if row["status"] == "running" and last.get("type") in ("case_started", "turn_started", "turn_finished"):
        current_case = last.get("case_id")
        current_turn = last.get("turn_id") if last.get("type") == "turn_started" else None
    return EvalProgress(cases_done=int(row.get("cases_done") or 0), cases_total=len(row["case_ids"] or []),
                        current_case=current_case, current_turn=current_turn)


def _run_view(row: Mapping[str, Any]) -> EvalRun:
    summary = _j(row["summary"])
    by = _j(row["requested_by"])
    budget = (EvalBudget(max_tokens=int(row["budget_max_tokens"]), max_cost_microusd=int(row["budget_max_cost_microusd"]))
              if row["budget_max_tokens"] is not None else None)
    return EvalRun(
        id=str(row["id"]),
        tenant=row["tenant"],
        source=row["source"],
        mode=row["mode"],
        status=row["status"],
        case_ids=list(row["case_ids"] or []),
        requested_by=EvalActor.model_validate(by) if by else None,
        note=row["note"],
        budget=budget,
        counters=EvalCounters(tokens_live=int(row["tokens_live"]), tokens_replayed=int(row["tokens_replayed"]),
                              llm_calls_live=int(row["llm_calls_live"]), llm_calls_replayed=int(row["llm_calls_replayed"]),
                              replay_misses=int(row["replay_misses"]), cost_microusd=int(row["cost_microusd"])),
        cancel_requested=bool(row["cancel_requested"]),
        suite_hash=row["suite_hash"],
        summary=EvalRunSummary.model_validate(summary) if summary else None,
        thresholds=[EvalThreshold.model_validate(t) for t in _j(row["thresholds"]) or []],
        threshold_results=[EvalThresholdResult.model_validate(t) for t in _j(row["threshold_results"]) or []],
        breaches=int(row["breaches"]),
        doc_schema=row["doc_schema"],
        file_name=row["file_name"],
        target=row["target"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        progress=_progress(row),
    )


def _actor(actor: Mapping[str, Any]) -> dict[str, Any]:
    return {"sub": str(actor.get("sub") or "unknown")[:200], "username": str(actor.get("username") or "unknown")[:200],
            "name": (str(actor["name"])[:200] if actor.get("name") else None)}


async def _fetch_run(s: AsyncSession, run_id: str, *, lock: bool = False) -> dict[str, Any] | None:
    sql = f"SELECT {_RUN_COLS} FROM eval_run r WHERE r.id = :id" + (" FOR UPDATE" if lock else "")
    row = (await s.execute(text(sql), {"id": run_id})).mappings().first()
    return dict(row) if row is not None else None


async def _case_results(s: AsyncSession, run_id: str) -> list[EvalCaseResult]:
    rows = (await s.execute(text("SELECT result FROM eval_case_result WHERE run_id = :id ORDER BY ordinal, case_id"),
                            {"id": run_id})).all()
    return [EvalCaseResult.model_validate(_j(r[0])) for r in rows]


# ───────────────────────────── finishing / reaping ─────────────────────────────


async def _finalize(s: AsyncSession, row: Mapping[str, Any], status: str, error: str | None) -> dict[str, Any]:
    """Final status + the authoritative summary, threshold outcomes and (in-app runs) the
    document identity; returns the audit payload."""
    run_id = str(row["id"])
    cases = await _case_results(s, run_id)
    summary = S.summarise(cases)
    thresholds = [EvalThreshold.model_validate(t) for t in _j(row["thresholds"]) or []]
    outcomes = evaluate_thresholds(summary, thresholds, row["mode"])
    finished = _utc_now()
    doc_sha = row.get("doc_sha256")
    if row["source"] == "in_app" and row["started_at"] is not None:
        header = EvalDocHeader(doc_schema=EVALS_SCHEMA_V2, started_at=row["started_at"], finished_at=finished,
                               mode=row["mode"], runner="in_app", suite_hash=row["suite_hash"], target=None)
        doc_sha = results_doc_sha256(header, cases)
        clash = (await s.execute(text("SELECT 1 FROM eval_run WHERE doc_sha256 = :h AND id <> :id"),
                                 {"h": doc_sha, "id": run_id})).first()
        if clash is not None:
            doc_sha = None
    await s.execute(
        text(
            "UPDATE eval_run SET status = :st, finished_at = :fin, error = :err, summary = CAST(:sum AS jsonb), "
            "threshold_results = CAST(:tr AS jsonb), breaches = :br, doc_sha256 = :sha, lease_expires_at = NULL, "
            "updated_at = now() WHERE id = :id"
        ),
        {"st": status, "fin": finished, "err": clean_text(error, 2000) if error else None,
         "sum": dumps(summary), "tr": dumps([o.model_dump(mode="json") for o in outcomes]),
         "br": breach_count(outcomes), "sha": doc_sha, "id": run_id},
    )
    return {"run_id": run_id, "status": status, "mode": row["mode"], "cases": summary.cases, "passed": summary.passed,
            "breaches": breach_count(outcomes), "error": (clean_text(error, 200) if error else None)}


async def reap_expired() -> list[str]:
    """Runs whose runner stopped renewing its lease → ``failed`` (``runner_lost``), or
    ``cancelled`` when a cancel was pending; their case results stay."""
    audits: list[dict[str, Any]] = []
    async with system_session() as s:
        rows = (await s.execute(text(
            f"SELECT {_RUN_COLS} FROM eval_run r WHERE r.status = 'running' AND r.lease_expires_at < now() "
            "FOR UPDATE SKIP LOCKED"))).mappings().all()
        for row in rows:
            cancelled = bool(row["cancel_requested"])
            audits.append(await _finalize(
                s, row, "cancelled" if cancelled else "failed",
                None if cancelled else "runner_lost: the eval runner stopped reporting (its lease expired)"))
    for payload in audits:
        log.warning("eval run reaped", run_id=payload["run_id"], status=payload["status"])
        await admin_audit(RUNNER_ACTOR, "evals.run_finished", f"eval_run:{payload['run_id']}", None, payload,
                          platform=True)
    return [a["run_id"] for a in audits]


# ───────────────────────────── runner (platform table) ─────────────────────────────


async def runner_heartbeat(runner_id: str, version: str, suite: EvalSuiteInfo) -> None:
    if _jsonb_text_bytes(suite.model_dump(mode="json")) > SUITE_MAX_BYTES:  # what the table refuses: a 422
        raise AdminError(422, "suite_too_large", f"the case catalogue exceeds {SUITE_MAX_BYTES} bytes")
    async with system_session() as s:
        await s.execute(
            text(
                "INSERT INTO eval_runner (id, version, suite_hash, suite, last_seen_at) "
                "VALUES (:id, :v, :h, CAST(:suite AS jsonb), now()) ON CONFLICT (id) DO UPDATE SET "
                "version = EXCLUDED.version, suite_hash = EXCLUDED.suite_hash, suite = EXCLUDED.suite, "
                "last_seen_at = now()"
            ),
            {"id": runner_id, "v": version, "h": suite.suite_hash, "suite": dumps(suite)},
        )
        await s.execute(text("DELETE FROM eval_runner WHERE last_seen_at < now() - make_interval(days => :d)"),
                        {"d": RUNNER_ROWS_KEPT_DAYS})


async def runner_info() -> tuple[EvalRunnerInfo, EvalSuiteInfo | None]:
    """The most recently seen runner (online = seen within ``EVALS_RUNNER_STALE_S``) and its suite."""
    settings = get_settings()
    configured = bool(settings.evals_runner_token.get_secret_value())
    async with system_session() as s:
        row = (await s.execute(text(
            "SELECT id, version, suite_hash, suite, last_seen_at, "
            "(last_seen_at >= now() - make_interval(secs => :stale)) AS fresh "
            "FROM eval_runner ORDER BY last_seen_at DESC LIMIT 1"), {"stale": settings.evals_runner_stale_s})).mappings().first()
    if row is None:
        return EvalRunnerInfo(configured=configured, online=False, runner_id=None, version=None, last_seen_at=None,
                              suite_hash=None), None
    try:
        suite: EvalSuiteInfo | None = EvalSuiteInfo.model_validate(_j(row["suite"]))
    except ValidationError:
        suite = None
    info = EvalRunnerInfo(configured=configured, online=configured and bool(row["fresh"]), runner_id=row["id"],
                          version=row["version"], last_seen_at=row["last_seen_at"], suite_hash=row["suite_hash"])
    return info, suite


def mode_availability(runner: EvalRunnerInfo, *, demo_mode: bool, live_enabled: bool, process_mode: str,
                      gateway_budget_spent: bool = False) -> list[EvalModeAvailability]:
    """Whether each mode can start now, with the first reason it cannot (pure)."""
    base: tuple[str, str] | None = None
    if not runner.configured:
        base = ("runner_not_configured", "The in-app runner is off (EVALS_RUNNER_TOKEN is not set).")
    elif not demo_mode:
        base = ("demo_mode_off", "Evals drive the demo personas and reset Merxa's demo data: DEMO_MODE is off.")
    elif not runner.online:
        base = ("runner_offline", "The eval runner is offline. Start it: docker compose --profile evals up -d "
                                  "evals-runner")
    out: list[EvalModeAvailability] = []
    for mode in EVAL_MODES:
        reason: tuple[str, str] | None = base
        if reason is None and mode in EVAL_LIVE_MODES:
            if process_mode in ("replay", "invalid"):
                reason = ("replay_only", "This installation only replays recorded model answers "
                                         "(LLM_REPLAY_MODE=replay): live runs are impossible.")
            elif not live_enabled:
                reason = ("live_disabled", "Live eval runs are switched off (EVALS_LIVE_ENABLED=0).")
            elif gateway_budget_spent:
                reason = ("gateway_budget_low", "The demo tenant's AI gateway budget is used up: the gateway would "
                                                "refuse the model calls.")
        out.append(EvalModeAvailability(mode=mode, available=reason is None,  # type: ignore[arg-type]
                                        reason=reason[0] if reason else None,  # type: ignore[arg-type]
                                        message=reason[1] if reason else None))
    return out


async def gateway_budget_spent(tenant: str) -> bool:
    """Whether ``tenant``'s AI gateway team has a budget and spent it (LiteLLM ``spend ≥
    max_budget``, both in the gateway's own units: the eval budget's micro-USD are never compared
    with it). ``False`` when no budget is set or the gateway cannot be asked: this is a pre-check
    for a clear refusal, the gateway itself stays the outer cap."""
    from keelson_engine.core import gateway  # noqa: PLC0415 - platform module, only for live modes

    try:
        team = await gateway.get_admin().team_info(gateway.team_id(tenant))
    except Exception as exc:  # noqa: BLE001 - transport or API failure: unknown, not a refusal
        log.warning("eval gateway budget lookup failed", error=type(exc).__name__)
        return False
    if not team or team.get("max_budget") is None:
        return False
    try:
        return float(team.get("spend") or 0.0) >= float(team["max_budget"])
    except (TypeError, ValueError):
        return False


# ───────────────────────────── runs (admin API) ─────────────────────────────


async def create_run(*, tenant: str, mode: str, case_ids: Sequence[str], actor: Mapping[str, Any],
                     note: str | None, budget: EvalBudget | None, suite_hash: str) -> EvalRun:
    """Queue an in-app run (409 ``eval_running`` while another run is queued or running)."""
    params = {
        "t": tenant, "m": mode, "c": list(case_ids), "by": dumps(_actor(actor)),
        "n": clean_text(note, 200) or None if note else None,
        "bt": budget.max_tokens if budget is not None and mode in EVAL_LIVE_MODES else None,
        "bc": budget.max_cost_microusd if budget is not None and mode in EVAL_LIVE_MODES else None,
        "h": suite_hash, "th": dumps([t.model_dump(mode="json") for t in DEFAULT_EVAL_THRESHOLDS]),
    }
    try:
        async with system_session() as s:
            row = (await s.execute(text(
                "INSERT INTO eval_run (tenant, source, mode, status, case_ids, requested_by, note, budget_max_tokens, "
                "budget_max_cost_microusd, suite_hash, thresholds) VALUES (:t, 'in_app', :m, 'queued', "
                "CAST(:c AS text[]), CAST(:by AS jsonb), :n, :bt, :bc, :h, CAST(:th AS jsonb)) RETURNING id"),
                params)).first()
    except IntegrityError as exc:
        if "eval_run_one_active_uq" in str(exc.orig):
            current = await active_run()
            raise AdminError(409, "eval_running", "another eval run is queued or running",
                             run_id=current.id if current else None) from None
        raise
    assert row is not None
    run = await get_run(str(row[0]))
    assert run is not None
    return run


async def active_run() -> EvalRun | None:
    async with system_session() as s:
        row = (await s.execute(text(f"{_RUN_SELECT} WHERE r.status IN ('queued', 'running') LIMIT 1"))).mappings().first()
    return _run_view(row) if row is not None else None


async def get_run(run_id: str) -> EvalRun | None:
    _uuid(run_id)
    async with system_session() as s:
        row = (await s.execute(text(f"{_RUN_SELECT} WHERE r.id = :id"), {"id": run_id})).mappings().first()
    return _run_view(row) if row is not None else None


async def list_runs(*, limit: int, before: str | None = None, mode: str | None = None,
                    source: str | None = None) -> dict[str, Any]:
    """Newest first; ``next_before`` = the last item's id when more exist."""
    conds = ["true"]
    params: dict[str, Any] = {"n": limit + 1}
    if before:
        _uuid(before)
        conds.append("(r.created_at, r.id) < (SELECT created_at, id FROM eval_run WHERE id = :before)")
        params["before"] = before
    if mode == "live":
        conds.append("r.mode <> 'replay'")
    elif mode:
        conds.append("r.mode = :mode")
        params["mode"] = mode
    if source:
        conds.append("r.source = :source")
        params["source"] = source
    async with system_session() as s:
        rows = (await s.execute(text(f"{_RUN_SELECT} WHERE {' AND '.join(conds)} "
                                     "ORDER BY r.created_at DESC, r.id DESC LIMIT :n"), params)).mappings().all()
    items = [_run_view(r) for r in rows[:limit]]
    return {"items": [i.model_dump(mode="json") for i in items],
            "next_before": items[-1].id if len(rows) > limit and items else None}


async def run_detail(run_id: str) -> EvalRunDetail:
    run = await get_run(run_id)
    if run is None:
        raise _not_found()
    async with system_session() as s:
        rows = (await s.execute(text(
            "SELECT case_id, ordinal, title, tags, status, error_code, error, keelson_case_id, duration_ms, checks_total, "
            "checks_failed, soft_failed, entity_recall_bp, numeric_grounding_bp, trajectory_recall_bp, first_stage_ms, "
            "options_ms, tokens, cost_microusd, failed_checks FROM eval_case_result WHERE run_id = :id "
            "ORDER BY ordinal, case_id"), {"id": run_id})).mappings().all()
    cases = [EvalCaseRow.model_validate({**dict(r), "tags": list(r["tags"] or []),
                                         "failed_checks": list(r["failed_checks"] or [])}) for r in rows]
    return EvalRunDetail(run=run, cases=cases)


async def case_result(run_id: str, case_id: str) -> EvalCaseResult:
    _uuid(run_id)
    async with system_session() as s:
        row = (await s.execute(text("SELECT result FROM eval_case_result WHERE run_id = :r AND case_id = :c"),
                               {"r": run_id, "c": case_id[:64]})).first()
    if row is None:
        raise AdminError(404, "not_found", "case result not found")
    return EvalCaseResult.model_validate(_j(row[0]))


async def run_events(run_id: str, *, after: int, limit: int = EVENTS_PAGE) -> dict[str, Any]:
    run = await get_run(run_id)
    if run is None:
        raise _not_found()
    async with system_session() as s:
        rows = (await s.execute(text(
            "SELECT seq, type, case_id, turn_id, data, at FROM eval_run_event WHERE run_id = :r AND seq > :a "
            "ORDER BY seq LIMIT :n"), {"r": run_id, "a": after, "n": limit})).mappings().all()
    items = [EvalRunEvent.model_validate({**dict(r), "data": _j(r["data"]) or {}}) for r in rows]
    return {"items": [i.model_dump(mode="json") for i in items],
            "next_after": items[-1].seq if items else after, "status": run.status,
            "progress": run.progress.model_dump(mode="json"), "counters": run.counters.model_dump(mode="json")}


async def cancel_run(run_id: str, actor: Mapping[str, Any]) -> EvalRun:
    """A queued run is cancelled at once; a running one gets ``cancel_requested`` (the runner stops
    before its next turn) and the in-flight Keelson runs of its linked cases are cancelled."""
    _uuid(run_id)
    payload: dict[str, Any] | None = None
    async with system_session() as s:
        row = await _fetch_run(s, run_id, lock=True)
        if row is None:
            raise _not_found()
        if row["status"] in TERMINAL:
            raise AdminError(409, "run_not_active", f"the run already ended ({row['status']})")
        if row["status"] == "queued":
            payload = await _finalize(s, row, "cancelled", None)
        else:
            await s.execute(text("UPDATE eval_run SET cancel_requested = true, updated_at = now() WHERE id = :id"),
                            {"id": run_id})
    cancelled = await _cancel_linked_runs(run_id) if payload is None else 0
    await admin_audit(_actor(actor), "evals.run_cancelled", f"eval_run:{run_id}", None,
                      {"previous_status": row["status"], "keelson_runs_cancelled": cancelled}, platform=True)
    run = await get_run(run_id)
    assert run is not None
    return run


async def _cancel_linked_runs(eval_run_id: str) -> int:
    from keelson_engine.core.runs import RunNotFound, emit_run_error, request_cancel

    async with system_session() as s:
        rows = (await s.execute(text(
            "SELECT r.id, r.tenant, r.case_id FROM run r JOIN eval_case_link l ON l.case_id = r.case_id "
            "WHERE l.run_id = :id AND r.status IN ('queued', 'running')"), {"id": eval_run_id})).all()
    n = 0
    for run_id, tenant, case_id in rows:
        try:
            previous, status = await request_cancel(str(tenant), str(run_id))
        except RunNotFound:
            continue
        n += 1
        if previous == "queued" and status == "cancelled":
            await emit_run_error(str(tenant), str(case_id), str(run_id), message="Run cancelled.", code="CANCELLED")
    return n


# ───────────────────────────── runner protocol ─────────────────────────────


async def claim(runner_id: str) -> EvalRunnerJob | None:
    await reap_expired()
    lease = get_settings().evals_lease_s
    async with system_session() as s:
        row = (await s.execute(text(
            "UPDATE eval_run SET status = 'running', runner_id = :w, "
            "lease_expires_at = now() + make_interval(secs => :lease), started_at = date_trunc('second', now()), "
            "updated_at = now() WHERE id = (SELECT id FROM eval_run WHERE status = 'queued' ORDER BY created_at, id "
            "FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING id, mode, case_ids, budget_max_tokens, budget_max_cost_microusd, lease_expires_at"),
            {"w": runner_id, "lease": lease})).mappings().first()
    if row is None:
        return None
    budget = (EvalBudget(max_tokens=int(row["budget_max_tokens"]), max_cost_microusd=int(row["budget_max_cost_microusd"]))
              if row["budget_max_tokens"] is not None else None)
    log.info("eval run claimed", run_id=str(row["id"]), runner_id=runner_id, mode=row["mode"])
    return EvalRunnerJob(run_id=str(row["id"]), mode=row["mode"], case_ids=list(row["case_ids"]), budget=budget,
                         lease_expires_at=row["lease_expires_at"])


async def _owned(s: AsyncSession, run_id: str, runner_id: str) -> dict[str, Any]:
    """The running run held by ``runner_id`` (row-locked; lease renewed), else 409. The call is
    also the runner's sign of life: the loop only heartbeats between runs, and a live run can take
    an hour, so the status must not call a working runner offline."""
    _uuid(run_id)
    row = await _fetch_run(s, run_id, lock=True)
    if row is None or row["status"] != "running" or row["runner_id"] != runner_id:
        raise AdminError(409, "run_not_running", "this runner does not hold a running lease on the run")
    renewed = (await s.execute(text(
        "UPDATE eval_run SET lease_expires_at = now() + make_interval(secs => :lease), updated_at = now() "
        "WHERE id = :id RETURNING lease_expires_at"), {"id": run_id, "lease": get_settings().evals_lease_s})).scalar_one()
    await s.execute(text("UPDATE eval_runner SET last_seen_at = now() WHERE id = :w"), {"w": runner_id})
    row["lease_expires_at"] = renewed
    return row


async def runner_events(run_id: str, runner_id: str, events: Sequence[EvalRunEventIn]) -> dict[str, Any]:
    """Append progress events (``seq`` assigned here), renew the lease, answer the run's flags."""
    if len(events) > MAX_EVENTS_PER_POST:
        raise AdminError(422, "too_many_events", f"at most {MAX_EVENTS_PER_POST} events per call")
    payloads = [{k: clean_text(v, 300) if isinstance(v, str) else v for k, v in ev.data.items()} for ev in events]
    if any(_jsonb_text_bytes(p) > EVENT_DATA_MAX_BYTES for p in payloads):
        # 300 characters per value can still be 4 bytes each: refuse what the table would (a 422, not a 500)
        raise AdminError(422, "event_too_large", f"an event's data exceeds {EVENT_DATA_MAX_BYTES} bytes")
    async with system_session() as s:
        row = await _owned(s, run_id, runner_id)
        if events:
            last = int((await s.execute(text("SELECT coalesce(max(seq), 0) FROM eval_run_event WHERE run_id = :r"),
                                        {"r": run_id})).scalar_one())
            room = max(0, MAX_EVENTS_PER_RUN - last)
            for i, (ev, data) in enumerate(list(zip(events, payloads, strict=True))[:room], start=1):
                await s.execute(text(
                    "INSERT INTO eval_run_event (tenant, run_id, seq, type, case_id, turn_id, data) "
                    "VALUES (:t, :r, :seq, :type, :c, :turn, CAST(:d AS jsonb))"),
                    {"t": row["tenant"], "r": run_id, "seq": last + i, "type": ev.type, "c": ev.case_id,
                     "turn": clean_text(ev.turn_id, 64) if ev.turn_id else None, "d": dumps(data)})
    return {"cancel_requested": bool(row["cancel_requested"]), "budget_exhausted": _live_budget_exhausted(row),
            "lease_expires_at": row["lease_expires_at"].isoformat()}


async def attach_case(run_id: str, runner_id: str, eval_case_id: str, keelson_case_id: str) -> dict[str, Any]:
    """Link the Keelson case the runner just created to the run: it must be in the run's tenant,
    created after the run started and not linked to anything else (idempotent for the same pair)."""
    async with system_session() as s:
        row = await _owned(s, run_id, runner_id)
        if eval_case_id not in (row["case_ids"] or []):
            raise AdminError(422, "unknown_case", "the case is not part of this run")
        case = (await s.execute(text('SELECT tenant, created_at FROM "case" WHERE id = :id'),
                                {"id": keelson_case_id[:64]})).mappings().first()
        if case is None or case["tenant"] != row["tenant"]:
            raise AdminError(404, "case_not_found", "no such case in the run's tenant")
        if row["started_at"] is not None and case["created_at"] < row["started_at"]:
            raise AdminError(409, "case_predates_run", "the case was created before the run started")
        existing = (await s.execute(text("SELECT run_id, eval_case_id FROM eval_case_link WHERE case_id = :c"),
                                    {"c": keelson_case_id})).first()
        if existing is not None:
            if str(existing[0]) == run_id and existing[1] == eval_case_id:
                return {"linked": True, "case_id": keelson_case_id}
            raise AdminError(409, "case_already_linked", "the case is linked to another eval case or run")
        await s.execute(text(
            "INSERT INTO eval_case_link (tenant, case_id, run_id, eval_case_id, llm_mode) VALUES (:t, :c, :r, :e, :m)"),
            {"t": row["tenant"], "c": keelson_case_id, "r": run_id, "e": eval_case_id, "m": row["mode"]})
    return {"linked": True, "case_id": keelson_case_id}


async def _store_case(s: AsyncSession, *, tenant: str, run_id: str, ordinal: int, result: EvalCaseResult) -> None:
    cols = S.case_columns(result)
    await s.execute(
        text(
            "INSERT INTO eval_case_result (tenant, run_id, case_id, ordinal, title, tags, status, error_code, error, "
            "keelson_case_id, duration_ms, checks_total, checks_failed, soft_failed, entity_recall_bp, "
            "numeric_grounding_bp, trajectory_recall_bp, first_stage_ms, options_ms, tokens, cost_microusd, "
            "failed_checks, result) VALUES (:tenant, :run, :case_id, :ordinal, :title, CAST(:tags AS text[]), :status, "
            ":error_code, :error, :keelson_case_id, :duration_ms, :checks_total, :checks_failed, :soft_failed, "
            ":entity_recall_bp, :numeric_grounding_bp, :trajectory_recall_bp, :first_stage_ms, :options_ms, :tokens, "
            ":cost_microusd, CAST(:failed_checks AS text[]), CAST(:result AS jsonb)) "
            "ON CONFLICT (run_id, case_id) DO UPDATE SET ordinal = EXCLUDED.ordinal, title = EXCLUDED.title, "
            "tags = EXCLUDED.tags, status = EXCLUDED.status, error_code = EXCLUDED.error_code, error = EXCLUDED.error, "
            "keelson_case_id = EXCLUDED.keelson_case_id, duration_ms = EXCLUDED.duration_ms, "
            "checks_total = EXCLUDED.checks_total, checks_failed = EXCLUDED.checks_failed, "
            "soft_failed = EXCLUDED.soft_failed, entity_recall_bp = EXCLUDED.entity_recall_bp, "
            "numeric_grounding_bp = EXCLUDED.numeric_grounding_bp, trajectory_recall_bp = EXCLUDED.trajectory_recall_bp, "
            "first_stage_ms = EXCLUDED.first_stage_ms, options_ms = EXCLUDED.options_ms, tokens = EXCLUDED.tokens, "
            "cost_microusd = EXCLUDED.cost_microusd, failed_checks = EXCLUDED.failed_checks, result = EXCLUDED.result, "
            "updated_at = now()"
        ),
        {**cols, "tenant": tenant, "run": run_id, "case_id": result.case_id, "ordinal": ordinal,
         "result": dumps(result)},
    )


async def put_case_result(run_id: str, runner_id: str, eval_case_id: str, raw: Any) -> EvalCaseResult:
    """Store the runner's result for one case (idempotent: a retry replaces it), merged with the
    engine's own accounting of the linked Keelson case. A case that failed after a replay miss
    the runner could not see (a tolerated agent failure) is an ``error`` (``replay_miss``)."""
    try:
        result = case_result_from_doc(raw)
    except EvalDocError as exc:
        raise AdminError(422, "invalid_result", str(exc)) from None
    if result.case_id != eval_case_id:
        raise AdminError(422, "invalid_result", "the result is for another case")
    async with system_session() as s:
        row = await _owned(s, run_id, runner_id)
        if eval_case_id not in (row["case_ids"] or []):
            raise AdminError(422, "unknown_case", "the case is not part of this run")
        links = (await s.execute(text(
            "SELECT case_id, llm_calls_live, llm_calls_replayed, tokens_live, replay_misses, cost_microusd "
            "FROM eval_case_link WHERE run_id = :r AND eval_case_id = :e ORDER BY created_at"),
            {"r": run_id, "e": eval_case_id})).mappings().all()
        update: dict[str, Any] = {}
        linked = {str(link["case_id"]) for link in links}
        if result.keelson_case_id is not None and result.keelson_case_id not in linked:
            update["keelson_case_id"] = None  # never trust an id the engine did not link
        if links:
            accounting = EvalEngineAccounting(
                llm_calls_live=sum(int(x["llm_calls_live"]) for x in links),
                llm_calls_replayed=sum(int(x["llm_calls_replayed"]) for x in links),
                tokens_live=sum(int(x["tokens_live"]) for x in links),
                replay_misses=sum(int(x["replay_misses"]) for x in links),
                cost_microusd=sum(int(x["cost_microusd"]) for x in links),
            )
            update["engine"] = accounting
            if accounting.replay_misses and result.status == "fail" and result.error_code is None:
                update.update(status="error", error_code="replay_miss",
                              error="a model request had no recording in replay mode (replay miss)")
        if update:
            result = EvalCaseResult.model_validate({**result.model_dump(mode="json"),
                                                    **{k: (v.model_dump(mode="json") if hasattr(v, "model_dump") else v)
                                                       for k, v in update.items()}})
        await _store_case(s, tenant=row["tenant"], run_id=run_id,
                          ordinal=list(row["case_ids"]).index(eval_case_id), result=result)
    return result


async def finish_run(run_id: str, runner_id: str, status: str, error: str | None) -> EvalRun:
    async with system_session() as s:
        row = await _owned(s, run_id, runner_id)
        final = "cancelled" if row["cancel_requested"] else status
        payload = await _finalize(s, row, final, error if final == "failed" else None)
    await admin_audit(RUNNER_ACTOR, "evals.run_finished", f"eval_run:{run_id}", None, payload, platform=True)
    run = await get_run(run_id)
    assert run is not None
    return run


# ───────────────────────────── runtime port ─────────────────────────────


@dataclass(frozen=True, slots=True)
class EvalLink:
    """A Keelson case's eval link + its run's state (``runtime.execute`` reads it at run start)."""

    run_id: str
    eval_case_id: str
    llm_mode: str
    status: str
    cancel_requested: bool
    budget_max_tokens: int | None
    budget_max_cost_microusd: int | None
    tokens_live: int
    cost_microusd: int

    @property
    def live(self) -> bool:
        return self.llm_mode in EVAL_LIVE_MODES


async def get_eval_link(tenant: str, case_id: str) -> EvalLink | None:
    async with tenant_session(tenant) as s:
        row = (await s.execute(text(
            "SELECT l.run_id, l.eval_case_id, l.llm_mode, r.status, r.cancel_requested, r.budget_max_tokens, "
            "r.budget_max_cost_microusd, r.tokens_live, r.cost_microusd FROM eval_case_link l "
            "JOIN eval_run r ON r.id = l.run_id WHERE l.case_id = :c"), {"c": case_id})).mappings().first()
    if row is None:
        return None
    return EvalLink(run_id=str(row["run_id"]), eval_case_id=row["eval_case_id"], llm_mode=row["llm_mode"],
                    status=row["status"], cancel_requested=bool(row["cancel_requested"]),
                    budget_max_tokens=row["budget_max_tokens"], budget_max_cost_microusd=row["budget_max_cost_microusd"],
                    tokens_live=int(row["tokens_live"]), cost_microusd=int(row["cost_microusd"]))


async def add_eval_usage(tenant: str, case_id: str, run_id: str, *, calls_live: int, calls_replayed: int,
                         tokens_live: int, tokens_replayed: int, misses: int, cost_microusd: int) -> None:
    """Add one Keelson run's metered usage to its linked case and eval run (atomic increments)."""
    params = {"c": case_id, "r": run_id, "cl": max(0, calls_live), "cr": max(0, calls_replayed),
              "tl": max(0, tokens_live), "tr": max(0, tokens_replayed), "m": max(0, misses),
              "cost": max(0, cost_microusd)}
    async with tenant_session(tenant) as s:
        await s.execute(text(
            "UPDATE eval_case_link SET llm_calls_live = llm_calls_live + :cl, "
            "llm_calls_replayed = llm_calls_replayed + :cr, tokens_live = tokens_live + :tl, "
            "replay_misses = replay_misses + :m, cost_microusd = cost_microusd + :cost "
            "WHERE case_id = :c AND run_id = :r"), params)
        await s.execute(text(
            "UPDATE eval_run SET llm_calls_live = llm_calls_live + :cl, llm_calls_replayed = llm_calls_replayed + :cr, "
            "tokens_live = tokens_live + :tl, tokens_replayed = tokens_replayed + :tr, "
            "replay_misses = replay_misses + :m, cost_microusd = cost_microusd + :cost, updated_at = now() "
            "WHERE id = :r"), params)


async def recordings(tenant: str) -> EvalRecordings:
    """What a replay run of ``tenant`` can answer from (``llm_replay`` rows per agent)."""
    async with tenant_session(tenant) as s:
        total = (await s.execute(text(
            "SELECT count(*) AS n, coalesce(sum(bytes), 0) AS b, max(created_at) AS newest FROM llm_replay"))).mappings().one()
        agents = (await s.execute(text(
            "SELECT agent, count(*) AS n FROM llm_replay GROUP BY agent ORDER BY count(*) DESC, agent LIMIT 50"))).all()
    return EvalRecordings(rows=int(total["n"]), bytes=int(total["b"]), newest_at=total["newest"],
                          agents={(str(a) or "unknown")[:80]: int(n) for a, n in agents})


# ───────────────────────────── imports & exports ─────────────────────────────


async def import_doc(doc: Any, *, tenant: str, actor: Mapping[str, Any], file_name: str | None = None
                     ) -> tuple[str, bool]:
    """Store a ``keelson-evals/1|2`` document as a finished run: ``(run_id, duplicate)`` — the same
    normalised document (whatever its file name or layout) is imported once."""
    try:
        header, cases = results_doc_to_case_results(doc)
    except EvalDocError as exc:
        raise AdminError(422, "invalid_results", str(exc)) from None
    except ValidationError:
        raise AdminError(422, "invalid_results", "the results document does not validate") from None
    sha = results_doc_sha256(header, cases)
    summary = S.summarise(cases)
    thresholds = list(DEFAULT_EVAL_THRESHOLDS)
    outcomes = evaluate_thresholds(summary, thresholds, header.mode)
    name = clean_text(file_name, 120) if file_name else None
    try:
        async with system_session() as s:
            found = (await s.execute(text("SELECT id FROM eval_run WHERE doc_sha256 = :h"), {"h": sha})).first()
            if found is not None:
                return str(found[0]), True
            row = (await s.execute(text(
                "INSERT INTO eval_run (tenant, source, mode, status, case_ids, requested_by, suite_hash, thresholds, "
                "summary, threshold_results, breaches, doc_schema, doc_sha256, file_name, target, started_at, "
                "finished_at) VALUES (:t, 'import', :m, 'succeeded', CAST(:c AS text[]), CAST(:by AS jsonb), :h, "
                "CAST(:th AS jsonb), CAST(:sum AS jsonb), CAST(:tr AS jsonb), :br, :schema, :sha, :file, :target, "
                ":started, :finished) RETURNING id"),
                {"t": tenant, "m": header.mode, "c": [c.case_id for c in cases], "by": dumps(_actor(actor)),
                 "h": header.suite_hash, "th": dumps([t.model_dump(mode="json") for t in thresholds]),
                 "sum": dumps(summary), "tr": dumps([o.model_dump(mode="json") for o in outcomes]),
                 "br": breach_count(outcomes), "schema": header.doc_schema, "sha": sha, "file": name,
                 "target": header.target, "started": header.started_at,
                 "finished": header.finished_at or header.started_at})).first()
            assert row is not None
            run_id = str(row[0])
            for i, case in enumerate(cases):
                await _store_case(s, tenant=tenant, run_id=run_id, ordinal=i, result=case)
    except IntegrityError as exc:
        if "eval_run_doc_uq" not in str(exc.orig):
            raise
        async with system_session() as s:
            found = (await s.execute(text("SELECT id FROM eval_run WHERE doc_sha256 = :h"), {"h": sha})).first()
        if found is None:
            raise
        return str(found[0]), True
    await admin_audit(_actor(actor), "evals.run_imported", f"eval_run:{run_id}", None,
                      {"file_name": name, "cases": len(cases), "schema": header.doc_schema, "mode": header.mode},
                      platform=True)
    return run_id, False


def _read_results_file(path: Path, limit: int) -> Any:
    if path.stat().st_size > limit:
        raise EvalDocError("file too large")
    return json.loads(path.read_text(encoding="utf-8"))


async def import_folder(*, tenant: str, actor: Mapping[str, Any], only: str | None = None) -> dict[str, Any]:
    """Import every ``<UTC stamp>.json`` in the mounted results folder not imported yet (or only
    the file named ``only``; never a path: the name must be a results file name)."""
    from keelson_engine.core.audit import evals_dir

    directory = evals_dir()
    if not directory.is_dir():
        raise AdminError(409, "results_folder_unavailable", "the results folder is not mounted")
    if only is not None and not _EVAL_FILE_RE.fullmatch(only):
        raise AdminError(422, "invalid_file_name", "not a results file name (<UTC stamp>.json)")
    limit = get_settings().evals_max_import_bytes
    files = sorted(p for p in directory.iterdir() if p.is_file() and _EVAL_FILE_RE.fullmatch(p.name)
                   and (only is None or p.name == only))[-500:]
    if only is not None and not files:
        raise AdminError(404, "not_found", "no such results file")
    imported: list[dict[str, str]] = []
    duplicates = 0
    errors: list[dict[str, str]] = []
    for path in files:
        try:
            doc = await asyncio.to_thread(_read_results_file, path, limit)
            run_id, duplicate = await import_doc(doc, tenant=tenant, actor=actor, file_name=path.name)
        except (OSError, ValueError, RecursionError) as exc:  # RecursionError: pathological JSON nesting
            errors.append({"file": path.name, "error": f"unreadable: {type(exc).__name__}"})
            continue
        except AdminError as exc:
            errors.append({"file": path.name, "error": f"{exc.code}: {exc.message}"[:300]})
            continue
        if duplicate:
            duplicates += 1
        else:
            imported.append({"file": path.name, "run_id": run_id})
    return {"imported": imported, "duplicates": duplicates, "errors": errors}


async def list_files() -> dict[str, Any]:
    """The M7 results-folder listing plus the run each file was imported as (by file name)."""
    from keelson_engine.core.audit import list_evals

    listing = await asyncio.to_thread(list_evals, get_settings().evals_list_limit)
    names = [str(item.get("file")) for item in listing.get("items") or []]
    async with system_session() as s:
        rows = (await s.execute(text(
            "SELECT file_name, id FROM eval_run WHERE source = 'import' AND file_name = ANY(CAST(:n AS text[]))"),
            {"n": names})).all()
    imported = {str(f): str(i) for f, i in rows}
    for item in listing.get("items") or []:
        item["imported_run_id"] = imported.get(str(item.get("file")))
    return listing


async def export_doc(run_id: str) -> tuple[str, dict[str, Any]]:
    """``(file name, keelson-evals/2 document)`` of a finished run; it re-imports as that run."""
    run = await get_run(run_id)
    if run is None:
        raise _not_found()
    if run.status not in TERMINAL or run.started_at is None:
        raise AdminError(409, "run_not_finished", "only a finished run can be exported")
    async with system_session() as s:
        cases = await _case_results(s, run_id)
    started = run.started_at.astimezone(dt.UTC).replace(microsecond=0)
    finished = (run.finished_at or run.started_at).astimezone(dt.UTC).replace(microsecond=0)
    doc = {
        "schema": EVALS_SCHEMA_V2, "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "keelson_url": run.target, "merxa_url": None, "mode": run.mode,
        "runner": "in_app" if run.source == "in_app" else "cli", "suite_hash": run.suite_hash,
        "cases": [case_result_to_doc(c) for c in cases],
        "summary": run.summary.model_dump(mode="json") if run.summary is not None else None,
    }
    return f"{started.strftime('%Y%m%dT%H%M%SZ')}.json", doc


# ───────────────────────────── compare & trends ─────────────────────────────


async def compare(base_id: str, head_id: str) -> EvalCompare:
    base, head = await get_run(base_id), await get_run(head_id)
    if base is None or head is None:
        raise _not_found()
    async with system_session() as s:
        base_cases = await _case_results(s, base_id)
        head_cases = await _case_results(s, head_id)
    return S.compare_runs(base, base_cases, head, head_cases)


async def trends(*, limit: int, mode: str | None = None) -> EvalTrends:
    """Finished runs (newest ``limit``, oldest first) with their metrics and a status strip per case.
    ``mode``: ``replay``, ``live`` (every other mode, incl. imported CLI runs of unknown mode) or
    ``None`` (all)."""
    cond = "r.status IN ('succeeded', 'failed', 'cancelled') AND r.summary IS NOT NULL"
    if mode == "replay":
        cond += " AND r.mode = 'replay'"
    elif mode == "live":
        cond += " AND r.mode <> 'replay'"
    async with system_session() as s:
        rows = (await s.execute(text(f"{_RUN_SELECT} WHERE {cond} ORDER BY coalesce(r.started_at, r.created_at) DESC, "
                                     "r.id DESC LIMIT :n"), {"n": limit})).mappings().all()
        runs = [_run_view(r) for r in reversed(rows)]
        ids = [r.id for r in runs]
        case_rows = (await s.execute(text(
            "SELECT run_id, case_id, title, status FROM eval_case_result WHERE run_id::text = ANY(CAST(:ids AS text[]))"),
            {"ids": ids})).all() if ids else []
    by_run: dict[str, list[tuple[str, str, str]]] = {}
    for run_id, case_id, title, status in case_rows:
        by_run.setdefault(str(run_id), []).append((str(case_id), str(title), str(status)))
    return S.trends(runs, by_run, DEFAULT_EVAL_THRESHOLDS)
