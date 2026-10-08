"""ADK events → AG-UI 1.0 events (``30-engine-api-events.md`` mapping), persisted to
``run_event`` and streamed to ``keelson:case:<id>`` through the EventBus.

Rules implemented here:

* ``STEP_STARTED``/``STEP_FINISHED`` follow ``stage_for_node`` of the top-level node.
* ``SUBAGENT_*`` only for the LLM roles in ``AGENT_DISPLAY_NAMES``: the specialist wrapper's
  start marker, or the first event under a static LLM node (``intake``, ``recommend``);
  ``subagentRunId = <invocation_id>:<node path>``; finished on that path's output, error on an
  ``error_code`` event under it.
* ``TOOL_CALL_*`` for tool calls under a subagent: ``TOOL_CALL_START`` carries the running
  sentence ("Reading open purchase orders from Merxa"), ``TOOL_CALL_RESULT`` the done or error
  sentence (:mod:`runtime.activity_labels`), with the vendor of the connector the subagent reads.
* ``SUBAGENT_STARTED.description`` is the spec's display name for the role (M2 override); M7
  tenant custom specialists (``custom_agents``) open their row with ``custom=True`` and their
  admin-chosen display name.
* Reasoning (thought parts): deltas live-only, one aggregate persisted (coalescer).
* LLM text partials are **never** ``TEXT_MESSAGE``; only ``sc_text`` events from composing
  nodes are.
* ``adk_request_input`` → interrupt; ``finish`` creates the interrupt rows and emits
  ``RUN_FINISHED{outcome: interrupt}``.
* Replayed outputs (fast-forwarded nodes on resume) are de-duplicated on
  ``(invocation_id, path)``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog
from google.adk.events.event import Event
from keelson_contracts import agui
from keelson_contracts.agui import (
    AGENT_DISPLAY_NAMES,
    STAGE_LABELS,
    InterruptSpec,
    ScMetadata,
    Stage,
    stage_for_node,
)
from keelson_contracts.common import Provenance, is_evidence_id

from ..public_errors import GENERIC_SUBAGENT_FAILURE, report_exception, safe_code, user_message
from .activity_labels import activity_labels, humanise_tool
from .deps import BusPort, InterruptsPort
from .quarantine import QUARANTINE_NODE_NAMES

__all__ = [
    "PendingInterrupt",
    "Translator",
    "humanise_tool",
    "node_name_of",
    "seen_output_keys",
    "unresolved_request_inputs",
]

log = structlog.get_logger(__name__)

REQUEST_INPUT = "adk_request_input"
STATIC_LLM_ROLES = {"intake": "intake", "planner": "planner", "recommend": "recommender", "critic": "critic"}
"""Top-level nodes whose LLM agent is announced as a SUBAGENT row (M4: the ``planner`` and ``critic``
function nodes run their agent as a child; a deterministic plan without an LLM call has no row)."""
CHILD_LLM_ROLES = frozenset({"planner"})
"""Static roles whose row opens only on an event of the child agent (path deeper than the node):
the planner node often decides without a model call (single specialist, Edit reuse)."""


def _with_error_id(event: dict[str, Any], error_id: str | None) -> dict[str, Any]:
    """Add ``metadata.errorId`` (AG-UI allows free-form ``metadata``; unknown fields live there)."""
    if error_id:
        event["metadata"]["errorId"] = error_id
        agui.validate_event(event)
    return event


def _segments(path: str) -> list[str]:
    return [s for s in (path or "").split("/") if s]


def node_name_of(segment: str) -> str:
    return segment.split("@", 1)[0]


def _top_node(path: str) -> str | None:
    segs = _segments(path)
    return node_name_of(segs[1]) if len(segs) >= 2 else None


def seen_output_keys(events: Iterable[Event]) -> set[tuple[str, str]]:
    """``(invocation_id, path)`` of every output already in the session (replay dedupe)."""
    return {
        (e.invocation_id, e.node_info.path)
        for e in events
        if e.output is not None and e.node_info is not None and e.node_info.path
    }


def unresolved_request_inputs(events: Iterable[Event]) -> list[PendingInterrupt]:
    """``adk_request_input`` calls in the session that no function response answered yet.

    ADK matches request-input calls and responses *by count* per interrupt id, so an id is
    open while it has more calls than responses. Used after a crash-retry: the paused node's
    request may already be in the session although its interrupt row was never created."""
    calls: dict[str, int] = {}
    answers: dict[str, int] = {}
    latest: dict[str, tuple[dict[str, Any], str]] = {}
    for event in events:
        if event.content is None:
            continue
        path = event.node_info.path if event.node_info is not None else ""
        for part in event.content.parts or []:
            fc = part.function_call
            if fc is not None and fc.name == REQUEST_INPUT and fc.id:
                calls[fc.id] = calls.get(fc.id, 0) + 1
                latest[fc.id] = (dict(fc.args or {}), path or "")
            fr = part.function_response
            if fr is not None and fr.name == REQUEST_INPUT and fr.id:
                answers[fr.id] = answers.get(fr.id, 0) + 1
    out: list[PendingInterrupt] = []
    for interrupt_id, count in calls.items():
        if count > answers.get(interrupt_id, 0):
            args, path = latest[interrupt_id]
            out.append(
                PendingInterrupt(
                    interrupt_id=interrupt_id,
                    message=str(args.get("message") or "Input needed"),
                    payload=dict(args.get("payload") or {}),
                    path=path,
                )
            )
    return out


@dataclass
class PendingInterrupt:
    interrupt_id: str
    message: str
    payload: dict[str, Any]
    path: str


@dataclass
class _Subagent:
    run_id: str
    slug: str
    path: str


@dataclass
class _Reasoning:
    message_id: str
    sc: ScMetadata
    subagent_run_id: str | None
    parts: list[str] = field(default_factory=list)


class Translator:
    def __init__(
        self,
        *,
        case_id: str,
        run_id: str,
        tenant: str,
        bus: BusPort,
        interrupts: InterruptsPort | None = None,
        vendor: str = "Merxa",
        seen: set[tuple[str, str]] | None = None,
        reasoning_sink: Any | None = None,
        vendors: Mapping[str, str] | None = None,
        display_names: Mapping[str, str] | None = None,
        custom_agents: Iterable[str] = (),
    ) -> None:
        """``vendors``: agent slug → vendor label of the connector its tools read (default
        ``vendor``); ``display_names``: agent slug → display name (spec overrides);
        ``custom_agents``: M7 tenant custom specialist slugs (``SUBAGENT_STARTED`` with
        ``custom=True`` and their display name from ``display_names``)."""
        self.case_id = case_id
        self.run_id = run_id
        self.tenant = tenant
        self._bus = bus
        self._interrupts = interrupts
        self._vendor = vendor
        self._vendors = dict(vendors or {})
        self._display_names = dict(display_names or {})
        self._custom = frozenset(c for c in custom_agents if c in self._display_names)
        self._seen = set(seen or ())
        self._stage: Stage | None = None
        self._subagents: dict[str, _Subagent] = {}
        self._closed_subagents: set[str] = set()
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._reasoning: dict[str, _Reasoning] = {}
        self._coalescer = reasoning_sink
        self.pending_interrupts: list[PendingInterrupt] = []
        self.invocation_id: str | None = None
        self.emitted: list[dict[str, Any]] = []
        self.usage_provider: Callable[[], Mapping[str, Any]] | None = None
        """Token diet: the run's LLM usage summary (``RunContext.usage``), added to the final event as
        ``metadata.usage`` (``RUN_FINISHED`` / ``RUN_ERROR``; persisted with the run's events)."""

    # ─────────────────────────────── plumbing ────────────────────────────────

    def _sc(
        self,
        *,
        prov: Provenance | str,
        label: str,
        agent: str | None = None,
        evidence_refs: Iterable[str] = (),
        stage: Stage | None = None,
    ) -> ScMetadata:
        refs = [r for r in dict.fromkeys(evidence_refs) if is_evidence_id(r)]
        return ScMetadata(
            tenant_id=self.tenant,
            case_id=self.case_id,
            run_id=self.run_id,
            stage=stage or self._stage or Stage.UNDERSTANDING,
            prov=Provenance(prov),
            label=label,
            agent=agent,
            evidence_refs=refs,
        )

    async def _emit(self, event: dict[str, Any], *, persist: bool = True, live: bool = True) -> None:
        etype = event.get("type")
        if self._coalescer is not None and etype in ("REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_END"):
            await self._coalescer.append(tenant=self.tenant, case_id=self.case_id, run_id=self.run_id, event=event)
        else:
            await self._bus.append(
                tenant=self.tenant, case_id=self.case_id, run_id=self.run_id, event=event, persist=persist, live=live
            )
        self.emitted.append(copy.deepcopy(event) | {"_persist": persist, "_live": live})

    # ───────────────────────────────── stages ────────────────────────────────

    async def _enter_stage(self, stage: Stage) -> None:
        if stage == self._stage:
            return
        if self._stage is not None:
            await self._emit(agui.step_finished(self._sc(prov="fact", label=STAGE_LABELS[self._stage])))
        self._stage = stage
        await self._emit(agui.step_started(self._sc(prov="fact", label=STAGE_LABELS[stage], stage=stage)))

    # ─────────────────────────────── lifecycle ───────────────────────────────

    async def start(self, initial_stage: Stage | None = None) -> None:
        await self._emit(
            agui.run_started(
                self._sc(prov="fact", label="Run started", stage=initial_stage),
                thread_id=self.case_id,
                run_id=self.run_id,
            )
        )
        if initial_stage is not None:
            await self._enter_stage(initial_stage)

    def _subagent_for(self, path: str) -> _Subagent | None:
        best: _Subagent | None = None
        for sub in self._subagents.values():
            if sub.run_id in self._closed_subagents:
                continue
            if path == sub.path or path.startswith(sub.path + "/"):
                if best is None or len(sub.path) > len(best.path):
                    best = sub
        return best

    async def _open_subagent(self, slug: str, path: str, invocation_id: str) -> _Subagent:
        run_id = f"{invocation_id}:{path}"
        existing = self._subagents.get(run_id)
        if existing is not None:
            return existing
        sub = _Subagent(run_id=run_id, slug=slug, path=path)
        self._subagents[run_id] = sub
        display = self._display(slug)
        await self._emit(
            agui.subagent_started(
                self._sc(prov="inference", label=f"{display} started", agent=slug),
                subagent_run_id=run_id,
                name=slug,
                description=display,
                custom=slug in self._custom,
            )
        )
        return sub

    def _display(self, slug: str) -> str:
        return self._display_names.get(slug) or AGENT_DISPLAY_NAMES[slug]

    def _vendor_for(self, agent: str | None) -> str:
        return self._vendors.get(agent or "", self._vendor)

    async def _close_subagent(
        self, sub: _Subagent, *, error: str | None = None, code: str | None = None, error_id: str | None = None
    ) -> None:
        """Finish a subagent; ``error`` is a user-facing message (never exception text)."""
        if sub.run_id in self._closed_subagents:
            return
        for key in [k for k, r in self._reasoning.items() if r.subagent_run_id == sub.run_id]:
            await self._end_reasoning(key)
        self._closed_subagents.add(sub.run_id)
        if error is None:
            await self._emit(
                agui.subagent_finished(
                    self._sc(prov="inference", label=f"{self._display(sub.slug)} finished", agent=sub.slug),
                    subagent_run_id=sub.run_id,
                )
            )
        else:
            event = agui.subagent_error(
                self._sc(prov="inference", label=f"{self._display(sub.slug)} failed", agent=sub.slug),
                subagent_run_id=sub.run_id,
                message=error[:500] or GENERIC_SUBAGENT_FAILURE,
                code=code,
            )
            await self._emit(_with_error_id(event, error_id))

    def _with_usage(self, event: dict[str, Any]) -> dict[str, Any]:
        """``metadata.usage`` = the run's LLM usage summary (AG-UI ``metadata`` is free-form)."""
        if self.usage_provider is None:
            return event
        try:
            usage = dict(self.usage_provider())
        except Exception as exc:  # noqa: BLE001 - metering never blocks the final event
            log.warning("usage summary unavailable", error=type(exc).__name__)
            return event
        event["metadata"]["usage"] = usage
        agui.validate_event(event)
        return event

    # ──────────────────────────────── handle ─────────────────────────────────

    async def handle(self, event: Event) -> None:
        actions = event.actions
        if actions is not None and (actions.agent_state is not None or actions.end_of_agent):
            return
        path = event.node_info.path if event.node_info is not None else ""
        if not path:
            return
        invocation_id = event.invocation_id
        self.invocation_id = self.invocation_id or invocation_id
        if event.output is not None and (invocation_id, path) in self._seen:
            return  # replayed fast-forward output (resume / crash-retry)
        if any(node_name_of(seg) in QUARANTINE_NODE_NAMES for seg in _segments(path)):
            # guard / extractor of a quarantine wrapper: their inputs, reasoning and outputs quote
            # untrusted text — never streamed or persisted as run events (the wrapper audits flags)
            return
        top = _top_node(path)
        stage = stage_for_node(top) if top else None
        if stage is not None:
            await self._enter_stage(stage)
        meta = event.custom_metadata or {}

        if meta.get("lifecycle") == "agent_started" and (
            meta.get("agent") in AGENT_DISPLAY_NAMES or meta.get("agent") in self._custom
        ):
            await self._open_subagent(str(meta["agent"]), path, invocation_id)
            return

        segs = _segments(path)
        if top in STATIC_LLM_ROLES and len(segs) >= (3 if top in CHILD_LLM_ROLES else 2):
            static_path = "/".join(segs[:2])
            if f"{invocation_id}:{static_path}" not in self._closed_subagents:
                await self._open_subagent(STATIC_LLM_ROLES[top], static_path, invocation_id)
        sub = self._subagent_for(path)

        if "sc_block" in meta:
            block = meta["sc_block"]
            await self._emit(
                agui.activity_snapshot(
                    self._sc(prov=block["provenance"]["kind"], label=str(block.get("title") or block["kind"])),
                    block=block,
                )
            )
            return
        if "sc_text" in meta:
            await self._text(meta["sc_text"])
            return
        if "sc_case" in meta:
            case = meta["sc_case"]
            await self._emit(
                agui.case_updated(
                    self._sc(prov="decision", label="Case updated"),
                    mode=case["mode"], status=str(case["status"]), title=str(case.get("title") or ""),
                )
            )
            return
        if "sc_progress" in meta:
            # M1 emits only the AG-UI events listed in the contract; function-node progress
            # is kept in the structured log (trace id) rather than invented as a CUSTOM event.
            log.info("run progress", case_id=self.case_id, run_id=self.run_id, label=meta["sc_progress"].get("label"))
            return

        if event.error_code:
            if sub is not None and (path == sub.path or path.startswith(sub.path + "/")):
                if path == sub.path or not event.content:
                    # The ADK error text can quote the model/provider error verbatim: log it
                    # with an error id and show only a generic message + that id.
                    error_id = report_exception(
                        log, "subagent failed", error_id=None, run_id=self.run_id, case_id=self.case_id,
                        agent=sub.slug, error_code=str(event.error_code), error_message=event.error_message,
                    )
                    await self._close_subagent(
                        sub, error=user_message(error_id, GENERIC_SUBAGENT_FAILURE),
                        code=safe_code(event.error_code, default="SUBAGENT_FAILED"), error_id=error_id,
                    )
            return

        if event.content is not None and event.content.parts:
            await self._content(event, sub, path)

        if event.output is not None:
            self._seen.add((invocation_id, path))
            if sub is not None and path == sub.path:
                await self._close_subagent(sub)

    async def _content(self, event: Event, sub: _Subagent | None, path: str) -> None:
        sub_run_id = sub.run_id if sub is not None else None
        agent = sub.slug if sub is not None else None
        reasoning_key = f"{event.invocation_id}:{path}"
        for part in event.content.parts or []:
            if part.thought and part.text:
                if event.partial:
                    await self._reason_delta(reasoning_key, part.text, sub_run_id, agent)
                elif reasoning_key not in self._reasoning:
                    await self._reason_delta(reasoning_key, part.text, sub_run_id, agent)
                    await self._end_reasoning(reasoning_key)
                else:
                    await self._end_reasoning(reasoning_key)
                continue
            if event.partial:
                continue  # LLM text partials are never TEXT_MESSAGE
            if part.function_call is not None:
                fc = part.function_call
                if fc.name == REQUEST_INPUT:
                    args = dict(fc.args or {})
                    self.pending_interrupts.append(
                        PendingInterrupt(
                            interrupt_id=str(fc.id or args.get("interruptId") or args.get("interrupt_id")),
                            message=str(args.get("message") or "Input needed"),
                            payload=dict(args.get("payload") or {}),
                            path=path,
                        )
                    )
                    continue
                if sub is None or not fc.id:
                    continue
                self._tool_calls[fc.id] = {"name": fc.name, "args": dict(fc.args or {})}
                label = activity_labels(str(fc.name), dict(fc.args or {}), None, self._vendor_for(agent)).running
                sc = self._sc(prov="fact", label=label, agent=agent)
                await self._emit(
                    agui.tool_call_start(sc, tool_call_id=fc.id, tool_call_name=str(fc.name), subagent_run_id=sub_run_id)
                )
                await self._emit(agui.tool_call_end(sc, tool_call_id=fc.id, subagent_run_id=sub_run_id))
            elif part.function_response is not None:
                fr = part.function_response
                call = self._tool_calls.pop(fr.id or "", None)
                if call is None or sub is None:
                    continue
                response = dict(fr.response or {})
                label = humanise_tool(str(call["name"]), call["args"], response, self._vendor_for(agent))
                refs = [response["_evidence_id"]] if isinstance(response.get("_evidence_id"), str) else []
                if isinstance(response.get("calc_id"), str):
                    refs.append(response["calc_id"])
                await self._emit(
                    agui.tool_call_result(
                        self._sc(prov="fact", label=label, agent=agent, evidence_refs=refs),
                        tool_call_id=str(fr.id),
                        content=label,
                        subagent_run_id=sub_run_id,
                    )
                )
        if not event.partial and reasoning_key in self._reasoning:
            await self._end_reasoning(reasoning_key)

    async def _reason_delta(self, key: str, text: str, sub_run_id: str | None, agent: str | None) -> None:
        state = self._reasoning.get(key)
        if state is None:
            message_id = f"rsn_{self.run_id}_{len(self.emitted)}"  # unique per run; stable across processes
            sc = self._sc(prov="inference", label="Thinking", agent=agent)
            state = _Reasoning(message_id=message_id, sc=sc, subagent_run_id=sub_run_id)
            self._reasoning[key] = state
            await self._emit(agui.reasoning_start(sc, message_id=message_id, subagent_run_id=sub_run_id))
            await self._emit(agui.reasoning_message_start(sc, message_id=message_id, subagent_run_id=sub_run_id))
        state.parts.append(text)
        event = agui.reasoning_message_content(
            state.sc, message_id=state.message_id, delta=text, subagent_run_id=sub_run_id
        )
        if self._coalescer is not None:
            await self._emit(event)
        else:
            await self._emit(event, persist=False, live=True)

    async def _end_reasoning(self, key: str) -> None:
        state = self._reasoning.pop(key, None)
        if state is None:
            return
        if self._coalescer is None and state.parts:
            await self._emit(
                agui.reasoning_message_content(
                    state.sc, message_id=state.message_id, delta="".join(state.parts),
                    subagent_run_id=state.subagent_run_id,
                ),
                persist=True,
                live=False,
            )
        await self._emit(
            agui.reasoning_message_end(state.sc, message_id=state.message_id, subagent_run_id=state.subagent_run_id)
        )
        await self._emit(agui.reasoning_end(state.sc, message_id=state.message_id, subagent_run_id=state.subagent_run_id))

    async def _text(self, data: Mapping[str, Any]) -> None:
        message_id = str(data["message_id"])
        text = str(data.get("text") or "")
        if not text:
            return
        sc = self._sc(prov="inference", label="Answer")
        await self._emit(agui.text_message_start(sc, message_id=message_id))
        await self._emit(agui.text_message_content(sc, message_id=message_id, delta=text))
        await self._emit(agui.text_message_end(sc, message_id=message_id))

    def adopt_pending(self, pending: Iterable[PendingInterrupt]) -> int:
        """Add interrupts that are open in the session but were not seen in this run's events
        (crash between the paused node's request and its interrupt row). Returns how many."""
        known = {p.interrupt_id for p in self.pending_interrupts}
        added = 0
        for item in pending:
            if item.interrupt_id not in known:
                self.pending_interrupts.append(item)
                known.add(item.interrupt_id)
                added += 1
        return added

    # ───────────────────────────────── finish ────────────────────────────────

    async def _close_open(self, *, error: str | None = None, error_id: str | None = None) -> None:
        for key in list(self._reasoning):
            await self._end_reasoning(key)
        for sub in list(self._subagents.values()):
            if sub.run_id not in self._closed_subagents:
                await self._close_subagent(sub, error=error or "Stopped before it finished.", error_id=error_id)

    async def finish_success(self) -> None:
        await self._close_open()
        if self._stage is not None:
            await self._emit(agui.step_finished(self._sc(prov="fact", label=STAGE_LABELS[self._stage])))
        await self._emit(self._with_usage(
            agui.run_finished_success(
                self._sc(prov="fact", label="Run finished"), thread_id=self.case_id, run_id=self.run_id
            )
        ))

    async def finish_interrupted(self, *, invocation_id: str, session_id: str) -> None:
        """Create the interrupt rows, then emit ``RUN_FINISHED{outcome: interrupt}``."""
        await self._close_open()
        specs: list[InterruptSpec] = []
        for pending in self.pending_interrupts:
            payload = dict(pending.payload)
            kind = "approval" if str(payload.get("kind")) == "approval" else "approach"
            response_schema = payload.pop("response_schema", None) or (
                agui.approval_response_schema() if kind == "approval" else agui.approach_response_schema()
            )
            quorum = dict(payload.get("quorum_spec") or {})
            if self._interrupts is not None:
                await self._interrupts.create_interrupt(
                    tenant=self.tenant,
                    case_id=self.case_id,
                    run_id=self.run_id,
                    kind=kind,  # type: ignore[arg-type]
                    interrupt_id=pending.interrupt_id,
                    payload={**payload, "message": pending.message, "session_id": session_id,
                             "invocation_id": invocation_id, "response_schema": response_schema},
                    quorum_spec=quorum,
                    plan_version=payload.get("plan_version"),
                    change_set_hash=payload.get("change_set_hash"),
                    invocation_id=invocation_id,
                    session_id=session_id,
                    message=pending.message,
                )
            specs.append(
                InterruptSpec(id=pending.interrupt_id, reason=kind, message=pending.message, response_schema=response_schema)
            )
        if self._stage is not None:
            await self._emit(agui.step_finished(self._sc(prov="decision", label=STAGE_LABELS[self._stage])))
        await self._emit(self._with_usage(
            agui.run_finished_interrupt(
                self._sc(prov="decision", label="Waiting for your decision"),
                thread_id=self.case_id,
                run_id=self.run_id,
                interrupts=specs,
            )
        ))

    async def finish_error(self, message: str, code: str | None = None, *, error_id: str | None = None) -> None:
        """``RUN_ERROR``. ``message`` must be user-facing (see ``keelson_engine.public_errors``);
        ``error_id`` (failures, not cancellations) is also set as ``metadata.errorId``."""
        await self._close_open(error="Stopped because the run ended early.", error_id=error_id)
        event = agui.run_error(self._sc(prov="fact", label="Run failed"), message=message[:1000] or "error", code=code)
        await self._emit(self._with_usage(_with_error_id(event, error_id)))
