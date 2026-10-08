"""Per-run context, carried in a contextvar.

``run_case`` binds a :class:`RunContext` before driving ``runner.run_async``; ADK creates its
node tasks from that context, so every node, plugin, tool and model call of the run sees it.
The cached ``Runner``/``App`` stay run-independent (tenant/run data never lives in them).

Nothing in here is persisted: durable case state lives in the keelson DB and the ADK session.
Credentials never enter the context (the Token Broker is called at use time).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from keelson_contracts.runs import Actor, ResumePayload

from .deps import RuntimeDeps
from .spec import AgentSpec
from .usage import UsageMeter

__all__ = [
    "AbortController",
    "AbortReason",
    "RunContext",
    "TaskBinding",
    "bind_run",
    "bind_task",
    "current_run",
    "current_task",
    "maybe_current_run",
]

AbortReason = Literal["cancelled", "budget", "shutdown", "lease_lost"]


@dataclass
class AbortController:
    """Shared between the worker (cancel watcher, heartbeat, shutdown) and the run's plugins.

    ``signal`` is handed to ``runner.run_async(abort_signal=...)``; the first reason wins.
    """

    signal: asyncio.Event = field(default_factory=asyncio.Event)
    reason: AbortReason | None = None
    message: str | None = None

    def abort(self, reason: AbortReason, message: str | None = None) -> None:
        if self.reason is None:
            self.reason = reason
            self.message = message
        self.signal.set()


@dataclass
class RunContext:
    tenant: str
    case_id: str
    run_id: str
    kind: str
    """``chat`` | ``action`` | ``resume`` | ``crash_retry``."""
    actor: Actor
    payload: dict[str, Any]
    spec: AgentSpec
    deps: RuntimeDeps
    invocation_id: str
    controller: AbortController = field(default_factory=AbortController)
    resume: ResumePayload | None = None
    trace_id: str = ""
    case_title: str = ""
    case_mode: str = "actual"
    history: list[dict[str, str]] = field(default_factory=list)
    worker_id: str | None = None
    """Lease owner that claimed the run (``finish_run`` lease guard)."""
    llm_calls: int = 0
    total_tokens: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    """Per-agent tool call counter (BudgetPlugin / investigator cap)."""
    calc_values: dict[str, Any] = field(default_factory=dict)
    evidence_log: list[dict[str, Any]] = field(default_factory=list)
    """Evidence written by tools in this run: ``{id, agent, tool, args}`` (structurer index)."""
    verified_tools: dict[str, Any] = field(default_factory=dict)
    """Connector → snapshot tools verified against the live server (once per run)."""
    toolset_errors: dict[str, str] = field(default_factory=dict)
    """Agent name → binding problem; the agent fails loudly before its first model call."""
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    """Per-run single-flight locks (parallel specialists share one run)."""
    quarantine_results: dict[str, Any] = field(default_factory=dict)
    """Quarantine wrapper results of this run by (wrapper, connector, args): an identical read by
    another task reuses the screened extraction instead of re-running guard + extractor."""
    quarantine_calls: dict[str, int] = field(default_factory=dict)
    """Quarantine wrapper calls per task and wrapper (each call may cost several LLM calls)."""
    model_turns: dict[str, int] = field(default_factory=dict)
    """Model turns per investigator task (``<agent>:<task key>``; ``investigation.max_investigator_turns``)."""
    usage: UsageMeter = field(default_factory=UsageMeter)
    """LLM usage of this run per agent (tokens, latency, cost); ``runtime.usage``."""

    def lock(self, name: str) -> asyncio.Lock:
        return self.locks.setdefault(name, asyncio.Lock())

    def abort(self, reason: AbortReason, message: str | None = None) -> None:
        """Stop the invocation (ADK checks ``abort_signal`` between steps)."""
        self.controller.abort(reason, message)

    @property
    def abort_signal(self) -> asyncio.Event:
        return self.controller.signal

    @property
    def abort_reason(self) -> AbortReason | None:
        return self.controller.reason

    @property
    def abort_message(self) -> str | None:
        return self.controller.message

    @property
    def llm_headers(self) -> dict[str, str]:
        """Per-run LiteLLM headers (trace correlation + spend attribution)."""
        return {
            "x-litellm-trace-id": self.trace_id or self.run_id,
            "x-litellm-customer-id": self.tenant,
        }


@dataclass(frozen=True)
class TaskBinding:
    """The investigation task a specialist coroutine is working on (M4 parallel waves).

    ``investigate`` binds it inside each task's asyncio task before ``ctx.run_node``; ADK creates
    the child node tasks from that context, so the specialist's tools, plugins and instruction
    providers see *their* task even while several tasks of one specialist run in parallel."""

    task_id: str
    specialist: str
    tools: tuple[str, ...]
    """The plan's tool subset for this task (empty = the specialist's whole allowlist)."""
    rev: int = 1

    @property
    def key(self) -> str:
        return f"{self.task_id}@r{self.rev}"


_current_task: ContextVar[TaskBinding | None] = ContextVar("keelson_task_ctx", default=None)


def current_task() -> TaskBinding | None:
    return _current_task.get()


@contextmanager
def bind_task(task: TaskBinding) -> Iterator[TaskBinding]:
    token = _current_task.set(task)
    try:
        yield task
    finally:
        _current_task.reset(token)


_current: ContextVar[RunContext | None] = ContextVar("keelson_run_ctx", default=None)


def current_run() -> RunContext:
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("no run bound: runtime code must execute inside run_case()")
    return ctx


def maybe_current_run() -> RunContext | None:
    return _current.get()


@contextmanager
def bind_run(ctx: RunContext) -> Iterator[RunContext]:
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
