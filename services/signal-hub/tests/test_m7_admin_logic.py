"""M7 offline: alert-rule semantic checks, watch-region model validation, source catalogue limits,
the at-rest secret box and the extraction-key provisioning against a mock LiteLLM (no DB, no model)."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from fakes import MockLiteLlm
from keelson_contracts.signals import DEFAULT_ALERT_RULES, AlertRule
from signal_hub import admin
from signal_hub.config import RssFeed, Settings
from signal_hub.llm_key import (
    SECRET_NAME,
    LiteLlmAdmin,
    LiteLlmAdminError,
    key_restrictions,
    provision_signals_key,
    redact,
)
from signal_hub.quarantine import LiteLlmClient
from signal_hub.secretbox import SecretBoxError, derive_kek, open_box, seal


def _rule(rid: str = "r1", *, action: str = "alert", sev: int = 5000, conf: int = 0, nodes: int = 1,
          kinds: tuple[str, ...] = (), node_types: tuple[str, ...] = (), hysteresis: int = 1000) -> AlertRule:
    return AlertRule.model_validate({"id": rid, "action": action, "hysteresis_bp": hysteresis, "when": {
        "min_severity_bp": sev, "kinds": list(kinds), "min_confidence_bp": conf, "min_exposed_nodes": nodes,
        "node_types": list(node_types)}})


# ─────────────────────────── alert rules ───────────────────────────

def test_default_rules_are_valid() -> None:
    assert admin.rule_problems(DEFAULT_ALERT_RULES) == []


@pytest.mark.parametrize(("rules", "needle"), [
    ([_rule("a"), _rule("a")], "duplicate rule id"),
    ([_rule(action="alert_and_draft_case", nodes=0, sev=9000)], "needs min_exposed_nodes >= 1"),
    ([_rule(nodes=0, node_types=("port",), sev=9000)], "node_types has no effect"),
    ([_rule(action="ignore", sev=0, nodes=0)], "suppresses every alert"),
    ([_rule(hysteresis=0)], "hysteresis_bp must be at least"),
    ([_rule(nodes=0, sev=1000)], "alerts without exposure needs min_severity_bp"),
    ([_rule(f"r{i}") for i in range(admin.MAX_RULES + 1)], "at most"),
])
def test_rule_problems(rules: list[AlertRule], needle: str) -> None:
    problems = admin.rule_problems(rules)
    assert any(needle in p["problem"] for p in problems), problems


def test_rule_problems_accept_reasonable_rules() -> None:
    ok = [
        _rule("mute-quakes", action="ignore", sev=0, nodes=0, kinds=("natural_hazard",)),  # scoped ignore
        _rule("big-anywhere", nodes=0, sev=8500),  # severe events even without exposure
        _rule("ports", action="alert_and_draft_case", sev=7000, node_types=("port",)),
        _rule("ignore-hysteresis", action="ignore", sev=0, nodes=0, kinds=("demand",), hysteresis=0),
    ]
    assert admin.rule_problems(ok) == []


# ─────────────────────────── watch regions ───────────────────────────

SQUARE = {"type": "Polygon", "coordinates": [[[99, 9], [109, 9], [109, 14.5], [99, 14.5], [99, 9]]]}


def test_watch_region_model() -> None:
    poly = admin.WatchRegionIn.model_validate({"id": "gulf", "name": "  Gulf of Thailand ", "kind": "polygon",
                                               "geometry": SQUARE, "node_types": ["port", "lane"]})
    assert poly.name == "Gulf of Thailand" and poly.geometry is not None
    rad = admin.WatchRegionIn.model_validate({"id": "bkk-1000", "name": "Bangkok 1000 km", "kind": "radius",
                                              "node_type": "port", "node_code": "THBKK", "radius_km": 1000})
    assert rad.radius_km == 1000 and rad.enabled is True
    bad: list[dict[str, Any]] = [
        {"id": "x", "name": "n", "kind": "polygon"},  # no geometry
        {"id": "x", "name": "n", "kind": "polygon", "geometry": SQUARE, "radius_km": 5},
        {"id": "x", "name": "n", "kind": "radius", "node_type": "port", "node_code": "A"},  # no radius
        {"id": "x", "name": "n", "kind": "radius", "node_type": "port", "node_code": "A", "radius_km": 2001},
        {"id": "x", "name": "n", "kind": "radius", "node_type": "port", "node_code": "A", "radius_km": 10,
         "node_types": ["port"]},
        {"id": "x", "name": "n", "kind": "radius", "node_type": "port", "node_code": "A", "radius_km": True},
        {"id": "x", "name": "line\nbreak", "kind": "radius", "node_type": "port", "node_code": "A", "radius_km": 5},
        {"id": "Bad", "name": "n", "kind": "radius", "node_type": "port", "node_code": "A", "radius_km": 5},
        {"id": "x\n", "name": "n", "kind": "radius", "node_type": "port", "node_code": "A", "radius_km": 5},
        {"id": "x", "name": "n", "kind": "polygon", "geometry": {"type": "Point", "coordinates": [1, 2]}},
        {"id": "x", "name": "n", "kind": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]}},
        {"id": "x", "name": "n", "kind": "polygon", "geometry": SQUARE, "node_types": ["port", "port"]},
        {"id": "x", "name": "n", "kind": "polygon", "geometry": SQUARE, "extra": 1},
    ]
    for body in bad:
        with pytest.raises(ValidationError):
            admin.WatchRegionIn.model_validate(body)
    ring = [[float(i % 50) * 0.01, float(i // 50) * 0.01] for i in range(admin.MAX_REGION_POSITIONS + 1)]
    ring.append(ring[0])
    with pytest.raises(ValidationError, match="positions"):
        admin.WatchRegionBody.model_validate({"name": "n", "kind": "polygon",
                                              "geometry": {"type": "Polygon", "coordinates": [ring]}})


# ─────────────────────────── sources ───────────────────────────

def test_source_catalogue_limits() -> None:
    s = Settings(rss_feeds=(RssFeed("rss.x", "X Daily", "https://x.example/feed"),), cadence_usgs_s=60)
    cat = {c.connector_id: c for c in admin.source_catalogue(s)}
    assert list(cat) == ["gdacs", "usgs", "rss.x", "nhc", "copernicus_ems", "ofac", "gdelt"]  # M8: 124 §0
    assert (cat["gdacs"].min_cadence_s, cat["gdacs"].max_cadence_s, cat["gdacs"].default_cadence_s) == (300, 86400, 600)
    assert cat["usgs"].min_cadence_s == 60  # a configured default below the floor stays valid
    assert cat["rss.x"].license.startswith("Headline") and cat["rss.x"].host == "x.example" and cat["rss.x"].polled
    assert not admin.source_catalogue(dataclasses.replace(s, enabled_adapters=("gdacs",)))[1].polled
    with pytest.raises(ValidationError):
        admin.SubscriptionUpdate.model_validate({"enabled": "yes"})
    with pytest.raises(ValidationError):
        admin.SubscriptionUpdate.model_validate({"enabled": True, "cadence_s": 600, "tenant": "x"})


# ─────────────────────────── secret box ───────────────────────────

def test_secret_box_roundtrip_and_binding() -> None:
    kek = derive_kek(data_key=None, shared_secret="s" * 48)
    assert kek is not None and kek.kek_id.startswith("shared:") and "s" * 10 not in repr(kek)
    token = seal(kek, "litellm_signals_key", "sk-virtual-secret")
    assert "sk-virtual" not in token and token.startswith("v1.")
    assert open_box(kek, "litellm_signals_key", token) == "sk-virtual-secret"
    assert seal(kek, "n", "same") != seal(kek, "n", "same")  # random nonce
    with pytest.raises(SecretBoxError):
        open_box(kek, "other_name", token)  # associated data binds the name
    other = derive_kek(data_key=None, shared_secret="t" * 48)
    assert other is not None and other.kek_id != kek.kek_id
    with pytest.raises(SecretBoxError):
        open_box(other, "litellm_signals_key", token)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretBoxError):
        open_box(kek, "litellm_signals_key", tampered)
    data = derive_kek(data_key="d" * 40, shared_secret="s" * 48)
    assert data is not None and data.kek_id.startswith("data:")  # the dedicated key wins
    assert derive_kek(data_key=None, shared_secret=None) is None


def test_redact_and_reprs_hide_keys() -> None:
    assert "sk-abc123456" not in redact("bad key sk-abc123456 for alias")
    assert "topsecret" not in redact("x topsecret y", "topsecret")
    client = LiteLlmClient(httpx.AsyncClient(), base_url="http://litellm:4000", api_key="sk-virtual-xyz")
    assert "sk-virtual" not in repr(client) and client.has_key
    client.set_key(None)
    assert not client.has_key
    s = Settings(shared_secret="jwt-secret-value", litellm_key="sk-virtual-xyz", data_key="data-key-value")
    assert "jwt-secret-value" not in repr(s) and "sk-virtual-xyz" not in repr(s) and "data-key-value" not in repr(s)
    a = LiteLlmAdmin(httpx.AsyncClient(), base_url="http://litellm:4000", master_key="sk-master-zzz")
    assert "sk-master" not in repr(a)


# ─────────────────────────── provisioning (mock LiteLLM, in-memory store) ───────────────────────────

class MemConn:
    """The two queries provisioning issues on a plain connection, against a dict."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def execute(self, sql: str, *args: Any) -> str:
        if sql.lstrip().startswith("INSERT INTO hub_secret"):
            name, ciphertext, kek_id, meta = args
            self.rows[name] = {"ciphertext": ciphertext, "kek_id": kek_id, "meta": json.loads(meta)}
        return "OK"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return self.rows.get(args[0])


SETTINGS = Settings(shared_secret="x" * 48, litellm_base_url="http://litellm:4000")


async def _provision(mock: MockLiteLlm, conn: MemConn, settings: Settings = SETTINGS) -> str:
    async with httpx.AsyncClient(transport=mock.transport()) as http:
        adm = LiteLlmAdmin(http, base_url=settings.litellm_base_url, master_key=mock.master_key)
        return await provision_signals_key(settings, conn, adm)  # type: ignore[arg-type]


async def test_provision_generate_reuse_update_rotate(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    mock, conn = MockLiteLlm(), MemConn()
    assert await _provision(mock, conn) == "generated"
    (info,) = mock.active_keys("keelson-signal-hub")
    assert info["models"] == ["extract-fast", "guard-default"] and info["max_budget"] == 5.0
    assert info["budget_duration"] == "30d" and info["key_type"] == "llm_api"
    stored = conn.rows[SECRET_NAME]
    raw = next(iter(mock.raw.values()))
    assert raw not in stored["ciphertext"] and stored["meta"]["key_alias"] == "keelson-signal-hub"
    # second run: the stored key is active and in shape → nothing to do
    assert await _provision(mock, conn) == "unchanged"
    assert len(mock.keys) == 1
    # restrictions drift (someone widened the key in the UI) → re-synced on the same key
    next(iter(mock.keys.values())).update(models=["extract-fast", "guard-default", "reasoning-default"], max_budget=500.0)
    assert await _provision(mock, conn) == "updated"
    (info,) = mock.active_keys("keelson-signal-hub")
    assert info["models"] == ["extract-fast", "guard-default"] and info["max_budget"] == 5.0
    assert ("POST", "/key/update") in mock.calls
    upd = next(b for b in mock.bodies if "key" in b and "models" in b)
    assert re.fullmatch(r"[0-9a-f]{64}", upd["key"])  # the sha256 travels, never the raw key
    # revoked key → replaced (old alias deleted, exactly one active key)
    next(iter(mock.keys.values()))["status"] = "revoked"
    assert await _provision(mock, conn) == "generated"
    assert len(mock.active_keys("keelson-signal-hub")) == 1 and len(mock.keys) == 1
    # SIGNAL_HUB_SHARED_SECRET rotated → the stored key no longer decrypts → replaced
    rotated = dataclasses.replace(SETTINGS, shared_secret="y" * 48)
    assert await _provision(mock, conn, rotated) == "generated"
    assert len(mock.keys) == 1 and conn.rows[SECRET_NAME]["kek_id"] == derive_kek(data_key=None, shared_secret="y" * 48).kek_id  # type: ignore[union-attr]
    # no key or master key ever reached the logs
    text = caplog.text
    assert "sk-virtual" not in text and mock.master_key not in text


async def test_provision_restrictions_follow_settings() -> None:
    mock, conn = MockLiteLlm(), MemConn()
    s = dataclasses.replace(SETTINGS, llm_key_max_budget=2.5, llm_key_alias="sh-key", extract_alias="extract-x")
    assert key_restrictions(s)["models"] == ["extract-x", "guard-default"]
    assert await _provision(mock, conn, s) == "generated"
    (info,) = mock.active_keys("sh-key")
    assert info["max_budget"] == 2.5 and info["models"] == ["extract-x", "guard-default"]


async def test_provision_errors_are_redacted() -> None:
    class Refusing(MockLiteLlm):
        def handle(self, request: Any) -> Any:
            if request.url.path == "/key/generate":
                return httpx.Response(400, json={"error": "bad request for sk-leaky-value-123 by sk-master-test"})
            return super().handle(request)

    with pytest.raises(LiteLlmAdminError) as err:
        await _provision(Refusing(), MemConn())
    assert "sk-leaky" not in str(err.value) and "sk-master-test" not in str(err.value) and err.value.status == 400
    with pytest.raises(SecretBoxError):
        await _provision(MockLiteLlm(), MemConn(), dataclasses.replace(SETTINGS, shared_secret=None))


async def test_wrong_master_key_fails_cleanly() -> None:
    mock = MockLiteLlm()
    async with httpx.AsyncClient(transport=mock.transport()) as http:
        adm = LiteLlmAdmin(http, base_url="http://litellm:4000", master_key="sk-wrong-master")
        with pytest.raises(LiteLlmAdminError) as err:
            await provision_signals_key(SETTINGS, MemConn(), adm)  # type: ignore[arg-type]
    assert "sk-wrong-master" not in str(err.value)
