"""AG-UI builders produce events valid under ag-ui-protocol 1.0.0 (and round-trip exactly)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from examples import envelope
from keelson_contracts import agui
from keelson_contracts.agui import STAGE_LABELS, AguiEventError, InterruptSpec, ScMetadata, Stage
from keelson_contracts.blocks import parse_block

ag_ui_core = pytest.importorskip("ag_ui.core")
EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(ag_ui_core.Event)

TS = 1_791_190_800_000


def sc(**kw: Any) -> ScMetadata:
    data: dict[str, Any] = {
        "tenantId": "demo-retail",
        "caseId": "case-1",
        "stage": "checking_systems",
        "prov": "fact",
        "label": "Read 12 open purchase orders from Merxa",
        "agent": "inventory_demand",
        "evidenceRefs": ["ev_0192f0c0a1b27c3d8e9f0a1b2c3d4e5f"],
    }
    data.update(kw)
    return ScMetadata.model_validate(data)


INTERRUPT = InterruptSpec(
    id="approach-case-1-1",
    reason="approach",
    message="Confirm how we read this disruption",
    response_schema=agui.approach_response_schema(),
)

BUILDERS: dict[str, tuple[str, Callable[[ScMetadata], dict[str, Any]], str]] = {
    "run_started": ("RUN_STARTED", lambda s: agui.run_started(s, thread_id="case-1", run_id="run-1", timestamp=TS), "RunStartedEvent"),
    "run_finished_success": ("RUN_FINISHED", lambda s: agui.run_finished_success(s, thread_id="case-1", run_id="run-1", timestamp=TS), "RunFinishedEvent"),
    "run_finished_interrupt": ("RUN_FINISHED", lambda s: agui.run_finished_interrupt(s, thread_id="case-1", run_id="run-1", interrupts=[INTERRUPT], timestamp=TS), "RunFinishedEvent"),
    "run_error": ("RUN_ERROR", lambda s: agui.run_error(s, message="Cancelled by user", code="CANCELLED", timestamp=TS), "RunErrorEvent"),
    "step_started": ("STEP_STARTED", lambda s: agui.step_started(s, timestamp=TS), "StepStartedEvent"),
    "step_finished": ("STEP_FINISHED", lambda s: agui.step_finished(s, stage=Stage.MEASURING_IMPACT, timestamp=TS), "StepFinishedEvent"),
    "subagent_started": ("SUBAGENT_STARTED", lambda s: agui.subagent_started(s, subagent_run_id="inv-1:wf/investigate/inventory_demand", name="inventory_demand", timestamp=TS), "SubagentStartedEvent"),
    "subagent_finished": ("SUBAGENT_FINISHED", lambda s: agui.subagent_finished(s, subagent_run_id="inv-1:wf/x", timestamp=TS), "SubagentFinishedEvent"),
    "subagent_suspended": ("SUBAGENT_FINISHED", lambda s: agui.subagent_finished(s, subagent_run_id="inv-1:wf/x", suspended_interrupt_ids=["approval-cs-1"], timestamp=TS), "SubagentFinishedEvent"),
    "subagent_error": ("SUBAGENT_ERROR", lambda s: agui.subagent_error(s, subagent_run_id="inv-1:wf/x", message="Tool timed out", timestamp=TS), "SubagentErrorEvent"),
    "tool_call_start": ("TOOL_CALL_START", lambda s: agui.tool_call_start(s, tool_call_id="tc-1", tool_call_name="list_purchase_orders", subagent_run_id="inv-1:wf/x", timestamp=TS), "ToolCallStartEvent"),
    "tool_call_end": ("TOOL_CALL_END", lambda s: agui.tool_call_end(s, tool_call_id="tc-1", timestamp=TS), "ToolCallEndEvent"),
    "tool_call_result": ("TOOL_CALL_RESULT", lambda s: agui.tool_call_result(s, tool_call_id="tc-1", content="12 open purchase orders", timestamp=TS), "ToolCallResultEvent"),
    "reasoning_start": ("REASONING_START", lambda s: agui.reasoning_start(s, message_id="r-1", timestamp=TS), "ReasoningStartEvent"),
    "reasoning_message_start": ("REASONING_MESSAGE_START", lambda s: agui.reasoning_message_start(s, message_id="r-1", timestamp=TS), "ReasoningMessageStartEvent"),
    "reasoning_message_content": ("REASONING_MESSAGE_CONTENT", lambda s: agui.reasoning_message_content(s, message_id="r-1", delta="Checking UK1 cover", timestamp=TS), "ReasoningMessageContentEvent"),
    "reasoning_message_end": ("REASONING_MESSAGE_END", lambda s: agui.reasoning_message_end(s, message_id="r-1", timestamp=TS), "ReasoningMessageEndEvent"),
    "reasoning_end": ("REASONING_END", lambda s: agui.reasoning_end(s, message_id="r-1", timestamp=TS), "ReasoningEndEvent"),
    "activity_snapshot": ("ACTIVITY_SNAPSHOT", lambda s: agui.activity_snapshot(s, block=parse_block(envelope("kpi_strip@1")), timestamp=TS), "ActivitySnapshotEvent"),
    "text_message_start": ("TEXT_MESSAGE_START", lambda s: agui.text_message_start(s, message_id="m-1", timestamp=TS), "TextMessageStartEvent"),
    "text_message_content": ("TEXT_MESSAGE_CONTENT", lambda s: agui.text_message_content(s, message_id="m-1", delta="Moving stock", timestamp=TS), "TextMessageContentEvent"),
    "text_message_end": ("TEXT_MESSAGE_END", lambda s: agui.text_message_end(s, message_id="m-1", timestamp=TS), "TextMessageEndEvent"),
    "case_updated": ("CUSTOM", lambda s: agui.case_updated(s, mode="actual", status="open", title="Trim delay", timestamp=TS), "CustomEvent"),
}


def test_agui_validation_is_active() -> None:
    assert agui.AGUI_VALIDATION_AVAILABLE
    assert ag_ui_core.PROTOCOL_VERSION == agui.AGUI_PROTOCOL_VERSION == "1.0"


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_builders_validate_against_installed_models(name: str) -> None:
    wire_type, build, class_name = BUILDERS[name]
    event = build(sc())
    assert event["type"] == wire_type
    model = EVENT_ADAPTER.validate_python(event)
    assert type(model).__name__ == class_name
    assert type(model) is getattr(ag_ui_core, class_name)
    assert not model.model_extra
    # camelCase on the wire; exact round trip through the official models.
    assert model.model_dump(mode="json", by_alias=True) == event
    assert all("_" not in k for k in event if k != "metadata")
    assert event["metadata"]["sc"]["tenantId"] == "demo-retail"
    json.dumps(event)


def test_event_specific_shapes() -> None:
    s = sc()
    started = agui.run_started(s, thread_id="case-1", run_id="run-1", timestamp=TS)
    assert started["protocolVersion"] == "1.0" and "parentRunId" not in started

    interrupted = agui.run_finished_interrupt(s, thread_id="case-1", run_id="run-1", interrupts=[INTERRUPT], timestamp=TS)
    outcome = interrupted["outcome"]
    assert outcome["type"] == "interrupt"
    assert outcome["interrupts"][0]["responseSchema"]["properties"]["decision"]["enum"] == [
        "cancel", "confirm", "edit", "what_if"
    ]

    result = agui.tool_call_result(s, tool_call_id="tc-1", content="ok", timestamp=TS)
    assert result["messageId"] == "tc-1:result" and result["role"] == "tool"

    sub = agui.subagent_started(s, subagent_run_id="i:p", name="recommender", timestamp=TS)
    assert sub["description"] == "Strategist"

    snap = agui.activity_snapshot(s, block=envelope("approval_card@1"), timestamp=TS)
    assert snap["activityType"] == "sc.render" and snap["replace"] is True
    assert snap["messageId"] == "blk_approval_card"
    assert snap["content"] == parse_block(envelope("approval_card@1")).model_dump(mode="json")

    step = agui.step_started(s, timestamp=TS)
    assert step["stepName"] == "checking_systems"


def test_validation_catches_bad_events() -> None:
    good = agui.text_message_end(sc(), message_id="m-1", timestamp=TS)
    with pytest.raises(AguiEventError):
        agui.validate_event({**good, "message_id": "m-1"})  # snake_case key
    snake = {k: v for k, v in good.items() if k != "messageId"} | {"message_id": "m-1"}
    with pytest.raises(AguiEventError, match="round-trip"):
        agui.validate_event(snake)
    with pytest.raises(AguiEventError):
        agui.validate_event({k: v for k, v in good.items() if k != "messageId"})
    with pytest.raises(AguiEventError, match="not in AG-UI"):
        agui.validate_event({**good, "surprise": 1})
    with pytest.raises(AguiEventError):
        agui.validate_event({**good, "type": "THINKING_START"})  # removed in 1.0


def test_builder_argument_checks() -> None:
    s = sc()
    with pytest.raises(ValueError, match="delta"):
        agui.text_message_content(s, message_id="m", delta="")
    with pytest.raises(ValueError, match="unknown agent"):
        agui.subagent_started(s, subagent_run_id="x", name="frame_case")
    with pytest.raises(ValueError, match="at least one"):
        agui.run_finished_interrupt(s, thread_id="c", run_id="r", interrupts=[])
    with pytest.raises(ValidationError):
        agui.activity_snapshot(s, block={**envelope("kpi_strip@1"), "kind": "unknown"})
    with pytest.raises(TypeError):
        agui.custom(s, name="sc.x", value={1, 2})


class TestScMetadata:
    def test_camel_case_wire(self) -> None:
        wire = sc().wire()
        assert set(wire) == {"tenantId", "caseId", "seq", "stage", "prov", "label", "agent", "evidenceRefs"}
        assert wire["seq"] is None

    def test_run_id_is_optional_and_additive(self) -> None:
        """Regression: run-level events (RUN_ERROR has no runId in AG-UI 1.0) can be attributed
        to their run through ``metadata.sc.runId``; without it the wire form is unchanged."""
        assert "runId" not in sc().wire()
        with_run = sc(runId="019a0000-0000-7000-8000-000000000001")
        assert with_run.wire()["runId"] == "019a0000-0000-7000-8000-000000000001"
        assert ScMetadata(tenant_id="demo-retail", case_id="c", run_id="r-1", stage=Stage.APPLYING,
                          prov="fact", label="x").run_id == "r-1"
        event = agui.run_error(with_run, message="Run cancelled.", code="CANCELLED", timestamp=TS)
        agui.validate_event(event)
        assert event["metadata"]["sc"]["runId"] == with_run.run_id
        assert ScMetadata.model_validate(event["metadata"]["sc"]).run_id == with_run.run_id
        # old events (no runId) still validate
        assert ScMetadata.model_validate(sc().wire()).run_id is None
        with pytest.raises(ValidationError):
            sc(runId="")

    def test_accepts_snake_case_names(self) -> None:
        meta = ScMetadata(
            tenant_id="demo-retail", case_id="c", stage=Stage.APPLYING, prov="committed", label="Applying"
        )
        assert meta.wire()["evidenceRefs"] == []

    @pytest.mark.parametrize(
        "mutation",
        [{"stage": "thinking"}, {"prov": "rumour"}, {"evidenceRefs": ["e1"]}, {"seq": -1}, {"extra": 1}, {"tenantId": "Demo Retail"}],
    )
    def test_rejects_bad(self, mutation: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            sc(**mutation)

    def test_with_seq_and_sse_frame(self) -> None:
        event = agui.step_started(sc(), timestamp=TS)
        stamped = agui.with_seq(event, 123)
        assert stamped["metadata"]["sc"]["seq"] == 123 and event["metadata"]["sc"]["seq"] is None
        agui.validate_event(stamped)
        frame = agui.sse_frame(123, stamped)
        assert frame.startswith("id: 123\nevent: message\ndata: {") and frame.endswith("}\n\n")
        assert json.loads(frame.split("data: ", 1)[1]) == stamped


def test_stages() -> None:
    assert [s.value for s in agui.STAGE_ORDER] == [
        "understanding", "checking_systems", "measuring_impact", "building_options",
        "ready_for_review", "preparing_changes", "awaiting_approval", "applying", "verified",
    ]
    assert STAGE_LABELS[Stage.CHECKING_SYSTEMS] == "Checking your systems"
    assert len(STAGE_LABELS) == 9
    assert agui.stage_for_node("dry_run") is Stage.PREPARING_CHANGES
    assert agui.stage_for_node("reject_run") is None


def test_builders_work_without_optional_agui_package() -> None:
    """With the ``agui`` extra absent, builders still emit the same wire events (unvalidated)."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import sys
        sys.modules["ag_ui"] = None  # make `import ag_ui...` raise ImportError
        from keelson_contracts import agui
        assert agui.AGUI_VALIDATION_AVAILABLE is False
        sc = agui.ScMetadata(tenant_id="demo-retail", case_id="c", stage="verified", prov="committed", label="Done")
        event = agui.run_finished_success(sc, thread_id="c", run_id="r", timestamp=1)
        assert event["outcome"] == {"type": "success"} and event["threadId"] == "c"
        print("ok")
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"


def test_m5_node_stages_and_stageless_nodes() -> None:
    """M5 nodes have stages; stage-less nodes are an explicit, disjoint list (the engine's workflow
    test checks that every graph node is in one of the two)."""
    assert agui.stage_for_node("prepare_replan") is Stage.BUILDING_OPTIONS
    assert agui.stage_for_node("prepare_rollback") is Stage.PREPARING_CHANGES
    assert agui.stage_for_node("checklist_step") is Stage.APPLYING
    assert not set(agui.NODE_STAGES) & agui.STAGELESS_NODES
    assert {"pre_route", "reject_run", "end_turn", "close_case"} <= agui.STAGELESS_NODES
    assert all(agui.stage_for_node(n) is None for n in agui.STAGELESS_NODES)
    assert set(agui.NODE_STAGES.values()) <= set(agui.STAGE_ORDER)
