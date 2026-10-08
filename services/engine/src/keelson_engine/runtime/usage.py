"""Per-run LLM usage metering (token diet, 2026-10-07).

Every model call of a run is metered per agent (ADK agent name): calls, prompt / completion /
reasoning / cached tokens, wall time, and the LiteLLM cost when the proxy reports one. The run's
summary (:meth:`UsageMeter.summary`) is logged as ``run.usage`` and carried by the run's final
event (``RUN_FINISHED`` / ``RUN_ERROR`` ``metadata.usage``, persisted with the run's events), so a
case's spend can be read per run without the LiteLLM UI. ``trace_id`` is the ``x-litellm-trace-id``
the run already sends: LiteLLM's spend logs hold the same calls under that session id.

* Tokens come from the model response's ``usage_metadata`` (``UsagePlugin`` in ``plugins``). A call
  served by the record/replay layer (``runtime.replay``) reports zero tokens and is counted in
  ``replayed`` (its calls are not billed again).
* Cost comes from a LiteLLM success callback (:func:`install_cost_tracking`): the proxy's
  ``x-litellm-response-cost`` header, else LiteLLM's own ``response_cost``. LiteLLM dispatches its
  callbacks with a copy of the caller's context, so the run contextvar and the calling agent
  (:data:`current_agent`, set by ``UsagePlugin`` before each call) are visible there. A cost that
  arrives after the run ended is not counted (the summary says ``cost_usd: null`` when none came).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import structlog

__all__ = [
    "AgentUsage",
    "UsageMeter",
    "current_agent",
    "install_cost_tracking",
    "record_cost",
    "response_cost",
]

log = structlog.get_logger(__name__)

current_agent: ContextVar[str | None] = ContextVar("keelson_llm_agent", default=None)
"""The agent whose model call is in flight in this task (set by ``UsagePlugin.before_model_callback``)."""

COST_HEADER = "x-litellm-response-cost"


@dataclass
class AgentUsage:
    calls: int = 0
    replayed: int = 0
    """Calls answered from a recording (``custom_metadata['keelson_replay']``): zero tokens, no cost."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float | None = None

    def add(self, other: AgentUsage) -> None:
        self.calls += other.calls
        self.replayed += other.replayed
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cached_tokens += other.cached_tokens
        self.total_tokens += other.total_tokens
        self.latency_ms += other.latency_ms
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "replayed": self.replayed,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
        }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class UsageMeter:
    """Usage of one run by agent name (thread-safe: LiteLLM may log from its own thread)."""

    agents: dict[str, AgentUsage] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _agent(self, agent: str | None) -> AgentUsage:
        return self.agents.setdefault(agent or "unknown", AgentUsage())

    def record(self, agent: str | None, usage: Any, *, latency_ms: int = 0, replayed: bool = False) -> None:
        """One model response (``GenerateContentResponseUsageMetadata`` or ``None``)."""
        with self._lock:
            row = self._agent(agent)
            row.calls += 1
            row.replayed += int(bool(replayed))
            row.latency_ms += max(0, int(latency_ms))
            if usage is None:
                return
            prompt = _int(getattr(usage, "prompt_token_count", 0))
            completion = _int(getattr(usage, "candidates_token_count", 0))
            row.prompt_tokens += prompt
            row.completion_tokens += completion
            row.reasoning_tokens += _int(getattr(usage, "thoughts_token_count", 0))
            row.cached_tokens += _int(getattr(usage, "cached_content_token_count", 0))
            row.total_tokens += _int(getattr(usage, "total_token_count", 0)) or prompt + completion

    def add_cost(self, agent: str | None, cost: float) -> None:
        with self._lock:
            row = self._agent(agent)
            row.cost_usd = (row.cost_usd or 0.0) + float(cost)

    def total(self) -> AgentUsage:
        out = AgentUsage()
        with self._lock:
            for row in self.agents.values():
                out.add(row)
        return out

    def summary(self, trace_id: str | None = None) -> dict[str, Any]:
        """``{trace_id, total: {...}, agents: {agent: {...}}}`` (agents by name)."""
        with self._lock:
            agents = {name: row.as_dict() for name, row in sorted(self.agents.items())}
        out: dict[str, Any] = {"total": self.total().as_dict(), "agents": agents}
        if trace_id:
            out["trace_id"] = trace_id
        return out


# ─────────────────────────────── LiteLLM cost ───────────────────────────────


def response_cost(kwargs: Mapping[str, Any] | None, response_obj: Any) -> float | None:
    """The call's cost: the proxy's ``x-litellm-response-cost`` header (authoritative: the proxy
    prices the provider model behind the alias), else LiteLLM's own ``response_cost``."""
    hidden = getattr(response_obj, "_hidden_params", None)
    if not isinstance(hidden, Mapping) and isinstance(response_obj, Mapping):
        hidden = response_obj.get("_hidden_params")
    headers = (hidden or {}).get("additional_headers") if isinstance(hidden, Mapping) else None
    for source in (headers or {}, (hidden or {}) if isinstance(hidden, Mapping) else {}):
        for key in (f"llm_provider-{COST_HEADER}", COST_HEADER, "response_cost"):
            value = source.get(key) if isinstance(source, Mapping) else None
            cost = _float(value)
            if cost is not None:
                return cost
    return _float((kwargs or {}).get("response_cost"))


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    return cost if cost >= 0 else None


def record_cost(kwargs: Mapping[str, Any] | None, response_obj: Any) -> None:
    """LiteLLM success callback body: add the call's cost to the current run (if any)."""
    from .context import maybe_current_run  # noqa: PLC0415 - avoid an import cycle

    run = maybe_current_run()
    if run is None:
        return
    cost = response_cost(kwargs, response_obj)
    if cost is not None:
        run.usage.add_cost(current_agent.get(), cost)


_installed = False
_install_lock = threading.Lock()


def install_cost_tracking() -> bool:
    """Register the LiteLLM success callback once per process (``False`` without LiteLLM)."""
    global _installed
    with _install_lock:
        if _installed:
            return True
        try:
            import litellm  # noqa: PLC0415
            from litellm.integrations.custom_logger import CustomLogger  # noqa: PLC0415
        except ImportError:  # pragma: no cover - LiteLLM is a runtime dependency
            return False

        class _KeelsonCostLogger(CustomLogger):  # type: ignore[misc]
            # ADK calls ``litellm.acompletion``: only the async hook fires (in the caller's context);
            # the sync hook runs on a thread without the run contextvar, so it is not used.
            async def async_log_success_event(
                self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
            ) -> None:
                _safe_record(kwargs, response_obj)

        callbacks = list(getattr(litellm, "callbacks", None) or [])
        callbacks.append(_KeelsonCostLogger())
        litellm.callbacks = callbacks
        _installed = True
        return True


def _safe_record(kwargs: Any, response_obj: Any) -> None:
    try:
        record_cost(kwargs if isinstance(kwargs, Mapping) else None, response_obj)
    except Exception as exc:  # noqa: BLE001 - metering must never break a model call
        log.warning("llm cost metering failed", error=type(exc).__name__)
