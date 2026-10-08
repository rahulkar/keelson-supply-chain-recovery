"""Agent playground (``docs/contracts/110-m7-backoffice-generic.md`` §3): one investigator +
structurer turn of a saved or draft custom specialist on a **scratch case**.

:func:`run_playground` is what ``POST /api/admin/agents/playground`` calls (engine-api; the route
validates the draft with :func:`~keelson_contracts.agents.custom_specialist_violations` first):

* **Scratch case, never commits.** The turn runs on its own in-memory ADK session and a
  :class:`ScratchRepo`: evidence lives in memory for the turn (the structurer's index and the
  ``calc`` tool read it back) and is dropped afterwards; every case/plan/change-set write raises
  :class:`PlaygroundWriteError`. Only security audit rows (denied tools, flagged untrusted text)
  reach the tenant's real audit log. The workflow is one node running the specialist wrapper (as
  ``investigate`` runs a task: dynamic child on its own sub-branch) — there is
  no planner, no simulation, no compile/apply node to reach, and the investigator holds read
  tools only (write tools are never bound to an LLM).
* **Tenant-scoped.** Tools run with the tenant's reader principal through the same toolsets,
  quarantine wrappers and plugins (policy, budget, turn cap, evidence) as a case run.
* **Budget-capped.** ``budgets.max_llm_calls`` / ``max_total_tokens`` are the smaller of the
  tenant's and :data:`PLAYGROUND_MAX_LLM_CALLS` / :data:`PLAYGROUND_MAX_TOKENS`; a breach ends the
  turn with ``status="budget_exceeded"``.
* **Streamed.** Activity (``SUBAGENT_*``, ``TOOL_CALL_*``, reasoning) goes through the normal
  :class:`~keelson_engine.runtime.translator.Translator` into ``emit`` (an async callable taking
  one AG-UI event dict) instead of the case stream; nothing is persisted.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Final

import structlog
from google.adk.agents.context import Context
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.apps.app import App
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.adk.workflow import START, FunctionNode, Workflow
from google.adk.workflow._errors import DynamicNodeFailError
from google.genai import types
from keelson_contracts.agents import (
    CustomSpecialistSpec,
    PlaygroundError,
    PlaygroundRequest,
    PlaygroundResult,
    PlaygroundToolCall,
    PlaygroundUsage,
    is_pro_model,
)
from keelson_contracts.common import new_evidence_id
from keelson_contracts.llm import Findings
from keelson_contracts.runs import Actor
from pydantic import ValidationError

from ..public_errors import report_exception
from .agents import fill_nullable, make_agent
from .context import AbortController, RunContext, TaskBinding, bind_run, bind_task
from .custom_specialists import custom_investigator
from .deps import RuntimeDeps, get_deps
from .errors import RuntimeConfigError
from .llm import ModelFactory
from .nodes.investigate import make_specialist_wrapper
from .nodes.prompts import extractor_instruction, guard_instruction, structurer_instruction
from .plugins import default_plugins
from .spec import AgentSpec, Budgets, CustomSpecialist
from .toolsets import KEELSON_META
from .translator import Translator

__all__ = [
    "PLAYGROUND_MAX_LLM_CALLS",
    "PLAYGROUND_MAX_TOKENS",
    "PlaygroundWriteError",
    "ScratchRepo",
    "run_playground",
]

log = structlog.get_logger(__name__)

PLAYGROUND_MAX_LLM_CALLS: Final = 30
"""LLM calls of one playground turn: investigator turns (≤ 10) + structurer + quarantine screens."""
PLAYGROUND_MAX_TOKENS: Final = 300_000
PLAYGROUND_TASK_ID: Final = "playground"
PLAYGROUND_RUN_NODE_ID: Final = "task-playground-r1"
PLAYGROUND_APP: Final = "keelson-playground"

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class PlaygroundWriteError(RuntimeError):
    """Something tried to persist case state from the playground (it never commits)."""


class ScratchRepo:
    """The playground's repository: evidence in memory, audits forwarded, every write refused."""

    def __init__(self, real: Any, *, case_id: str) -> None:
        self._real = real
        self._case_id = case_id
        self.evidence: dict[str, dict[str, Any]] = {}

    async def save_evidence(self, *, tenant: str, case_id: str, run_id: str, connector: str, tool: str,
                            args: Mapping[str, Any], result: Any, prov: str = "fact") -> str:
        evidence_id = new_evidence_id()  # the contract's ``ev_…`` form (Findings / PlaygroundToolCall cite it)
        self.evidence[evidence_id] = {
            "id": evidence_id, "tenant": tenant, "case_id": self._case_id, "run_id": run_id,
            "connector": connector, "tool": tool, "args": json.loads(json.dumps(dict(args), default=str)),
            "result": json.loads(json.dumps(result, default=str)), "prov": prov,
        }
        return evidence_id

    async def get_evidence(self, *, tenant: str, ids: Sequence[str]) -> list[dict[str, Any]]:
        return [dict(self.evidence[i]) for i in ids if i in self.evidence]

    async def latest_evidence(self, *, tenant: str, case_id: str, tool: str) -> dict[str, Any] | None:
        rows = [r for r in self.evidence.values() if r["tool"] == tool]
        return dict(rows[-1]) if rows else None

    async def admin_audit(self, *, tenant: str, actor: str, action: str, target: str, before: Any = None,
                          after: Any = None) -> None:
        # security signals (tool.denied, untrusted_text.flagged) stay visible to the tenant's admins
        await self._real.admin_audit(tenant=tenant, actor=actor, action=action, target=target, before=before,
                                     after={**dict(after or {}), "playground": True})

    async def get_case(self, *, tenant: str, case_id: str) -> dict[str, Any] | None:
        return {"id": self._case_id, "title": "Agent playground", "mode": "what_if", "status": "open"}

    async def recent_messages(self, *, tenant: str, case_id: str, limit: int) -> list[dict[str, Any]]:
        return []

    async def latest_scenario(self, *, tenant: str, case_id: str) -> None:
        return None

    async def latest_plan(self, *, tenant: str, case_id: str) -> None:
        return None

    async def list_change_sets(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return []

    async def list_investigation_tasks(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return []

    async def get_agent_spec(self, *, tenant: str, version: str) -> dict[str, Any] | None:
        return await self._real.get_agent_spec(tenant=tenant, version=version)

    def __getattr__(self, name: str) -> Any:
        # every other repo method writes case state: refused
        async def refused(*args: Any, **kwargs: Any) -> Any:
            raise PlaygroundWriteError(f"the playground never writes ({name})")

        return refused


class _NoInterrupts:
    async def create_interrupt(self, **kwargs: Any) -> None:
        raise PlaygroundWriteError("the playground never asks for approvals")

    async def supersede_open(self, *, tenant: str, case_id: str, reason: str) -> int:
        return 0


class _SinkBus:
    """``BusPort`` that forwards each AG-UI event to ``emit`` (nothing persisted)."""

    def __init__(self, emit: Emit | None) -> None:
        self._emit = emit
        self.events: list[dict[str, Any]] = []

    async def append(self, *, tenant: str, case_id: str, run_id: str | None, event: dict[str, Any],
                     persist: bool = True, live: bool = True) -> int:
        self.events.append(event)
        if self._emit is not None:
            try:
                await self._emit(event)
            except Exception as exc:  # a closed admin stream never fails the turn
                log.debug("playground stream sink failed", error=type(exc).__name__)
        return len(self.events)


class _ToolRecorder(BasePlugin):
    """Records every tool call of the turn (first plugin: sees denied calls too)."""

    def __init__(self) -> None:
        super().__init__(name="keelson_playground_tools")
        self.calls: list[dict[str, Any]] = []
        self._open: dict[int, dict[str, Any]] = {}

    async def before_tool_callback(self, *, tool: BaseTool, tool_args: dict[str, Any],
                                   tool_context: ToolContext) -> dict[str, Any] | None:
        meta = (tool.custom_metadata or {}).get(KEELSON_META) or {}
        entry = {"tool": tool.name, "connector": str(meta.get("connector") or "keelson"),
                 "quarantined": meta.get("kind") == "quarantine", "started": time.monotonic(),
                 "ok": False, "error": "the tool call was refused", "duration_ms": 0}
        self.calls.append(entry)
        self._open[id(tool_context)] = entry
        return None

    async def after_tool_callback(self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext,
                                  result: dict[str, Any]) -> dict[str, Any] | None:
        entry = self._open.pop(id(tool_context), None)
        if entry is None:
            return None
        entry["duration_ms"] = int((time.monotonic() - entry["started"]) * 1000)
        failed = isinstance(result, Mapping) and isinstance(result.get("error"), str)
        entry["ok"] = not failed
        entry["error"] = str(result["error"])[:300] if failed else None
        return None


def _resolve(spec: AgentSpec, request: PlaygroundRequest) -> CustomSpecialistSpec:
    if isinstance(request.specialist, CustomSpecialistSpec):
        draft = request.specialist
        if draft.slug in {s.slug for s in spec.specialists}:
            raise RuntimeConfigError(f"{draft.slug!r} is a built-in specialist slug")
        return draft
    saved = next((c for c in spec.custom_specialists if c.slug == request.specialist), None)
    if saved is None:
        raise LookupError(f"no custom specialist {request.specialist!r} in this tenant's agent spec")
    return saved


def _scratch_spec(spec: AgentSpec, draft: CustomSpecialistSpec) -> AgentSpec:
    budgets = Budgets(
        max_llm_calls=min(spec.budgets.max_llm_calls, PLAYGROUND_MAX_LLM_CALLS),
        max_total_tokens=min(spec.budgets.max_total_tokens, PLAYGROUND_MAX_TOKENS),
    )
    # only the specialist under test: the scratch spec never reaches a planner or another specialist
    enabled = draft.model_copy(update={"enabled": True})
    return AgentSpec.model_validate({
        **spec.model_dump(mode="python", exclude={"custom_specialists", "budgets"}),
        "budgets": budgets, "custom_specialists": (enabled,),
    })


def _brief(question: str) -> dict[str, Any]:
    return {
        "task_id": PLAYGROUND_TASK_ID,
        "mode": "playground",
        "objective": "Answer the admin's test question with your read-only tools.",
        "user_request": question,
        "note": "Agent playground: there is no disruption scenario; nothing is planned or applied.",
    }


def _tool_calls(recorder: _ToolRecorder, evidence_log: Sequence[Mapping[str, Any]]) -> list[PlaygroundToolCall]:
    pending = [dict(e) for e in evidence_log if e.get("tool") != "calc"]
    out: list[PlaygroundToolCall] = []
    for call in recorder.calls[:15]:
        evidence_id = None
        if call["ok"]:
            match = next((e for e in pending if e.get("tool") == call["tool"]), None)
            if match is not None:
                pending.remove(match)
                evidence_id = str(match.get("id")) if match.get("id") else None
        connector = call["connector"] if call["connector"] else "keelson"
        out.append(PlaygroundToolCall(
            connector=connector, tool=call["tool"], ok=bool(call["ok"]), duration_ms=max(0, int(call["duration_ms"])),
            quarantined=bool(call["quarantined"]), evidence_id=evidence_id if call["ok"] else None,
            error=None if call["ok"] else (call["error"] or "tool call failed")[:300],
        ))
    return out


async def run_playground(
    request: PlaygroundRequest,
    *,
    tenant: str,
    spec: AgentSpec,
    models: ModelFactory,
    actor: Actor | Mapping[str, Any],
    deps: RuntimeDeps | None = None,
    emit: Emit | None = None,
    playground_id: str | None = None,
    controller: AbortController | None = None,
) -> PlaygroundResult:
    """Run one playground turn (see the module docstring) and return its :class:`PlaygroundResult`."""
    deps = deps or get_deps()
    playground_id = playground_id or str(uuid.uuid4())
    started_at = dt.datetime.now(dt.UTC)
    t0 = time.monotonic()
    draft = _resolve(spec, request)
    if is_pro_model(draft.model_alias):  # the contract model refuses it too; defence in depth
        raise RuntimeConfigError("Pro models are not allowed for custom specialists")
    scratch_spec = _scratch_spec(spec, draft)
    specialist = next(s for s in scratch_spec.all_specialists
                      if isinstance(s, CustomSpecialist) and s.slug == draft.slug)
    case_id = f"playground-{playground_id}"
    run_id = f"pg-{playground_id}"
    repo = ScratchRepo(deps.repo, case_id=case_id)
    bus = _SinkBus(emit)
    scratch_deps = dataclasses.replace(
        deps, repo=repo, bus=bus, interrupts=_NoInterrupts(), checklists=None, signals=None, plans=None,
        reasoning_sink=None,
    )
    aliases = scratch_spec.models
    guard = make_agent(name="guard", alias=aliases.guard, models=models, instruction=guard_instruction,
                       output_schema=_guard_schema())
    extractor = make_agent(name="extractor", alias=aliases.extractor, models=models,
                           instruction=extractor_instruction, output_schema=_extraction_schema())
    investigator = custom_investigator(specialist, spec=scratch_spec, models=models, guard=guard, extractor=extractor)
    structurer = make_agent(name=f"{specialist.slug}_structurer", alias=aliases.structurer, models=models,
                            instruction=structurer_instruction(specialist.slug), output_schema=Findings)
    wrapper = make_specialist_wrapper(specialist, investigator, structurer)
    outcome: dict[str, Any] = {}
    turn = _turn_node(wrapper, _brief(request.question), outcome)
    recorder = _ToolRecorder()
    app = App(name=PLAYGROUND_APP, root_agent=Workflow(name="playground", edges=[(START, turn)]),
              plugins=[recorder, *default_plugins()])
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    ctx = RunContext(
        tenant=tenant, case_id=case_id, run_id=run_id, kind="playground",
        actor=actor if isinstance(actor, Actor) else Actor.model_validate(dict(actor)),
        payload={}, spec=scratch_spec, deps=scratch_deps, invocation_id=f"e-{playground_id}",
        trace_id=run_id, case_title="Agent playground", case_mode="what_if",
        controller=controller or AbortController(),
    )
    translator = Translator(
        case_id=case_id, run_id=run_id, tenant=tenant, bus=bus, vendor=scratch_spec.vendor_name,
        display_names={specialist.slug: specialist.display_name}, custom_agents=[specialist.slug],
    )
    session = await sessions.create_session(app_name=PLAYGROUND_APP, user_id=case_id, session_id=run_id)
    # the question travels in the task brief (node input), exactly like a case run's user_request
    message = types.Content(role="user", parts=[types.Part(text="Agent playground test turn.")])
    findings: Findings | None = None
    error: PlaygroundError | None = None
    status = "completed"
    # bound like an investigation task: the specialist's turn cap (max_turns, last turn tool-free)
    # and its per-task tool budget apply exactly as in a case run
    task = TaskBinding(task_id=PLAYGROUND_TASK_ID, specialist=specialist.slug, tools=())
    with bind_run(ctx), bind_task(task):
        await translator.start()
        try:
            async for event in runner.run_async(user_id=case_id, session_id=session.id, new_message=message,
                                                invocation_id=ctx.invocation_id,
                                                run_config=RunConfig(streaming_mode=StreamingMode.SSE),
                                                abort_signal=ctx.abort_signal):
                await translator.handle(event)
            failure = outcome.get("error")
            if isinstance(failure, BaseException):
                raise failure
            findings = _findings(outcome.get("findings"))
        except Exception as exc:  # the turn's boundary: report, never raise into the admin API
            error_id = report_exception(log, "playground turn failed", exc, specialist=specialist.slug)
            status = "failed"
            error = PlaygroundError(code=_error_code(exc), message=f"The test turn failed (error {error_id}).")
        if ctx.abort_reason == "budget":
            status = "budget_exceeded"
            error = PlaygroundError(code="budget_exceeded",
                                    message="The test turn used up its AI budget before the specialist finished.")
            findings = None
        elif ctx.abort_reason is not None and status == "completed":
            status = "failed"
            code = re.sub(r"[^a-z0-9_]", "_", str(ctx.abort_reason).lower()).strip("_")[:64] or "stopped"
            error = PlaygroundError(code=code if code[0].isalpha() else f"stopped_{code}"[:64],
                                    message="The test turn was stopped.")
        elif status == "completed" and findings is None:
            status = "failed"
            error = PlaygroundError(code="no_findings", message="The specialist returned no findings.")
        if status == "completed":
            await translator.finish_success()
        else:
            assert error is not None
            await translator.finish_error(error.message, code=error.code.upper())
    total = ctx.usage.total()
    calls = _tool_calls(recorder, ctx.evidence_log)
    return PlaygroundResult(
        playground_id=playground_id,
        specialist=specialist.slug,
        display_name=specialist.display_name,
        status=status,  # type: ignore[arg-type]
        findings=findings if status == "completed" else None,
        tool_calls=calls,
        usage=PlaygroundUsage(llm_calls=ctx.llm_calls, tool_calls=len(calls), tokens_in=total.prompt_tokens,
                              tokens_out=total.completion_tokens),
        error=error,
        started_at=started_at,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


def _turn_node(wrapper: FunctionNode, brief: dict[str, Any], outcome: dict[str, Any]) -> FunctionNode:
    """The playground's only node: the specialist wrapper as a dynamic child on its own sub-branch
    (like ``investigate`` runs a task), so the investigator's history, its quarantine guard /
    extractor calls and the structurer are scoped exactly as in a case run."""

    async def playground_turn(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        try:
            outcome["findings"] = await ctx.run_node(wrapper, node_input=brief, run_id=PLAYGROUND_RUN_NODE_ID,
                                                     use_sub_branch=True)
        except DynamicNodeFailError as exc:
            outcome["error"] = exc.error if isinstance(exc.error, BaseException) else exc
        yield {"done": True}

    return FunctionNode(func=playground_turn, name="playground_turn", rerun_on_resume=True)


def _findings(output: Any) -> Findings | None:
    if output is None:
        return None
    try:
        return Findings.model_validate(fill_nullable(Findings, output if isinstance(output, Mapping) else {}))
    except ValidationError:
        return None


def _error_code(exc: BaseException) -> str:
    current: BaseException | None = exc
    for _ in range(6):
        if current is None:
            break
        if isinstance(current, RuntimeConfigError):
            return "configuration_error"
        if isinstance(current, PlaygroundWriteError):
            return "write_refused"
        nested = getattr(current, "error", None)
        current = nested if isinstance(nested, BaseException) else (current.__cause__ or current.__context__)
    return "internal_error"


def _guard_schema() -> Any:
    from keelson_contracts.llm import GuardVerdict  # noqa: PLC0415

    return GuardVerdict


def _extraction_schema() -> Any:
    from keelson_contracts.llm import UntrustedExtraction  # noqa: PLC0415

    return UntrustedExtraction
