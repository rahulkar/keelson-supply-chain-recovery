"""App-level ADK plugins (shared by all runs of a cached Runner; per-run state lives in the
run contextvar).

* :class:`PolicyPlugin` — second line of defence (the credential is already read-only): denies
  any tool that is not a Keelson read tool on the calling agent's allowlist, and enforces the
  per-agent tool-call cap.
* :class:`BudgetPlugin` — per-run LLM call and token caps; on breach it aborts the invocation
  through ``abort_signal`` (the run ends ``RUN_ERROR{code: BUDGET_EXCEEDED}``). It also caps the
  model turns of each investigator task (``investigation.max_investigator_turns``): the last
  allowed turn runs with tool calling off, so the analyst writes its notes instead of reading on.
  (M8) A run of a live eval case also stops at what is left of the eval run's token / cost budget
  (``RunContext.eval_token_cap`` / ``eval_cost_cap_microusd``, set at run start).
* :class:`UsagePlugin` — meters every model call per agent (tokens, reasoning, latency) into
  ``RunContext.usage`` (``runtime.usage``; cost via the LiteLLM callback).
* :class:`EvidencePlugin` — every tool result becomes an ``evidence`` row (``prov: fact``) and
  the returned dict gains ``_evidence_id`` for citation. The model sees a compacted copy
  (:func:`compact_for_model`, ~3k tokens at most): a long list shows its first rows plus a
  ``_summary`` over all rows (count, sums, value counts, date ranges — citable through virtual
  pointers, ``runtime.jsonptr``), so JSON pointers into the stored evidence stay valid while a
  500-row read no longer costs thousands of prompt tokens on every later turn.
* :class:`AuditPlugin` — structured audit log of model and tool calls (JSON logs with trace id).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

import structlog
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .context import RunContext, current_task, maybe_current_run
from .jsonptr import list_summary
from .replay import META_KEY as REPLAY_META_KEY
from .toolsets import KEELSON_META, UNTRUSTED_SOURCE_TOOLS
from .usage import current_agent

__all__ = [
    "FINAL_TURN_NOTE",
    "LIST_TOP_N",
    "MAX_MODEL_RESULT_CHARS",
    "MODEL_RESULT_TOKEN_BUDGET",
    "AuditPlugin",
    "BudgetPlugin",
    "EvidencePlugin",
    "PolicyPlugin",
    "UsagePlugin",
    "approx_tokens",
    "compact_for_model",
    "default_plugins",
]

log = structlog.get_logger(__name__)

DENIED_PREFIX = "tool_denied"
MODEL_RESULT_TOKEN_BUDGET = 3_000
"""Per-result budget (tokens) of one tool result as the model sees it (the evidence keeps it all)."""
JSON_CHARS_PER_TOKEN = 2
"""Conservative characters per token for code- and number-heavy JSON (measured ~2.5 on Merxa reads)."""
MAX_MODEL_RESULT_CHARS = MODEL_RESULT_TOKEN_BUDGET * JSON_CHARS_PER_TOKEN
"""Hard cap of one compacted result (JSON characters)."""
LIST_TOP_N = 10
"""A top-level list longer than this is shown as its first rows plus a summary over all rows."""
NESTED_TOP_N = 3
"""Lists inside rows (a schedule line's orders, a PO's lines inside a list of POs) keep this prefix."""

_SUMMARY_NOTE = (
    "First {top} rows per list (nested: {nested}); _summary covers ALL rows of each cut list by path "
    "(e.g. items/0/lines): cite /_summary/<path>/total, sums/<col>, counts/<col>/<value>, "
    "range/<col>/min|max. Use narrower filters to see other rows."
)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def approx_tokens(value: Any) -> int:
    """Conservative token estimate of ``value`` as compact JSON (the per-result budget's unit)."""
    return -(-_size(value) // JSON_CHARS_PER_TOKEN)


def _cut_nested(row: Any, limit: int) -> tuple[Any, dict[str, int]]:
    """``row`` with every list inside it cut to a pointer-stable prefix of ``limit`` entries, and
    the full length of each cut list by key (so the model is told how many rows it does not see)."""
    if not isinstance(row, Mapping):
        return row, {}
    out: dict[str, Any] = {}
    cut: dict[str, int] = {}
    for key, value in row.items():
        if isinstance(value, list) and len(value) > limit:
            out[key] = value[:limit]
            cut[str(key)] = len(value)
        else:
            out[key] = value
    return out, cut


def compact_for_model(
    payload: Mapping[str, Any], max_chars: int = MAX_MODEL_RESULT_CHARS, *, top_n: int = LIST_TOP_N,
    nested_top_n: int = NESTED_TOP_N,
) -> dict[str, Any]:
    """Shrink a tool result for the model; the stored evidence keeps the whole result.

    * Every top-level list longer than ``top_n`` keeps its first ``top_n`` rows (so row ``i`` keeps
      the JSON pointer ``/<field>/i`` of the stored evidence) and gets a ``_summary`` entry over all
      rows (:func:`~keelson_engine.runtime.jsonptr.list_summary`: total, sums, counts, date ranges —
      each citable through a virtual pointer).
    * Lists inside rows (and inside top-level objects) keep a prefix of ``nested_top_n`` /
      ``top_n`` entries.
    * If the result is still larger than ``max_chars``, the top-level lists are cut further, largest
      first, to the longest prefix that fits; their ``_summary.shown`` says how many rows remain.

    Every cut list — top-level or nested — gets a ``_summary`` entry under its path (``lines``,
    ``lines/0/orders``, ``po/lines``), so the model knows how many rows it does not see and can cite
    that count (``/_summary/lines/0/orders/total`` resolves against the stored evidence).

    Results with nothing to cut are returned unchanged (as a new dict).
    """
    out: dict[str, Any] = dict(payload)
    summary: dict[str, dict[str, Any]] = {}
    nested: dict[str, dict[str, Any]] = {}
    """``<list>/<row>/<key>`` → ``{total, shown}`` of lists cut inside the rows of a top-level list."""
    for key, value in payload.items():
        if isinstance(value, list):
            rows = value[:top_n]
            kept = []
            for index, row in enumerate(rows):
                trimmed, cut = _cut_nested(row, nested_top_n)
                kept.append(trimmed)
                for sub, total in cut.items():
                    nested[f"{key}/{index}/{sub}"] = {"total": total, "shown": nested_top_n}
            out[key] = kept
            if len(value) > top_n:
                summary[key] = list_summary(value, shown=len(kept))
        elif isinstance(value, Mapping):
            trimmed, cut = _cut_nested(value, top_n)
            out[key] = trimmed
            for sub in cut:
                summary[f"{key}/{sub}"] = list_summary(value[sub], shown=top_n)
    cut_any = bool(nested)
    if not summary and not cut_any and _size(out) <= max_chars:
        return dict(payload)

    def finish() -> dict[str, Any]:
        merged = {**summary, **nested}
        if merged:
            out["_summary"] = merged
        else:
            out.pop("_summary", None)
        if merged or cut_any:
            out["_note"] = _SUMMARY_NOTE.format(top=top_n, nested=nested_top_n)
        return out

    finish()
    if _size(out) <= max_chars:
        return out
    lists = sorted(
        ((k, v) for k, v in out.items() if isinstance(v, list) and v and not k.startswith("_")),
        key=lambda kv: _size(kv[1]),
        reverse=True,
    )
    for key, items in lists:
        total = len(payload[key]) if isinstance(payload.get(key), list) else len(items)
        entry = summary.get(key) or list_summary(payload[key], shown=len(items))
        summary[key] = entry
        out[key] = []
        entry["shown"] = 0
        _drop_nested(nested, key, 0)
        finish()
        budget = max_chars - _size(out)
        lo, hi = 0, len(items)
        while lo < hi:  # longest prefix whose JSON fits in the remaining budget
            mid = (lo + hi + 1) // 2
            # "[]" is already counted in _size(out); each kept row also brings back its nested counts
            if _size(items[:mid]) - 2 + _nested_size(_nested_of(payload, key, mid, nested_top_n)) <= budget:
                lo = mid
            else:
                hi = mid - 1
        out[key] = items[:lo]
        entry["shown"] = lo
        nested.update(_nested_of(payload, key, lo, nested_top_n))
        if lo == total and key in summary and len(payload[key]) <= top_n:
            summary.pop(key)  # nothing of this list was cut after all
        finish()
        if _size(out) <= max_chars:
            break
    if _size(out) > max_chars:
        # Still too large (very wide rows): keep only the row counts of the summaries.
        for entry in summary.values():
            for detail in ("counts", "range", "sums"):
                entry.pop(detail, None)
                if _size(finish()) <= max_chars:
                    return out
    return finish()


def _nested_of(payload: Mapping[str, Any], key: str, rows: int, limit: int) -> dict[str, dict[str, Any]]:
    """Nested-list counts of the first ``rows`` rows of the top-level list ``key``."""
    out: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(list(payload.get(key) or [])[:rows]):
        for sub, total in _cut_nested(row, limit)[1].items():
            out[f"{key}/{index}/{sub}"] = {"total": total, "shown": limit}
    return out


def _nested_size(entries: Mapping[str, Any]) -> int:
    return sum(_size({k: v}) for k, v in entries.items())


def _drop_nested(nested: dict[str, dict[str, Any]], key: str, keep_rows: int) -> None:
    prefix = f"{key}/"
    for path in [p for p in nested if p.startswith(prefix)]:
        row = path[len(prefix):].split("/", 1)[0]
        if not row.isdigit() or int(row) >= keep_rows:
            del nested[path]


def _meta(tool: BaseTool) -> Mapping[str, Any] | None:
    meta = tool.custom_metadata or {}
    value = meta.get(KEELSON_META)
    return value if isinstance(value, Mapping) else None


class PolicyPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="keelson_policy")

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        run = maybe_current_run()
        agent = tool_context.agent_name
        meta = _meta(tool)
        reason: str | None = None
        if run is None:
            reason = "no run bound"
        elif meta is None:
            reason = "not a Keelson connector/calc tool"
        elif meta.get("access") != "read":
            reason = f"{meta.get('access')} tools are never available to LLM agents"
        elif meta.get("kind") == "quarantine":
            if meta.get("content_trust") != "structured_inference":
                reason = "quarantine wrappers must return structured extractions only"
        elif meta.get("content_trust", "trusted_data") != "trusted_data" or tool.name in UNTRUSTED_SOURCE_TOOLS:
            reason = "untrusted_text tools are not bound to investigators"
        task = current_task()
        kind = meta.get("kind") if meta is not None else None
        source = meta.get("connector") if meta is not None else None
        if reason is not None or run is None:
            pass
        elif tool.name not in run.spec.tool_allowlist(agent):
            reason = f"{tool.name} is not on the allowlist of {agent}"
        elif (
            task is not None and task.tools and kind != "calc" and agent == f"{task.specialist}_investigator"
            and not _task_allows(task.tools, tool.name, meta.get("source") if meta is not None else None)
        ):
            reason = f"{tool.name} is not one of the tools the plan gave task {task.task_id}"
        elif kind in ("registry", "quarantine") and source not in run.spec.connectors_of(agent):
            reason = f"{tool.name} comes from connector {source!r}, not one bound to {agent}"
        if reason is None and run is not None:
            key = f"{agent}:{task.key}" if task is not None else agent
            used = run.tool_calls.get(key, 0)
            cap = _tool_cap(run, agent)
            if used >= cap:
                reason = f"tool call budget of {cap} exhausted for {agent}"
            else:
                run.tool_calls[key] = used + 1
        if reason is None:
            return None
        log.warning("tool call denied", tool=tool.name, agent=agent, reason=reason)
        if run is not None:
            await run.deps.repo.admin_audit(
                tenant=run.tenant,
                actor=f"agent:{agent}",
                action="tool.denied",
                target=tool.name,
                before=None,
                after={"reason": reason, "run_id": run.run_id},
            )
        return {"error": f"{DENIED_PREFIX}: {reason}"}


def _task_allows(task_tools: tuple[str, ...], name: str, source: Any) -> bool:
    """The plan's tool subset names this tool, the source tool of this quarantine wrapper, or its
    wrapper (M7: a custom specialist's untrusted tool is bound as ``<tool>_structured``)."""
    wanted = set(task_tools)
    return name in wanted or (isinstance(source, str) and source in wanted) or f"{name}_structured" in wanted


def _tool_cap(run: Any, agent: str) -> int:
    specialist = run.spec.specialist_of(agent)
    return int(specialist.max_tool_calls) if specialist is not None else 15


FINAL_TURN_NOTE = (
    "Turn budget reached: tool calling is now off. Write your findings now from the tool results "
    "above, one per line with their citations, and list what you could not check."
)
TURN_CAP_TEXT = "The analysis stopped at its turn budget before the analyst wrote notes."
"""Safety net: a model that still asked for tools on its tool-free last turn gets no further turn."""


def _investigator_turn_cap(run: RunContext, agent: str, llm_request: LlmRequest) -> LlmResponse | None:
    """Count the model turns of an investigator task; the last allowed turn has tool calling off
    (``tool_choice=none``; the declarations stay, so the conversation's earlier function calls
    remain valid), a turn beyond the cap is answered without calling the model."""
    task = current_task()
    if task is None or agent != f"{task.specialist}_investigator" or run.spec.specialist_of(agent) is None:
        return None
    key = f"{agent}:{task.key}"
    turn = run.model_turns.get(key, 0) + 1
    run.model_turns[key] = turn
    specialist = run.spec.specialist_of(agent)
    cap = (specialist.max_turns if specialist is not None else None) or run.spec.investigation.max_investigator_turns
    if turn > cap:
        log.warning("investigator turn cap exceeded", agent=agent, task=task.key, cap=cap)
        return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=TURN_CAP_TEXT)]),
                           turn_complete=True)
    if turn == cap:
        llm_request.config.tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.NONE)
        )
        llm_request.append_instructions([FINAL_TURN_NOTE])
    return None


def _eval_budget_breach(run: RunContext) -> str | None:
    """(M8) The live eval run's remaining budget, set on the run at its start: tokens (metered
    after each response) and cost (the LiteLLM callback may report it after the response, so
    it is also checked before the next call)."""
    if run.eval_token_cap is not None and run.total_tokens >= run.eval_token_cap:
        return f"eval run token budget exhausted ({run.total_tokens} of {run.eval_token_cap} tokens left at run start)"
    if run.eval_cost_cap_microusd is not None:
        cost = run.usage.total().cost_usd
        spent = round((cost or 0.0) * 1_000_000)
        if spent >= run.eval_cost_cap_microusd:
            return f"eval run cost budget exhausted ({spent} of {run.eval_cost_cap_microusd} micro-USD left at run start)"
    return None


class BudgetPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="keelson_budget")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        run = maybe_current_run()
        if run is None:
            return None
        run.llm_calls += 1
        budgets = run.spec.budgets
        message: str | None = None
        if run.llm_calls > budgets.max_llm_calls or run.total_tokens > budgets.max_total_tokens:
            message = (
                f"LLM budget exhausted ({run.llm_calls - 1} calls, {run.total_tokens} tokens; "
                f"caps {budgets.max_llm_calls} calls / {budgets.max_total_tokens} tokens)"
            )
        else:
            message = _eval_budget_breach(run)
        if message is not None:
            log.warning("llm budget exceeded", agent=callback_context.agent_name, message=message)
            run.abort("budget", message)
            return LlmResponse(error_code="BUDGET_EXCEEDED", error_message=message)
        return _investigator_turn_cap(run, callback_context.agent_name, llm_request)

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        run = maybe_current_run()
        if run is None or llm_response.partial:
            return None
        usage = llm_response.usage_metadata
        if usage is not None and usage.total_token_count:
            run.total_tokens += int(usage.total_token_count)
        if run.total_tokens > run.spec.budgets.max_total_tokens:
            run.abort(
                "budget",
                f"LLM token budget exhausted ({run.total_tokens} > {run.spec.budgets.max_total_tokens})",
            )
        elif run.eval_token_cap is not None and run.total_tokens > run.eval_token_cap:
            run.abort("budget", f"eval run token budget exhausted ({run.total_tokens} > {run.eval_token_cap})")
        return None


_call_started: ContextVar[float | None] = ContextVar("keelson_llm_call_started", default=None)


class UsagePlugin(BasePlugin):
    """Meters every model call of a run per agent (``RunContext.usage``): tokens from the final
    response's ``usage_metadata`` and the wall time from ``before_model`` to that response. It also
    marks the calling agent (``usage.current_agent``) for the LiteLLM cost callback."""

    def __init__(self) -> None:
        super().__init__(name="keelson_usage")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        if maybe_current_run() is not None:
            current_agent.set(callback_context.agent_name)
            _call_started.set(time.monotonic())
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        run = maybe_current_run()
        if run is None or llm_response.partial:
            return None
        started = _call_started.get()
        latency_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
        _call_started.set(None)
        replayed = bool((llm_response.custom_metadata or {}).get(REPLAY_META_KEY))
        run.usage.record(callback_context.agent_name, llm_response.usage_metadata, latency_ms=latency_ms,
                         replayed=replayed)
        return None


class EvidencePlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="keelson_evidence")

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        run = maybe_current_run()
        meta = _meta(tool)
        if run is None or meta is None or meta.get("evidence") == "self":
            return None
        if isinstance(result, Mapping) and "error" in result and len(result) == 1:
            return None
        payload = dict(result) if isinstance(result, Mapping) else {"result": result}
        payload.pop("_evidence_id", None)
        evidence_id = await run.deps.repo.save_evidence(
            tenant=run.tenant,
            case_id=run.case_id,
            run_id=run.run_id,
            connector=str(meta.get("connector") or "unknown"),
            tool=tool.name,
            args=dict(tool_args),
            result=payload,
            prov="fact",
        )
        entry: dict[str, Any] = {"id": evidence_id, "agent": tool_context.agent_name, "tool": tool.name,
                                 "args": dict(tool_args)}
        lists = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
        if lists:
            entry["lists"] = lists  # row counts for the compact structurer index
        task = current_task()
        if task is not None:
            entry["task"] = task.key  # M4: the structurer's evidence index is per task
        run.evidence_log.append(entry)
        return {**compact_for_model(payload), "_evidence_id": evidence_id}


class AuditPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="keelson_audit")

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        run = maybe_current_run()
        log.info(
            "audit.tool_call",
            agent=tool_context.agent_name,
            tool=tool.name,
            arg_keys=sorted(tool_args),
            run_id=run.run_id if run else None,
            case_id=run.case_id if run else None,
        )
        return None

    async def on_tool_error_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, error: Exception
    ) -> dict[str, Any] | None:
        log.warning("audit.tool_error", agent=tool_context.agent_name, tool=tool.name, error=str(error))
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        if llm_response.partial:
            return None
        run = maybe_current_run()
        usage = llm_response.usage_metadata
        log.info(
            "audit.model_call",
            agent=callback_context.agent_name,
            run_id=run.run_id if run else None,
            tokens=int(usage.total_token_count or 0) if usage else 0,
            error_code=llm_response.error_code,
        )
        return None


def default_plugins() -> list[BasePlugin]:
    """Order matters: deny first, then budget (and the turn cap), usage metering; audit before evidence
    (evidence rewrites results)."""
    return [PolicyPlugin(), BudgetPlugin(), UsagePlugin(), AuditPlugin(), EvidencePlugin()]
