"""M8 (``docs/contracts/122-m8-adapter-sidecar.md`` §8): a tier-B source that is not point-in-time
(an adapter sidecar crawling a REST API) labels its manifest ``consistency: crawled`` with an
``as_of_range``; the sync stores it, so the snapshot is advisory like a tier-A crawl. A manifest
without the fields (Merxa) stays ``consistent``."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_env import TENANT_A
from e1_scn_server import ScnState, ScnTestServer
from keelson_engine.config import Settings
from keelson_engine.core import scn_sync
from keelson_engine.core.db import tenant_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

SLUG = "scn-sidecar-m8"
TOOLS = ("scn.snapshot_open", "scn.snapshot_export", "scn.changes_since")
RANGE = {"from": "2026-10-08T09:00:00+00:00", "to": "2026-10-08T09:00:07+00:00"}


async def _drop() -> None:
    async with tenant_session(TENANT_A) as s:
        await scn_sync.purge_connector(s, TENANT_A, SLUG)
        await s.execute(text("DELETE FROM connector WHERE slug = :s"), {"s": SLUG})


async def _add(url: str) -> None:
    await _drop()
    async with tenant_session(TENANT_A) as s:
        await s.execute(
            text("INSERT INTO connector (tenant, slug, name, mcp_url, issuer, status, auth_kind) "
                 "VALUES (:t, :s, 'REST sidecar', :u, '', 'active', 'none')"),
            {"t": TENANT_A, "s": SLUG, "u": url},
        )
        for name in TOOLS:
            await s.execute(
                text("INSERT INTO connector_tool (tenant, connector, name, classification, definition_hash, status, "
                     "access, content_trust, egress, idempotency) VALUES (:t, :s, :n, '{}'::jsonb, 'h', 'approved', "
                     "'read', 'trusted_data', 'internal', 'none')"),
                {"t": TENANT_A, "s": SLUG, "n": name},
            )


@pytest_asyncio.fixture(loop_scope="session")
async def source(platform: None) -> AsyncIterator[ScnTestServer]:
    server = ScnTestServer(ScnState(manifest_extra={"consistency": "crawled", "as_of_range": RANGE}))
    await server.start()
    await _add(server.url)
    yield server
    await server.stop()
    await _drop()


@pytest.fixture
def fast(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(settings, "scn_sync_retry_seconds", 60.0)
    return settings


async def test_crawled_manifest_makes_an_advisory_snapshot(source: ScnTestServer, fast: Settings) -> None:
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="test")
    assert snap.status == "ready" and snap.consistency == "crawled" and snap.advisory
    assert snap.as_of_range is not None
    assert snap.as_of_range["from"].startswith("2026-10-08T09:00:00")
    assert snap.as_of_range["to"].startswith("2026-10-08T09:00:07")
    assert snap.label["consistency"] == "crawled"
    current = await scn_sync.current_snapshot(TENANT_A, SLUG)
    assert current is not None and current.id == snap.id and current.advisory


async def test_plain_manifest_stays_consistent(source: ScnTestServer, fast: Settings) -> None:
    source.state.manifest_extra = {}
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="test")
    assert snap.consistency == "consistent" and not snap.advisory and snap.as_of_range is None


async def test_inconsistent_label_is_refused(source: ScnTestServer, fast: Settings) -> None:
    source.state.manifest_extra = {"consistency": "crawled"}  # a crawled snapshot must name its range
    with pytest.raises(scn_sync.SyncFailed) as err:
        await scn_sync.sync_connector(TENANT_A, SLUG, reason="test")
    assert err.value.code == "manifest_invalid"
