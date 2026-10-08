"""M5 full demo reset (``95`` §6): ``POST /api/demo/reset`` with Merxa (REST as merxa.admin) and the
Keycloak admin API mocked: step log in order, demo-tenant rows truncated (other tenant untouched),
ADK sessions, Valkey keys, consents + offline tokens revoked, Token Broker rows deleted, agent spec
back to the pack default (stable version names), scn re-sync requested; ``keep_consents``; a
failing Merxa; roles / DEMO_MODE / concurrency guards."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
import respx
from pydantic import SecretStr
from sqlalchemy import text

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import HAVE_PACKS, TENANT_A, TENANT_B
from e1_idp import FakeRealm
from e1_keycloak_admin import FakeKeycloakAdmin
from keelson_contracts.demo import DEMO_RESET_STEPS, DemoResetResult
from keelson_contracts.runs import case_stream_key
from keelson_engine.config import Settings
from keelson_engine.core import agent_specs, checklists, connectors, demo_reset, repo, runs
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.tokens import get_token_broker
from keelson_engine.core.valkey import get_valkey
from test_tokens import MERXA_SPEC, _link

pytestmark = pytest.mark.asyncio(loop_scope="session")

ADMIN_DSN = os.environ.get("E1_TEST_ADMIN_DSN", "postgresql://postgres:test@e1-test-pg:5432/postgres")
KC = "http://keycloak-m5.test:8080"
MERXA_API = "http://merxa-api:8100"
MERXA_PUBLIC = "http://merxa.localhost"
PRIYA = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}
ADK_TABLES = """
CREATE TABLE sessions (app_name varchar(128), user_id varchar(128), id varchar(128), state jsonb,
                       PRIMARY KEY (app_name, user_id, id));
CREATE TABLE events (id varchar(128), app_name varchar(128), user_id varchar(128), session_id varchar(128),
                     invocation_id varchar(256), PRIMARY KEY (id, app_name, user_id, session_id),
                     FOREIGN KEY (app_name, user_id, session_id) REFERENCES sessions (app_name, user_id, id)
                     ON DELETE CASCADE);
CREATE TABLE app_states (app_name varchar(128) PRIMARY KEY, state jsonb);
CREATE TABLE user_states (app_name varchar(128), user_id varchar(128), state jsonb, PRIMARY KEY (app_name, user_id));
"""


class FakeMerxa:
    def __init__(self) -> None:
        self.resets = 0
        self.fail_reset = False
        self.logins: list[dict[str, Any]] = []

    def install(self, router: respx.Router) -> None:
        router.post(f"{MERXA_API}/v1/auth/demo/login-as", name="merxa-login-as").mock(side_effect=self._login)
        router.post(f"{MERXA_API}/v1/admin/scenario:reset", name="merxa-reset").mock(side_effect=self._reset)
        router.post(f"{MERXA_API}/v1/auth/logout", name="merxa-logout").mock(return_value=httpx.Response(204))

    def _login(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.logins.append({"username": body.get("username"), "origin": req.headers.get("origin")})
        if body.get("username") != "merxa.admin" or req.headers.get("origin") != MERXA_PUBLIC:
            return httpx.Response(403)
        return httpx.Response(200, json={"user": {"username": "merxa.admin"}, "demo_mode": True},
                              headers={"set-cookie": "merxa_session=s3ss10n; Path=/; HttpOnly; SameSite=Lax"})

    def _reset(self, req: httpx.Request) -> httpx.Response:
        if req.headers.get("cookie") != "merxa_session=s3ss10n" or req.headers.get("origin") != MERXA_PUBLIC:
            return httpx.Response(401)
        if not req.headers.get("idempotency-key"):
            return httpx.Response(400)
        if self.fail_reset:
            return httpx.Response(500, json={"title": "boom"})
        self.resets += 1
        return httpx.Response(200, json={"status": "reset", "profile": "full", "duration_ms": 900,
                                         "counts": {"purchase_order": 12, "sku": 180, "Bad Key!": 3, "x": -1}})


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def adk_db(settings: Settings) -> AsyncIterator[str]:
    name = f"adk_t_{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(ADK_TABLES)
    finally:
        await conn.close()
    demo_reset.set_adk_url(dsn.replace("postgresql://", "postgresql+asyncpg://"))
    yield dsn
    demo_reset.set_adk_url(None)
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture
def env(settings: Settings, http_mock: respx.Router, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(settings, "keycloak_internal_url", KC)
    monkeypatch.setattr(settings, "kc_admin_password", SecretStr("kc-pw"))
    monkeypatch.setattr(settings, "merxa_api_url", MERXA_API)
    monkeypatch.setattr(settings, "merxa_public_url", MERXA_PUBLIC)
    kc = FakeKeycloakAdmin(KC, "kc-pw", realm="merxa")
    kc.install(http_mock)
    merxa = FakeMerxa()
    merxa.install(http_mock)
    return {"kc": kc, "merxa": merxa}


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, platform: None) -> dict[str, Browser]:
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    return {n: await browser_factory(n) for n in ("ava", "priya", "sam")}


async def _adk_rows(dsn: str) -> dict[str, int]:
    conn = await asyncpg.connect(dsn)
    try:
        return {t: int(await conn.fetchval(f"SELECT count(*) FROM {t} WHERE app_name = 't_demo_retail'"))
                for t in ("sessions", "events", "app_states", "user_states")} | {
            "other": int(await conn.fetchval("SELECT count(*) FROM sessions WHERE app_name = 't_northwind_test'"))}
    finally:
        await conn.close()


async def _seed_adk(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for app in ("t_demo_retail", "t_northwind_test"):
            sid = uuid.uuid4().hex
            await conn.execute("INSERT INTO sessions VALUES ($1, 'u', $2, '{}')", app, sid)
            await conn.execute("INSERT INTO events VALUES ($1, $2, 'u', $3, 'inv')", uuid.uuid4().hex, app, sid)
            await conn.execute("INSERT INTO app_states VALUES ($1, '{}') ON CONFLICT DO NOTHING", app)
            await conn.execute("INSERT INTO user_states VALUES ($1, 'u', '{}') ON CONFLICT DO NOTHING", app)
    finally:
        await conn.close()


async def _seed_keelson(merxa_realm: FakeRealm) -> dict[str, Any]:
    case_id = await repo.create_case("demo", created_by=PRIYA, tenant=TENANT_A)
    await runs.enqueue_run(tenant=TENANT_A, case_id=case_id, kind="chat", payload={"text": "x"}, actor=PRIYA,
                           idempotency_key=None)
    await repo.save_plan(case_id, 1, [{"id": "opt-A", "actions": [{"lane_code": "CGP-UK1-SA"}]}], "opt-A",
                         tenant=TENANT_A)
    await checklists.create_checklist(TENANT_A, case_id, "merxa", [{
        "title": "t", "instructions_md": "i", "expected": {"entity_type": "x", "entity_ref": None, "match": {}}}])
    other = await repo.create_case("keep", created_by=PRIYA, tenant=TENANT_B)
    await get_valkey().xadd(case_stream_key(case_id), {"seq": "1", "data": "{}"})
    await get_valkey().set(f"keelson:wh:rl:{TENANT_A}:merxa:1", "3")
    await get_valkey().set(f"keelson:wh:rl:{TENANT_B}:merxa:1", "3")
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("INSERT INTO connector_event (tenant, connector, event_id, type, payload, received_at) "
                             "VALUES (:t, 'merxa', :e, 'x', '{}'::jsonb, now() - interval '1 hour')"),
                        {"t": TENANT_A, "e": f"old-{uuid.uuid4().hex}"})
    broker = get_token_broker()
    await _link(broker, merxa_realm, "sub-marco", username="marco.rossi")
    return {"case": case_id, "other": other}


async def _count(tenant: str, sql: str) -> int:
    async with tenant_session(tenant) as s:
        return int((await s.execute(text(sql))).scalar_one())


async def test_full_reset(people: dict[str, Browser], env: dict[str, Any], adk_db: str,
                          merxa_realm: FakeRealm) -> None:
    kc: FakeKeycloakAdmin = env["kc"]
    merxa: FakeMerxa = env["merxa"]
    marco_id = kc.add_user("marco.rossi")
    priya_id = kc.add_user("priya.shah")
    kc.consents[marco_id] = {"keelson-connect"}
    kc.offline[marco_id] = {"keelson-connect"}
    kc.offline[priya_id] = {"keelson-connect"}  # offline session without an explicit consent
    await reset_specs(TENANT_A)
    async with system_session() as s:  # the demo tenant was created from the value-fashion-eu pack
        await s.execute(text("UPDATE tenant SET config = jsonb_set(config, '{pack}', :p) WHERE slug = :t"),
                        {"p": json.dumps("value-fashion-eu" if HAVE_PACKS else None), "t": TENANT_A})
    try:
        from keelson_engine.core.packs import load_pack

        overrides = load_pack("value-fashion-eu").spec_overrides if HAVE_PACKS else {}
        v1 = await agent_specs.create_initial(TENANT_A, overrides, actor="bootstrap", summary="pack")
        assert v1 == "v1"
        await agent_specs.put_spec(TENANT_A, "v1", {"prompts": {"intake": "Be brief."}}, {"username": "sam"},
                                   platform_admin=True)
        seeded = await _seed_keelson(merxa_realm)
        await _seed_adk(adk_db)

        r = await people["ava"].post("/api/demo/reset", json={})
        assert r.status_code == 200, r.text
        result = DemoResetResult.model_validate(r.json())
        assert result.ok, r.text
        assert [s.step for s in result.steps] == list(DEMO_RESET_STEPS)
        assert result.within_target and result.keep_consents is False
        steps = {s.step: s for s in result.steps}
        assert merxa.resets == 1 and merxa.logins == [{"username": "merxa.admin", "origin": MERXA_PUBLIC}]
        assert steps["merxa_scenario_reset"].counts == {"purchase_order": 12, "sku": 180}
        assert steps["keelson_rows"].counts["case"] >= 1 and steps["keelson_rows"].counts["checklist"] >= 1
        assert steps["adk_sessions"].counts == {"events": 1, "sessions": 1, "app_states": 1, "user_states": 1}
        assert steps["valkey_keys"].counts["case_streams"] >= 1 and steps["valkey_keys"].counts["rate_limit_keys"] >= 1
        assert steps["merxa_consents"].counts["revoked"] == 2
        assert steps["token_broker"].counts["connector_identity_link"] >= 1
        assert steps["agent_spec"].detail == "v1 (reactivated)"
        assert steps["sim_clock"].status == "ok" and steps["scn_resync"].status == "ok"

        # keelson rows of the demo tenant are gone; the other tenant is untouched
        assert await _count(TENANT_A, 'SELECT count(*) FROM "case"') == 0
        assert await _count(TENANT_A, "SELECT count(*) FROM run") == 0
        assert await _count(TENANT_A, "SELECT count(*) FROM checklist") == 0
        assert await _count(TENANT_A, "SELECT count(*) FROM connector_identity_link") == 0
        assert await _count(TENANT_A, "SELECT count(*) FROM connector_credential WHERE principal_type = 'user'") == 0
        assert await _count(TENANT_A, "SELECT count(*) FROM connector_event WHERE received_at < now() - "
                                      "interval '30 minutes'") == 0
        assert await repo.get_case(seeded["other"], tenant=TENANT_B) is not None
        assert await _count(TENANT_A, "SELECT count(*) FROM connector") >= 1  # configuration stays
        assert await _adk_rows(adk_db) == {"sessions": 0, "events": 0, "app_states": 0, "user_states": 0,
                                           "other": 1}
        assert not await get_valkey().exists(case_stream_key(seeded["case"]))
        assert await get_valkey().exists(f"keelson:wh:rl:{TENANT_B}:merxa:1")
        assert sorted(kc.revoked) == [("marco.rossi", "keelson-connect"), ("priya.shah", "keelson-connect")]
        latest = await agent_specs.latest(TENANT_A)
        assert latest["version"] == "v1" and (latest["spec"].get("prompts") or {}).get("intake") in (None, "")
        async with tenant_session(TENANT_A) as s:
            audit = (await s.execute(text("SELECT after FROM admin_audit WHERE action = 'demo.reset' "
                                          "ORDER BY id DESC LIMIT 1"))).scalar_one()
        assert audit["ok"] is True and audit["keep_consents"] is False

        # a second reset changes nothing in the spec (stable version names for LLM replay)
        r = await people["ava"].post("/api/demo/reset", json={"keep_consents": True})
        result = DemoResetResult.model_validate(r.json())
        steps = {s.step: s for s in result.steps}
        assert steps["agent_spec"].detail == "v1 (unchanged)"
        assert steps["merxa_consents"].status == steps["token_broker"].status == "skipped"

        # an admin edit restored by saving the old values again leaves a newer copy of v1; the reset
        # keeps the oldest equal name (v1), not the copy, so LLM replay keys never move
        cur = (await agent_specs.latest(TENANT_A))["version"]
        edited, _ = await agent_specs.put_spec(TENANT_A, cur, {"prompts": {"intake": "Be brief."}},
                                               {"username": "sam"}, platform_admin=True)
        restored, _ = await agent_specs.put_spec(TENANT_A, edited["version"], {"prompts": {"intake": None}},
                                                 {"username": "sam"}, platform_admin=True)
        assert restored["version"] not in ("v1", edited["version"])
        v1_spec = (await agent_specs.get_version(TENANT_A, "v1"))["spec"]
        assert (await agent_specs.get_version(TENANT_A, restored["version"]))["spec"] == v1_spec  # an equal copy
        r = await people["ava"].post("/api/demo/reset", json={"keep_consents": True})
        steps = {s.step: s for s in DemoResetResult.model_validate(r.json()).steps}
        assert steps["agent_spec"].detail == "v1 (reactivated)", steps["agent_spec"].detail
        assert (await agent_specs.latest(TENANT_A))["version"] == "v1"
    finally:
        async with system_session() as s:
            await s.execute(text("UPDATE tenant SET config = config - 'pack' WHERE slug = :t"), {"t": TENANT_A})
        await reset_specs(TENANT_A)


async def test_keep_consents_keeps_links(people: dict[str, Browser], env: dict[str, Any], adk_db: str,
                                         merxa_realm: FakeRealm) -> None:
    kc: FakeKeycloakAdmin = env["kc"]
    marco_id = kc.add_user("marco.rossi")
    kc.consents[marco_id] = {"keelson-connect"}
    await _link(get_token_broker(), merxa_realm, "sub-marco", username="marco.rossi")
    r = await people["ava"].post("/api/demo/reset", json={"keep_consents": True})
    assert r.status_code == 200
    result = DemoResetResult.model_validate(r.json())
    assert result.ok and result.keep_consents
    assert {s.step: s.status for s in result.steps}["merxa_consents"] == "skipped"
    assert kc.revoked == [] and kc.consents[marco_id] == {"keelson-connect"}
    assert await _count(TENANT_A, "SELECT count(*) FROM connector_identity_link WHERE status = 'active'") == 1
    assert (await get_token_broker().link_status(tenant=TENANT_A, connector="merxa",
                                                 keelson_sub="sub-marco"))["status"] == "active"


async def test_failing_merxa_is_reported_and_the_rest_still_runs(people: dict[str, Browser], env: dict[str, Any],
                                                                 adk_db: str) -> None:
    env["merxa"].fail_reset = True
    case_id = await repo.create_case("x", created_by=PRIYA, tenant=TENANT_A)
    r = await people["ava"].post("/api/demo/reset", json={})
    assert r.status_code == 200
    result = DemoResetResult.model_validate(r.json())
    assert not result.ok and result.failed_steps == ["merxa_scenario_reset", "sim_clock"]
    steps = {s.step: s for s in result.steps}
    assert steps["merxa_scenario_reset"].detail == "Merxa scenario:reset failed (HTTP 500)"
    assert steps["keelson_rows"].status == "ok" and await repo.get_case(case_id, tenant=TENANT_A) is None


async def test_keycloak_down_fails_only_that_step(people: dict[str, Browser], env: dict[str, Any], adk_db: str,
                                                  merxa_realm: FakeRealm) -> None:
    env["kc"].down = True
    await _link(get_token_broker(), merxa_realm, "sub-marco", username="marco.rossi")
    result = DemoResetResult.model_validate((await people["ava"].post("/api/demo/reset", json={})).json())
    assert result.failed_steps == ["merxa_consents"]
    assert {s.step: s for s in result.steps}["merxa_consents"].detail.startswith("Keycloak admin API")
    # the Token Broker rows are deleted anyway (the next approval asks to connect again)
    assert await _count(TENANT_A, "SELECT count(*) FROM connector_identity_link") == 0


async def test_access_and_guards(people: dict[str, Browser], env: dict[str, Any], settings: Settings,
                                 monkeypatch: pytest.MonkeyPatch, adk_db: str) -> None:
    ava, priya, sam = people["ava"], people["priya"], people["sam"]
    assert (await priya.post("/api/demo/reset", json={})).status_code == 403
    assert (await sam.post("/api/demo/reset", json={})).status_code == 403  # tenant-admin is not enough
    assert (await ava.post("/api/demo/reset", json={"keep_consents": "yes"})).status_code == 422
    assert (await ava.post("/api/demo/reset", json={"tenant": "northwind-test"})).status_code == 422
    no_csrf = await ava.client.post("/api/demo/reset", json={},
                                    headers={"Cookie": f"keelson_session={ava.cookie}", "Origin": "http://keelson.localhost"})
    assert no_csrf.status_code == 403
    await get_valkey().set(f"keelson:demo_reset:{TENANT_A}", "1", ex=30)
    try:
        r = await ava.post("/api/demo/reset", json={})
        assert r.status_code == 409 and r.json()["code"] == "reset_in_progress"
    finally:
        await get_valkey().delete(f"keelson:demo_reset:{TENANT_A}")
    assert not await get_valkey().exists(f"keelson:demo_reset:{TENANT_A}")
    monkeypatch.setattr(settings, "demo_mode", False)
    assert (await ava.post("/api/demo/reset", json={})).status_code == 404


async def test_presenter_must_belong_to_the_demo_tenant(browser_factory: Any, platform: None,
                                                         keelson_realm: FakeRealm, env: dict[str, Any],
                                                         adk_db: str) -> None:
    """Realm roles are global: a ``demo-presenter`` of another tenant must not reset the demo tenant
    (regression: any presenter could wipe it); a presenter of the demo tenant may."""
    nora, lena = keelson_realm.users["nora"], keelson_realm.users["lena"]
    try:
        keelson_realm.add_user("nora", roles=["planner", "demo-presenter"], name=nora.name, tenants=[TENANT_B])
        keelson_realm.add_user("lena", roles=["compliance", "demo-presenter"], name=lena.name, tenants=[TENANT_A])
        outsider = await browser_factory("nora")
        r = await outsider.post("/api/demo/reset", json={"keep_consents": True})
        assert r.status_code == 403 and r.json()["code"] == "forbidden", r.text
        assert env["merxa"].resets == 0  # nothing ran
        presenter = await browser_factory("lena")
        r = await presenter.post("/api/demo/reset", json={"keep_consents": True})
        assert r.status_code == 200, r.text
        assert env["merxa"].resets == 1
    finally:
        keelson_realm.users["nora"], keelson_realm.users["lena"] = nora, lena
