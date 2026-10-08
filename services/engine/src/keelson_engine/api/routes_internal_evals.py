"""M8 in-app eval runner protocol (``docs/contracts/128-m8-evals-ui.md`` §4), under
``/api/internal/evals/runner``. Called by the ``evals-runner`` sidecar only.

Authentication: ``Authorization: Bearer <EVALS_RUNNER_TOKEN>`` (constant-time compare). Unset token
= the protocol does not exist (``404``); a wrong one is ``401``. Caddy routes ``/api/*`` publicly,
so these routes authenticate themselves; the runner also sends ``Origin: <PUBLIC_URL>`` for the
middleware's check on non-GET calls. The token lets a runner claim and report runs, never queue
one or change configuration.

=================================================  ==========================================
``POST /heartbeat {runner_id, version, suite}``    case catalogue → ``{poll_after_s}``
``POST /claim {runner_id}``                        ``200 {job}`` (lease) | ``204``
``POST /runs/{id}/events {runner_id, events}``     progress (≤ 50 per call; empty = renew the lease)
                                                   → ``{cancel_requested, budget_exhausted, lease_expires_at}``
``POST /runs/{id}/cases/{case}/attach``            link the Keelson case ``{runner_id, keelson_case_id}``
``PUT  /runs/{id}/cases/{case}/result``            the case's ``keelson-evals/2`` result ``{runner_id, result}``
``POST /runs/{id}/finish {runner_id, status, error?}``  the engine computes the summary + thresholds
=================================================  ==========================================

Every call on a run requires that runner's running lease (``409 run_not_running`` otherwise) and
renews it.
"""

from __future__ import annotations

import hmac
import json
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from keelson_contracts.evals import EvalRunEventIn, EvalSuiteInfo
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from keelson_engine.api.errors import ApiError
from keelson_engine.config import get_settings
from keelson_engine.core import evals

__all__ = ["router"]

RESULT_MAX_BYTES = 4 * 1024 * 1024
"""One case result report (the stored result is capped at 1 MiB after sanitising)."""
POLL_AFTER_S = 5

RunnerId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")]
CaseId = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


def _authorised(request: Request) -> None:
    token = get_settings().evals_runner_token.get_secret_value()
    if not token:
        raise ApiError(404, "not_found", "not found")
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip().encode(), token.encode()):
        raise ApiError(401, "unauthorized", "invalid runner token")


router = APIRouter(prefix="/api/internal/evals/runner", dependencies=[Depends(_authorised)])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeartbeatBody(_Body):
    runner_id: RunnerId
    version: Annotated[str, Field(min_length=1, max_length=40)]
    suite: EvalSuiteInfo


class ClaimBody(_Body):
    runner_id: RunnerId


class EventsBody(_Body):
    runner_id: RunnerId
    events: Annotated[list[EvalRunEventIn], Field(max_length=evals.MAX_EVENTS_PER_POST)]


class AttachBody(_Body):
    runner_id: RunnerId
    keelson_case_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class FinishBody(_Body):
    runner_id: RunnerId
    status: Literal["succeeded", "failed", "cancelled"]
    error: Annotated[str, Field(max_length=2000)] | None = None


def _case_id(value: str) -> str:
    if not re.fullmatch(CaseId, value):
        raise ApiError(422, "unknown_case", "invalid case id")
    return value


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatBody) -> dict[str, Any]:
    await evals.runner_heartbeat(body.runner_id, body.version, body.suite)
    return {"poll_after_s": POLL_AFTER_S}


@router.post("/claim", response_model=None)
async def claim(body: ClaimBody) -> Response | dict[str, Any]:
    job = await evals.claim(body.runner_id)
    if job is None:
        return Response(status_code=204)
    return {"job": job.model_dump(mode="json")}


@router.post("/runs/{run_id}/events")
async def events(run_id: str, body: EventsBody) -> dict[str, Any]:
    return await evals.runner_events(run_id, body.runner_id, body.events)


@router.post("/runs/{run_id}/cases/{eval_case_id}/attach")
async def attach(run_id: str, eval_case_id: str, body: AttachBody) -> dict[str, Any]:
    return await evals.attach_case(run_id, body.runner_id, _case_id(eval_case_id), body.keelson_case_id)


@router.put("/runs/{run_id}/cases/{eval_case_id}/result")
async def result(run_id: str, eval_case_id: str, request: Request) -> dict[str, Any]:
    """Body ``{runner_id, result}`` read with a size cap (a report may carry long check details
    that the sanitiser shortens)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > RESULT_MAX_BYTES:
        raise ApiError(413, "result_too_large", "the case result report is too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > RESULT_MAX_BYTES:
            raise ApiError(413, "result_too_large", "the case result report is too large")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(422, "invalid_result", "the body is not JSON") from None
    if not isinstance(payload, dict) or set(payload) != {"runner_id", "result"}:
        raise ApiError(422, "invalid_result", "the body is {runner_id, result}")
    try:
        runner_id = ClaimBody.model_validate({"runner_id": payload["runner_id"]}).runner_id
    except ValidationError:
        raise ApiError(422, "invalid_result", "invalid runner_id") from None
    stored = await evals.put_case_result(run_id, runner_id, _case_id(eval_case_id), payload["result"])
    return {"stored": True, "case_id": stored.case_id, "status": stored.status, "error_code": stored.error_code}


@router.post("/runs/{run_id}/finish")
async def finish(run_id: str, body: FinishBody) -> dict[str, Any]:
    run = await evals.finish_run(run_id, body.runner_id, body.status, body.error)
    return {"status": run.status, "breaches": run.breaches}
