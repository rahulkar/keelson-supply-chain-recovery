"""M8 Evals UI admin API (``docs/contracts/128-m8-evals-ui.md`` §4): platform-admin RBAC on every
route, mode availability, run requests (confirmation, budget, unknown cases, one active run),
imports (idempotent, size cap, invalid documents, the results folder), export → re-import,
compare, trends, recordings coverage, RLS on the eval tables and the case-link cascade."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A, TENANT_B
from keelson_engine.config import Settings
from keelson_engine.core import evals, repo
from keelson_engine.core.db import system_session, tenant_session
from m8_evals import FIXTURES, RUNNER_TOKEN, clean_evals, fixture_doc, heartbeat, runner_call

pytestmark = pytest.mark.asyncio(loop_scope="session")

V1_A = "results-v1-20261007T080848Z.json"
V1_B = "results-v1-20261007T085022Z.json"
V2 = "results-v2.json"
ACTOR = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}
RUN = "0199c1a2-7b3c-7d4e-8f50-0123456789ab"


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, session_browser: Any, platform: None, settings: Settings,
                 monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Browser]]:
    monkeypatch.setattr(settings, "evals_runner_token", SecretStr(RUNNER_TOKEN))
    await clean_evals()
    out = {n: await browser_factory(n) for n in ("sam", "ava", "priya")}
    out["nadia"] = await session_browser("nadia", ["tenant-admin"], [TENANT_B])
    yield out
    await clean_evals()


def _need(name: str) -> dict[str, Any]:
    if not (FIXTURES / name).is_file():
        pytest.skip(f"repository fixture {name} not available")
    return fixture_doc(name)


async def _import(ava: Browser, doc: Any, name: str | None = None) -> httpx.Response:
    url = "/api/admin/evals/import" + (f"?file_name={name}" if name else "")
    return await ava.post(url, json=doc)


# ───────────────────────────── RBAC ─────────────────────────────


async def test_every_route_is_platform_admin_only(api: httpx.AsyncClient, people: dict[str, Browser]) -> None:
    reads = ["/status", "/runs", f"/runs/{RUN}", f"/runs/{RUN}/events", f"/runs/{RUN}/cases/x", f"/runs/{RUN}/export",
             "/files", f"/compare?base={RUN}&head={RUN}", "/trends"]
    writes = [("/runs", {"acknowledge_reset": True}), (f"/runs/{RUN}/cancel", None), ("/import", {"x": 1}),
              ("/import/folder", None)]
    for who, status in (("sam", 403), ("nadia", 403), ("priya", 403)):
        user = people[who]
        for path in reads:
            resp = await user.get(f"/api/admin/evals{path}")
            assert resp.status_code == status, (who, path, resp.text)
        for path, body in writes:
            resp = await user.post(f"/api/admin/evals{path}", json=body)
            assert resp.status_code == status, (who, path, resp.text)
    resp = await people["sam"].get("/api/admin/evals/status")
    assert resp.json()["code"] == "platform_admin_required"
    ava = people["ava"]
    assert (await ava.get(f"/api/admin/evals/runs/{RUN}")).status_code == 404
    assert (await ava.get("/api/admin/evals/runs/not-a-uuid")).status_code == 404
    # mutations need the session's CSRF token
    no_csrf = await api.post("/api/admin/evals/runs", json={"acknowledge_reset": True},
                             headers={"Cookie": f"keelson_session={ava.cookie}", "Origin": "http://keelson.localhost"})
    assert no_csrf.status_code == 403


# ───────────────────────────── availability & requests ─────────────────────────────


async def test_status_and_availability_reasons(api: httpx.AsyncClient, people: dict[str, Browser], settings: Settings,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    ava = people["ava"]

    def reasons(body: dict[str, Any]) -> dict[str, str | None]:
        return {m["mode"]: m["reason"] for m in body["modes"]}

    body = (await ava.get("/api/admin/evals/status")).json()
    assert body["tenant"] == TENANT_A and body["runner"]["configured"] is True and body["runner"]["online"] is False
    assert reasons(body)["replay"] == "runner_offline" and body["suite"] is None
    assert body["live_caps"] == {"max_tokens": 3_000_000, "max_cost_microusd": 5_000_000}
    assert body["recordings"]["rows"] == 0 and body["active_run"] is None
    assert (await heartbeat(api)).status_code == 200
    body = (await ava.get("/api/admin/evals/status")).json()
    assert body["runner"]["online"] is True and len(body["suite"]["cases"]) == 4
    assert reasons(body) == {"replay": None, "record_or_replay": "live_disabled", "record": "live_disabled"}
    monkeypatch.setattr(settings, "evals_live_enabled", True)
    monkeypatch.setenv("LLM_REPLAY_MODE", "replay")
    body = (await ava.get("/api/admin/evals/status")).json()
    assert reasons(body) == {"replay": None, "record_or_replay": "replay_only", "record": "replay_only"}
    assert body["process_replay_mode"] == "replay"
    monkeypatch.setenv("LLM_REPLAY_MODE", "record_or_replay")
    assert set(reasons((await ava.get("/api/admin/evals/status")).json()).values()) == {None}
    async with system_session() as s:  # stale heartbeat → offline
        await s.execute(text("UPDATE eval_runner SET last_seen_at = now() - interval '10 minutes'"))
    assert reasons((await ava.get("/api/admin/evals/status")).json())["replay"] == "runner_offline"
    await heartbeat(api)
    monkeypatch.setattr(settings, "demo_mode", False)
    assert reasons((await ava.get("/api/admin/evals/status")).json())["replay"] == "demo_mode_off"
    monkeypatch.setattr(settings, "evals_runner_token", SecretStr(""))
    body = (await ava.get("/api/admin/evals/status")).json()
    assert body["runner"]["configured"] is False and reasons(body)["replay"] == "runner_not_configured"


async def test_run_requests(api: httpx.AsyncClient, people: dict[str, Browser], settings: Settings,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    ava = people["ava"]

    async def post(**body: Any) -> httpx.Response:
        return await ava.post("/api/admin/evals/runs", json={"acknowledge_reset": True, **body})

    offline = await post()
    assert offline.status_code == 409 and offline.json() == {
        "code": "evals_unavailable", "message": offline.json()["message"], "reason": "runner_offline"}
    await heartbeat(api)
    assert (await ava.post("/api/admin/evals/runs", json={})).status_code == 422  # no acknowledgement
    unknown = await post(case_ids=["trim_delay_actual", "nope"])
    assert unknown.status_code == 422 and unknown.json()["cases"] == ["nope"]
    live = await post(mode="record", confirm_live="RUN LIVE", budget={"max_tokens": 10, "max_cost_microusd": 10})
    assert live.status_code == 409 and live.json()["reason"] == "live_disabled"
    monkeypatch.setattr(settings, "evals_live_enabled", True)
    for body, code in (
        ({"mode": "record", "budget": {"max_tokens": 10, "max_cost_microusd": 10}}, "confirm_required"),
        ({"mode": "record", "confirm_live": "RUN LIVE"}, "budget_required"),
        ({"mode": "record_or_replay", "confirm_live": "RUN LIVE",
          "budget": {"max_tokens": 3_000_001, "max_cost_microusd": 10}}, "budget_too_high"),
    ):
        resp = await post(**body)
        assert resp.status_code == 422 and resp.json()["code"] == code, resp.text
    ok = await post(note="nightly ‮check")
    assert ok.status_code == 202, ok.text
    run = ok.json()
    assert run["case_ids"] == ["trim_delay_actual", "store_shortage_question", "adversarial_kb_doc",
                               "adversarial_supplier_message"]  # all = the runner's suite
    assert run["note"] == "nightly check" and run["requested_by"]["username"] == "ava" and run["budget"] is None
    busy = await post(case_ids=["trim_delay_actual"])
    assert busy.status_code == 409 and busy.json()["code"] == "eval_running" and busy.json()["run_id"] == run["id"]
    listed = (await ava.get("/api/admin/evals/runs")).json()
    assert [r["id"] for r in listed["items"]] == [run["id"]] and listed["next_before"] is None
    async with system_session() as s:
        n = (await s.execute(text("SELECT count(*) FROM admin_audit WHERE action = 'evals.run_requested' "
                                  "AND target = :t AND tenant IS NULL"), {"t": f"eval_run:{run['id']}"})).scalar_one()
    assert n == 1
    status = (await ava.get("/api/admin/evals/status")).json()
    assert status["active_run"]["id"] == run["id"] and status["active_run"]["status"] == "queued"


# ───────────────────────────── import / export ─────────────────────────────


async def test_import_is_idempotent_and_validated(people: dict[str, Browser], settings: Settings,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    ava = people["ava"]
    doc = _need(V1_A)
    first = await _import(ava, doc, "20261007T080848Z.json")
    assert first.status_code == 201 and first.json()["duplicate"] is False
    again = await _import(ava, json.loads(json.dumps(doc, indent=4)), "renamed.json")
    assert again.status_code == 200 and again.json() == {"run_id": first.json()["run_id"], "duplicate": True}
    detail = (await ava.get(f"/api/admin/evals/runs/{first.json()['run_id']}")).json()
    run = detail["run"]
    assert run["source"] == "import" and run["mode"] == "unknown" and run["doc_schema"] == "keelson-evals/1"
    assert run["status"] == "succeeded" and run["file_name"] == "20261007T080848Z.json"
    assert run["target"] == "http://keelson.localhost" and run["summary"]["tokens_source"] == "gateway"
    assert len(detail["cases"]) == 6 and detail["run"]["breaches"] >= 1
    assert (await _import(ava, {"schema": "keelson-evals/9", "cases": []})).json()["code"] == "invalid_results"
    garbage = await ava.post("/api/admin/evals/import", content=b"{not json", headers={"Content-Type": "application/json"})
    assert garbage.status_code == 422 and garbage.json()["code"] == "invalid_results"
    monkeypatch.setattr(settings, "evals_max_import_bytes", 1024)
    too_big = await _import(ava, doc)
    assert too_big.status_code == 413 and too_big.json()["code"] == "file_too_large"


async def test_v2_import_export_reimport_and_in_app_export(api: httpx.AsyncClient, people: dict[str, Browser]) -> None:
    ava = people["ava"]
    created = await _import(ava, _need(V2))
    rid = created.json()["run_id"]
    run = (await ava.get(f"/api/admin/evals/runs/{rid}")).json()["run"]
    assert run["mode"] == "replay" and run["summary"]["replay_misses"] == 1 and run["summary"]["tokens_source"] == "engine"
    exported = await ava.get(f"/api/admin/evals/runs/{rid}/export")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    doc = exported.json()
    assert doc["schema"] == "keelson-evals/2" and len(doc["cases"]) == 2
    again = await _import(ava, doc)
    assert again.status_code == 200 and again.json() == {"run_id": rid, "duplicate": True}
    case = (await ava.get(f"/api/admin/evals/runs/{rid}/cases/trim_delay_actual")).json()
    assert case["turns"][0]["trajectory"]["missing"] == ["playbook:*"]
    assert (await ava.get(f"/api/admin/evals/runs/{rid}/cases/nope")).status_code == 404

    # an in-app run's export re-imports as that run
    await heartbeat(api)
    queued = (await ava.post("/api/admin/evals/runs", json={"acknowledge_reset": True,
                                                            "case_ids": ["trim_delay_actual"]})).json()
    assert (await ava.get(f"/api/admin/evals/runs/{queued['id']}/export")).status_code == 409  # not finished
    await runner_call(api, "POST", "/claim", {"runner_id": "runner-1"})
    from m8_evals import case_doc

    await runner_call(api, "PUT", f"/runs/{queued['id']}/cases/trim_delay_actual/result",
                      {"runner_id": "runner-1", "result": case_doc("trim_delay_actual")})
    await runner_call(api, "POST", f"/runs/{queued['id']}/finish", {"runner_id": "runner-1", "status": "succeeded"})
    doc = (await ava.get(f"/api/admin/evals/runs/{queued['id']}/export")).json()
    assert doc["runner"] == "in_app" and doc["mode"] == "replay"
    assert (await _import(ava, doc)).json() == {"run_id": queued["id"], "duplicate": True}


async def test_import_folder_and_file_listing(people: dict[str, Browser], settings: Settings,
                                              monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ava = people["ava"]
    monkeypatch.setattr(settings, "evals_results_dir", tmp_path / "missing")
    assert (await ava.post("/api/admin/evals/import/folder")).json()["code"] == "results_folder_unavailable"
    monkeypatch.setattr(settings, "evals_results_dir", tmp_path)
    (tmp_path / "20261007T080848Z.json").write_text(json.dumps(_need(V1_A)), encoding="utf-8")
    (tmp_path / "20261007T085022Z.json").write_text(json.dumps(_need(V1_B)), encoding="utf-8")
    (tmp_path / "20261008T000000Z.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "20261008T010000Z.json").write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
    out = (await ava.post("/api/admin/evals/import/folder")).json()
    assert sorted(i["file"] for i in out["imported"]) == ["20261007T080848Z.json", "20261007T085022Z.json"]
    assert out["duplicates"] == 0
    assert {e["file"]: e["error"].split(":")[0] for e in out["errors"]} == {
        "20261008T000000Z.json": "unreadable", "20261008T010000Z.json": "invalid_results"}
    again = (await ava.post("/api/admin/evals/import/folder")).json()
    assert again["imported"] == [] and again["duplicates"] == 2
    files = (await ava.get("/api/admin/evals/files")).json()
    by_file = {f["file"]: f for f in files["items"]}
    imported = {i["file"]: i["run_id"] for i in out["imported"]}
    assert by_file["20261007T085022Z.json"]["imported_run_id"] == imported["20261007T085022Z.json"]
    assert by_file["20261008T000000Z.json"]["imported_run_id"] is None
    # the M7 listing is unchanged
    m7 = (await ava.get("/api/admin/evals")).json()
    assert m7["directory_available"] is True and "imported_run_id" not in m7["items"][0]


# ───────────────────────────── compare & trends ─────────────────────────────


async def test_compare_and_trends_of_the_real_runs(people: dict[str, Browser]) -> None:
    ava = people["ava"]
    a = (await _import(ava, _need(V1_A))).json()["run_id"]
    b = (await _import(ava, _need(V1_B))).json()["run_id"]
    v2 = (await _import(ava, _need(V2))).json()["run_id"]
    cmp = (await ava.get(f"/api/admin/evals/compare?base={a}&head={b}")).json()
    changes = {c["case_id"]: c["change"] for c in cmp["cases"]}
    assert changes["adversarial_kb_doc"] == "fixed" and changes["adversarial_supplier_message"] == "changed"
    assert sorted(k for k, v in changes.items() if v == "removed") == [
        "followup_delay_doubles", "store_shortage_question", "suez_whatif", "trim_delay_actual"]
    assert cmp["summary_deltas"]["pass_rate_bp"] == {"base": 0, "head": 5000, "delta": 5000, "better": True}
    assert (await ava.get(f"/api/admin/evals/compare?base={a}&head={RUN}")).status_code == 404
    assert (await ava.get(f"/api/admin/evals/compare?base={a}&head=x")).status_code == 422

    trends = (await ava.get("/api/admin/evals/trends")).json()
    assert [r["id"] for r in trends["runs"]] == [a, b, v2]  # oldest first (by start time)
    strip = {c["case_id"]: c["statuses"] for c in trends["cases"]}
    assert strip["adversarial_kb_doc"] == ["fail", "pass", None]
    assert strip["store_shortage_question"] == ["fail", None, "error"]
    replay = (await ava.get("/api/admin/evals/trends?mode=replay")).json()
    assert [r["id"] for r in replay["runs"]] == [v2]
    live = (await ava.get("/api/admin/evals/trends?mode=live")).json()
    assert [r["id"] for r in live["runs"]] == [a, b]  # imported CLI runs of unknown mode count as live
    listed = (await ava.get("/api/admin/evals/runs?limit=2")).json()
    assert len(listed["items"]) == 2 and listed["next_before"] is not None
    rest = (await ava.get(f"/api/admin/evals/runs?limit=2&before={listed['next_before']}")).json()
    assert len(rest["items"]) == 1 and rest["next_before"] is None
    assert [r["id"] for r in (await ava.get("/api/admin/evals/runs?mode=replay")).json()["items"]] == [v2]


# ───────────────────────────── data: recordings, RLS, cascade ─────────────────────────────


async def test_recordings_coverage_counts_the_eval_tenant(people: dict[str, Browser]) -> None:
    ava = people["ava"]
    async with system_session() as s:
        await s.execute(text("DELETE FROM llm_replay"))
        for i, (tenant, agent) in enumerate(((TENANT_A, "intake"), (TENANT_A, "intake"), (TENANT_A, "planner"),
                                             (TENANT_B, "intake"))):
            await s.execute(text(
                "INSERT INTO llm_replay (tenant, key, alias, agent, model, responses, bytes) VALUES "
                "(:t, :k, 'fast-default', :a, 'gemini', '[]'::jsonb, 100)"), {"t": tenant, "k": f"{i:064x}", "a": agent})
    rec = (await ava.get("/api/admin/evals/status")).json()["recordings"]
    assert rec["rows"] == 3 and rec["bytes"] == 300 and rec["agents"] == {"intake": 2, "planner": 1}
    assert rec["newest_at"] is not None
    async with system_session() as s:
        await s.execute(text("DELETE FROM llm_replay"))


async def test_eval_tables_are_tenant_isolated_and_links_cascade_with_cases(api: httpx.AsyncClient,
                                                                            people: dict[str, Browser]) -> None:
    ava = people["ava"]
    rid = (await _import(ava, _need(V1_B))).json()["run_id"]
    await heartbeat(api)
    queued = (await ava.post("/api/admin/evals/runs", json={"acknowledge_reset": True,
                                                            "case_ids": ["trim_delay_actual"]})).json()
    await runner_call(api, "POST", "/claim", {"runner_id": "runner-1"})
    kc = await repo.create_case("Eval case", created_by=ACTOR, tenant=TENANT_A)
    await runner_call(api, "POST", f"/runs/{queued['id']}/cases/trim_delay_actual/attach",
                      {"runner_id": "runner-1", "keelson_case_id": kc})
    await runner_call(api, "POST", f"/runs/{queued['id']}/events", {"runner_id": "runner-1", "events": [
        {"type": "note", "data": {"x": 1}}]})
    for table in ("eval_run", "eval_case_result", "eval_run_event", "eval_case_link"):
        async with tenant_session(TENANT_B) as s:
            assert (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0, table
        async with tenant_session(TENANT_A) as s:
            assert (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() > 0, table
    assert await evals.get_eval_link(TENANT_B, kc) is None
    # a demo reset deletes the Keelson case: the link goes, the history stays
    async with tenant_session(TENANT_A) as s:
        await s.execute(text('DELETE FROM "case" WHERE id = :id'), {"id": kc})
    async with system_session() as s:
        links = (await s.execute(text("SELECT count(*) FROM eval_case_link"))).scalar_one()
        runs_left = (await s.execute(text("SELECT count(*) FROM eval_run WHERE id IN (:a, :b)"),
                                     {"a": rid, "b": queued["id"]})).scalar_one()
    assert links == 0 and runs_left == 2
    assert len((await ava.get(f"/api/admin/evals/runs/{rid}")).json()["cases"]) == 2
