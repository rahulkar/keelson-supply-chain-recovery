"""M7 advisory labelling + crawl-sync scheduling (``docs/contracts/110-m7-backoffice-generic.md`` §4).

* A plan built on a **crawled** snapshot (tier-A paging, ``consistency: crawled``) is advisory:
  ``recommended_option@1``, ``scenario_compare@1`` and ``narrative@1`` carry ``advisory`` (reason,
  text, ``as_of_range``), the narrative says so in words, and the scenario's ``data_source``
  assumption names it explicitly. A consistent (tier-B) snapshot is not advisory.
* The worker crawls tier-A connectors without tier B on the sync schedule: start-up request, a
  re-crawl request every ``scn_crawl_interval_s`` (``0`` = off) instead of a change-feed poll; a
  tier-B connector is never crawled.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from fakes.core import FakeCore
from fakes.harness import PRIYA, Harness
from fakes.scripts import chat_flow_script
from keelson_contracts.blocks import parse_block
from keelson_contracts.scn import CRAWLED_ADVISORY_TEXT

from keelson_engine.runtime.advisory import ADVISORY_SENTENCE, advisory_from_kpis, advisory_from_label
from keelson_engine.runtime.deps import ConnectorInfo
from keelson_engine.runtime.mcp_client import get_gateway
from keelson_engine.runtime.nodes import framing
from keelson_engine.runtime.problem import gather_problem_data
from keelson_engine.runtime.settings import WorkerSettings
from keelson_engine.worker import Worker

RANGE = {"from": "2026-10-05T06:00:00+00:00", "to": "2026-10-05T06:40:00+00:00"}
CRAWLED = {"consistency": "crawled", "as_of_range": RANGE}
DECIDER = {"sub": PRIYA["sub"], "username": PRIYA["username"], "name": PRIYA["name"]}


# ─────────────────────────────── advisory ───────────────────────────────


def _label_snapshots(monkeypatch: pytest.MonkeyPatch, label: dict[str, Any]) -> None:
    """The case's ProblemData comes from a "snapshot" with ``label`` (built from the Merxa stub's
    reads, like a crawl loads them) — no Postgres needed."""

    async def bundle(run: Any, connector: str, *, info: Any, planning_date: Any, snapshot: Any = None) -> Any:
        async with get_gateway().session(tenant=run.tenant, connector=connector, principal="reader") as session:
            caller = framing._RecordingCaller(session, run, connector)
            out = await gather_problem_data(run.tenant, session, lanes=info.lanes, as_of=planning_date,
                                            horizon_weeks=run.spec.planning.horizon_weeks, record=caller.record)
        out.source, out.snapshot_id, out.snapshot_label = "scn", "snap-crawl-1", dict(label)
        out.snapshot_as_of = dt.date(2026, 10, 5)
        return out

    monkeypatch.setattr(framing, "_snapshot_bundle", bundle)


async def _plan(h: Harness) -> dict[str, Any]:
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    row = h.row("chat", {"text": "Trims from Meghna are 7 days late"})
    status = await h.run(row)
    if status == "paused":  # the approach card (a snapshot-built scenario may need a confirmation)
        status = await h.run(h.resume_row(row, "confirm", [DECIDER]))
    assert status == "succeeded"
    plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    assert plan is not None
    return plan


async def test_plan_on_a_crawled_snapshot_is_advisory_everywhere(harness: Harness,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness
    _label_snapshots(monkeypatch, CRAWLED)
    plan = await _plan(h)
    assert plan["kpis"]["snapshot_label"] == CRAWLED and plan["kpis"]["snapshot_id"] == "snap-crawl-1"
    for kind in ("recommended_option", "scenario_compare", "narrative"):
        props = parse_block(h.core.repo.blocks_of(kind)[-1]).props
        advisory = props.advisory  # type: ignore[union-attr]
        assert advisory is not None, kind
        assert advisory.reason == "crawled_snapshot" and advisory.text == CRAWLED_ADVISORY_TEXT
        assert advisory.as_of_range.from_.isoformat() == RANGE["from"]  # type: ignore[union-attr]
    narrative = h.core.repo.blocks_of("narrative")[-1]
    assert ADVISORY_SENTENCE in parse_block(narrative).props.markdown  # type: ignore[union-attr]
    assert narrative["title"] == "Recommendation (advisory)"
    # the explicit assumption: where the numbers come from
    _, scenario = h.core.repo.scenarios[h.case_id][-1]
    source = next(a for a in scenario.assumptions if a.key == "data_source")
    assert "gathered by paging" in source.value_text and "advisory" in source.value_text
    # the plan stays applicable (per op, with approval as usual): the snapshot is pinned
    assert ("demo-retail", "snap-crawl-1") in [(t, s) for t, s, _ in h.core.scn.pins]


async def test_plan_on_a_consistent_snapshot_is_not_advisory(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness
    _label_snapshots(monkeypatch, {"consistency": "consistent", "as_of_range": None})
    plan = await _plan(h)
    assert advisory_from_kpis(plan["kpis"]) is None
    for kind in ("recommended_option", "scenario_compare", "narrative"):
        assert parse_block(h.core.repo.blocks_of(kind)[-1]).props.advisory is None  # type: ignore[union-attr]
    narrative = h.core.repo.blocks_of("narrative")[-1]
    assert narrative["title"] == "Recommendation" and ADVISORY_SENTENCE not in str(narrative)
    _, scenario = h.core.repo.scenarios[h.case_id][-1]
    source = next(a for a in scenario.assumptions if a.key == "data_source")
    assert "snapshot" in source.value_text and "advisory" not in source.value_text


@pytest.mark.parametrize("raw", [
    {"consistency": "crawled", "as_of_range": {"from": RANGE["to"], "to": RANGE["from"]}},  # reversed
    {"consistency": "crawled", "as_of_range": {"from": RANGE["from"], "to": "not a date"}},
    {"consistency": "crawled", "as_of_range": None},
    {"consistency": "crawled"},
])
def test_a_malformed_crawl_label_is_repaired_never_dropped(raw: dict[str, Any]) -> None:
    advisory = advisory_from_label(raw)
    assert advisory is not None and advisory.reason == "crawled_snapshot"
    rng = advisory.as_of_range
    assert rng is not None and rng.from_ <= rng.to  # type: ignore[union-attr]


@pytest.mark.parametrize("raw", [None, {}, {"consistency": "consistent"}, "crawled", {"consistency": "CRAWLED"}])
def test_only_crawled_labels_are_advisory(raw: Any) -> None:
    assert advisory_from_label(raw) is None


def test_advisory_sentence_has_no_numbers() -> None:
    """The narrative's advisory line is appended after the numbers rule ran: it must carry none."""
    assert not any(ch.isdigit() for ch in ADVISORY_SENTENCE)


# ─────────────────────────────── crawl-sync scheduling ───────────────────────────────

FAST = WorkerSettings(
    heartbeat_interval_s=0.02, cancel_poll_interval_s=0.02, reaper_interval_s=0.02, idle_poll_interval_s=0.02,
    shutdown_grace_s=0.5, snapshot_backoff_initial_s=0.01, snapshot_backoff_max_s=0.02, scn_due_interval_s=0.02,
    scn_sync_timeout_s=2.0, scn_poll_interval_s=0.03, scn_nightly_hour_utc=23, scn_crawl_interval_s=0.05,
)
NOON = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
TIERS = {"merxa": ("A", "B", "C", "W"), "lite": ("A",), "unknown": ()}


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _core(targets: list[tuple[str, str]], tiers: dict[str, tuple[str, ...]] | None = None) -> FakeCore:
    core = FakeCore()
    core.scn.targets = targets
    table = tiers or TIERS

    async def get_connector(*, tenant: str, slug: str) -> ConnectorInfo:
        return ConnectorInfo(slug=slug, name=slug, url="", issuer="", lanes=(), tiers=table[slug])

    core.connectors.get_connector = get_connector  # type: ignore[method-assign]
    return core


async def _run(worker: Worker, until: Any) -> None:
    task = asyncio.create_task(worker.run_forever())
    try:
        await _until(until)
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, 5)


async def test_tier_a_connectors_are_re_crawled_on_their_interval_never_polled() -> None:
    core = _core([("a", "merxa"), ("a", "lite")])
    worker = Worker(deps=core.deps(), settings=FAST, refresh_snapshot=False, clock=lambda: NOON)
    crawls = lambda: [s for s in worker.syncs if s[:3] == ("a", "lite", "crawl_interval")]  # noqa: E731
    await _run(worker, lambda: len(crawls()) >= 2 and len(core.scn.polls) >= 2)
    assert worker.crawl_targets == {("a", "lite")}
    assert worker.syncs[:2] == [("a", "merxa", "startup", "ok"), ("a", "lite", "startup", "ok")]
    assert all(outcome == "ok" for *_, outcome in crawls())
    # tier B (snapshot export) is never crawled; tier A has no change feed to poll
    assert not [r for r in core.scn.requests if r[2] == "crawl_interval" and r[1] != "lite"]
    assert set(core.scn.polls) == {("a", "merxa")}
    assert set(worker.crawl_requests) == {("a", "lite")}


async def test_crawl_interval_zero_only_syncs_on_start_up_and_due_marks() -> None:
    core = _core([("a", "lite")])
    worker = Worker(deps=core.deps(), settings=FAST.model_copy(update={"scn_crawl_interval_s": 0.0}),
                    refresh_snapshot=False, clock=lambda: NOON)
    await _run(worker, lambda: core.scn.due_calls >= 10)
    assert worker.crawl_requests == [] and worker.syncs == [("a", "lite", "startup", "ok")]
    assert core.scn.polls == []


async def test_unknown_tiers_are_never_crawled_on_the_interval() -> None:
    core = _core([("a", "unknown")])
    worker = Worker(deps=core.deps(), settings=FAST, refresh_snapshot=False, clock=lambda: NOON)
    await _run(worker, lambda: core.scn.due_calls >= 10 and len(core.scn.polls) >= 1)
    assert worker.crawl_targets == set() and worker.crawl_requests == []


async def test_a_failing_crawl_never_stops_the_loop_and_the_registry_outage_keeps_targets() -> None:
    core = _core([("a", "lite"), ("b", "lite")])
    core.scn.sync_errors[("a", "lite")] = RuntimeError("crawl failed: max_pages")
    worker = Worker(deps=core.deps(), settings=FAST, refresh_snapshot=False, clock=lambda: NOON)
    task = asyncio.create_task(worker.run_forever())
    try:
        await _until(lambda: ("b", "lite", "crawl_interval", "ok") in worker.syncs)
        assert ("a", "lite", "crawl_interval", "failed") in worker.syncs

        async def down(*, tenant: str, slug: str) -> ConnectorInfo:
            raise ConnectionError("registry down")

        core.connectors.get_connector = down  # type: ignore[method-assign]
        seen = len(worker.crawl_requests)
        await _until(lambda: len(worker.crawl_requests) >= seen + 4)
        assert worker.crawl_targets == {("a", "lite"), ("b", "lite")}  # kept while the registry is down
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, 5)
