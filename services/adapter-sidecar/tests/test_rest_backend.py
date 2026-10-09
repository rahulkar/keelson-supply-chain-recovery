"""REST backend (read-only): page / offset / cursor / link pagination, JSONPath-lite extraction,
same-origin next links, size and page caps, crawled snapshots with an as-of range, drift on
re-crawl, tiers A·B·C without write tools."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml
from fastmcp.exceptions import ToolError

from conftest import SERVICE, Running, sidecar_env, start_app
from helpers import call
from keelson_contracts.connectors import ConnectorTier, detect_tiers
from keelson_contracts.scn import SnapshotManifest

BASE = "http://erp.example.test"
TOKEN = "erp_api_token_0123456789abcdefghijklmn"


class FakeErpApi:
    def __init__(self) -> None:
        self.warehouses = [
            {"id": "W1", "name": "Leeds", "address": {"country": "GB", "city": "Leeds"},
             "geo": {"lat": 53.8, "lon": -1.55}, "version": 1},
            {"id": "W2", "name": "Rotterdam", "address": {"country": "NL", "city": "Rotterdam"},
             "geo": {"lat": 51.92, "lon": 4.48}, "version": 3},
            {"id": "W3", "name": "Lyon", "address": {"country": "FR"}, "geo": {"lat": 45.76, "lon": 4.84},
             "version": 1},
        ]
        self.products = [{"code": "P1", "name": "Parka", "family": "outerwear", "colour": "navy",
                          "retail_price_cents": 12900, "currency": "GBP", "rev": 1},
                         {"code": "P2", "name": "Fleece", "family": "fleece", "colour": "green",
                          "retail_price_cents": 4900, "currency": "GBP", "rev": 1}]
        self.skus = [{"sku": f"{p}-{s}", "style": p, "size": s, "size_seq": i, "cost": {"cents": 1000 + i}, "rev": 1}
                     for p in ("P1", "P2") for i, s in enumerate(("S", "M", "L"), start=1)]
        self.stock = [{"site": w, "site_kind": "dc", "sku": s["sku"], "on_hand": 10, "allocated": 0, "reserved": 0,
                       "in_transit": 0, "rev": 1} for w in ("W1", "W2") for s in self.skus]
        self.requests: list[httpx.Request] = []
        self.next_link: str | None = None
        self.oversize = False

    def install(self, router: respx.Router) -> None:
        router.get(f"{BASE}/api/v1/warehouses").mock(side_effect=self._warehouses)
        router.get(f"{BASE}/api/v1/products").mock(side_effect=self._products)
        router.get(f"{BASE}/api/v1/skus").mock(side_effect=self._skus)
        router.get(f"{BASE}/api/v1/stock").mock(side_effect=self._stock)

    def _seen(self, request: httpx.Request) -> None:
        self.requests.append(request)
        assert request.headers.get("X-Api-Key") == TOKEN

    def _warehouses(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        page, size = int(request.url.params["page"]), int(request.url.params["per_page"])
        return httpx.Response(200, json={"data": self.warehouses[(page - 1) * size: page * size], "meta": {}})

    def _products(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        if self.oversize:
            return httpx.Response(200, json=[{"code": "X" * 10_000}])
        if request.url.params.get("after") == "P1":
            return httpx.Response(200, json=self.products[1:])
        nxt = self.next_link or f"{BASE}/api/v1/products?after=P1"
        return httpx.Response(200, json=self.products[:1], headers={"Link": f'<{nxt}>; rel="next"'})

    def _skus(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        assert request.url.params["active"] == "true"
        start = int(request.url.params.get("cursor") or 0)
        size = int(request.url.params["limit"])
        chunk = self.skus[start:start + size]
        nxt = str(start + size) if start + size < len(self.skus) else None
        return httpx.Response(200, json={"items": chunk, "next_cursor": nxt})

    def _stock(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        off, size = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(200, json={"results": self.stock[off:off + size]})


def config(tmp_path: Path, **rest: Any) -> Path:
    raw = yaml.safe_load((SERVICE / "examples" / "rest-readonly.yaml").read_text(encoding="utf-8"))
    for t in raw["tables"].values():
        pag = t["endpoint"].get("pagination") or {}
        if "size" in pag:
            pag["size"] = 2
    raw["backend"]["rest"].update(rest)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "rest.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.fixture
def api() -> FakeErpApi:
    return FakeErpApi()


async def boot(tmp_path: Path, api: FakeErpApi, router: respx.Router, **rest: Any):  # type: ignore[no-untyped-def]
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    api.install(router)
    settings = Settings.from_env(sidecar_env(tmp_path, None, config(tmp_path, **rest), ERP_API_TOKEN=TOKEN,
                                             DEMO_MODE="0"))
    sidecar = Sidecar(settings)
    app, shutdown = await start_app(settings, sidecar)
    return Running(app, sidecar, None, tmp_path), shutdown


async def test_crawled_snapshot(tmp_path: Path, api: FakeErpApi) -> None:
    with respx.mock(assert_all_called=False) as router:
        run, shutdown = await boot(tmp_path, api, router)
        api.requests.clear()  # the start-up probe read one page
        try:
            async with run.client() as c:
                names = [t.name for t in await c.list_tools()]
                assert detect_tiers(names) == [ConnectorTier.A, ConnectorTier.B, ConnectorTier.C]
                assert not {"create_change_set", "apply_change_set"} & set(names)
                assert "list_transfer_orders" not in names
                m = await call(c, "scn.snapshot_open", {"entities": ["location", "item", "inventory_position"]})
            parsed = SnapshotManifest.model_validate(m)
            assert parsed.consistency == "crawled" and parsed.label.advisory
            assert parsed.as_of_range is not None and parsed.as_of_range.from_ <= parsed.as_of_range.to
            assert m["entities"]["location"]["rows"] == 3
            assert m["entities"]["item"]["rows"] == 2 + 6
            assert m["entities"]["inventory_position"]["rows"] == 12
            wh = (await run.sidecar.rows("warehouses", {"code": "W2"}))["items"][0]
            assert wh == {"code": "W2", "name": "Rotterdam", "country": "NL", "city": "Rotterdam", "lat": 51.92,
                          "lon": 4.48, "rev": 3}
            assert (await run.sidecar.rows("warehouses", {"code": "W3"}))["items"][0]["city"] is None
            sku = (await run.sidecar.rows("skus", {"sku": "P2-L"}))["items"][0]
            assert sku["unit_cost_cents"] == 1003
            paths = [r.url.path for r in api.requests]
            assert paths.count("/api/v1/warehouses") == 2 and paths.count("/api/v1/products") == 2
            assert paths.count("/api/v1/skus") == 3 and paths.count("/api/v1/stock") == 7
        finally:
            await shutdown()


async def test_recrawl_detects_drift_and_respects_interval(tmp_path: Path, api: FakeErpApi) -> None:
    with respx.mock(assert_all_called=False) as router:
        run, shutdown = await boot(tmp_path, api, router, min_crawl_interval_s=0)
        try:
            async with run.client() as c:
                m = await call(c, "scn.snapshot_open", {"entities": ["inventory_position"]})
                quiet = await call(c, "scn.changes_since", {"watermark": m["watermark"]})
                assert "full_resync_required" not in quiet
                api.stock[0]["on_hand"] = 3
                drift = await call(c, "scn.changes_since", {"watermark": m["watermark"]})
                assert drift["full_resync_required"] is True
            run.sidecar.backend.opts = run.sidecar.backend.opts.model_copy(update={"min_crawl_interval_s": 3600})
            before = len(api.requests)
            await run.sidecar.changes_since(m["watermark"])
            assert len(api.requests) == before  # no re-crawl within the interval
        finally:
            await shutdown()


async def test_foreign_next_link_is_refused(tmp_path: Path, api: FakeErpApi) -> None:
    with respx.mock(assert_all_called=False) as router:
        run, shutdown = await boot(tmp_path, api, router)
        try:
            api.next_link = "http://evil.example.test/api/v1/products?after=P1"
            async with run.client() as c:
                with pytest.raises(ToolError, match="foreign_next_link"):
                    await c.call_tool("scn.snapshot_open", {"entities": ["item"]})
        finally:
            await shutdown()


async def test_response_and_page_caps(tmp_path: Path, api: FakeErpApi) -> None:
    with respx.mock(assert_all_called=False) as router:
        run, shutdown = await boot(tmp_path, api, router, max_response_bytes=4096)
        try:
            api.oversize = True
            async with run.client() as c:
                with pytest.raises(ToolError, match="response_too_large"):
                    await c.call_tool("scn.snapshot_open", {"entities": ["item"]})
        finally:
            await shutdown()
    api2 = FakeErpApi()
    with respx.mock(assert_all_called=False) as router:
        run, shutdown = await boot(tmp_path / "b", api2, router, max_pages=3)
        try:
            async with run.client() as c:
                with pytest.raises(ToolError, match="max_pages"):
                    await c.call_tool("scn.snapshot_open", {"entities": ["inventory_position"]})
        finally:
            await shutdown()


async def test_redirects_are_not_followed(tmp_path: Path, api: FakeErpApi) -> None:
    with respx.mock(assert_all_called=False) as router:
        api.install(router)
        router.get(f"{BASE}/api/v1/stock").mock(
            return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"}))
        from adapter_sidecar.service import Sidecar
        from adapter_sidecar.settings import Settings

        settings = Settings.from_env(sidecar_env(tmp_path, None, config(tmp_path), ERP_API_TOKEN=TOKEN, DEMO_MODE="0"))
        sidecar = Sidecar(settings)
        app, shutdown = await start_app(settings, sidecar)
        try:
            async with Running(app, sidecar, None, tmp_path).client() as c:
                with pytest.raises(ToolError, match="HTTP 302"):
                    await c.call_tool("scn.snapshot_open", {"entities": ["inventory_position"]})
            assert not any(call.request.url.host == "169.254.169.254" for call in router.calls)
        finally:
            await shutdown()


def test_rest_config_rules(tmp_path: Path) -> None:
    from adapter_sidecar.config import parse_config
    from adapter_sidecar.errors import ConfigError

    raw = yaml.safe_load((SERVICE / "examples" / "rest-readonly.yaml").read_text(encoding="utf-8"))
    bad = copy.deepcopy(raw)
    bad["tables"]["skus"]["endpoint"]["pagination"] = {"kind": "cursor", "param": "cursor"}
    with pytest.raises(ConfigError, match="cursor_path"):
        parse_config(bad)
    bad = copy.deepcopy(raw)
    bad["tables"]["skus"]["columns"]["sku"] = {"type": "text", "path": "$.a..b"}
    with pytest.raises(ConfigError, match="path"):
        parse_config(bad)
