"""AG-UI 1.0 event builders with Keelson ``metadata.sc`` (``30-engine-api-events.md``).

Every builder returns a JSON-ready ``dict`` in AG-UI wire form (camelCase keys,
optional fields omitted) carrying ``metadata.sc`` (:class:`ScMetadata`). When the
optional ``ag-ui-protocol`` package is installed (extra ``agui``), each event is
validated against the official models *and* round-tripped: the event must
re-serialise to exactly the same dict, which catches snake_case keys and
unknown fields (the AG-UI models themselves allow extra fields).

Verified against ``ag-ui-protocol==1.0.0`` (``ag_ui.core``): ``RunStartedEvent``,
``RunFinishedEvent`` (+ ``RunFinishedSuccessOutcome``, ``RunFinishedInterruptOutcome``,
``Interrupt``), ``RunErrorEvent``, ``StepStartedEvent``, ``StepFinishedEvent``,
``SubagentStartedEvent``, ``SubagentFinishedEvent`` (+ ``SubagentFinishedSuccessOutcome``,
``SubagentFinishedSuspendedOutcome``), ``SubagentErrorEvent``, ``ToolCallStartEvent``,
``ToolCallEndEvent``, ``ToolCallResultEvent``, ``ReasoningStartEvent``,
``ReasoningMessageStartEvent``, ``ReasoningMessageContentEvent``,
``ReasoningMessageEndEvent``, ``ReasoningEndEvent``, ``ActivitySnapshotEvent``,
``TextMessageStartEvent``, ``TextMessageContentEvent``, ``TextMessageEndEvent``,
``CustomEvent``. SUBAGENT_* and the interrupt outcome are native in 1.0, so no
``CUSTOM sc.subagent.*`` fallback is needed or emitted.

Notes on 1.0 shapes that differ from the short-hand in the contract:

* ``TOOL_CALL_RESULT`` requires ``messageId`` (defaults to ``"<toolCallId>:result"``)
  and is emitted with ``role: "tool"``;
* ``RUN_STARTED`` carries ``protocolVersion: "1.0"`` (the spec requires producers
  to declare it on the event that opens a run);
* run-scoped events (``RUN_*``) have no ``subagentRunId``; all others accept one.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel

from .common import ContractModel, EvidenceId, NonNegInt, Provenance, TenantSlug
from .runs import APPROACH_DECISIONS, APPROVAL_DECISIONS, MAX_DECISION_REASON_CHARS, MAX_EDIT_IDS, MAX_PAYLOAD_KEYS

__all__ = [
    "ACTIVITY_TYPE_RENDER",
    "AGENT_DISPLAY_NAMES",
    "AGUI_PROTOCOL_VERSION",
    "AGUI_VALIDATION_AVAILABLE",
    "AguiEventError",
    "CASE_UPDATED_EVENT",
    "InterruptSpec",
    "NODE_STAGES",
    "STAGE_LABELS",
    "STAGELESS_NODES",
    "STAGE_ORDER",
    "ScMetadata",
    "Stage",
    "activity_snapshot",
    "approach_response_schema",
    "approval_response_schema",
    "case_updated",
    "custom",
    "reasoning_end",
    "reasoning_message_content",
    "reasoning_message_end",
    "reasoning_message_start",
    "reasoning_start",
    "run_error",
    "run_finished_interrupt",
    "run_finished_success",
    "run_started",
    "sse_frame",
    "stage_for_node",
    "step_finished",
    "step_started",
    "subagent_error",
    "subagent_finished",
    "subagent_started",
    "text_message_content",
    "text_message_end",
    "text_message_start",
    "tool_call_end",
    "tool_call_result",
    "tool_call_start",
    "validate_event",
    "with_seq",
]

logger = logging.getLogger(__name__)

AGUI_PROTOCOL_VERSION: Final = "1.0"
ACTIVITY_TYPE_RENDER: Final = "sc.render"
CASE_UPDATED_EVENT: Final = "sc.case.updated"

AguiEvent = dict[str, Any]


# ──────────────────────────────── stages ────────────────────────────────────


class Stage(StrEnum):
    """Fixed stage vocabulary (``STEP_*.stepName`` and ``metadata.sc.stage``)."""

    UNDERSTANDING = "understanding"
    CHECKING_SYSTEMS = "checking_systems"
    MEASURING_IMPACT = "measuring_impact"
    BUILDING_OPTIONS = "building_options"
    READY_FOR_REVIEW = "ready_for_review"
    PREPARING_CHANGES = "preparing_changes"
    AWAITING_APPROVAL = "awaiting_approval"
    APPLYING = "applying"
    VERIFIED = "verified"


STAGE_LABELS: Final[Mapping[Stage, str]] = {
    Stage.UNDERSTANDING: "Understanding",
    Stage.CHECKING_SYSTEMS: "Checking your systems",
    Stage.MEASURING_IMPACT: "Measuring impact",
    Stage.BUILDING_OPTIONS: "Building options",
    Stage.READY_FOR_REVIEW: "Ready for review",
    Stage.PREPARING_CHANGES: "Preparing changes",
    Stage.AWAITING_APPROVAL: "Awaiting approval",
    Stage.APPLYING: "Applying",
    Stage.VERIFIED: "Verified",
}

STAGE_ORDER: Final[tuple[Stage, ...]] = tuple(Stage)

NODE_STAGES: Final[Mapping[str, Stage]] = {
    "intake": Stage.UNDERSTANDING,
    "frame_case": Stage.UNDERSTANDING,
    "approach_gate": Stage.UNDERSTANDING,
    "approach_router": Stage.UNDERSTANDING,
    "planner": Stage.UNDERSTANDING,
    "followup_delta": Stage.UNDERSTANDING,
    "prepare_resimulate": Stage.UNDERSTANDING,
    "prepare_reinvestigate": Stage.UNDERSTANDING,
    "promote_case": Stage.UNDERSTANDING,
    "confirm_assumptions": Stage.UNDERSTANDING,
    "investigate": Stage.CHECKING_SYSTEMS,
    "verify_numbers": Stage.CHECKING_SYSTEMS,
    "critic": Stage.CHECKING_SYSTEMS,
    "refine_scenario": Stage.CHECKING_SYSTEMS,
    "quick_exposure": Stage.MEASURING_IMPACT,
    "impact": Stage.MEASURING_IMPACT,
    "simulate": Stage.BUILDING_OPTIONS,
    "recommend": Stage.BUILDING_OPTIONS,
    "compose": Stage.READY_FOR_REVIEW,
    "explain": Stage.READY_FOR_REVIEW,
    "compile_changeset": Stage.PREPARING_CHANGES,
    "dry_run": Stage.PREPARING_CHANGES,
    "approval_gate": Stage.AWAITING_APPROVAL,
    "approval_check": Stage.AWAITING_APPROVAL,
    "execute": Stage.APPLYING,
    "reconcile": Stage.VERIFIED,
    # M5 (95 §2-§4)
    "prepare_replan": Stage.BUILDING_OPTIONS,
    "prepare_rollback": Stage.PREPARING_CHANGES,
    "checklist_step": Stage.APPLYING,
}
"""Workflow node → stage (``50-engine-workflow.md`` stage mapping; M4 nodes per ``90`` §2, §5, §6:
planner/followup_delta/prepare_*/promote_case/confirm_assumptions → understanding,
verify_numbers/critic/refine_scenario → checking_systems, explain → ready_for_review; M5 nodes per
``95``: prepare_replan (a lane signal re-simulates the affected options) → building_options,
prepare_rollback (the undo request is prepared for approval) → preparing_changes, checklist_step (a
Guide-mode step done by hand in the target system) → applying)."""

STAGELESS_NODES: Final[frozenset[str]] = frozenset({
    "pre_route", "route_intake", "reject_run", "end_turn", "close_case", "compose_smalltalk",
    "answer_investigator", "answer_structurer", "compose_answer",
})
"""Workflow nodes that deliberately never change the stage: routing / turn-control nodes and the
question-answer path (answering a question does not move the case along). Every other workflow
node has a :data:`NODE_STAGES` entry (tested against the engine's workflow graph)."""

AGENT_DISPLAY_NAMES: Final[Mapping[str, str]] = {
    "intake": "Intake",
    "planner": "Planner",
    "inventory_demand": "Inventory & demand analyst",
    "sourcing": "Sourcing analyst",
    "logistics": "Logistics analyst",
    "playbook": "Playbook analyst",
    "recommender": "Strategist",
    "critic": "Critic",
    "external_intel": "External intelligence",
}
"""The only LLM roles that emit SUBAGENT_* events."""


def stage_for_node(node_name: str) -> Stage | None:
    """Stage for a workflow node name (``None`` for nodes outside the mapping)."""
    return NODE_STAGES.get(node_name)


# ───────────────────────────── metadata.sc ──────────────────────────────────


class ScMetadata(BaseModel):
    """``metadata.sc`` carried by every event (camelCase on the wire)."""

    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        validate_assignment=True,
    )

    tenant_id: TenantSlug
    case_id: Annotated[str, Field(min_length=1)]
    run_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    """The run that emitted the event (optional; set by the engine on every event). Lets a
    client attribute run-level events (e.g. ``RUN_ERROR``, which has no ``runId`` field in
    AG-UI 1.0) and skip a replayed earlier run. Omitted from the wire when unset, so events
    built without it are unchanged (backward compatible)."""
    seq: NonNegInt | None = None
    """``run_event.id``; ``None`` until the event is persisted (see :func:`with_seq`)."""
    stage: Stage
    prov: Provenance
    label: str
    """User-facing one-liner, e.g. "Read 12 open purchase orders from Merxa"."""
    agent: str | None = None
    evidence_refs: list[EvidenceId] = Field(default_factory=list)

    def wire(self) -> dict[str, Any]:
        out = self.model_dump(mode="json", by_alias=True)
        if out.get("runId") is None:
            out.pop("runId", None)  # optional and additive: absent rather than null
        return out


class InterruptSpec(ContractModel):
    """One interrupt reported in ``RUN_FINISHED{outcome: interrupt}``."""

    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )

    id: Annotated[str, Field(min_length=1)]
    reason: Literal["approach", "approval"]
    message: Annotated[str, Field(min_length=1)]
    response_schema: dict[str, Any]


def _decision_schema(decisions: Iterable[str], extra: Mapping[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {"decision": {"type": "string", "enum": sorted(decisions)}}
    props.update(extra)
    return {
        "type": "object",
        "properties": props,
        "required": ["decision"],
        "additionalProperties": False,
    }


def approach_response_schema() -> dict[str, Any]:
    """``responseSchema`` for the approach-card interrupt: ``edits`` is the M1 free text or (M4)
    typed edits ``[{key, value_int | value_ids}]``; ``removed_checks`` are card task ids (M4)."""
    typed_edit = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$", "maxLength": 64},
            "value_int": {"type": ["integer", "null"]},
            "value_ids": {
                "type": ["array", "null"], "items": {"type": "string", "minLength": 1},
                "minItems": 1, "maxItems": MAX_EDIT_IDS, "uniqueItems": True,
            },
        },
        "required": ["key"],
        "additionalProperties": False,
    }
    return _decision_schema(APPROACH_DECISIONS, {
        "edits": {"anyOf": [
            {"type": "string"},
            {"type": "array", "items": typed_edit, "maxItems": MAX_PAYLOAD_KEYS},
        ]},
        "removed_checks": {
            "type": "array", "items": {"type": "string", "minLength": 1},
            "maxItems": MAX_PAYLOAD_KEYS, "uniqueItems": True,
        },
    })


def approval_response_schema() -> dict[str, Any]:
    """``responseSchema`` for the approval-card interrupt (approve needs hash + plan version;
    M5: an optional ``reason`` note with reject/revise)."""
    return _decision_schema(
        APPROVAL_DECISIONS,
        {
            "plan_version": {"type": "integer", "minimum": 1},
            "change_set_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "reason": {"type": "string", "minLength": 1, "maxLength": MAX_DECISION_REASON_CHARS},
        },
    )


# ─────────────────────────────── validation ─────────────────────────────────


class AguiEventError(ValueError):
    """A built event does not conform to the installed AG-UI models."""


try:  # optional dependency (extra "agui")
    from ag_ui.core import Event as _AguiEventUnion

    _EVENT_ADAPTER: TypeAdapter[Any] | None = TypeAdapter(_AguiEventUnion)
except ImportError:  # pragma: no cover - exercised only without the extra
    _EVENT_ADAPTER = None
    logger.debug("ag-ui-protocol not installed; AG-UI events are built but not validated")

AGUI_VALIDATION_AVAILABLE: Final = _EVENT_ADAPTER is not None


def _strict_json(value: Any) -> str:
    """Serialise as RFC 8259 JSON (``NaN``/``Infinity`` are not JSON and break JS clients)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _extras(model: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(model, BaseModel):
        for key in model.model_extra or {}:
            found.append(f"{path}/{key}")
        for name in type(model).model_fields:
            found.extend(_extras(getattr(model, name), f"{path}/{name}"))
    elif isinstance(model, (list, tuple)):
        for i, item in enumerate(model):
            found.extend(_extras(item, f"{path}/{i}"))
    return found


def validate_event(event: Mapping[str, Any]) -> None:
    """Validate a wire event against ``ag_ui.core.Event`` (no-op without the extra).

    Raises :class:`AguiEventError` if the event is not strict JSON (``NaN``,
    ``Infinity``, non-JSON types — checked even without the extra, since the event
    is persisted and streamed as JSON), if validation fails, if the event carries
    fields the protocol does not define, or if it does not round-trip exactly.
    """
    try:
        _strict_json(event)
    except (TypeError, ValueError) as exc:
        raise AguiEventError(f"{event.get('type')}: not strict JSON: {exc}") from exc
    if _EVENT_ADAPTER is None:
        return
    try:
        model = _EVENT_ADAPTER.validate_python(event)
    except ValidationError as exc:
        raise AguiEventError(f"{event.get('type')}: {exc}") from exc
    extras = _extras(model)
    if extras:
        raise AguiEventError(f"{event.get('type')}: fields not in AG-UI 1.0: {extras}")
    roundtrip = model.model_dump(mode="json", by_alias=True)
    if roundtrip != dict(event):
        raise AguiEventError(
            f"{event.get('type')}: event does not round-trip through AG-UI models "
            f"(sent keys {sorted(event)}, canonical keys {sorted(roundtrip)})"
        )


# ──────────────────────────────── builders ──────────────────────────────────


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _event(
    event_type: str,
    sc: ScMetadata,
    fields: Mapping[str, Any],
    *,
    timestamp: int | None,
) -> AguiEvent:
    event: AguiEvent = {"type": event_type}
    event.update({k: v for k, v in fields.items() if v is not None})
    event["timestamp"] = _now_ms() if timestamp is None else timestamp
    event["metadata"] = {"sc": sc.wire()}
    validate_event(event)
    return event


def _non_empty(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def run_started(
    sc: ScMetadata,
    *,
    thread_id: str,
    run_id: str,
    parent_run_id: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``RUN_STARTED {threadId: caseId, runId}``."""
    return _event(
        "RUN_STARTED",
        sc,
        {
            "threadId": _non_empty(thread_id, "thread_id"),
            "runId": _non_empty(run_id, "run_id"),
            "protocolVersion": AGUI_PROTOCOL_VERSION,
            "parentRunId": parent_run_id,
        },
        timestamp=timestamp,
    )


def run_finished_success(
    sc: ScMetadata, *, thread_id: str, run_id: str, timestamp: int | None = None
) -> AguiEvent:
    """``RUN_FINISHED {outcome: {type: "success"}}``."""
    return _event(
        "RUN_FINISHED",
        sc,
        {
            "threadId": _non_empty(thread_id, "thread_id"),
            "runId": _non_empty(run_id, "run_id"),
            "outcome": {"type": "success"},
        },
        timestamp=timestamp,
    )


def run_finished_interrupt(
    sc: ScMetadata,
    *,
    thread_id: str,
    run_id: str,
    interrupts: Iterable[InterruptSpec | Mapping[str, Any]],
    timestamp: int | None = None,
) -> AguiEvent:
    """``RUN_FINISHED {outcome: {type: "interrupt", interrupts: [{id, reason, message, responseSchema}]}}``."""
    specs = [i if isinstance(i, InterruptSpec) else InterruptSpec.model_validate(i) for i in interrupts]
    if not specs:
        raise ValueError("an interrupt outcome needs at least one interrupt")
    return _event(
        "RUN_FINISHED",
        sc,
        {
            "threadId": _non_empty(thread_id, "thread_id"),
            "runId": _non_empty(run_id, "run_id"),
            "outcome": {
                "type": "interrupt",
                "interrupts": [s.model_dump(mode="json", by_alias=True) for s in specs],
            },
        },
        timestamp=timestamp,
    )


def run_error(
    sc: ScMetadata, *, message: str, code: str | None = None, timestamp: int | None = None
) -> AguiEvent:
    """``RUN_ERROR {message, code}`` (``code="CANCELLED"`` for cancellations)."""
    return _event(
        "RUN_ERROR", sc, {"message": _non_empty(message, "message"), "code": code}, timestamp=timestamp
    )


def step_started(
    sc: ScMetadata, *, stage: Stage | None = None, subagent_run_id: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``STEP_STARTED {stepName: <stage>}`` (defaults to ``sc.stage``)."""
    step = Stage(stage) if stage is not None else sc.stage
    return _event(
        "STEP_STARTED",
        sc,
        {"stepName": step.value, "subagentRunId": subagent_run_id},
        timestamp=timestamp,
    )


def step_finished(
    sc: ScMetadata, *, stage: Stage | None = None, subagent_run_id: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``STEP_FINISHED {stepName: <stage>}`` (defaults to ``sc.stage``)."""
    step = Stage(stage) if stage is not None else sc.stage
    return _event(
        "STEP_FINISHED",
        sc,
        {"stepName": step.value, "subagentRunId": subagent_run_id},
        timestamp=timestamp,
    )


def subagent_started(
    sc: ScMetadata,
    *,
    subagent_run_id: str,
    name: str,
    description: str | None = None,
    parent_subagent_run_id: str | None = None,
    timestamp: int | None = None,
    custom: bool = False,
) -> AguiEvent:
    """``SUBAGENT_STARTED {subagentRunId, name: <agent slug>, description: <display name>}``.

    Only the LLM roles in :data:`AGENT_DISPLAY_NAMES` emit SUBAGENT_* (never
    plumbing nodes), so any other slug is rejected. ``description`` defaults to the
    role's display name.

    M7: a tenant **custom specialist** (``110`` §3) passes ``custom=True``; its slug must be a
    valid custom slug that does not shadow a built-in role
    (:func:`keelson_contracts.agents.is_custom_specialist_slug`) and ``description`` (its
    display name, 1..60 chars) is required.
    """
    if custom:
        from .agents import is_custom_specialist_slug

        if not is_custom_specialist_slug(name):
            raise ValueError(f"{name!r} is not a custom specialist slug (or shadows a built-in role)")
        if (
            description is None
            or not 1 <= len(description) <= 60
            or description != description.strip()
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in description)
        ):
            raise ValueError(
                "a custom specialist's SUBAGENT_STARTED needs its display name (one trimmed line, 1..60 chars)"
            )
    elif name not in AGENT_DISPLAY_NAMES:
        raise ValueError(
            f"unknown agent slug {name!r}; only {sorted(AGENT_DISPLAY_NAMES)} emit SUBAGENT_*"
        )
    if description is None:
        description = AGENT_DISPLAY_NAMES[name]
    return _event(
        "SUBAGENT_STARTED",
        sc,
        {
            "subagentRunId": _non_empty(subagent_run_id, "subagent_run_id"),
            "name": name,
            "description": description,
            "parentSubagentRunId": parent_subagent_run_id,
        },
        timestamp=timestamp,
    )


def subagent_finished(
    sc: ScMetadata,
    *,
    subagent_run_id: str,
    suspended_interrupt_ids: list[str] | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``SUBAGENT_FINISHED {subagentRunId, outcome}`` (success, or suspended on interrupts)."""
    outcome: dict[str, Any] = {"type": "success"}
    if suspended_interrupt_ids is not None:
        outcome = {"type": "suspended", "interruptIds": list(suspended_interrupt_ids)}
    return _event(
        "SUBAGENT_FINISHED",
        sc,
        {"subagentRunId": _non_empty(subagent_run_id, "subagent_run_id"), "outcome": outcome},
        timestamp=timestamp,
    )


def subagent_error(
    sc: ScMetadata,
    *,
    subagent_run_id: str,
    message: str,
    code: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``SUBAGENT_ERROR {subagentRunId, message}``."""
    return _event(
        "SUBAGENT_ERROR",
        sc,
        {
            "subagentRunId": _non_empty(subagent_run_id, "subagent_run_id"),
            "message": _non_empty(message, "message"),
            "code": code,
        },
        timestamp=timestamp,
    )


def tool_call_start(
    sc: ScMetadata,
    *,
    tool_call_id: str,
    tool_call_name: str,
    parent_message_id: str | None = None,
    subagent_run_id: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``TOOL_CALL_START {toolCallId, toolCallName, parentMessageId?}``."""
    return _event(
        "TOOL_CALL_START",
        sc,
        {
            "toolCallId": _non_empty(tool_call_id, "tool_call_id"),
            "toolCallName": _non_empty(tool_call_name, "tool_call_name"),
            "parentMessageId": parent_message_id,
            "subagentRunId": subagent_run_id,
        },
        timestamp=timestamp,
    )


def tool_call_end(
    sc: ScMetadata,
    *,
    tool_call_id: str,
    subagent_run_id: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``TOOL_CALL_END {toolCallId}``."""
    return _event(
        "TOOL_CALL_END",
        sc,
        {"toolCallId": _non_empty(tool_call_id, "tool_call_id"), "subagentRunId": subagent_run_id},
        timestamp=timestamp,
    )


def tool_call_result(
    sc: ScMetadata,
    *,
    tool_call_id: str,
    content: str,
    message_id: str | None = None,
    subagent_run_id: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``TOOL_CALL_RESULT {messageId, toolCallId, content, role: "tool"}``.

    ``content`` must be a short humanised summary, never the raw tool payload.
    """
    return _event(
        "TOOL_CALL_RESULT",
        sc,
        {
            "messageId": message_id or f"{_non_empty(tool_call_id, 'tool_call_id')}:result",
            "toolCallId": tool_call_id,
            "content": content,
            "role": "tool",
            "subagentRunId": subagent_run_id,
        },
        timestamp=timestamp,
    )


def _reasoning(
    event_type: str,
    sc: ScMetadata,
    message_id: str,
    subagent_run_id: str | None,
    timestamp: int | None,
    **fields: Any,
) -> AguiEvent:
    return _event(
        event_type,
        sc,
        {"messageId": _non_empty(message_id, "message_id"), "subagentRunId": subagent_run_id, **fields},
        timestamp=timestamp,
    )


def reasoning_start(
    sc: ScMetadata, *, message_id: str, subagent_run_id: str | None = None, timestamp: int | None = None
) -> AguiEvent:
    return _reasoning("REASONING_START", sc, message_id, subagent_run_id, timestamp)


def reasoning_message_start(
    sc: ScMetadata, *, message_id: str, subagent_run_id: str | None = None, timestamp: int | None = None
) -> AguiEvent:
    return _reasoning(
        "REASONING_MESSAGE_START", sc, message_id, subagent_run_id, timestamp, role="reasoning"
    )


def reasoning_message_content(
    sc: ScMetadata,
    *,
    message_id: str,
    delta: str,
    subagent_run_id: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    return _reasoning(
        "REASONING_MESSAGE_CONTENT", sc, message_id, subagent_run_id, timestamp,
        delta=_non_empty(delta, "delta"),
    )


def reasoning_message_end(
    sc: ScMetadata, *, message_id: str, subagent_run_id: str | None = None, timestamp: int | None = None
) -> AguiEvent:
    return _reasoning("REASONING_MESSAGE_END", sc, message_id, subagent_run_id, timestamp)


def reasoning_end(
    sc: ScMetadata, *, message_id: str, subagent_run_id: str | None = None, timestamp: int | None = None
) -> AguiEvent:
    return _reasoning("REASONING_END", sc, message_id, subagent_run_id, timestamp)


def activity_snapshot(
    sc: ScMetadata,
    *,
    block: BaseModel | Mapping[str, Any],
    message_id: str | None = None,
    timestamp: int | None = None,
) -> AguiEvent:
    """``ACTIVITY_SNAPSHOT {messageId, activityType: "sc.render", content: <block envelope>, replace: true}``.

    ``block`` is a block envelope model (``keelson_contracts.blocks``) or its wire
    dict; it is validated as a block. ``messageId`` defaults to the block id so a
    re-emitted block replaces the earlier snapshot.
    """
    from .blocks import block_to_wire, parse_block  # local import keeps agui import-light

    model = parse_block(block.model_dump(mode="json") if isinstance(block, BaseModel) else block)
    content = block_to_wire(model)
    return _event(
        "ACTIVITY_SNAPSHOT",
        sc,
        {
            "messageId": message_id or content["id"],
            "activityType": ACTIVITY_TYPE_RENDER,
            "content": content,
            "replace": True,
        },
        timestamp=timestamp,
    )


def text_message_start(
    sc: ScMetadata, *, message_id: str, timestamp: int | None = None
) -> AguiEvent:
    """``TEXT_MESSAGE_START {messageId, role: "assistant"}`` (only from ``compose``)."""
    return _event(
        "TEXT_MESSAGE_START",
        sc,
        {"messageId": _non_empty(message_id, "message_id"), "role": "assistant"},
        timestamp=timestamp,
    )


def text_message_content(
    sc: ScMetadata, *, message_id: str, delta: str, timestamp: int | None = None
) -> AguiEvent:
    """``TEXT_MESSAGE_CONTENT {messageId, delta}`` (``delta`` must be non-empty)."""
    return _event(
        "TEXT_MESSAGE_CONTENT",
        sc,
        {"messageId": _non_empty(message_id, "message_id"), "delta": _non_empty(delta, "delta")},
        timestamp=timestamp,
    )


def text_message_end(sc: ScMetadata, *, message_id: str, timestamp: int | None = None) -> AguiEvent:
    return _event(
        "TEXT_MESSAGE_END", sc, {"messageId": _non_empty(message_id, "message_id")}, timestamp=timestamp
    )


def custom(sc: ScMetadata, *, name: str, value: Any, timestamp: int | None = None) -> AguiEvent:
    """``CUSTOM {name, value}``; ``value`` must be strict JSON (no ``NaN``/``Infinity``)."""
    _strict_json(value)  # fail fast (TypeError/ValueError) on non-JSON values
    event: AguiEvent = {
        "type": "CUSTOM",
        "name": _non_empty(name, "name"),
        "value": value,
        "timestamp": _now_ms() if timestamp is None else timestamp,
        "metadata": {"sc": sc.wire()},
    }
    validate_event(event)
    return event


def case_updated(
    sc: ScMetadata,
    *,
    mode: Literal["actual", "what_if"],
    status: str,
    title: str,
    timestamp: int | None = None,
) -> AguiEvent:
    """``CUSTOM {name: "sc.case.updated", value: {mode, status, title}}``."""
    return custom(
        sc,
        name=CASE_UPDATED_EVENT,
        value={"mode": mode, "status": status, "title": title},
        timestamp=timestamp,
    )


# ──────────────────────────────── transport ─────────────────────────────────


def with_seq(event: Mapping[str, Any], seq: int) -> AguiEvent:
    """Copy of ``event`` with ``metadata.sc.seq`` set (after ``run_event`` insert)."""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError("seq must be a non-negative int")
    out = copy.deepcopy(dict(event))
    try:
        out["metadata"]["sc"]["seq"] = seq
    except (KeyError, TypeError) as exc:
        raise ValueError("event has no metadata.sc") from exc
    return out


def sse_frame(seq: int, event: Mapping[str, Any]) -> str:
    """One SSE message: ``id: <seq>\\nevent: message\\ndata: <json>\\n\\n``.

    The JSON is single-line by construction (``json.dumps`` escapes CR/LF inside
    strings) and strict (``NaN`` raises ``ValueError``).
    """
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError("seq must be a non-negative int")
    return f"id: {seq}\nevent: message\ndata: {_strict_json(event)}\n\n"
