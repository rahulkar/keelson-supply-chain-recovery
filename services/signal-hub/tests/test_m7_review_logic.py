"""M7 review regressions (offline): extraction-key provisioning never blocks the bootstrap and re-syncs
widened routes, cadence limits never leave the table's CHECK range, the engine's whole-set shapes
(``keelson_engine.core.signals_admin``) validate, and the alert-rule hard/soft split."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from fakes import MockLiteLlm
from keelson_contracts.signals import AlertRule
from signal_hub import admin
from signal_hub.bootstrap import provision_llm_key
from signal_hub.config import RssFeed, Settings
from signal_hub.llm_key import LLM_API_ROUTES, LiteLlmAdmin, provision_signals_key
from test_m7_admin_logic import SETTINGS, MemConn

# ─────────────────────────── provisioning ───────────────────────────

async def test_widened_routes_are_resynced() -> None:
    mock, conn = MockLiteLlm(), MemConn()
    async with httpx.AsyncClient(transport=mock.transport()) as http:
        adm = LiteLlmAdmin(http, base_url=SETTINGS.litellm_base_url, master_key=mock.master_key)
        assert await provision_signals_key(SETTINGS, conn, adm) == "generated"  # type: ignore[arg-type]
        (info,) = mock.active_keys("keelson-signal-hub")
        assert info["allowed_routes"] == list(LLM_API_ROUTES)
        assert await provision_signals_key(SETTINGS, conn, adm) == "unchanged"  # type: ignore[arg-type]
        info["allowed_routes"] = ["llm_api_routes", "management_routes"]  # someone widened it in the UI
        assert await provision_signals_key(SETTINGS, conn, adm) == "updated"  # type: ignore[arg-type]
        (info,) = mock.active_keys("keelson-signal-hub")
        assert info["allowed_routes"] == ["llm_api_routes"] and len(mock.keys) == 1


class _BrokenConn(MemConn):
    """A store that fails with an unexpected error type whose text carries the master key."""

    async def execute(self, sql: str, *args: Any) -> str:
        if sql.lstrip().startswith("INSERT INTO hub_secret"):
            raise RuntimeError("disk full while writing for sk-master-test")
        return await super().execute(sql, *args)


async def test_unexpected_provisioning_error_never_fails_bootstrap(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    mock = MockLiteLlm()
    s = dataclasses.replace(SETTINGS, litellm_wait_s=0.1)
    async with httpx.AsyncClient(transport=mock.transport()) as http:
        result = await provision_llm_key(s, _BrokenConn(), mock.master_key, http=http)  # type: ignore[arg-type]
    assert result == "failed"
    rec = next(r for r in caplog.records if r.getMessage() == "extraction key not provisioned")
    assert "RuntimeError: disk full" in rec.error  # type: ignore[attr-defined]
    everything = " ".join(str(v) for r in caplog.records for v in vars(r).values())  # messages + extras
    assert mock.master_key not in everything and "sk-virtual" not in everything


# ─────────────────────────── cadence limits ───────────────────────────

def test_cadence_limits_stay_inside_the_check_range() -> None:
    """A deployment default below 60 s / above a day must not let a tenant store a value the
    ``source_subscription.cadence_s`` CHECK rejects (that was a 500)."""
    s = Settings(cadence_rss_s=30, cadence_gdacs_s=200_000,
                 rss_feeds=(RssFeed("rss.x", "X", "https://x.example/feed"),))
    cat = {c.connector_id: c for c in admin.source_catalogue(s)}
    assert cat["rss.x"].min_cadence_s == admin.CADENCE_DB_RANGE[0] == 60
    assert cat["gdacs"].max_cadence_s == admin.CADENCE_DB_RANGE[1] == 86_400
    assert cat["usgs"].min_cadence_s == 120


# ─────────────────────────── engine whole-set shapes ───────────────────────────

GULF = {"type": "Polygon", "coordinates": [[[99.0, 9.0], [109.0, 9.0], [109.0, 14.5], [99.0, 14.5], [99.0, 9.0]]]}


def test_watch_region_set_accepts_engine_shape() -> None:
    body = admin.WatchRegionSet.model_validate({"regions": [
        {"id": "bkk", "name": "Bangkok", "kind": "radius", "polygon": None, "node": {"type": "port", "code": "THBKK"},
         "radius_km": 2000, "enabled": True},
        {"id": "gulf", "name": "Gulf", "kind": "polygon", "polygon": GULF, "node": None, "radius_km": None,
         "enabled": False},
    ]})
    bkk, gulf = body.regions
    assert (bkk.node_type, bkk.node_code, bkk.radius_km, bkk.geometry) == ("port", "THBKK", 2000, None)
    assert gulf.geometry is not None and gulf.geometry.type == "Polygon" and gulf.enabled is False


@pytest.mark.parametrize("region", [
    {"id": "x", "name": "n", "kind": "radius", "node": {"type": "port", "code": "A"}, "radius_km": 2001},
    {"id": "x", "name": "n", "kind": "radius", "node": {"type": "port", "code": "A"}, "node_type": "port",
     "radius_km": 5},
    {"id": "x", "name": "n", "kind": "radius", "node": {"type": "port", "code": "A", "extra": 1}, "radius_km": 5},
    {"id": "x", "name": "n", "kind": "radius", "node": ["port", "A"], "radius_km": 5},
    {"id": "x", "name": "n", "kind": "polygon", "polygon": GULF, "geometry": GULF},
    {"id": "x", "name": "n", "kind": "polygon", "polygon": GULF, "unknown": True},
    {"id": "x", "name": "n", "kind": "radius", "node": {"type": "planet", "code": "A"}, "radius_km": 5},
])
def test_watch_region_set_item_rejects(region: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        admin.WatchRegionSetItem.model_validate(region)


def test_whole_sets_need_unique_ids_and_bounded_size() -> None:
    r = {"id": "a", "name": "n", "kind": "radius", "node": {"type": "port", "code": "A"}, "radius_km": 5}
    with pytest.raises(ValidationError):
        admin.WatchRegionSet.model_validate({"regions": [r, r]})
    with pytest.raises(ValidationError):
        admin.WatchRegionSet.model_validate({"regions": [{**r, "id": f"r{i}"} for i in range(admin.MAX_WATCH_REGIONS + 1)]})
    with pytest.raises(ValidationError):
        admin.SubscriptionSet.model_validate({"sources": [{"connector_id": "gdacs", "enabled": True}] * 2})
    with pytest.raises(ValidationError):
        admin.SubscriptionSet.model_validate({"sources": [{"connector_id": "gdacs", "enabled": "yes"}]})
    s = admin.SubscriptionSet.model_validate({"sources": [{"connector_id": "gdacs", "enabled": False}]})
    assert "cadence_s" not in s.sources[0].model_fields_set  # omitted = keep the stored wish


# ─────────────────────────── alert rules: hard vs editor advice ───────────────────────────

def _rule(rid: str, hysteresis: int = 1000) -> AlertRule:
    return AlertRule.model_validate({"id": rid, "action": "alert", "hysteresis_bp": hysteresis, "when": {
        "min_severity_bp": 5000, "kinds": [], "min_confidence_bp": 0, "min_exposed_nodes": 1, "node_types": []}})


def test_hard_rule_problems_are_a_subset() -> None:
    noisy = [_rule("a", hysteresis=0)]
    assert admin.hard_rule_problems(noisy) == [] and admin.rule_problems(noisy)
    dup = [_rule("a"), _rule("a")]
    assert admin.hard_rule_problems(dup) == admin.rule_problems(dup) == [{"rule": "a", "problem": "duplicate rule id"}]
    many = [_rule(f"r{i}") for i in range(admin.MAX_RULES + 1)]
    assert admin.hard_rule_problems(many)[0]["rule"] == "*"
