"""Agent playground API (``docs/contracts/110-m7-backoffice-generic.md`` §3), under ``/api/admin``.

==============================================  =================================================
``GET  /agents/playground``                      ``{available, mode, reason}``: whether a turn can run
``POST /agents/playground {specialist, question}``  one investigator + structurer turn (SSE)
==============================================  =================================================

Everything is checked **before** the stream starts (real HTTP errors): the request shape (422),
availability (409 ``playground_unavailable``: the engine replays recorded model answers only, or
the playground is switched off), the specialist (404 unknown slug, 422 a draft that reuses a
built-in slug or fails the tenant-state checks of
:func:`keelson_engine.core.agent_specs.custom_specialist_problems`) and the per-tenant / process
concurrency caps (429 ``playground_busy``). The turn itself is
:func:`keelson_engine.runtime.playground.run_playground` (scratch case, never commits, budget
capped, read tools only); its AG-UI activity streams as ``message`` events and the outcome is the
final ``CUSTOM {name: "sc.playground.result", value: PlaygroundResult}``. Closing the stream aborts
the turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import ValidationError

from keelson_engine.api.admin_deps import AdminRead, AdminWrite
from keelson_engine.api.errors import ApiError
from keelson_engine.api.shutdown import until_shutdown
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit

__all__ = ["PLAYGROUND_RESULT_EVENT", "playground_availability", "router"]

log = get_logger(__name__)
router = APIRouter(prefix="/api/admin")

PLAYGROUND_RESULT_EVENT = "sc.playground.result"
MAX_CONCURRENT = 2
"""Playground turns per engine-api process (each holds model calls for up to a few minutes)."""
_running: dict[str, int] = {}


def playground_availability() -> dict[str, Any]:
    """``PLAYGROUND_ENABLED=0`` switches it off; ``LLM_REPLAY_MODE=replay`` (fail-closed demos /
    evals) cannot answer a free-form question, so the playground is unavailable there too."""
    from keelson_engine.runtime.errors import RuntimeConfigError
    from keelson_engine.runtime.replay import replay_mode

    try:
        mode: str = replay_mode()
    except RuntimeConfigError:
        mode = "invalid"
    if os.environ.get("PLAYGROUND_ENABLED", "1").strip().lower() in ("0", "false", "off", "no"):
        return {"available": False, "mode": mode, "reason": "disabled",
                "message": "The playground is switched off in this installation."}
    if mode in ("replay", "invalid"):
        return {"available": False, "mode": mode, "reason": "replay_only",
                "message": "This installation only replays recorded model answers, so the playground cannot "
                           "run a new question."}
    return {"available": True, "mode": mode, "reason": None, "message": None}


@router.get("/agents/playground")
async def playground_status(ctx: AdminRead) -> dict[str, Any]:
    del ctx
    return playground_availability()


@dataclass
class _Target:
    tenant: str
    request: Any
    spec: Any
    actor: dict[str, Any]
    slug: str
    draft: bool
    playground_id: str = field(default_factory=lambda: str(uuid.uuid4()))


async def _target(ctx: AdminWrite, body: Annotated[dict[str, Any], Body()]) -> _Target:
    from keelson_contracts.agents import CustomSpecialistSpec, PlaygroundRequest

    from keelson_engine.core import agent_specs
    from keelson_engine.runtime.runner_cache import default_spec_loader

    try:
        request = PlaygroundRequest.model_validate(body)
    except ValidationError as exc:
        raise ApiError(422, "invalid_request", "the playground request is invalid",
                       errors=[{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg"))[:300]}
                               for e in exc.errors()[:20]]) from None
    status = playground_availability()
    if not status["available"]:
        raise ApiError(409, "playground_unavailable", status["message"], reason=status["reason"])
    spec = await default_spec_loader(ctx.tenant, await agent_specs.latest_version_name(ctx.tenant))
    if isinstance(request.specialist, CustomSpecialistSpec):
        draft: Any = request.specialist
        if draft.slug in {s.slug for s in spec.specialists}:
            raise ApiError(422, "custom_specialist_invalid", f"{draft.slug!r} is a built-in specialist slug",
                           field="specialist.slug")
        is_draft = True
    else:
        saved = next((c for c in spec.custom_specialists if c.slug == request.specialist), None)
        if saved is None:
            raise ApiError(404, "not_found", f"no custom specialist {request.specialist!r} in this tenant's agent spec")
        draft = CustomSpecialistSpec.model_validate(saved.model_dump(mode="json") if hasattr(saved, "model_dump")
                                                    else saved)
        is_draft = False
    problems = await agent_specs.custom_specialist_problems(ctx.tenant, draft)
    if problems:
        raise ApiError(422, "custom_specialist_invalid", problems[0], problems=problems[:20])
    if _running.get(ctx.tenant) or sum(_running.values()) >= MAX_CONCURRENT:
        raise ApiError(429, "playground_busy", "a playground turn is already running; try again when it finished")
    return _Target(tenant=ctx.tenant, request=request, spec=spec, actor=ctx.actor(), slug=draft.slug, draft=is_draft)


def _frame(event: dict[str, Any]) -> ServerSentEvent:
    return ServerSentEvent(raw_data=json.dumps(event, separators=(",", ":"), ensure_ascii=False, default=str),
                           event="message")


@router.post("/agents/playground", response_class=EventSourceResponse)
async def playground_run(target: Annotated[_Target, Depends(_target)]) -> AsyncIterator[ServerSentEvent]:
    from keelson_engine.runtime.context import AbortController
    from keelson_engine.runtime.playground import run_playground
    from keelson_engine.runtime.runner_cache import default_bindings

    _running[target.tenant] = _running.get(target.tenant, 0) + 1
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
    controller = AbortController()

    async def emit(event: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):  # a slow reader loses activity, never the result
            queue.put_nowait(event)

    async def turn() -> Any:
        bindings = await default_bindings(target.tenant, target.spec)
        return await run_playground(target.request, tenant=target.tenant, spec=target.spec, models=bindings.models,
                                    actor=target.actor, emit=emit, playground_id=target.playground_id,
                                    controller=controller)

    async def frames() -> AsyncIterator[dict[str, Any]]:
        task = asyncio.create_task(turn(), name=f"playground-{target.playground_id}")
        try:
            while True:
                if task.done() and queue.empty():
                    break
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                else:
                    getter.cancel()
            try:
                result = task.result()
            except Exception as exc:  # noqa: BLE001 - configuration errors before the turn started
                log.warning("playground turn could not start", error=type(exc).__name__)
                yield {"type": "RUN_ERROR", "message": "The test turn could not start.", "code": "PLAYGROUND_FAILED"}
                return
            value = result.model_dump(mode="json")
            yield {"type": "CUSTOM", "name": PLAYGROUND_RESULT_EVENT, "value": value}
            await admin_audit(target.actor, "agents.playground", f"specialist:{target.slug}", None,
                              {"status": value["status"], "draft": target.draft,
                               "llm_calls": value["usage"]["llm_calls"],
                               "tool_calls": value["usage"]["tool_calls"]}, tenant=target.tenant)
        finally:
            if not task.done():
                controller.abort("cancelled", "the admin closed the playground stream")
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    try:
        yield ServerSentEvent(retry=3000, comment="keelson playground")
        async for event in until_shutdown(frames()):
            yield _frame(event)
    finally:
        _running[target.tenant] = max(0, _running.get(target.tenant, 1) - 1)
        if not _running[target.tenant]:
            _running.pop(target.tenant, None)
