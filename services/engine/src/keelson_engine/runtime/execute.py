"""``run_case(run)``: drive one queued run through the cached Runner.

Run kinds:

* ``chat`` / ``action`` — a new invocation in a new ADK session (``session_id = run_id``) with a
  server-built JSON envelope as the user message. The invocation id is deterministic
  (``e-<run_id>``) so a crash-retry can find it even if the worker died before reporting it.
* ``resume`` — the paused session (``run.session_id``) and invocation (``run.invocation_id``),
  with a server-built ``FunctionResponse(id=interrupt_id, name="adk_request_input",
  response={decision, interrupt_id})``.
* ``crash_retry`` — ``run_async(invocation_id=…)`` with no new message (the reaper re-queued an
  expired lease); if the original message never reached the session it is re-sent.

The translator streams AG-UI events; ``finish_run`` records ``succeeded`` / ``paused`` (open
interrupts) / ``failed`` / ``cancelled``. Shutdown or a lost lease leaves the run alone for the
reaper (no finish).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import structlog
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events.event import Event
from google.adk.sessions.base_session_service import BaseSessionService
from google.genai import types
from keelson_contracts.agui import Stage
from keelson_contracts.runs import (
    ACTION_TYPES,
    ActionPayload,
    Actor,
    ChatPayload,
    ResumePayload,
    RunRequest,
    SignalPayload,
    adk_app_name,
)
from pydantic import ValidationError

from ..public_errors import GENERIC_RUN_FAILURE, INTERNAL_ERROR, report_exception, user_message
from .context import AbortController, RunContext, bind_run
from .deps import RuntimeDeps, field_of, get_deps
from .errors import ConnectRequiredError, RuntimeConfigError
from .replay import ReplayMiss, effective_mode, replay_mode, replay_stats
from .runner_cache import RunnerLease, get_runner
from .spec import KB_CONNECTOR, SIGNAL_HUB_CONNECTOR, AgentSpec
from .translator import Translator, seen_output_keys, unresolved_request_inputs
from .usage import install_cost_tracking

__all__ = ["DEFAULT_CONNECTOR_LABELS", "REQUEST_INPUT", "build_new_message", "fail_run", "initial_stage", "invocation_id_for", "run_case"]

log = structlog.get_logger(__name__)

REQUEST_INPUT = "adk_request_input"

DEFAULT_CONNECTOR_LABELS = {SIGNAL_HUB_CONNECTOR: "Signal Hub"}
"""Activity-label vendor of a first-party connector whose row cannot be read ("Reading hazard
alerts from Signal Hub")."""


def invocation_id_for(run_id: str) -> str:
    return f"e-{run_id}"


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        data = json.loads(value)
        return dict(data) if isinstance(data, Mapping) else {}
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    return dict(value)


def _original_kind(kind: str, payload: Mapping[str, Any], origin_kind: str | None = None) -> str:
    """``crash_retry`` keeps the original payload; E1 records ``origin_kind`` — else the shape tells."""
    if kind != "crash_retry":
        return kind
    if origin_kind and origin_kind != "crash_retry":
        return origin_kind
    if "interrupt_id" in payload and "decision" in payload:
        return "resume"
    if "text" in payload:
        return "chat"
    if str(payload.get("type") or "") in ACTION_TYPES:
        return "action"
    if "signal_id" in payload or "event_id" in payload:
        return "signal"
    return "chat"


def build_new_message(kind: str, payload: Mapping[str, Any]) -> types.Content:
    """The server-built user message for a new invocation or a resume."""
    if kind == "resume":
        resume = ResumePayload.model_validate(payload)
        return types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=resume.interrupt_id,
                        name=REQUEST_INPUT,
                        response={"decision": resume.decision, "interrupt_id": resume.interrupt_id},
                    )
                )
            ],
        )
    models = {"chat": ChatPayload, "action": ActionPayload, "signal": SignalPayload}
    model = models.get(kind)
    if model is None:
        raise ValueError(f"unsupported run kind {kind!r}")
    # signal runs: {signal_id} (M5) → prepare_replan (deterministic re-plan, no LLM);
    # {event_id, alert_id} (M6) → frame_case (deterministic frame from the Signal Hub event).
    body = model.model_validate(payload).model_dump(mode="json")
    envelope = {"kind": kind, "payload": body}
    return types.Content(role="user", parts=[types.Part(text=json.dumps(envelope, ensure_ascii=False))])


def _session_has_message(events: list[Event], invocation_id: str, *, interrupt_id: str | None) -> bool:
    for event in events:
        if event.invocation_id != invocation_id or event.author != "user" or event.content is None:
            continue
        for part in event.content.parts or []:
            if interrupt_id is None and part.text:
                return True
            if interrupt_id is not None and part.function_response and part.function_response.id == interrupt_id:
                return True
    return False


async def _case_context(deps: RuntimeDeps, tenant: str, case_id: str, limit: int) -> tuple[str, str, list[dict[str, str]]]:
    case = await deps.repo.get_case(tenant=tenant, case_id=case_id) or {}
    messages = await deps.repo.recent_messages(tenant=tenant, case_id=case_id, limit=limit)
    return str(case.get("title") or ""), str(case.get("mode") or "actual"), messages


async def fail_run(
    deps: RuntimeDeps,
    tenant: str,
    case_id: str,
    run_id: str,
    *,
    code: str,
    detail: str,
    exc: BaseException | None = None,
    message: str = GENERIC_RUN_FAILURE,
    worker_id: str | None = None,
    emit_first: bool = True,
) -> str:
    """Fail a run outside the translator (before it started, or after ``run_case`` crashed).

    The case stream gets a generic ``RUN_ERROR`` (+ ``error_id``); ``detail`` / ``exc`` go only
    to the server log and ``run.error`` under that id. With ``emit_first=False`` the event is
    only sent when ``finish_run`` actually changed the run (it never overwrites a final status).
    Returns the error id."""
    from keelson_contracts.agui import ScMetadata, Stage, run_error  # noqa: PLC0415

    error_id = report_exception(log, "run failed", exc, run_id=run_id, case_id=case_id, code=code, detail=detail)

    async def emit() -> None:
        try:
            sc = ScMetadata(tenant_id=tenant, case_id=case_id, run_id=run_id, stage=Stage.UNDERSTANDING,
                            prov="fact", label="Run failed")
            event = run_error(sc, message=user_message(error_id, message), code=code)
            event["metadata"]["errorId"] = error_id
            await deps.bus.append(tenant=tenant, case_id=case_id, run_id=run_id, event=event)
        except Exception as emit_exc:  # the run outcome is what matters; the event is informative
            log.warning("could not emit RUN_ERROR", error_id=error_id, error=type(emit_exc).__name__)

    if emit_first:
        await emit()
    cause = f"{detail}: {type(exc).__name__}: {exc}" if exc is not None else detail
    # run.error is server-side only (never returned by the API): keep the cause for operators.
    changed = await deps.runs.finish_run(run_id, "failed", error=f"[{error_id}] {cause}"[:2000], worker_id=worker_id)
    if not emit_first and changed is not False:
        await emit()
    return error_id


async def _fail(
    deps: RuntimeDeps, tenant: str, case_id: str, run_id: str, detail: str, *, code: str,
    exc: BaseException | None = None, worker_id: str | None = None,
) -> None:
    await fail_run(deps, tenant, case_id, run_id, code=code, detail=detail, exc=exc, worker_id=worker_id)


def _public_failure(exc: BaseException) -> tuple[str, str]:
    """``(code, user-facing message)`` for an exception that ended a run (never its text).

    Looks through ADK's wrappers (``DynamicNodeFailError.error``, ``__cause__``)."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 8:
        chain.append(current)
        nested = getattr(current, "error", None)
        current = nested if isinstance(nested, BaseException) else (current.__cause__ or current.__context__)
    if any(isinstance(e, ConnectRequiredError) for e in chain):
        return "CONNECT_REQUIRED", (
            "The approver's account for the target system is not connected (or the connection expired), "
            "so the changes were not applied. Connect it and approve again."
        )
    miss = next((e for e in chain if isinstance(e, ReplayMiss)), None)
    if miss is not None:
        # M8: replay-only installations and replay eval cases fail closed; the agent name is no secret
        return "LLM_REPLAY_MISS", (
            f"No recorded model answer matched (agent {_agent_label(miss.agent)}); "
            "this run only replays recorded answers, so it stopped without calling the model."
        )
    return INTERNAL_ERROR, GENERIC_RUN_FAILURE


def _agent_label(agent: str) -> str:
    """The ADK agent name of a replay miss as plain text (``[A-Za-z0-9_.-]``, ≤ 80 characters)."""
    cleaned = "".join(ch for ch in agent if ch.isalnum() or ch in "_.-")[:80]
    return cleaned or "unknown"


# ───────────────────────────── M8 eval cases ─────────────────────────────


def _eval_stop(link: Any) -> tuple[str, str, str] | None:
    """``(final status, code, message)`` when a run of an eval-linked case must not start: its eval
    run was cancelled, or a live eval run's budget is used up. Neither path calls a model."""
    if field_of(link, "cancel_requested") or field_of(link, "status") == "cancelled":
        return "cancelled", "EVAL_CANCELLED", "The eval run was cancelled, so this run did not start."
    max_tokens = field_of(link, "budget_max_tokens")
    max_cost = field_of(link, "budget_max_cost_microusd")
    if str(field_of(link, "llm_mode")) != "replay" and max_tokens is not None and max_cost is not None and (
        int(field_of(link, "tokens_live") or 0) >= int(max_tokens)
        or int(field_of(link, "cost_microusd") or 0) >= int(max_cost)
    ):
        return "failed", "EVAL_BUDGET_EXHAUSTED", "The eval run used up its budget, so this run did not start."
    return None


async def _stop_eval_run(
    deps: RuntimeDeps, tenant: str, case_id: str, run_id: str, *, status: str, code: str, message: str,
    worker_id: str | None,
) -> None:
    from keelson_contracts.agui import ScMetadata, Stage, run_error  # noqa: PLC0415

    log.warning("eval case run not started", code=code)
    try:
        sc = ScMetadata(tenant_id=tenant, case_id=case_id, run_id=run_id, stage=Stage.UNDERSTANDING,
                        prov="decision", label=message)
        await deps.bus.append(tenant=tenant, case_id=case_id, run_id=run_id,
                              event=run_error(sc, message=message, code=code))
    except Exception as exc:  # the run outcome is what matters; the event is informative
        log.warning("could not emit RUN_ERROR", error=type(exc).__name__)
    await deps.runs.finish_run(run_id, status, error=message, worker_id=worker_id)


def _apply_eval_link(ctx: RunContext, link: Any) -> None:
    """Force the eval run's LLM mode on the run and, for live runs, what is left of its budget."""
    ctx.eval_run_id = str(field_of(link, "run_id"))
    ctx.llm_replay_override = str(field_of(link, "llm_mode"))
    max_tokens = field_of(link, "budget_max_tokens")
    max_cost = field_of(link, "budget_max_cost_microusd")
    if ctx.llm_replay_override != "replay" and max_tokens is not None and max_cost is not None:
        ctx.eval_token_cap = max(0, int(max_tokens) - int(field_of(link, "tokens_live") or 0))
        ctx.eval_cost_cap_microusd = max(0, int(max_cost) - int(field_of(link, "cost_microusd") or 0))


async def _account_eval_usage(ctx: RunContext) -> None:
    """Add the run's metered usage to its eval link and eval run (once per run; never fails the run)."""
    evals_port = getattr(ctx.deps, "evals", None)
    if ctx.eval_run_id is None or evals_port is None:
        return
    total = ctx.usage.total()
    stats = replay_stats(ctx.run_id)
    cost = 0 if total.cost_usd is None else int(round(total.cost_usd * 1_000_000))
    try:
        # a store miss only stops a run in replay mode (record_or_replay calls live on a miss)
        replay_only = effective_mode(replay_mode(), ctx.llm_replay_override) == "replay"
    except RuntimeConfigError:
        replay_only = False
    try:
        await evals_port.add_usage(
            tenant=ctx.tenant, case_id=ctx.case_id, run_id=ctx.eval_run_id, calls_live=total.calls - total.replayed,
            calls_replayed=total.replayed, tokens_live=total.total_tokens, tokens_replayed=stats.replayed_tokens,
            misses=stats.misses if replay_only else 0, cost_microusd=cost,
        )
    except Exception as exc:  # accounting is best effort; the run's own outcome stands
        log.warning("eval usage not recorded", eval_run_id=ctx.eval_run_id, error=type(exc).__name__)


async def specialist_vendors(deps: RuntimeDeps, tenant: str, spec: AgentSpec) -> dict[str, str]:
    """Specialist slug → vendor label of the connector it reads (activity labels). The planning
    connector is ``spec.vendor_name``; others use their connector row's vendor/name."""
    labels: dict[str, str] = {spec.connector: spec.vendor_name, KB_CONNECTOR: "the knowledge base"}
    out: dict[str, str] = {}
    for specialist in spec.all_specialists:
        slug = specialist.connector
        if slug not in labels:
            try:
                labels[slug] = (await deps.connectors.get_connector(tenant=tenant, slug=slug)).label
            except Exception as exc:  # labels are cosmetic: never fail a run over them
                log.warning("connector label unavailable", connector=slug, error=type(exc).__name__)
                labels[slug] = DEFAULT_CONNECTOR_LABELS.get(slug, slug)
        out[specialist.slug] = labels[slug]
    return out


def initial_stage(kind: str, payload: Mapping[str, Any]) -> Stage:
    """Stage shown as soon as a run starts (before its first node reports anything).

    The first node of a chat turn is the intake LLM, whose first ADK event only arrives with
    the model response; without this the UI would show no stage for that whole call."""
    if kind == "action":
        return Stage.PREPARING_CHANGES
    if kind == "resume" and str(payload.get("interrupt_id") or "").startswith("approval-"):
        return Stage.AWAITING_APPROVAL
    return Stage.UNDERSTANDING


async def run_case(
    run: Any,
    *,
    controller: AbortController | None = None,
    deps: RuntimeDeps | None = None,
    lease: RunnerLease | None = None,
) -> str:
    """Execute one run; returns the final status (``succeeded|paused|failed|cancelled|abandoned``)."""
    deps = deps or get_deps()
    run_id = str(field_of(run, "id"))
    tenant = str(field_of(run, "tenant"))
    case_id = str(field_of(run, "case_id"))
    kind = str(field_of(run, "kind"))
    payload = _as_dict(field_of(run, "payload"))
    original_kind = _original_kind(kind, payload, field_of(run, "origin_kind"))
    owner = field_of(run, "lease_owner")
    worker_id = str(owner) if owner else None
    structlog.contextvars.bind_contextvars(run_id=run_id, case_id=case_id, tenant=tenant)

    resume: ResumePayload | None = None
    try:
        actor = Actor.model_validate(_as_dict(field_of(run, "actor")))
        if original_kind == "resume":
            resume = ResumePayload.model_validate(payload)
        if kind != "crash_retry":
            RunRequest.model_validate(
                {"run_id": run_id, "tenant": tenant, "case_id": case_id, "kind": kind,
                 "actor": actor.model_dump(), "payload": payload}
            )
    except ValidationError as exc:
        await _fail(deps, tenant, case_id, run_id, f"invalid run request: {exc.errors()[:3]}", code="INVALID_RUN",
                    worker_id=worker_id)
        return "failed"

    if original_kind == "resume":
        session_id = str(field_of(run, "session_id") or "")
        invocation_id = str(field_of(run, "invocation_id") or "")
        if not session_id or not invocation_id:
            await _fail(deps, tenant, case_id, run_id, "resume run has no session_id/invocation_id", code="INVALID_RUN", worker_id=worker_id)
            return "failed"
    else:
        session_id = str(field_of(run, "session_id") or run_id)
        invocation_id = str(field_of(run, "invocation_id") or invocation_id_for(session_id))

    # M8 (128 §3): an eval-linked case runs in its eval run's LLM mode under its budget, and does not
    # start once the eval run is cancelled or its live budget is used up (no model call either way)
    link: Any = None
    evals_port = getattr(deps, "evals", None)
    if evals_port is not None:
        try:
            link = await evals_port.get_link(tenant=tenant, case_id=case_id)
        except Exception as exc:  # fail closed: a replay eval case must never run in the process's mode
            await _fail(deps, tenant, case_id, run_id, "eval link unavailable", code="RUNTIME_UNAVAILABLE", exc=exc,
                        worker_id=worker_id)
            return "failed"
    if link is not None:
        stop = _eval_stop(link)
        if stop is not None:
            final, code, message = stop
            await _stop_eval_run(deps, tenant, case_id, run_id, status=final, code=code, message=message,
                                 worker_id=worker_id)
            return final

    own_lease = lease is None
    if lease is None:
        try:
            lease = await get_runner(tenant, field_of(run, "agent_spec_version"))
        except Exception as exc:  # run boundary: no runner (LLM key, spec, DB) → visible failure
            await _fail(deps, tenant, case_id, run_id, "runner unavailable", code="RUNTIME_UNAVAILABLE", exc=exc,
                        worker_id=worker_id)
            return "failed"
    try:
        spec = lease.spec
        title, mode, history = await _case_context(deps, tenant, case_id, spec.history_messages)
        ctx = RunContext(
            tenant=tenant, case_id=case_id, run_id=run_id, kind=kind, actor=actor, payload=payload,
            spec=spec, deps=deps, invocation_id=invocation_id, resume=resume, trace_id=run_id,
            case_title=title, case_mode=mode, history=history, controller=controller or AbortController(),
            worker_id=worker_id,
        )
        if link is not None:
            _apply_eval_link(ctx, link)
        install_cost_tracking()
        status = "failed"
        try:
            status = await _drive(ctx, lease, session_id=session_id, original_kind=original_kind, kind=kind)
        finally:
            # token diet: per-run LLM usage (also on the final event as metadata.usage)
            log.info("run.usage", status=status, **ctx.usage.summary(ctx.trace_id or ctx.run_id))
            await _account_eval_usage(ctx)
        return status
    finally:
        if own_lease:
            await lease.release()


async def _drive(ctx: RunContext, lease: RunnerLease, *, session_id: str, original_kind: str, kind: str) -> str:
    deps = ctx.deps
    runner = lease.runner
    sessions: BaseSessionService = runner.session_service
    app_name = adk_app_name(ctx.tenant)
    user_id = ctx.case_id
    session = await sessions.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
    if session is None:
        if original_kind == "resume":
            await _fail(deps, ctx.tenant, ctx.case_id, ctx.run_id, f"ADK session {session_id} not found",
                        code="SESSION_NOT_FOUND", worker_id=ctx.worker_id)
            return "failed"
        session = await sessions.create_session(app_name=app_name, user_id=user_id, session_id=session_id)
    prior = list(session.events)

    new_message: types.Content | None
    interrupt_id = ctx.resume.interrupt_id if ctx.resume is not None else None
    if kind == "crash_retry" and _session_has_message(prior, ctx.invocation_id, interrupt_id=interrupt_id):
        new_message = None  # the original message is already in the session: just resume
    else:
        new_message = build_new_message(original_kind, ctx.payload)

    sink_factory = deps.reasoning_sink
    translator = Translator(
        case_id=ctx.case_id,
        run_id=ctx.run_id,
        tenant=ctx.tenant,
        bus=deps.bus,
        interrupts=deps.interrupts,
        vendor=ctx.spec.vendor_name,
        seen=seen_output_keys(prior),
        reasoning_sink=sink_factory() if callable(sink_factory) else None,
        vendors=await specialist_vendors(deps, ctx.tenant, ctx.spec),
        display_names={s.slug: ctx.spec.display_name(s.slug) for s in ctx.spec.all_specialists},
        custom_agents=[s.slug for s in ctx.spec.all_specialists if s.custom],
    )
    translator.usage_provider = lambda: ctx.usage.summary(ctx.trace_id or ctx.run_id)
    run_config = RunConfig(streaming_mode=StreamingMode.SSE)
    await deps.runs.record_invocation(ctx.run_id, ctx.invocation_id)
    with bind_run(ctx):
        await translator.start(initial_stage=initial_stage(original_kind, ctx.payload))
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                invocation_id=ctx.invocation_id,
                new_message=new_message,
                run_config=run_config,
                abort_signal=ctx.abort_signal,
            ):
                await translator.handle(event)
        except Exception as exc:  # runner-level boundary (not a node): record the failure
            if ctx.abort_reason in ("shutdown", "lease_lost"):
                log.warning("run abandoned", reason=ctx.abort_reason)
                return "abandoned"
            # Never put exception text on the case stream: log it under an error id, show the id.
            code, message = _public_failure(exc)
            error_id = report_exception(log, "run failed", exc, code=code)
            await translator.finish_error(user_message(error_id, message), code=code, error_id=error_id)
            await deps.runs.finish_run(ctx.run_id, "failed", invocation_id=ctx.invocation_id,
                                       error=f"[{error_id}] {type(exc).__name__}: {exc}"[:2000], worker_id=ctx.worker_id)
            return "failed"

        if ctx.abort_signal.is_set() or ctx.abort_reason is not None:
            reason = ctx.abort_reason or "cancelled"
            if reason in ("shutdown", "lease_lost"):
                log.warning("run abandoned", reason=reason)
                return "abandoned"
            if reason == "budget":
                log.warning("run stopped: budget exceeded", detail=ctx.abort_message)
                await translator.finish_error(
                    "This case used up its AI budget for this request, so the work was stopped.", code="BUDGET_EXCEEDED"
                )
                await deps.runs.finish_run(ctx.run_id, "failed", invocation_id=ctx.invocation_id,
                                           error=ctx.abort_message or "budget exceeded", worker_id=ctx.worker_id)
                return "failed"
            await translator.finish_error("The run was cancelled.", code="CANCELLED")
            await deps.runs.finish_run(ctx.run_id, "cancelled", invocation_id=ctx.invocation_id, worker_id=ctx.worker_id)
            return "cancelled"

        # A node can stay paused on a request that is already in the session while its
        # interrupt row was never written (the worker died in between, then crash-retried):
        # ADK does not re-emit that request, so recover it from the session.
        refreshed = await sessions.get_session(app_name=app_name, user_id=user_id, session_id=session_id)
        if refreshed is not None:
            recovered = translator.adopt_pending(unresolved_request_inputs(refreshed.events))
            if recovered:
                log.warning("recovered open interrupts from the session", count=recovered)
        if translator.pending_interrupts:
            await translator.finish_interrupted(invocation_id=ctx.invocation_id, session_id=session_id)
            await deps.runs.finish_run(ctx.run_id, "paused", invocation_id=ctx.invocation_id, worker_id=ctx.worker_id)
            return "paused"
        await translator.finish_success()
        await deps.runs.finish_run(ctx.run_id, "succeeded", invocation_id=ctx.invocation_id, worker_id=ctx.worker_id)
        return "succeeded"

