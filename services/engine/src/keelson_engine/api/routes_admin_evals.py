"""M8 Evals UI API (``docs/contracts/128-m8-evals-ui.md`` §4), under ``/api/admin/evals``.

Platform admins only: eval history is platform-wide (a tenant admin gets 403
``platform_admin_required``, like the M7 listing ``GET /api/admin/evals``). Mutations need the
session's CSRF token and pass the Origin check like every admin mutation.

==============================================  ===================================================
``GET  /status``                                 runner, suite, mode availability, live caps, active run,
                                                 recordings coverage of the eval tenant
``GET  /runs?limit=&before=&mode=&source=``      runs, newest first (``mode`` = a mode or ``live``)
``POST /runs`` (``EvalRunCreate``)               queue an in-app run → ``202`` ``EvalRun``
``GET  /runs/{id}``                              ``EvalRunDetail`` (the run + one row per case)
``GET  /runs/{id}/events?after=``                progress events after ``seq`` (polling) + status
``GET  /runs/{id}/cases/{case_id}``              ``EvalCaseResult`` (drill-down)
``POST /runs/{id}/cancel``                       cancel → ``202`` ``EvalRun``
``GET  /runs/{id}/export``                       ``keelson-evals/2`` JSON attachment (re-imports as the run)
``POST /import?file_name=`` (results JSON)       ``201 {run_id, duplicate: false}`` | ``200 {…, duplicate: true}``
``POST /import/folder?file=``                    import every new file of the mounted results folder (or one)
``GET  /files``                                  the M7 file listing + ``imported_run_id``
``GET  /compare?base=&head=``                    ``EvalCompare``
``GET  /trends?limit=&mode=``                    ``EvalTrends`` (``mode`` = ``replay`` | ``live``)
==============================================  ===================================================

``POST /runs`` errors: ``409 evals_unavailable {reason}`` (``runner_not_configured``,
``runner_offline``, ``demo_mode_off``, ``live_disabled``, ``replay_only``, ``gateway_budget_low``),
``409 eval_running``,
``422 unknown_cases {cases}`` (not in the runner's suite), ``422 too_many_cases`` (more than a run
holds), ``422 confirm_required`` /
``budget_required`` / ``budget_too_high``. Imports: ``413 file_too_large``, ``422 invalid_results``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from keelson_contracts.evals import MAX_DOC_CASES, EvalBudget, EvalRunCreate, eval_run_create_error

from keelson_engine.api.admin_deps import AdminCtx, AdminRead, AdminWrite, require_platform
from keelson_engine.api.errors import ApiError
from keelson_engine.config import get_settings
from keelson_engine.core import evals
from keelson_engine.core.repo import admin_audit

__all__ = ["eval_tenant", "router"]

router = APIRouter(prefix="/api/admin/evals")

RUN_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
CASE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


def eval_tenant() -> str:
    """The tenant in-app runs execute in: the demo tenant (the runner signs in as its personas)."""
    tenants = [t.strip() for t in get_settings().demo_tenants.split(",") if t.strip()]
    return tenants[0] if tenants else "demo-retail"


def _platform(ctx: AdminCtx) -> None:
    require_platform(ctx)


def _process_mode() -> str:
    from keelson_engine.runtime.errors import RuntimeConfigError
    from keelson_engine.runtime.replay import replay_mode

    try:
        return replay_mode()
    except RuntimeConfigError:
        return "invalid"


def _live_caps() -> EvalBudget:
    s = get_settings()
    return EvalBudget(max_tokens=s.evals_live_max_tokens, max_cost_microusd=s.evals_live_max_cost_microusd)


async def _availability() -> tuple[Any, Any, list[Any]]:
    settings = get_settings()
    runner, suite = await evals.runner_info()
    process = _process_mode()
    modes = evals.mode_availability(runner, demo_mode=settings.demo_mode, live_enabled=settings.evals_live_enabled,
                                    process_mode=process)
    # the gateway is only asked when a live mode could otherwise start (never on replay-only installs)
    if any(m.available and m.mode != "replay" for m in modes) and await evals.gateway_budget_spent(eval_tenant()):
        modes = evals.mode_availability(runner, demo_mode=settings.demo_mode,
                                        live_enabled=settings.evals_live_enabled, process_mode=process,
                                        gateway_budget_spent=True)
    return runner, suite, modes


# ───────────────────────────── status & runs ─────────────────────────────


@router.get("/status")
async def status(ctx: AdminRead) -> dict[str, Any]:
    _platform(ctx)
    await evals.reap_expired()
    runner, suite, modes = await _availability()
    tenant = eval_tenant()
    return {
        "tenant": tenant,
        "runner": runner.model_dump(mode="json"),
        "suite": suite.model_dump(mode="json") if suite is not None else None,
        "modes": [m.model_dump(mode="json") for m in modes],
        "process_replay_mode": _process_mode(),
        "live_caps": _live_caps().model_dump(mode="json"),
        "active_run": (a.model_dump(mode="json") if (a := await evals.active_run()) is not None else None),
        "recordings": (await evals.recordings(tenant)).model_dump(mode="json"),
    }


@router.get("/runs")
async def runs_list(
    ctx: AdminRead,
    limit: int = Query(20, ge=1, le=100),
    before: str | None = Query(None, pattern=RUN_ID),
    mode: Literal["replay", "record_or_replay", "record", "unknown", "live"] | None = None,
    source: Literal["in_app", "import"] | None = None,
) -> dict[str, Any]:
    _platform(ctx)
    await evals.reap_expired()
    return await evals.list_runs(limit=limit, before=before, mode=mode, source=source)


@router.post("/runs", status_code=202)
async def runs_create(body: EvalRunCreate, ctx: AdminWrite) -> dict[str, Any]:
    _platform(ctx)
    runner, suite, modes = await _availability()
    mode = next(m for m in modes if m.mode == body.mode)
    if not mode.available:
        raise ApiError(409, "evals_unavailable", mode.message or "this eval mode is unavailable", reason=mode.reason)
    if suite is None:
        raise ApiError(409, "evals_unavailable", "the runner has not reported its cases yet", reason="runner_offline")
    known = [c.id for c in suite.cases]
    case_ids = list(body.case_ids) if body.case_ids is not None else known
    unknown = [c for c in case_ids if c not in known]
    if unknown:
        raise ApiError(422, "unknown_cases", "these cases are not in the runner's suite", cases=unknown[:50])
    if len(case_ids) > MAX_DOC_CASES:  # "all" of a suite larger than a run may hold
        raise ApiError(422, "too_many_cases", f"a run holds at most {MAX_DOC_CASES} cases: choose them")
    problem = eval_run_create_error(body, _live_caps())
    if problem is not None:
        raise ApiError(422, problem[0], problem[1])
    run = await evals.create_run(tenant=eval_tenant(), mode=body.mode, case_ids=case_ids, actor=ctx.actor(),
                                 note=body.note, budget=body.budget, suite_hash=suite.suite_hash)
    await admin_audit(ctx.actor(), "evals.run_requested", f"eval_run:{run.id}", None,
                      {"mode": run.mode, "cases": case_ids, "budget": run.budget.model_dump() if run.budget else None},
                      platform=True)
    return run.model_dump(mode="json")


@router.get("/runs/{run_id}")
async def runs_get(run_id: str, ctx: AdminRead) -> dict[str, Any]:
    _platform(ctx)
    await evals.reap_expired()
    return (await evals.run_detail(run_id)).model_dump(mode="json")


@router.get("/runs/{run_id}/events")
async def runs_events(run_id: str, ctx: AdminRead, after: int = Query(0, ge=0, le=10_000_000)) -> dict[str, Any]:
    _platform(ctx)
    await evals.reap_expired()
    return await evals.run_events(run_id, after=after)


@router.get("/runs/{run_id}/cases/{case_id}")
async def runs_case(run_id: str, case_id: str, ctx: AdminRead) -> dict[str, Any]:
    _platform(ctx)
    return (await evals.case_result(run_id, case_id)).model_dump(mode="json")


@router.post("/runs/{run_id}/cancel", status_code=202)
async def runs_cancel(run_id: str, ctx: AdminWrite) -> dict[str, Any]:
    _platform(ctx)
    return (await evals.cancel_run(run_id, ctx.actor())).model_dump(mode="json")


@router.get("/runs/{run_id}/export")
async def runs_export(run_id: str, ctx: AdminRead) -> Response:
    _platform(ctx)
    name, doc = await evals.export_doc(run_id)
    body = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="keelson-evals-{name}"'})


# ───────────────────────────── imports ─────────────────────────────


async def _read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise ApiError(413, "file_too_large", f"the results document exceeds {limit} bytes")
        except ValueError:
            raise ApiError(400, "invalid_request", "invalid Content-Length") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "file_too_large", f"the results document exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/import")
async def import_document(request: Request, ctx: AdminWrite,
                          file_name: str | None = Query(None, max_length=120)) -> JSONResponse:
    """A results document as the JSON request body (``keelson-evals/1`` or ``/2``)."""
    _platform(ctx)
    raw = await _read_capped(request, get_settings().evals_max_import_bytes)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: pathological nesting
        raise ApiError(422, "invalid_results", "the body is not a JSON document") from None
    run_id, duplicate = await evals.import_doc(doc, tenant=ctx.tenant, actor=ctx.actor(), file_name=file_name)
    return JSONResponse({"run_id": run_id, "duplicate": duplicate}, status_code=200 if duplicate else 201)


@router.post("/import/folder")
async def import_results_folder(ctx: AdminWrite, file: str | None = Query(None, max_length=40)) -> dict[str, Any]:
    """Every new file of the mounted results folder, or only ``?file=<UTC stamp>.json``."""
    _platform(ctx)
    return await evals.import_folder(tenant=ctx.tenant, actor=ctx.actor(), only=file)


@router.get("/files")
async def files(ctx: AdminRead) -> dict[str, Any]:
    _platform(ctx)
    return await evals.list_files()


# ───────────────────────────── compare & trends ─────────────────────────────


@router.get("/compare")
async def compare(ctx: AdminRead, base: str = Query(..., pattern=RUN_ID), head: str = Query(..., pattern=RUN_ID)
                  ) -> dict[str, Any]:
    _platform(ctx)
    return (await evals.compare(base, head)).model_dump(mode="json")


@router.get("/trends")
async def trends(ctx: AdminRead, limit: int = Query(30, ge=2, le=100),
                 mode: Literal["replay", "live"] | None = None) -> dict[str, Any]:
    _platform(ctx)
    return (await evals.trends(limit=limit, mode=mode)).model_dump(mode="json")
