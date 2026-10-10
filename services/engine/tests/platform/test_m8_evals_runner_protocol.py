"""M8 in-app eval runner protocol (``docs/contracts/128-m8-evals-ui.md`` §4): token gate, heartbeat,
claim with a lease (one job for concurrent claims, expired leases reaped as ``runner_lost`` with
their results kept), progress events (server ``seq``, ``after`` paging), the cancel / budget flags,
attaching Keelson cases, sanitised + engine-merged case results, and the authoritative finish."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text

from e1_browser import Browser
from e1_env import PUBLIC, TENANT_A, TENANT_B
from keelson_engine.config import Settings
from keelson_engine.core import evals, repo, runs
from keelson_engine.core.db import system_session
from m8_evals import RUNNER, RUNNER_TOKEN, case_doc, clean_evals, heartbeat, runner_call

pytestmark = pytest.mark.asyncio(loop_scope="session")

ACTOR = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}


@pytest_asyncio.fixture(loop_scope="session")
async def runner_on(platform: None, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setattr(settings, "evals_runner_token", SecretStr(RUNNER_TOKEN))
    await clean_evals()
    yield
    await clean_evals()


@pytest_asyncio.fixture(loop_scope="session")
async def ava(browser_factory: Any, runner_on: None) -> Browser:
    return await browser_factory("ava")


async def _queue(api: httpx.AsyncClient, ava: Browser, cases: list[str] | None = None, **body: Any) -> dict[str, Any]:
    assert (await heartbeat(api)).status_code == 200
    resp = await ava.post("/api/admin/evals/runs", json={"acknowledge_reset": True,
                                                         "case_ids": cases or ["trim_delay_actual",
                                                                               "store_shortage_question"], **body})
    assert resp.status_code == 202, resp.text
    return dict(resp.json())


async def _claim(api: httpx.AsyncClient, runner_id: str = RUNNER) -> dict[str, Any]:
    resp = await runner_call(api, "POST", "/claim", {"runner_id": runner_id})
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["job"])


async def _new_case(tenant: str = TENANT_A) -> str:
    return await repo.create_case("Eval case", created_by=ACTOR, tenant=tenant)


# ───────────────────────────── token gate ─────────────────────────────


async def test_runner_api_is_off_without_a_token_and_needs_the_right_one(
        api: httpx.AsyncClient, platform: None, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "evals_runner_token", SecretStr(""))
    assert (await heartbeat(api)).status_code == 404
    monkeypatch.setattr(settings, "evals_runner_token", SecretStr(RUNNER_TOKEN))
    await clean_evals()
    wrong = await runner_call(api, "POST", "/claim", {"runner_id": RUNNER}, token="nope")
    assert wrong.status_code == 401 and wrong.json()["code"] == "unauthorized"
    no_origin = await api.post("/api/internal/evals/runner/claim", json={"runner_id": RUNNER},
                               headers={"Authorization": f"Bearer {RUNNER_TOKEN}"})
    assert no_origin.status_code == 403 and no_origin.json()["code"] == "bad_origin"
    assert (await runner_call(api, "POST", "/claim", {"runner_id": RUNNER})).status_code == 204
    bad = await runner_call(api, "POST", "/heartbeat", {"runner_id": RUNNER, "version": "x",
                                                        "suite": {"suite_hash": "x", "cases": []}})
    assert bad.status_code == 422
    # a catalogue the model accepts but the table's 256 KiB does not: a 422, never a 500
    wide = {"title": "\U0001f600" * 200, "tags": ["\U0001f600" * 40] * 20, "turns": 1, "turn_kinds": ["message"],
            "needs_live_embeddings": False, "injects": False}
    huge = await heartbeat(api, cases=[{**wide, "id": f"case_{i}"} for i in range(200)])
    assert huge.status_code == 422 and huge.json()["code"] == "suite_too_large", huge.text


# ───────────────────────────── the happy path ─────────────────────────────


async def test_claim_events_attach_results_finish(api: httpx.AsyncClient, ava: Browser) -> None:
    run = await _queue(api, ava)
    assert run["status"] == "queued" and run["mode"] == "replay" and run["source"] == "in_app"
    job = await _claim(api)
    assert job["run_id"] == run["id"] and job["mode"] == "replay" and job["budget"] is None
    assert job["case_ids"] == ["trim_delay_actual", "store_shortage_question"]
    assert (await runner_call(api, "POST", "/claim", {"runner_id": "runner-2"})).status_code == 204

    rid = run["id"]
    flags = await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": [
        {"type": "run_started", "data": {"mode": "replay", "cases": 2}},
        {"type": "case_started", "case_id": "trim_delay_actual", "data": {"title": "Trim"}},
        {"type": "turn_started", "case_id": "trim_delay_actual", "turn_id": "report", "data": {"kind": "message"}},
    ]})
    assert flags.status_code == 200 and flags.json()["cancel_requested"] is False
    assert flags.json()["budget_exhausted"] is False
    progress = (await ava.get(f"/api/admin/evals/runs/{rid}")).json()["run"]["progress"]
    assert progress == {"cases_done": 0, "cases_total": 2, "current_case": "trim_delay_actual",
                        "current_turn": "report"}

    kc = await _new_case()
    attach = await runner_call(api, "POST", f"/runs/{rid}/cases/trim_delay_actual/attach",
                               {"runner_id": RUNNER, "keelson_case_id": kc})
    assert attach.status_code == 200
    link = await evals.get_eval_link(TENANT_A, kc)
    assert link is not None and link.llm_mode == "replay" and link.run_id == rid and link.status == "running"

    stored = await runner_call(api, "PUT", f"/runs/{rid}/cases/trim_delay_actual/result",
                               {"runner_id": RUNNER, "result": case_doc("trim_delay_actual", case_id=kc)})
    assert stored.status_code == 200 and stored.json()["status"] == "pass"
    stored = await runner_call(api, "PUT", f"/runs/{rid}/cases/store_shortage_question/result",
                               {"runner_id": RUNNER, "result": case_doc("store_shortage_question", "fail")})
    assert stored.status_code == 200
    page1 = (await ava.get(f"/api/admin/evals/runs/{rid}/events?after=0")).json()
    assert [e["seq"] for e in page1["items"]] == [1, 2, 3] and page1["next_after"] == 3
    assert (await ava.get(f"/api/admin/evals/runs/{rid}/events?after=3")).json()["items"] == []

    done = await runner_call(api, "POST", f"/runs/{rid}/finish", {"runner_id": RUNNER, "status": "succeeded"})
    assert done.status_code == 200 and done.json()["status"] == "succeeded"
    detail = (await ava.get(f"/api/admin/evals/runs/{rid}")).json()
    summary = detail["run"]["summary"]
    assert (summary["cases"], summary["passed"], summary["failed"], summary["pass_rate_bp"]) == (2, 1, 1, 5000)
    outcomes = {t["metric"]: t["outcome"] for t in detail["run"]["threshold_results"]}
    assert outcomes["pass_rate_bp"] == "breach" and outcomes["options_max_ms"] == "not_applicable"
    assert detail["run"]["breaches"] == 1 and detail["run"]["finished_at"] is not None
    rows = {c["case_id"]: c for c in detail["cases"]}
    assert rows["trim_delay_actual"]["keelson_case_id"] == kc and rows["trim_delay_actual"]["ordinal"] == 0
    assert rows["store_shortage_question"]["failed_checks"] == ["report.run"]
    case = (await ava.get(f"/api/admin/evals/runs/{rid}/cases/trim_delay_actual")).json()
    assert case["turns"][0]["timings"]["options_ms"] == 100_000 and case["engine"]["llm_calls_live"] == 0
    async with system_session() as s:
        audit = (await s.execute(text("SELECT actor_username, after FROM admin_audit WHERE action = "
                                      "'evals.run_finished' AND target = :t"), {"t": f"eval_run:{rid}"})).all()
        sha = (await s.execute(text("SELECT doc_sha256 FROM eval_run WHERE id = :id"), {"id": rid})).scalar_one()
    assert len(audit) == 1 and audit[0][0] == "evals-runner" and sha is not None
    # the lease is gone: the runner can no longer report
    late = await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": []})
    assert late.status_code == 409 and late.json()["code"] == "run_not_running"


async def test_concurrent_claims_hand_out_one_job(api: httpx.AsyncClient, ava: Browser) -> None:
    await _queue(api, ava)
    answers = await asyncio.gather(*(runner_call(api, "POST", "/claim", {"runner_id": f"runner-{i}"})
                                     for i in range(4)))
    assert sorted(a.status_code for a in answers) == [200, 204, 204, 204]


async def test_an_expired_lease_fails_the_run_and_keeps_its_results(api: httpx.AsyncClient, ava: Browser) -> None:
    run = await _queue(api, ava)
    await _claim(api)
    rid = run["id"]
    await runner_call(api, "PUT", f"/runs/{rid}/cases/trim_delay_actual/result",
                      {"runner_id": RUNNER, "result": case_doc("trim_delay_actual")})
    async with system_session() as s:
        await s.execute(text("UPDATE eval_run SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
                        {"id": rid})
    status = (await ava.get("/api/admin/evals/status")).json()
    assert status["active_run"] is None
    detail = (await ava.get(f"/api/admin/evals/runs/{rid}")).json()
    assert detail["run"]["status"] == "failed" and detail["run"]["error"].startswith("runner_lost")
    assert [c["case_id"] for c in detail["cases"]] == ["trim_delay_actual"]
    assert detail["run"]["summary"]["cases"] == 1
    gone = await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": []})
    assert gone.status_code == 409
    # a new run can start (the active slot is free)
    await _queue(api, ava)


async def test_events_need_the_lease_holder_and_are_capped(api: httpx.AsyncClient, ava: Browser) -> None:
    run = await _queue(api, ava)
    await _claim(api)
    rid = run["id"]
    other = await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": "runner-x", "events": []})
    assert other.status_code == 409
    many = [{"type": "note", "data": {"i": i}} for i in range(51)]
    assert (await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": many})).status_code == 422
    nested = [{"type": "note", "data": {"x": {"y": 1}}}]
    assert (await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": nested})).status_code == 422
    ok = await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": [
        {"type": "note", "data": {"text": "a‮b" + "x" * 400}}]})
    assert ok.status_code == 422  # data strings are capped at 300 characters
    ok = await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": [
        {"type": "note", "data": {"text": "a‮b"}}]})
    assert ok.status_code == 200
    [event] = (await ava.get(f"/api/admin/evals/runs/{rid}/events")).json()["items"]
    assert event["data"]["text"] == "ab"
    # 20 keys x 300 four-byte characters pass the model but not the table's 16 KiB: a 422, never a 500
    big = await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": [
        {"type": "note", "data": {f"k{i}": "\U0001f600" * 300 for i in range(20)}}]})
    assert big.status_code == 422 and big.json()["code"] == "event_too_large", big.text
    assert len((await ava.get(f"/api/admin/evals/runs/{rid}/events")).json()["items"]) == 1


async def test_a_runner_working_on_a_run_stays_online(api: httpx.AsyncClient, ava: Browser) -> None:
    """A long run sends no heartbeat (the loop heartbeats between runs): every call on the run
    counts as a sign of life, so the status does not call a busy runner offline."""
    run = await _queue(api, ava)
    await _claim(api)
    async with system_session() as s:
        await s.execute(text("UPDATE eval_runner SET last_seen_at = now() - interval '10 minutes'"))
    assert (await ava.get("/api/admin/evals/status")).json()["runner"]["online"] is False
    await runner_call(api, "POST", f"/runs/{run['id']}/events", {"runner_id": RUNNER, "events": []})
    assert (await ava.get("/api/admin/evals/status")).json()["runner"]["online"] is True


# ───────────────────────────── cancel & budget ─────────────────────────────


async def test_cancel_a_running_run_flags_the_runner_and_cancels_its_keelson_runs(
        api: httpx.AsyncClient, ava: Browser) -> None:
    run = await _queue(api, ava)
    await _claim(api)
    rid = run["id"]
    kc = await _new_case()
    await runner_call(api, "POST", f"/runs/{rid}/cases/trim_delay_actual/attach",
                      {"runner_id": RUNNER, "keelson_case_id": kc})
    keelson_run = await runs.enqueue_run(tenant=TENANT_A, case_id=kc, kind="chat", payload={"text": "hello"},
                                         actor=ACTOR, idempotency_key=None, user_message="hello")
    resp = await ava.post(f"/api/admin/evals/runs/{rid}/cancel")
    assert resp.status_code == 202 and resp.json()["cancel_requested"] is True and resp.json()["status"] == "running"
    row = await runs.get_run(TENANT_A, keelson_run)
    assert row is not None and row.status == "cancelled"
    flags = (await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": []})).json()
    assert flags["cancel_requested"] is True
    # the next run of the linked case does not start (runtime pre-check reads the link)
    link = await evals.get_eval_link(TENANT_A, kc)
    assert link is not None and link.cancel_requested
    done = await runner_call(api, "POST", f"/runs/{rid}/finish", {"runner_id": RUNNER, "status": "succeeded"})
    assert done.json()["status"] == "cancelled"
    again = await ava.post(f"/api/admin/evals/runs/{rid}/cancel")
    assert again.status_code == 409 and again.json()["code"] == "run_not_active"


async def test_cancel_a_queued_run_ends_it_at_once(api: httpx.AsyncClient, ava: Browser) -> None:
    run = await _queue(api, ava)
    resp = await ava.post(f"/api/admin/evals/runs/{run['id']}/cancel")
    assert resp.status_code == 202 and resp.json()["status"] == "cancelled" and resp.json()["finished_at"]
    assert (await runner_call(api, "POST", "/claim", {"runner_id": RUNNER})).status_code == 204


async def test_live_budget_flag_and_engine_accounting(api: httpx.AsyncClient, ava: Browser, settings: Settings,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "evals_live_enabled", True)
    run = await _queue(api, ava, ["trim_delay_actual"], mode="record_or_replay", confirm_live="RUN LIVE",
                       budget={"max_tokens": 1000, "max_cost_microusd": 50_000})
    assert run["budget"] == {"max_tokens": 1000, "max_cost_microusd": 50_000}
    job = await _claim(api)
    assert job["budget"] == {"max_tokens": 1000, "max_cost_microusd": 50_000}
    rid = run["id"]
    kc = await _new_case()
    await runner_call(api, "POST", f"/runs/{rid}/cases/trim_delay_actual/attach",
                      {"runner_id": RUNNER, "keelson_case_id": kc})
    await evals.add_eval_usage(TENANT_A, kc, rid, calls_live=3, calls_replayed=1, tokens_live=600, tokens_replayed=50,
                               misses=0, cost_microusd=1200)
    flags = (await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": []})).json()
    assert flags["budget_exhausted"] is False
    await evals.add_eval_usage(TENANT_A, kc, rid, calls_live=2, calls_replayed=0, tokens_live=500, tokens_replayed=0,
                               misses=0, cost_microusd=800)
    flags = (await runner_call(api, "POST", f"/runs/{rid}/events", {"runner_id": RUNNER, "events": []})).json()
    assert flags["budget_exhausted"] is True
    counters = (await ava.get(f"/api/admin/evals/runs/{rid}")).json()["run"]["counters"]
    assert counters == {"tokens_live": 1100, "tokens_replayed": 50, "llm_calls_live": 5, "llm_calls_replayed": 1,
                        "replay_misses": 0, "cost_microusd": 2000}
    link = await evals.get_eval_link(TENANT_A, kc)
    assert link is not None and link.tokens_live == 1100 and link.budget_max_tokens == 1000
    stored = await runner_call(api, "PUT", f"/runs/{rid}/cases/trim_delay_actual/result",
                               {"runner_id": RUNNER, "result": case_doc("trim_delay_actual", case_id=kc)})
    case = (await ava.get(f"/api/admin/evals/runs/{rid}/cases/trim_delay_actual")).json()
    assert stored.status_code == 200 and case["engine"] == {"llm_calls_live": 5, "llm_calls_replayed": 1,
                                                            "tokens_live": 1100, "replay_misses": 0,
                                                            "cost_microusd": 2000}


# ───────────────────────────── attach & results ─────────────────────────────


async def test_attach_rules(api: httpx.AsyncClient, ava: Browser) -> None:
    before = await _new_case()  # created before the run started
    run = await _queue(api, ava)
    await _claim(api)
    rid = run["id"]

    async def attach(case: str, keelson: str) -> httpx.Response:
        return await runner_call(api, "POST", f"/runs/{rid}/cases/{case}/attach",
                                 {"runner_id": RUNNER, "keelson_case_id": keelson})

    async with system_session() as s:  # the claim truncates started_at to the second: make "before" clearly older
        await s.execute(text('UPDATE "case" SET created_at = now() - interval \'1 hour\' WHERE id = :id'),
                        {"id": before})
    foreign = await _new_case(TENANT_B)
    kc = await _new_case()
    assert (await attach("trim_delay_actual", foreign)).json()["code"] == "case_not_found"
    assert (await attach("trim_delay_actual", "no-such-case")).status_code == 404
    assert (await attach("trim_delay_actual", before)).json()["code"] == "case_predates_run"
    assert (await attach("not_in_run", kc)).json()["code"] == "unknown_case"
    assert (await attach("trim_delay_actual", kc)).status_code == 200
    assert (await attach("trim_delay_actual", kc)).status_code == 200  # idempotent
    relink = await attach("store_shortage_question", kc)
    assert relink.status_code == 409 and relink.json()["code"] == "case_already_linked"


async def test_results_are_sanitised_and_merged_with_the_engine_accounting(api: httpx.AsyncClient,
                                                                         ava: Browser) -> None:
    run = await _queue(api, ava)
    await _claim(api)
    rid = run["id"]
    kc = await _new_case()
    await runner_call(api, "POST", f"/runs/{rid}/cases/trim_delay_actual/attach",
                      {"runner_id": RUNNER, "keelson_case_id": kc})
    await evals.add_eval_usage(TENANT_A, kc, rid, calls_live=0, calls_replayed=2, tokens_live=0, tokens_replayed=900,
                               misses=1, cost_microusd=0)
    doc = case_doc("trim_delay_actual", "fail", title="Trim‮ delay\x07", case_id=kc)
    doc["turns"][0]["checks"] = [{"name": f"c{i}", "ok": False, "detail": "<b>x</b>" * 600} for i in range(400)]
    stored = await runner_call(api, "PUT", f"/runs/{rid}/cases/trim_delay_actual/result",
                               {"runner_id": RUNNER, "result": doc})
    assert stored.status_code == 200
    assert stored.json()["status"] == "error" and stored.json()["error_code"] == "replay_miss"  # merged miss
    case = (await ava.get(f"/api/admin/evals/runs/{rid}/cases/trim_delay_actual")).json()
    assert case["title"] == "Trim delay" and len(case["turns"][0]["checks"]) == 300
    assert case["engine"]["replay_misses"] == 1 and case["keelson_case_id"] == kc
    # an id the engine never linked is dropped, and so is "engine" accounting the engine never metered;
    # a result for another case is refused
    other = case_doc("store_shortage_question", case_id=await _new_case(), engine={
        "llm_calls_live": 0, "llm_calls_replayed": 99, "tokens_live": 0, "replay_misses": 0, "cost_microusd": 0})
    stored = await runner_call(api, "PUT", f"/runs/{rid}/cases/store_shortage_question/result",
                               {"runner_id": RUNNER, "result": other})
    assert stored.status_code == 200
    rows = {c["case_id"]: c for c in (await ava.get(f"/api/admin/evals/runs/{rid}")).json()["cases"]}
    assert rows["store_shortage_question"]["keelson_case_id"] is None
    unlinked = (await ava.get(f"/api/admin/evals/runs/{rid}/cases/store_shortage_question")).json()
    assert unlinked["engine"] is None
    wrong = await runner_call(api, "PUT", f"/runs/{rid}/cases/trim_delay_actual/result",
                              {"runner_id": RUNNER, "result": case_doc("store_shortage_question")})
    assert wrong.status_code == 422 and wrong.json()["code"] == "invalid_result"
    junk = await runner_call(api, "PUT", f"/runs/{rid}/cases/trim_delay_actual/result",
                             {"runner_id": RUNNER, "result": {"id": "../x"}})
    assert junk.status_code == 422
    extra = await runner_call(api, "PUT", f"/runs/{rid}/cases/trim_delay_actual/result",
                              {"runner_id": RUNNER, "result": case_doc("trim_delay_actual"), "x": 1})
    assert extra.status_code == 422
    huge = await api.put(f"/api/internal/evals/runner/runs/{rid}/cases/trim_delay_actual/result",
                         content=b"{" + b" " * (4 * 1024 * 1024 + 10) + b"}",
                         headers={"Authorization": f"Bearer {RUNNER_TOKEN}", "Origin": PUBLIC,
                                  "Content-Type": "application/json"})
    assert huge.status_code == 413
    deep = await api.put(f"/api/internal/evals/runner/runs/{rid}/cases/trim_delay_actual/result",
                         content=b'{"runner_id": "runner-1", "result": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
                         headers={"Authorization": f"Bearer {RUNNER_TOKEN}", "Origin": PUBLIC,
                                  "Content-Type": "application/json"})
    assert deep.status_code == 422 and deep.json()["code"] == "invalid_result", deep.text
