"""M5 case actions and list fields: ``rollback {change_set_id}`` (Undo, ``95`` §3) is accepted only
for an applied change set of the case; option actions still need ``option_id`` + ``plan_version``;
the case list carries ``last_signal_at`` for the "Updated" badge."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A
from keelson_contracts.runs import ActionPayload
from keelson_engine.core import repo, runs
from keelson_engine.core.db import tenant_session

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, platform: None) -> dict[str, Browser]:
    return {n: await browser_factory(n) for n in ("priya", "marco", "lena", "nora")}


async def _case_with_change_set(b: Browser, status: str) -> tuple[str, str]:
    case_id = (await b.post("/api/cases", json={})).json()["id"]
    await repo.save_plan(case_id, 1, [{"id": "opt-A"}], "opt-A", tenant=TENANT_A)
    cs = await repo.create_change_set(case_id, 1, "opt-A", "merxa", [{"seq": 1, "op": "allocation.set_qty"}],
                                      "e" * 64, tenant=TENANT_A)
    await repo.update_change_set(cs, status=status, tenant=TENANT_A)
    return case_id, cs


async def test_rollback_action(people: dict[str, Browser]) -> None:
    marco, lena, nora = people["marco"], people["lena"], people["nora"]
    case_id, cs = await _case_with_change_set(marco, "applied")
    url = f"/api/cases/{case_id}/actions"
    body = {"type": "rollback", "change_set_id": cs}
    assert (await lena.post(url, json=body, headers={"Idempotency-Key": "rb0"})).status_code == 403
    assert (await nora.post(url, json=body, headers={"Idempotency-Key": "rb0"})).status_code == 404
    r = await marco.post(url, json={**body, "option_id": "opt-A"}, headers={"Idempotency-Key": "rb1"})
    assert r.status_code == 422
    r = await marco.post(url, json={"type": "rollback"}, headers={"Idempotency-Key": "rb2"})
    assert r.status_code == 422
    r = await marco.post(url, json={"type": "rollback", "change_set_id": "nope"}, headers={"Idempotency-Key": "rb3"})
    assert r.status_code == 404
    r = await marco.post(url, json=body, headers={"Idempotency-Key": "rb4"})
    assert r.status_code == 202, r.text
    run = await runs.get_run(TENANT_A, r.json()["run_id"])
    assert run is not None and run.kind == "action"
    assert ActionPayload.model_validate(run.payload) == ActionPayload(type="rollback", change_set_id=cs)
    # a change set of another case, or one that is not applied, cannot be undone
    other_case, _ = await _case_with_change_set(marco, "applied")
    r = await marco.post(f"/api/cases/{other_case}/actions", json=body, headers={"Idempotency-Key": "rb5"})
    assert r.status_code == 404
    draft_case, draft = await _case_with_change_set(marco, "draft")
    r = await marco.post(f"/api/cases/{draft_case}/actions", json={"type": "rollback", "change_set_id": draft},
                         headers={"Idempotency-Key": "rb6"})
    assert r.status_code == 409 and r.json()["code"] == "not_undoable"
    # option actions keep their M1 shape
    r = await marco.post(url, json={"type": "apply", "option_id": "opt-A"}, headers={"Idempotency-Key": "rb7"})
    assert r.status_code == 422
    r = await marco.post(url, json={"type": "apply", "option_id": "opt-A", "plan_version": 1},
                         headers={"Idempotency-Key": "rb8"})
    assert r.status_code == 202


async def test_case_list_last_signal_at(people: dict[str, Browser]) -> None:
    priya = people["priya"]
    case_id = (await priya.post("/api/cases", json={})).json()["id"]
    items = {i["id"]: i for i in (await priya.get("/api/cases")).json()["items"]}
    assert items[case_id]["last_signal_at"] is None
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("INSERT INTO case_signal (tenant, case_id, connector, event_id, event_type, kind, payload) "
                             "VALUES (:t, :c, 'merxa', 'e1', 'lane.capacity_changed', 'replan', '{}'::jsonb)"),
                        {"t": TENANT_A, "c": case_id})
    items = {i["id"]: i for i in (await priya.get("/api/cases")).json()["items"]}
    assert items[case_id]["last_signal_at"] is not None


async def test_one_rollback_at_a_time(people: dict[str, Browser]) -> None:
    """Regression: a second Undo of the same change set (another person / another key) queued a second
    rollback run and a second approval card. Now 409 ``rollback_pending`` while one is queued, running
    or waiting on an open approval; the same Idempotency-Key still answers the first run."""
    marco, priya = people["marco"], people["priya"]
    case_id, cs = await _case_with_change_set(marco, "applied")
    url = f"/api/cases/{case_id}/actions"
    body = {"type": "rollback", "change_set_id": cs}
    first = await marco.post(url, json=body, headers={"Idempotency-Key": "undo-1"})
    assert first.status_code == 202, first.text
    run_id = first.json()["run_id"]
    again = await marco.post(url, json=body, headers={"Idempotency-Key": "undo-1"})
    assert again.status_code == 202 and again.json()["run_id"] == run_id
    second = await priya.post(url, json=body, headers={"Idempotency-Key": "undo-2"})
    assert second.status_code == 409 and second.json()["code"] == "rollback_pending", second.text
    assert second.json()["run_id"] == run_id
    # paused on an open approval → still pending; once that card is gone a new Undo is accepted
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE run SET status = 'paused' WHERE id = :r"), {"r": run_id})
        await s.execute(text(
            "INSERT INTO interrupt (id, tenant, case_id, run_id, kind, status, quorum_spec, payload, message, "
            "response_schema, invocation_id, session_id) VALUES (:i, :t, :c, :r, 'approval', 'PENDING', "
            "'{}'::jsonb, '{}'::jsonb, 'm', '{}'::jsonb, 'inv', :r)"),
            {"i": f"approval-{run_id}", "t": TENANT_A, "c": case_id, "r": run_id})
    assert (await priya.post(url, json=body, headers={"Idempotency-Key": "undo-3"})).status_code == 409
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE interrupt SET status = 'SUPERSEDED' WHERE run_id = :r"), {"r": run_id})
    r = await priya.post(url, json=body, headers={"Idempotency-Key": "undo-4"})
    assert r.status_code == 202 and r.json()["run_id"] != run_id
