"""M3 workflow end to end (``80-m3-deterministic-core.md`` §4) with FakeLlm, the Merxa and optimizer stubs.

* Snapshot path (needs the throwaway Postgres): ProblemData from the full-sized scn snapshot, the
  plan pins its snapshot, candidates A–D + do nothing (all applicable: the stub publishes Merxa's
  ops), the review blocks incl. map / graph / timeline payloads behind ``data_ref``; then applying
  the Hybrid option compiles every op (allocation groups expanded per store from the pinned
  snapshot, atomic groups kept), needs a human approver, applies and marks the snapshot stale.
* Fallbacks (no database): an unreadable snapshot store → live MCP reads labelled advisory;
  guided-only options cannot be applied.
"""

from __future__ import annotations

import uuid

import pytest
from fakes.harness import MARCO, PRIYA, Harness
from fakes.merxa_stub import MerxaStub
from fakes.scripts import chat_flow_script
from keelson_contracts.blocks import parse_block, parse_block_data
from keelson_contracts.changeset import change_set_hash
from keelson_contracts.optimizer import LEVER_OPS
from sqlalchemy.exc import ProgrammingError

from keelson_engine.runtime.problem_scn import GROUP_ALLOC_PREFIX
from keelson_engine.runtime.scn_store import SnapshotRef

DECIDER_MARCO = {"sub": MARCO["sub"], "username": MARCO["username"], "name": MARCO["name"], "role": "approver",
                 "roles": MARCO["roles"]}
DECIDER_LENA = {"sub": "sub-lena", "username": "lena", "name": "Lena Vogel", "role": "compliance",
                "roles": ["compliance"]}
ALL_OPS = tuple(sorted({op for ops in LEVER_OPS.values() for op in ops}))


async def _plan(h: Harness) -> None:
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    row = h.row("chat", {"text": "Trims from Meghna are 7 days late"})
    assert await h.run(row) == "succeeded"
    assert not h.core.interrupts.created


async def _scn_db() -> object:
    import asyncpg
    from fakes.scn_fixture import ADMIN_DSN, ScnDatabase

    try:
        conn = await asyncpg.connect(ADMIN_DSN, timeout=3)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"no throwaway Postgres ({type(exc).__name__}); set SCN_TEST_ADMIN_DSN")
    await conn.close()
    return await ScnDatabase().open()


async def test_snapshot_plan_blocks_apply_hybrid(tmp_path: object) -> None:
    from fakes.scn_fixture import full_snapshot

    db = await _scn_db()
    h = await Harness(tmp=tmp_path, merxa=MerxaStub(supported_ops=ALL_OPS)).setup()  # type: ignore[arg-type]
    try:
        snapshot_id = str(uuid.uuid4())
        await db.load("demo-retail", snapshot_id, full_snapshot())  # type: ignore[attr-defined]
        h.core.scn.snapshot = SnapshotRef(id=snapshot_id, connector="merxa")
        h.core.scn.reader_factory = db.reader  # type: ignore[attr-defined]
        await _plan(h)

        # ── frame: ProblemData from the snapshot, BOM walk on it, no live gathering reads ──
        [problem_ev] = h.core.repo.evidence_by_tool("problem_data")
        assert problem_ev["args"]["source"] == "scn" and problem_ev["args"]["snapshot_id"] == snapshot_id
        assert h.core.repo.evidence_by_tool("scn_snapshot")
        called = {tool for tool, _ in h.merxa.calls}
        assert not called & {"list_locations", "list_purchase_orders", "get_purchase_order", "get_bom_where_used"}
        card = parse_block(h.core.repo.blocks_of("approach_card")[-1])
        assert any(a.key == "data_source" and a.value_text == "Merxa snapshot" for a in card.props.assumptions)
        assert [c.task_id for c in card.props.checks if not isinstance(c, str)] == ["t1"]  # M4: the plan's checks
        scenario = (await h.core.repo.latest_scenario(tenant="demo-retail", case_id=h.case_id)).scenario
        shifted = set(scenario.perturbations[0].target.ids)
        assert shifted and all(ref.startswith("PO-BD-") for ref in shifted)

        # ── simulate: candidates, pin ──
        plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
        options = {o["id"]: o for o in plan["options"]}
        assert list(options) == ["opt-A", "opt-B", "opt-C", "opt-D", "do-nothing"]
        assert not any(o["guided_only"] for o in options.values())
        assert plan["kpis"]["snapshot_id"] == snapshot_id and plan["kpis"]["data_source"] == "scn"
        assert plan["kpis"]["capabilities"] == {"ops_source": "tool_schema", "missing_tools": []}
        assert h.core.scn.pins == [("demo-retail", snapshot_id, plan["id"])]
        assert {a.get("group") for a in options["opt-D"]["actions"]} >= {"exp-1", "alt-1", "rsq-1"}
        assert all(o["scorecard"]["changes"] == len(o["actions"]) for o in options.values())
        assert [lv["code"] for lv in h.optimizer.requests[-2][1]["plan_spec"]["levers"]] == [
            "reallocate_inventory", "expedite_mode_shift", "resource_alt_supplier", "prioritize_allocation",
            "resequence_production"]
        assert all(body["include_graph"] for path, body in h.optimizer.requests if path == "/v1/impact"
                   and body.get("include_graph"))

        # ── compose: blocks and data payloads ──
        kinds = [b["envelope"]["kind"] for b in h.core.repo.blocks if b["run_id"] == h.core.repo.blocks[-1]["run_id"]]
        for kind in ("scenario_compare", "network_map", "impact_graph", "stockout_timeline", "plan_diff", "evidence_pack"):
            assert kind in kinds
        compare = parse_block(h.core.repo.blocks_of("scenario_compare")[-1])
        assert [o.id for o in compare.props.options] == list(options)
        assert [o.id for o in compare.props.options if o.recommended] == ["opt-A"]
        for kind in ("network_map", "impact_graph", "stockout_timeline"):
            env = h.core.repo.blocks_of(kind)[-1]
            assert env["data_ref"] == f"/api/blocks/{env['id']}/data" and env["placement"] == "canvas"
            payload = parse_block_data(f"{kind}@1", h.core.repo.block_data(env["id"]))
            if kind == "network_map":
                links = {ln.id: ln for ln in payload.links}
                sea = links["lane:CGP-FXT-SEA+FXT-UK1-RD"]
                assert len(sea.path) > 20 and sea.status == "impacted"
                assert any(ln.option_id == "opt-D" and ln.kind == "nearshore" for ln in payload.links)
                assert any(ln.option_id == "opt-B" and ln.kind == "sea_air" for ln in payload.links)
                assert "factory:F-BD-01" in {n.id for n in payload.nodes}
            if kind == "stockout_timeline":
                assert all(set(r.by_option) == set(options) for r in payload.rows)
        evidence = parse_block(h.core.repo.blocks_of("evidence_pack")[-1])
        assert any(i.label.startswith("scn snapshot") for i in evidence.props.items)

        # ── apply the Hybrid option ──
        for a in options["opt-D"]["actions"]:  # the small Merxa stub stocks the full fixture's transfer SKUs
            if a["op"] == "transfer_order.create":
                for line in a["payload"]["lines"]:
                    for dc in (a["payload"]["from_dc"], a["payload"]["to_dc"]):
                        h.merxa.inventory[("dc", dc, line["sku"])] = {"on_hand": 5000, "allocated": 0, "reserved": 0,
                                                                     "in_transit": 0}
        action = h.row("action", {"type": "apply", "option_id": "opt-D", "plan_version": 1}, PRIYA)
        assert await h.run(action) == "paused", h.core.repo.messages[-1]["text"]
        [cs] = h.core.repo.change_sets.values()
        items = cs["items"]
        assert cs["status"] == "validated" and cs["hash"] == change_set_hash(items)
        ops = [i["op"] for i in items]
        assert set(ops) == {"transfer_order.create", *ALL_OPS} - {"po_line.cancel"}
        alloc = [i for i in items if i["op"] == "allocation.set_qty"]
        assert alloc and all(i["target_ref"].startswith("AL-") and i["expected_version"] >= 1 for i in alloc)
        planned = next(a for a in options["opt-D"]["actions"] if a["op"] == "allocation.set_qty")
        assert planned["target_ref"].startswith(GROUP_ALLOC_PREFIX)
        assert {i["group"] for i in alloc} == {f"alloc:{planned['target_ref']}"}
        groups = [i.get("group") for i in items]
        for g in {g for g in groups if g}:  # atomic groups are contiguous in the change set
            idx = [k for k, x in enumerate(groups) if x == g]
            assert idx == list(range(idx[0], idx[-1] + 1))
        approval = parse_block(h.core.repo.blocks_of("approval_card")[-1])
        assert "expedite, near-shore, resequencing" in approval.props.reason
        diff = parse_block(h.core.repo.blocks_of("plan_diff")[-1])
        assert len(diff.props.rows) == len(items) and any("applied together" in r.target_label for r in diff.props.rows)

        created = h.core.interrupts.created[-1]
        # M5 (95 §1 rule 3): po.create / resequencing → four-eyes (an approver + compliance)
        assert created["quorum_spec"] == {"roles": ["approver", "compliance"], "count": 2, "exclude_initiator": True}
        resume = h.resume_row(action, "approve", [DECIDER_MARCO, DECIDER_LENA], plan_version=created["plan_version"],
                              change_set_hash=created["change_set_hash"])
        assert await h.run(resume) == "succeeded"
        assert h.merxa.apply_calls == 1
        assert h.core.repo.change_sets[cs["id"]]["status"] == "verified"
        assert h.core.scn.stale == [("demo-retail", "merxa", "change_set.applied")]
    finally:
        await h.close()
        await db.close()  # type: ignore[attr-defined]


async def test_unreadable_snapshot_store_falls_back_to_advisory_mcp_reads(harness: Harness) -> None:
    h = harness
    h.core.scn.current_error = ProgrammingError("SELECT", {}, Exception('relation "scn_current" does not exist'))
    await _plan(h)
    [problem_ev] = h.core.repo.evidence_by_tool("problem_data")
    assert problem_ev["args"]["source"] == "mcp" and problem_ev["args"]["snapshot_id"] is None
    card = parse_block(h.core.repo.blocks_of("approach_card")[-1])
    source = next(a for a in card.props.assumptions if a.key == "data_source")
    assert "advisory" in source.value_text and source.prov == "fact"
    plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    assert plan["kpis"]["data_source"] == "mcp" and h.core.scn.pins == []
    # the advisory map still places the DCs published by list_locations, with the transfer lanes
    env = h.core.repo.blocks_of("network_map")[-1]
    data = parse_block_data("network_map@1", h.core.repo.block_data(env["id"]))
    assert {n.id for n in data.nodes} == {"dc:UK1", "dc:NL1", "dc:DE1", "dc:PL1"}
    assert {ln.kind for ln in data.links} == {"transfer"}
    assert any(ln.option_id == "opt-A" for ln in data.links)


async def test_snapshot_build_failure_falls_back(harness: Harness) -> None:
    h = harness
    h.core.scn.snapshot = SnapshotRef(id=str(uuid.uuid4()), connector="merxa")  # no reader configured
    await _plan(h)
    [problem_ev] = h.core.repo.evidence_by_tool("problem_data")
    assert problem_ev["args"]["source"] == "mcp"


async def test_guided_only_option_cannot_be_applied(harness: Harness) -> None:
    h = harness
    h.core.connectors.merxa_config = {"supported_ops": ["allocation.set_qty"]}  # transfers cannot be applied
    await _plan(h)
    plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    guided = next(o for o in plan["options"] if o["guided_only"])
    action = h.row("action", {"type": "apply", "option_id": guided["id"], "plan_version": 1}, PRIYA)
    assert await h.run(action) == "succeeded"
    assert not h.core.repo.change_sets and h.merxa.apply_calls == 0
    assert "cannot apply" in h.core.repo.messages[-1]["text"]


async def test_recommended_guided_option_has_apply_disabled(harness: Harness) -> None:
    from fakes.scripts import recommendation

    h = harness
    h.core.connectors.merxa_config = {"supported_ops": ["allocation.set_qty"]}  # transfers cannot be applied
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    h.script._turns["recommend"] = [recommendation(option="opt-B")]
    row = h.row("chat", {"text": "Trims from Meghna are 7 days late"})
    assert await h.run(row) == "succeeded"
    rec = parse_block(h.core.repo.blocks_of("recommended_option")[-1])
    assert rec.props.option.id == "opt-B"
    apply = next(a for a in rec.props.actions if a.type == "apply")
    assert not apply.enabled and "cannot apply" in (apply.disabled_reason or "")
    guide = next(a for a in rec.props.actions if a.type == "guide")  # M5: Guide me is offered instead
    assert guide.enabled and rec.props.remediation_mix.apply_lines == 0


async def test_recommended_mixed_option_offers_apply_and_guide(harness: Harness) -> None:
    """M5 (95 §2): the hybrid's transfer applies; its allocation change (not applicable here) is guided."""
    from fakes.scripts import recommendation

    h = harness
    h.core.connectors.merxa_config = {"supported_ops": ["transfer_order.create"]}
    original = h.optimizer._lever_actions

    def with_reschedule(req: object) -> object:  # the hybrid also moves a PO line (not applicable here)
        actions, decisions, cost = original(req)  # type: ignore[arg-type]
        if req.plan_spec.id == "opt-D":  # type: ignore[attr-defined]
            actions.append({"op": "po_line.reschedule", "target_ref": "PO-BD-0001#1", "expected_version": 1,
                            "payload": {"eta_dc": "2026-11-05"}, "seq": len(actions) + 1})
        return actions, decisions, cost

    h.optimizer._lever_actions = with_reschedule  # type: ignore[method-assign]
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    h.script._turns["recommend"] = [recommendation(option="opt-D")]
    row = h.row("chat", {"text": "Trims from Meghna are 7 days late"})
    assert await h.run(row) == "succeeded"
    rec = parse_block(h.core.repo.blocks_of("recommended_option")[-1])
    assert rec.props.option.id == "opt-D"
    mix = rec.props.remediation_mix
    assert mix is not None and mix.apply_lines >= 1 and mix.guide_lines >= 1
    labels = {a.type: (a.label, a.enabled) for a in rec.props.actions}
    s1 = "s" if mix.apply_lines != 1 else ""
    assert labels["apply"] == (f"Apply {mix.apply_lines} change{s1} for me", True)
    assert labels["guide"] == (f"Guide me through {mix.guide_lines}", True)


async def test_replanning_releases_the_superseded_plans_snapshot_pin(harness: Harness) -> None:
    """Review regression: every plan pins its snapshot, and only the latest plan version can be
    applied — so a new plan version releases the previous version's pin (otherwise every
    superseded plan keeps a full snapshot from the GC forever)."""
    h = harness
    await _plan(h)
    first = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    assert h.core.scn.unpins == []
    more = chat_flow_script(h.core.repo, delay_days=10)  # the runner keeps the first script: extend it
    for agent, turns in more._turns.items():
        h.script.add(agent, *turns)
    row = h.row("chat", {"text": "Make that 10 days late"})
    assert await h.run(row) == "succeeded"
    second = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    assert second["version"] == first["version"] + 1 and second["id"] != first["id"]
    assert h.core.scn.unpins == [("demo-retail", first["id"])]


async def test_oversized_plans_and_atomic_groups_are_refused_not_split(harness: Harness) -> None:
    """A plan larger than one change set is refused (never split into several change sets); an
    atomic group larger than one change set gets its own message (review regression: it used to
    surface as "allocation changes could not be matched")."""
    import datetime as dt

    from keelson_engine.runtime.spec import AgentSpec, PlanningDefaults

    h = harness
    h.spec = lambda tenant: AgentSpec(  # type: ignore[method-assign]
        tenant=tenant, planning=PlanningDefaults(as_of=dt.date(2026, 10, 5), max_change_set_items=1))
    await _plan(h)
    plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    option = next(o for o in plan["options"] if o["actions"] and not o["guided_only"] and not o["fallback"])
    if len(option["actions"]) < 2:  # the stored plan: a second change line (the cap is 1)
        option["actions"].append({**option["actions"][0], "seq": len(option["actions"]) + 1})
    action = h.row("action", {"type": "apply", "option_id": option["id"], "plan_version": 1}, PRIYA)
    assert await h.run(action) == "succeeded"
    assert not h.core.repo.change_sets and h.merxa.apply_calls == 0
    assert "more than the 1 Merxa accepts in one change batch" in h.core.repo.messages[-1]["text"]
    for a in option["actions"]:  # the stored plan: make every action one atomic group
        a["group"] = "grp-1"
    action = h.row("action", {"type": "apply", "option_id": option["id"], "plan_version": 1}, PRIYA)
    assert await h.run(action) == "succeeded"
    assert not h.core.repo.change_sets
    assert "must be applied together" in h.core.repo.messages[-1]["text"]


async def test_malformed_snapshot_rows_fall_back_to_advisory_reads(harness: Harness) -> None:
    """Review regression: rows of an unexpected shape (a NULL where the builder needs a value
    raises ``TypeError``) fall back to live reads labelled advisory instead of failing the run."""
    import contextlib

    class BadReader:
        async def fetch(self, sql: str, params: object) -> list[dict[str, object]]:
            raise TypeError("'<=' not supported between instances of 'NoneType' and 'datetime.date'")

    @contextlib.asynccontextmanager
    async def reader(tenant: str):  # type: ignore[no-untyped-def]
        yield BadReader()

    h = harness
    h.core.scn.snapshot = SnapshotRef(id=str(uuid.uuid4()), connector="merxa")
    h.core.scn.reader_factory = reader
    await _plan(h)
    [problem_ev] = h.core.repo.evidence_by_tool("problem_data")
    assert problem_ev["args"]["source"] == "mcp" and problem_ev["args"]["snapshot_id"] is None
