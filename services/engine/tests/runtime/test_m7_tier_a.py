"""M7 tier-A per-op commit (``110`` §4): template rendering, read-back matching, the commit-ledger
state machine (enforced / best_effort / none, in_doubt, stale read-backs, crash recovery), the
per-op capability table and atomic groups — plus the committer against a stateful FastMCP fake of
a ``/mcp-lite`` foreign ERP through the production MCP gateway (lost responses = in doubt)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fakes.harness import TENANT, Harness
from fakes.lite_server import API_KEY_HEADER, LiteServer
from keelson_contracts.commit import CommitLedgerEntry
from keelson_contracts.mapping import ActionMapping, ActionOpMapping, TemplateError

from keelson_engine.runtime.candidates import CapabilityTable, ConnectorCapabilities, per_op_capabilities
from keelson_engine.runtime.deps import ToolInfo
from keelson_engine.runtime.errors import McpToolError, McpTransportError
from keelson_engine.runtime.nodes.per_op import connector_call
from keelson_engine.runtime.remediation import partition_actions
from keelson_engine.runtime.tier_a import (
    LineRecord,
    PerOpCommitter,
    PerOpLine,
    RenderError,
    readback_match,
    render_call,
    render_template,
)

TO_ITEM: dict[str, Any] = {
    "seq": 1, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
    "payload": {"from_dc": "UK1", "to_dc": "NL1", "ship_date": "2026-10-12",
                "lines": [{"sku": "PUF-W-BLK-M", "qty": 12}]},
}
ALLOC_ITEM: dict[str, Any] = {
    "seq": 2, "op": "allocation.set_qty", "target_ref": "AL-1001", "expected_version": 3, "payload": {"qty": 30},
}

TO_MAPPING = {
    "op": "transfer_order.create", "tool": "create_transfer_order",
    "args": {"from_dc": "{{payload.from_dc}}", "to_dc": "{{payload.to_dc}}", "ship_date": "{{payload.ship_date}}",
             "lines_json": {"$json": "{{payload.lines}}"}},
    "idempotency": "best_effort", "request_id_arg": "request_id",
    "readback": {"tool": "list_items",
                 "args": {"type": "transfer_order",
                          "filter_json": {"$json": {"from_dc": "{{payload.from_dc}}", "to_dc": "{{payload.to_dc}}",
                                                    "ship_date": "{{payload.ship_date}}"}}},
                 "items_path": "$.items",
                 "match": {"$.from_dc": "{{payload.from_dc}}", "$.ship_date": "{{payload.ship_date}}",
                           "$.lines[0].qty": "{{payload.lines.0.qty}}"}},
}
ALLOC_MAPPING = {
    "op": "allocation.set_qty", "tool": "update_item",
    "args": {"type": "allocation", "key": "{{target_ref}}", "fields_json": {"$json": {"qty": "{{payload.qty}}"}},
             "expected_version": "{{expected_version}}"},
    "idempotency": "enforced", "request_id_arg": "request_id",
    "readback": {"tool": "get_item", "args": {"type": "allocation", "key": "{{target_ref}}"},
                 "items_path": "$.item", "match": {"$.qty": "{{payload.qty}}"}},
}


def mapping(**overrides: Any) -> ActionMapping:
    ops = [dict(TO_MAPPING), dict(ALLOC_MAPPING)]
    for op in ops:
        op.update(overrides.get(op["op"], {}))
    return ActionMapping.model_validate({"kind": "action", "ops": ops})


# ─────────────────────────────── rendering ───────────────────────────────


def test_render_keeps_json_types_and_canonical_json_text() -> None:
    call = render_call(mapping().for_op("transfer_order.create"), TO_ITEM, request_id="k:1")  # type: ignore[arg-type]
    assert call.tool == "create_transfer_order"
    assert call.args == {"from_dc": "UK1", "to_dc": "NL1", "ship_date": "2026-10-12",
                         "lines_json": '[{"qty":12,"sku":"PUF-W-BLK-M"}]', "request_id": "k:1"}
    alloc = render_call(mapping().for_op("allocation.set_qty"), ALLOC_ITEM, request_id="k:2")  # type: ignore[arg-type]
    assert alloc.args["expected_version"] == 3 and isinstance(alloc.args["expected_version"], int)
    assert alloc.args["fields_json"] == '{"qty":30}' and alloc.args["key"] == "AL-1001"


def test_render_text_placeholders_and_errors() -> None:
    values = {"payload": {"a": 7, "b": True, "c": None, "d": {"x": 1}}, "target_ref": "T-1", "expected_version": None,
              "request_id": None, "op": "x.y"}
    assert render_template({"k": "ref {{ target_ref }} / {{payload.a}} / {{payload.b}}"}, values) == \
        {"k": "ref T-1 / 7 / true"}
    assert render_template({"k": "{{payload.c}}"}, values) == {"k": None}  # exact: JSON null passes
    with pytest.raises(RenderError):
        render_template({"k": "x {{payload.c}}"}, values)  # null inside a text
    with pytest.raises(RenderError):
        render_template({"k": "x {{payload.d}}"}, values)  # an object inside a text
    with pytest.raises(RenderError):
        render_template({"k": "{{payload.missing}}"}, values)
    with pytest.raises(TemplateError):
        render_template({"k": "{{payload.__class__}}"}, values)  # no attribute access, ever
    with pytest.raises(TemplateError):
        render_template({"k": "{{os.environ}}"}, values)  # unknown root
    with pytest.raises(TemplateError):
        render_template({"k": 1.5}, values)


def test_readback_match_with_items_path_and_loose_integers() -> None:
    m = mapping().for_op("transfer_order.create")
    assert m is not None and m.readback is not None
    values = {"payload": TO_ITEM["payload"], "target_ref": None, "expected_version": None, "request_id": None,
              "op": "transfer_order.create"}
    found = {"items": [{"from_dc": "UK1", "ship_date": "2026-10-12", "lines": [{"qty": "12"}]}]}
    assert readback_match(m.readback, found, values) == (1, True)
    other = {"items": [{"from_dc": "UK1", "ship_date": "2026-10-13", "lines": [{"qty": 12}]}]}
    assert readback_match(m.readback, other, values) == (1, False)
    assert readback_match(m.readback, {"items": []}, values) == (0, False)
    assert readback_match(m.readback, {"nothing": 1}, values) == (0, False)


# ─────────────────────────────── ledger state machine ───────────────────────────────


class FakeTarget:
    """Scripted write outcomes + a read-back that reports what was really applied."""

    def __init__(self, outcomes: list[str], *, readback: str = "truth", preexisting: bool = False) -> None:
        self.outcomes = list(outcomes)
        self.readback = readback
        self.preexisting = preexisting
        """An entity that looks like the line's effect exists before anything is sent."""
        self.applied = 0
        self.writes: list[dict[str, Any]] = []
        self.reads = 0
        self.persisted: list[CommitLedgerEntry] = []

    async def write(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.writes.append(dict(args))
        mode = self.outcomes.pop(0) if self.outcomes else "ok"
        if mode == "ok":
            self.applied += 1
            return {"code": f"TO-{self.applied}"}
        if mode == "lost":  # applied, then the answer was lost
            self.applied += 1
            raise McpTransportError(tool, "ReadTimeout")
        if mode == "down":
            raise McpTransportError(tool, "ConnectError")
        if mode == "busy":
            raise McpToolError(tool, "idempotency_in_flight", "retry later")
        raise McpToolError(tool, mode, "refused")

    async def read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.reads += 1
        if self.readback == "error":
            raise McpTransportError(tool, "ConnectError")
        if self.applied or self.preexisting:
            return {"items": [{"from_dc": "UK1", "ship_date": "2026-10-12", "lines": [{"qty": 12}]}],
                    "item": {"qty": 30}}
        return {"items": [], "item": {"qty": 40}}

    async def persist(self, seq: int, record: LineRecord) -> None:
        assert record.entry is not None
        self.persisted.append(record.entry)


def committer(target: FakeTarget) -> PerOpCommitter:
    return PerOpCommitter(change_set_id="cs-1", connector="northwind", idempotency_key="a" * 64,
                          write=target.write, read=target.read, persist=target.persist,
                          clock=lambda: dt.datetime(2026, 10, 8, tzinfo=dt.UTC))


def line(op: str, idempotency: str | None = None) -> PerOpLine:
    m = mapping(**({op: {"idempotency": idempotency}} if idempotency else {})).for_op(op)
    assert m is not None
    return PerOpLine(seq=1, item=dict(TO_ITEM if op == "transfer_order.create" else ALLOC_ITEM, seq=1), mapping=m)


async def test_enforced_retries_with_the_same_request_id_and_never_reads_back() -> None:
    target = FakeTarget(["down", "busy", "ok"])
    record = await committer(target).apply(line("allocation.set_qty"))
    assert record.entry is not None and record.entry.state == "succeeded" and record.entry.attempts == 3
    assert {w["request_id"] for w in target.writes} == {"a" * 64 + ":1"} and len(target.writes) == 3
    assert target.reads == 0
    assert target.persisted[0].state == "attempted"  # persisted before the first call


async def test_enforced_gives_up_after_max_attempts_and_guides() -> None:
    target = FakeTarget(["down", "down", "down", "ok"])
    record = await committer(target).apply(line("allocation.set_qty"))
    assert record.entry is not None and record.entry.state == "in_doubt" and record.entry.attempts == 3
    assert len(target.writes) == 3


async def test_best_effort_in_doubt_reads_back_and_never_repeats_an_applied_call() -> None:
    target = FakeTarget(["lost"])
    record = await committer(target).apply(line("transfer_order.create"))
    assert record.entry is not None and record.entry.state == "verified" and record.entry.attempts == 1
    assert target.applied == 1 and len(target.writes) == 1
    assert target.reads == 2  # the baseline before the call + the read-back of the unknown outcome
    assert record.entry.readback is not None and record.entry.readback.found
    assert record.baseline_found is False


async def test_best_effort_reads_back_before_every_retry() -> None:
    target = FakeTarget(["down", "down", "ok"])
    record = await committer(target).apply(line("transfer_order.create"))
    assert record.entry is not None and record.entry.state == "succeeded" and record.entry.attempts == 3
    assert target.reads == 3  # the baseline + one fresh read-back before each of the two retries
    assert target.applied == 1


async def test_none_is_never_retried_and_guided_when_not_found() -> None:
    target = FakeTarget(["down"])
    record = await committer(target).apply(line("transfer_order.create", "none"))
    assert record.entry is not None and record.entry.state == "in_doubt" and record.entry.attempts == 1
    assert record.entry.readback is not None and record.entry.readback.ok and not record.entry.readback.found
    assert len(target.writes) == 1 and target.reads == 2  # baseline + read-back


async def test_none_found_by_read_back_is_verified() -> None:
    target = FakeTarget(["lost"])
    record = await committer(target).apply(line("transfer_order.create", "none"))
    assert record.entry is not None and record.entry.state == "verified" and len(target.writes) == 1


async def test_definitive_tool_error_fails_without_retry() -> None:
    target = FakeTarget(["version_conflict"])
    record = await committer(target).apply(line("allocation.set_qty"))
    assert record.entry is not None and record.entry.state == "failed" and record.entry.error is not None
    assert len(target.writes) == 1 and target.reads == 0


async def test_failing_read_back_stops_without_a_blind_retry() -> None:
    target = FakeTarget(["down"], readback="error")
    record = await committer(target).apply(line("transfer_order.create"))
    assert record.entry is not None and record.entry.state == "in_doubt"
    assert record.entry.readback is not None and not record.entry.readback.ok
    assert len(target.writes) == 1


async def test_crash_after_attempted_resumes_from_the_ledger() -> None:
    # the worker died after persisting "attempted" (the call may or may not have reached the system)
    l_enf = line("allocation.set_qty")
    rec = LineRecord(entry=CommitLedgerEntry.model_validate({
        "change_set_id": "cs-1", "line_seq": 1, "op": "allocation.set_qty", "connector": "northwind",
        "tool": "update_item", "idempotency": "enforced", "state": "attempted", "attempts": 1,
        "request_id": "a" * 64 + ":1", "updated_at": dt.datetime(2026, 10, 8, tzinfo=dt.UTC)}))
    target = FakeTarget(["ok"])
    out = await committer(target).apply(l_enf, rec)
    assert out.entry is not None and out.entry.state == "succeeded" and out.entry.attempts == 2
    assert target.writes[0]["request_id"] == "a" * 64 + ":1" and target.reads == 0
    # best effort: read back first — the effect is there, so it is never called again
    l_be = line("transfer_order.create")
    rec_be = LineRecord(entry=CommitLedgerEntry.model_validate({
        "change_set_id": "cs-1", "line_seq": 1, "op": "transfer_order.create", "connector": "northwind",
        "tool": "create_transfer_order", "idempotency": "best_effort", "state": "attempted", "attempts": 1,
        "request_id": "a" * 64 + ":1", "updated_at": dt.datetime(2026, 10, 8, tzinfo=dt.UTC)}))
    target_be = FakeTarget([])
    target_be.applied = 1
    out_be = await committer(target_be).apply(l_be, rec_be)
    assert out_be.entry is not None and out_be.entry.state == "verified" and target_be.writes == []


@pytest.mark.parametrize(("idempotency", "outcome"), [("best_effort", "lost"), ("best_effort", "down"),
                                                    ("none", "lost"), ("none", "down")])
async def test_a_read_back_that_matched_before_the_call_never_settles_a_line(idempotency: str, outcome: str) -> None:
    """CO review: the read-back can match an entity that existed before the call (e.g. another
    transfer order on the same lane and date). Such a match proves nothing: the line stays in
    doubt for a person, never ``verified`` and never blindly re-sent."""
    target = FakeTarget([outcome], preexisting=True)
    record = await committer(target).apply(line("transfer_order.create", idempotency))
    assert record.baseline_found is True
    assert record.entry is not None and record.entry.state == "in_doubt" and record.entry.attempts == 1
    rb = record.entry.readback
    assert rb is not None and not rb.ok and not rb.found and "before the call" in (rb.detail or "")
    assert len(target.writes) == 1


async def test_reconcile_never_verifies_by_a_pre_existing_match() -> None:
    target = FakeTarget(["ok"], preexisting=True)
    c = committer(target)
    l_be = line("transfer_order.create")
    record = await c.apply(l_be)
    assert record.entry is not None and record.entry.state == "succeeded" and record.baseline_found is True
    again = await c.verify(l_be, record)
    assert again.entry is not None and again.entry.state == "succeeded"  # confirmed by the call, not the read-back
    clean = FakeTarget(["ok"])
    c2 = committer(clean)
    rec2 = await c2.apply(l_be)
    assert rec2.baseline_found is False
    verified = await c2.verify(l_be, rec2)
    assert verified.entry is not None and verified.entry.state == "verified"


class FlakyBaselineTarget(FakeTarget):
    """The baseline read-back fails (transport), later read-backs work."""

    async def read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.reads == 0:
            self.reads += 1
            raise McpTransportError(tool, "ConnectError")
        return await super().read(tool, args)


@pytest.mark.parametrize("idempotency", ["best_effort", "none"])
async def test_a_failed_baseline_makes_a_later_match_ambiguous(idempotency: str) -> None:
    """Review: without a baseline a match may be a pre-existing entity — never ``verified`` (the
    line would be counted as applied when the call never reached the system)."""
    target = FlakyBaselineTarget(["down"], preexisting=True)  # nothing applied, an old look-alike exists
    record = await committer(target).apply(line("transfer_order.create", idempotency))
    assert record.baseline_found is None and record.baseline_unknown is True
    assert record.entry is not None and record.entry.state == "in_doubt" and record.entry.attempts == 1
    rb = record.entry.readback
    assert rb is not None and not rb.ok and not rb.found and "before the call failed" in (rb.detail or "")
    assert len(target.writes) == 1 and target.applied == 0
    back = LineRecord.from_json(record.to_json())
    assert back.baseline_unknown is True


def test_line_record_round_trips_the_baseline() -> None:
    rec = LineRecord(baseline_found=True, readback_attempt=2, call={"tool": "x", "args": {}})
    back = LineRecord.from_json(rec.to_json())
    assert back.baseline_found is True and back.readback_attempt == 2 and back.call == {"tool": "x", "args": {}}
    assert LineRecord.from_json({"baseline_found": "yes"}).baseline_found is None


# ─────────────────────────────── capabilities + groups ───────────────────────────────


def tool(name: str, access: str, *, idempotency: str | None = None, status: str = "approved") -> ToolInfo:
    return ToolInfo(name=name, description="", input_schema={}, output_schema=None, access=access, status=status,
                    idempotency=idempotency)


def test_per_op_capabilities_cap_claims_and_need_approved_tools() -> None:
    tools = {"create_transfer_order": tool("create_transfer_order", "write", idempotency="best_effort"),
             "update_item": tool("update_item", "write", idempotency="best_effort"),
             "list_items": tool("list_items", "read"), "get_item": tool("get_item", "read")}
    ops = per_op_capabilities(mapping(), tools)
    # update_item is confirmed best_effort only: the "enforced" claim is capped
    assert ops == {"transfer_order.create": "best_effort", "allocation.set_qty": "best_effort"}
    assert per_op_capabilities(mapping(), {**tools, "get_item": tool("get_item", "read", status="needs_review")}) == \
        {"transfer_order.create": "best_effort"}
    assert per_op_capabilities(mapping(), {**tools, "update_item": tool("update_item", "read")}) == \
        {"transfer_order.create": "best_effort"}  # a read tool can never be an action tool


def test_atomic_groups_on_a_per_op_connector_need_every_member_enforced() -> None:
    caps = ConnectorCapabilities("northwind", "Northwind", ("create_change_set",),
                                 frozenset({"transfer_order.create", "allocation.set_qty"}), "action_mapping",
                                 commit_mode="per_op",
                                 op_idempotency={"transfer_order.create": "best_effort",
                                                 "allocation.set_qty": "enforced"})
    table = CapabilityTable((caps,))
    mixed = [dict(TO_ITEM, group="g1"), dict(ALLOC_ITEM, group="g1"), dict(ALLOC_ITEM, seq=3, target_ref="AL-1002")]
    part = partition_actions(mixed, table)
    assert part.apply == {"northwind": [mixed[2]]} and part.guide == {"northwind": mixed[:2]}
    enforced = [dict(ALLOC_ITEM, group="g2"), dict(ALLOC_ITEM, seq=3, target_ref="AL-1002", group="g2")]
    assert partition_actions(enforced, table).apply == {"northwind": enforced}


# ─────────────────────────────── against a FastMCP lite ERP ───────────────────────────────


async def lite(h: Harness) -> LiteServer:
    server = LiteServer()
    await h.add_registry_connector("northwind", server, headers={API_KEY_HEADER: server.api_key})
    return server


def gateway_committer(h: Harness, sink: list[LineRecord]) -> PerOpCommitter:
    async def write(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return await connector_call(tenant=TENANT, connector="northwind", principal="autocommit", tool=tool_name,
                                    args=args)

    async def read(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return await connector_call(tenant=TENANT, connector="northwind", principal="reader", tool=tool_name,
                                    args=args)

    async def persist(seq: int, record: LineRecord) -> None:
        sink.append(record)

    return PerOpCommitter(change_set_id="cs-9", connector="northwind", idempotency_key="b" * 64, write=write,
                          read=read, persist=persist)


async def test_lite_erp_lost_response_is_read_back_not_repeated(harness: Harness) -> None:
    server = await lite(harness)
    server.fail_next["create_transfer_order"] = ["lost"]
    sink: list[LineRecord] = []
    record = await gateway_committer(harness, sink).apply(line("transfer_order.create"))
    assert record.entry is not None and record.entry.state == "verified"
    assert len([a for a in server.applied if a[0] == "create_transfer_order"]) == 1  # applied exactly once
    assert [c[0] for c in server.calls].count("create_transfer_order") == 1
    assert sink[0].entry is not None and sink[0].entry.state == "attempted"
    assert any(r.entry is not None and r.entry.state == "in_doubt" for r in sink)


async def test_lite_erp_enforced_replay_and_version_conflict(harness: Harness) -> None:
    server = await lite(harness)
    server.fail_next["update_item"] = ["lost"]
    record = await gateway_committer(harness, []).apply(line("allocation.set_qty"))
    # enforced: retried with the same request id; the server replays instead of applying twice
    assert record.entry is not None and record.entry.state == "succeeded" and record.entry.attempts == 2
    assert server.records["allocation"][0]["qty"] == 30 and server.records["allocation"][0]["version"] == 4
    assert len([a for a in server.applied if a[0] == "update_item"]) == 1
    again = await gateway_committer(harness, []).apply(line("allocation.set_qty"))
    # the same line from scratch (new ledger) replays too (request id = change set key + seq)
    assert again.entry is not None and again.entry.state == "succeeded"
    stale = PerOpLine(seq=7, item=dict(ALLOC_ITEM, seq=7), mapping=line("allocation.set_qty").mapping)
    refused = await gateway_committer(harness, []).apply(stale)
    assert refused.entry is not None and refused.entry.state == "failed"
    assert refused.entry.error is not None and refused.entry.error.startswith("version_conflict")


def test_action_op_mapping_contract_still_requires_read_back_for_non_enforced() -> None:
    with pytest.raises(ValueError):
        ActionOpMapping.model_validate({**TO_MAPPING, "readback": None})
