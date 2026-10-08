"""M7 end-to-end runtime flows with FakeLlm (``110`` §3, §4): a tier-A foreign ERP (FastMCP lite
fake) applied per op with human approval + guided fallback for unmapped / unapplied lines; advisory
plans on a crawled snapshot; custom specialists chosen by the planner, with quarantine auto-wrap,
translator display names and their own budgets; no write tool ever reaches an LLM."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any

from fakes.harness import MARCO, PRIYA, Harness
from fakes.lite_server import API_KEY_HEADER, LiteServer
from fakes.scripts import assert_no_write_tool_reached_an_llm, chat_flow_script, write_tools
from keelson_contracts.blocks import parse_block
from keelson_contracts.changeset import ChangeSet, idempotency_key

from keelson_engine.runtime.nodes.per_op import prior_ledger_summary
from keelson_engine.runtime.spec import PolicyConfig, RemediationDefaults

DECIDER_MARCO = {"sub": MARCO["sub"], "username": MARCO["username"], "name": MARCO["name"], "role": "approver",
                 "roles": MARCO["roles"]}

TO_MAPPING: dict[str, Any] = {
    "op": "transfer_order.create", "tool": "create_transfer_order",
    "args": {"from_dc": "{{payload.from_dc}}", "to_dc": "{{payload.to_dc}}", "ship_date": "{{payload.ship_date}}",
             "lines_json": {"$json": "{{payload.lines}}"}},
    "idempotency": "best_effort", "request_id_arg": "request_id",
    "readback": {"tool": "list_items",
                 "args": {"type": "transfer_order",
                          "filter_json": {"$json": {"from_dc": "{{payload.from_dc}}", "to_dc": "{{payload.to_dc}}",
                                                    "ship_date": "{{payload.ship_date}}"}}},
                 "items_path": "$.items",
                 "match": {"$.from_dc": "{{payload.from_dc}}", "$.to_dc": "{{payload.to_dc}}",
                           "$.ship_date": "{{payload.ship_date}}"}},
}


async def northwind(h: Harness, *, mapping_ops: list[dict[str, Any]] | None = None) -> LiteServer:
    """Register the foreign ERP as a further write connector (tier A: no change sets) with an
    action mapping; the platform connector cannot apply the plan's transfers (so they are routed
    to the foreign ERP, like a tenant whose only write system is the foreign ERP)."""
    server = LiteServer()
    await h.add_registry_connector("northwind", server, headers={API_KEY_HEADER: server.api_key})
    h.core.connectors.action_mappings["northwind"] = {"kind": "action", "ops": mapping_ops or [TO_MAPPING]}
    h.core.connectors.merxa_config["supported_ops"] = ["po.create"]
    h.spec_overrides["remediation"] = RemediationDefaults(connectors=("northwind",))
    h.spec_overrides["policy"] = PolicyConfig(auto_initiator_role="approver")
    return server


async def plan_ready(h: Harness) -> dict[str, Any]:
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    assert await h.run(h.row("chat", {"text": "Trims from Meghna are 7 days late"})) == "succeeded"
    plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    assert plan is not None
    return plan


def apply_row(h: Harness, option: str = "opt-A") -> Any:
    return h.row("action", {"type": "apply", "option_id": option, "plan_version": 1}, PRIYA)


def resume(h: Harness, paused: Any) -> Any:
    created = h.core.interrupts.created[-1]
    return h.resume_row(paused, "approve", [DECIDER_MARCO], plan_version=created["plan_version"],
                        change_set_hash=created["change_set_hash"])


def blocks(h: Harness, kind: str) -> list[dict[str, Any]]:
    return h.core.repo.blocks_of(kind)


async def test_tier_a_per_op_apply_needs_approval_and_is_verified_by_read_back(harness: Harness) -> None:
    h = harness
    server = await northwind(h)
    plan = await plan_ready(h)
    option = next(o for o in plan["options"] if o["id"] == "opt-A")
    transfers = [a for a in option["actions"] if a["op"] == "transfer_order.create"]
    assert transfers and option["remediation"]["apply_lines"] == len(transfers)

    action = apply_row(h)
    assert await h.run(action) == "paused"
    [cs] = [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"]
    assert cs["status"] == "validated" and cs["preview"]["mode"] == "per_op"
    assert [c["tool"] for c in cs["preview"]["calls"]] == ["create_transfer_order"] * len(transfers)
    assert server.writes == 0 and not server.applied  # the dry run renders, it never executes
    approval = h.core.interrupts.created[-1]
    assert approval["kind"] == "approval" and approval["change_set_hash"]

    assert await h.run(resume(h, action)) == "succeeded"
    row = h.core.repo.change_sets[cs["id"]]
    assert row["status"] == "verified"
    key = idempotency_key("demo-retail", cs["id"], 1)
    sent = [c[1] for c in server.calls if c[0] == "create_transfer_order"]
    assert len(sent) == len(transfers) == len(server.applied)  # one call per line, applied exactly once
    assert [c["request_id"] for c in sent] == [f"{key}:{i}" for i in range(1, len(transfers) + 1)]
    entries = h.core.repo.ledger_entries
    assert {e["state"] for e in entries if e["line_seq"] == 1} >= {"attempted", "succeeded", "verified"}
    final = {e["line_seq"]: e for e in entries}
    assert all(e["state"] == "verified" and e["idempotency"] == "best_effort" for e in final.values())
    writes = [c for c in h.mcp_calls if c.tool == "create_transfer_order"]
    assert writes and {c.principal for c in writes} == {"autocommit"}  # never the reader, never an LLM
    tracker = parse_block(blocks(h, "commit_tracker")[-1])
    assert tracker.props.status == "verified" and tracker.props.applied == len(transfers)
    assert h.core.repo.plan_status.get((h.case_id, 1)) == "committed"
    assert ("demo-retail", "northwind", "per_op.applied") in h.core.scn.stale
    # a second Apply of the same option is refused: nothing is sent twice
    again = apply_row(h)
    assert await h.run(again) == "succeeded"
    assert len([c for c in server.calls if c[0] == "create_transfer_order"]) == len(transfers)
    # no LLM agent was ever offered a write tool of either connector
    assert_no_write_tool_reached_an_llm(h.script, write_tools(h.core))


async def test_tier_a_unapplied_lines_become_guided_steps(harness: Harness) -> None:
    h = harness
    server = await northwind(h)
    await plan_ready(h)
    server.fail_next["create_transfer_order"] = ["tool:version_conflict"]
    action = apply_row(h)
    assert await h.run(action) == "paused"
    assert await h.run(resume(h, action)) == "succeeded"
    [cs] = [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"]
    row = h.core.repo.change_sets[cs["id"]]
    lines = len(row["items"])
    assert row["status"] in ("partial", "failed") and row["result"]["guided_seqs"] == [1]
    final = {e["line_seq"]: e for e in h.core.repo.ledger_entries}
    assert final[1]["state"] == "failed" and final[1]["attempts"] == 1  # a refusal is never retried
    assert h.core.checklists is not None
    checklists = h.core.checklists.rows
    assert checklists, "the refused line is handed to the guide"
    [checklist] = list(checklists.values())
    assert checklist["connector"] == "northwind" and len(checklist["steps"]) == 1
    assert all(step.get("instructions") or step.get("title") for step in checklist["steps"])
    assert blocks(h, "remediation_checklist")
    assert h.core.repo.plan_status.get((h.case_id, 1)) != "committed"
    assert len(server.applied) == lines - 1


async def test_tier_a_lost_response_is_read_back_never_applied_twice(harness: Harness) -> None:
    h = harness
    server = await northwind(h)
    await plan_ready(h)
    server.fail_next["create_transfer_order"] = ["lost"]
    action = apply_row(h)
    assert await h.run(action) == "paused"
    assert await h.run(resume(h, action)) == "succeeded"
    [cs] = [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"]
    assert h.core.repo.change_sets[cs["id"]]["status"] == "verified"
    first = [e for e in h.core.repo.ledger_entries if e["line_seq"] == 1]
    assert [e["state"] for e in first][:2] == ["attempted", "in_doubt"] and first[-1]["state"] == "verified"
    assert first[-1]["attempts"] == 1 and first[-1]["readback"]["found"] is True
    assert len([c for c in server.calls if c[0] == "create_transfer_order"]) == len(h.core.repo.change_sets[cs["id"]]["items"])


async def test_tier_a_mapping_changed_after_approval_refuses(harness: Harness) -> None:
    h = harness
    server = await northwind(h)
    await plan_ready(h)
    action = apply_row(h)
    assert await h.run(action) == "paused"
    changed = json.loads(json.dumps(TO_MAPPING))
    changed["args"]["ship_date"] = "2027-01-01"  # an admin edits the mapping while the approval waits
    h.core.connectors.action_mappings["northwind"] = {"kind": "action", "ops": [changed]}
    assert await h.run(resume(h, action)) == "succeeded"
    assert server.writes == 0 and not server.applied
    [cs] = [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"]
    assert h.core.repo.change_sets[cs["id"]]["status"] == "failed"


def add_actions(h: Harness, option_id: str, *actions: dict[str, Any]) -> None:
    """Extend the saved plan's option (the harness plans only produce transfers)."""
    plan = h.core.repo.plans[h.case_id][-1]
    option = next(o for o in plan["options"] if o["id"] == option_id)
    seq = max(int(a.get("seq") or 0) for a in option["actions"])
    for extra in actions:
        seq += 1
        option["actions"].append({"seq": seq, **extra})


async def test_tier_a_unmapped_ops_are_guided_with_generated_instructions(harness: Harness) -> None:
    h = harness
    server = await northwind(h)
    await plan_ready(h)
    add_actions(h, "opt-A", {"op": "allocation.set_qty", "target_ref": "AL-1001", "expected_version": 3,
                             "payload": {"qty": 30}})
    action = apply_row(h)
    assert await h.run(action) == "paused"  # the mapped transfer still needs approval
    assert h.core.checklists is not None
    [checklist] = list(h.core.checklists.rows.values())
    [step] = checklist["steps"]
    assert step["expected"]["entity_type"] == "allocation" and step["expected"]["entity_ref"] == "AL-1001"
    assert step["instructions_md"] and step["title"]  # generated instructions for the unmapped op
    assert parse_block(blocks(h, "remediation_checklist")[-1]).props.steps
    [cs] = [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"]
    assert {i["op"] for i in cs["items"]} == {"transfer_order.create"}
    assert await h.run(resume(h, action)) == "succeeded"
    assert {a[0] for a in server.applied} == {"create_transfer_order"}  # the guided op never ran


async def test_tier_a_atomic_group_with_a_best_effort_op_is_guided_whole(harness: Harness) -> None:
    h = harness
    server = await northwind(h)
    plan = await plan_ready(h)
    option = next(o for o in plan["options"] if o["id"] == "opt-A")
    for a in option["actions"]:
        a["group"] = "g-rebalance"
    h.core.repo.plans[h.case_id][-1] = plan
    action = apply_row(h)
    assert await h.run(action) == "succeeded"  # nothing to apply: the whole group is guided
    assert not [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"]
    assert h.core.checklists is not None and h.core.checklists.rows
    assert server.writes == 0 and not server.applied
    assert not [i for i in h.core.interrupts.created if i["kind"] == "approval"]  # nothing to approve


# ─────────────────────────────── review regressions ───────────────────────────────


def confirm_idempotency(h: Harness, connector: str, tool: str, idempotency: str) -> None:
    """The admin confirmed ``tool``'s idempotency (``connector_tool.idempotency``)."""
    tools = h.core.connectors.registry_tools[connector]
    h.core.connectors.registry_tools[connector] = [
        dataclasses.replace(t, idempotency=idempotency) if t.name == tool else t for t in tools
    ]


async def test_tier_a_tool_confirmed_idempotency_caps_the_mapping_claim(harness: Harness) -> None:
    """A mapping that claims ``enforced`` for a tool the admin confirmed ``none`` must never
    auto-retry it: the ledger runs on the effective (capped) idempotency."""
    h = harness
    claim = json.loads(json.dumps(TO_MAPPING))
    claim["idempotency"] = "enforced"
    server = await northwind(h, mapping_ops=[claim])
    confirm_idempotency(h, "northwind", "create_transfer_order", "none")
    await plan_ready(h)
    # transient: nothing applied, outcome unknown to the caller (an enforced line would be retried)
    server.fail_next["create_transfer_order"] = ["tool:idempotency_in_flight"]
    action = apply_row(h)
    assert await h.run(action) == "paused"
    [cs] = [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"]
    assert {c["idempotency"] for c in cs["preview"]["calls"]} == {"none"}
    assert await h.run(resume(h, action)) == "succeeded"
    row = h.core.repo.change_sets[cs["id"]]
    sent = [c for c in server.calls if c[0] == "create_transfer_order"]
    assert len(sent) == len(row["items"])  # line 1 was sent once, never retried
    final = {e["line_seq"]: e for e in h.core.repo.ledger_entries}
    assert final[1]["idempotency"] == "none" and final[1]["attempts"] == 1
    assert final[1]["state"] == "in_doubt" and 1 in row["result"]["guided_seqs"]


async def test_tier_a_in_doubt_lines_keep_the_option_committed(harness: Harness) -> None:
    """Every line in doubt and none confirmed: the row is ``partial`` (the target may hold the
    changes), so a second Apply of the option sends nothing."""
    h = harness
    server = await northwind(h)
    plan = await plan_ready(h)
    option = next(o for o in plan["options"] if o["id"] == "opt-A")
    n = len([a for a in option["actions"] if a["op"] == "transfer_order.create"])
    server.fail_next["create_transfer_order"] = ["tool:idempotency_in_flight"] * n
    server.fail_next["list_items"] = ["tool:unavailable"] * (2 * n)  # baseline + read-back cannot tell
    action = apply_row(h)
    assert await h.run(action) == "paused"
    assert await h.run(resume(h, action)) == "succeeded"
    [cs] = [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"]
    row = h.core.repo.change_sets[cs["id"]]
    assert row["status"] == "partial" and row["result"]["applied"] == 0
    assert sorted(row["result"]["in_doubt_seqs"]) == list(range(1, n + 1))
    calls = len([c for c in server.calls if c[0] == "create_transfer_order"])
    approvals = len([i for i in h.core.interrupts.created if i["kind"] == "approval"])
    server.fail_next.clear()
    again = apply_row(h)
    assert await h.run(again) == "succeeded"  # refused: no new change set, no new approval, no call
    assert len([c for c in server.calls if c[0] == "create_transfer_order"]) == calls
    assert len([i for i in h.core.interrupts.created if i["kind"] == "approval"]) == approvals
    assert [c for c in h.core.repo.change_sets.values() if c["connector"] == "northwind"] == [row]


def test_prior_ledger_summary_keeps_sent_lines_on_record() -> None:
    """A resumed apply refused after a mapping change keeps what was sent before (committed row)."""
    items = [{"seq": s, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
              "payload": {"from_dc": "UK1", "to_dc": "NL1", "ship_date": "2026-10-12",
                          "lines": [{"sku": "PUF-W-BLK-M", "qty": 12}]}} for s in (1, 2, 3)]
    change_set = ChangeSet.model_validate({"id": "cs-1", "tenant": "demo-retail", "connector": "northwind",
                                           "revision": 1, "items": items})
    key = change_set.idempotency_key

    def entry(seq: int, state: str) -> dict[str, Any]:
        return {"entry": {"change_set_id": "cs-1", "line_seq": seq, "op": "transfer_order.create",
                          "connector": "northwind", "tool": "create_transfer_order", "idempotency": "best_effort",
                          "state": state, "attempts": 1, "request_id": f"{key}:{seq}", "ext_ref": None,
                          "readback": None, "error": None,
                          "updated_at": dt.datetime(2026, 10, 8, tzinfo=dt.UTC).isoformat()}}

    assert prior_ledger_summary(change_set, {"result": {}}) is None
    row = {"result": {"ledger": {"1": entry(1, "succeeded"), "2": entry(2, "attempted")}}}
    summary = prior_ledger_summary(change_set, row)
    assert summary is not None and summary["status"] == "partial"
    assert summary["applied"] == 1 and summary["guided_seqs"] == [2, 3] and summary["in_doubt_seqs"] == [2]
    assert set(summary["ledger"]) == {"1", "2"}
    only_doubt = prior_ledger_summary(change_set, {"result": {"ledger": {"2": entry(2, "in_doubt")}}})
    assert only_doubt is not None and only_doubt["status"] == "partial"
