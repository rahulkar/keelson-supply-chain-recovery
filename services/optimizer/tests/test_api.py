"""HTTP surface: health, readiness, service-JWT auth, tenant binding, validation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from keelson_opt.config import Settings
from keelson_opt.main import create_app

from .conftest import TEST_SECRET, make_token

ENDPOINTS = [("get", "/v1/levers"), ("post", "/v1/impact"), ("post", "/v1/optimize")]


def _impact_body(raw: dict[str, Any]) -> dict[str, Any]:
    return {"data": raw["data"], "perturbations": raw["perturbations"]}


def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True}
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "checks": {"shared_secret": True, "glop": True}}


def test_not_ready_and_rejecting_without_secret() -> None:
    with TestClient(create_app(Settings(8200, None, "WARNING", 0))) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 503
        r = c.get("/v1/levers", headers={"Authorization": f"Bearer {make_token()}"})
        assert r.status_code == 401


def test_levers_catalogue(client: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    r = client.get("/v1/levers", headers=auth())
    assert r.status_code == 200
    body = r.json()
    assert [lv["code"] for lv in body] == [
        "reallocate_inventory",
        "expedite_mode_shift",
        "resource_alt_supplier",
        "prioritize_allocation",
        "resequence_production",
    ]
    by_code = {lv["code"]: lv for lv in body}
    lever = by_code["reallocate_inventory"]
    assert lever["ops"] == ["transfer_order.create"]
    assert set(lever["params_schema"]["properties"]) == {"max_units_total", "allowed_lanes"}
    assert by_code["expedite_mode_shift"]["ops"] == ["po_line.split", "capacity_booking.create"]
    assert by_code["resource_alt_supplier"]["ops"] == ["po.create", "po_line.change_qty", "po_line.cancel"]
    assert by_code["prioritize_allocation"]["ops"] == ["allocation.set_qty"]
    assert by_code["resequence_production"]["ops"] == ["production_order.resequence", "po_line.reschedule"]
    for lv in body:
        assert lv["title"] and lv["params_schema"]["type"] == "object"
    assert set(by_code["expedite_mode_shift"]["params_schema"]["properties"]) == {
        "modes",
        "allowed_lanes",
        "po_line_refs",
        "max_units_total",
        "max_premium_cents",
    }


def test_impact_over_http(client: TestClient, auth: Callable[..., dict[str, str]], small_raw: dict[str, Any]) -> None:
    r = client.post(
        "/v1/impact",
        json=_impact_body(small_raw),
        headers={**auth(), "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kpis"]["shortage_cluster_weeks"] == 4
    assert body["baseline_kpis"]["units_at_risk"] == 0
    assert body["provenance"]["engine"] == "keelson-opt"
    assert r.headers["x-trace-id"] == "0af7651916cd43dd8448eb211c80319c"
    assert isinstance(body["kpis"]["revenue_at_risk_cents"], int)


def test_optimize_over_http(client: TestClient, auth: Callable[..., dict[str, str]], small_raw: dict[str, Any]) -> None:
    r = client.post("/v1/optimize", json=small_raw, headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "OPTIMAL"
    assert body["actions"][0]["seq"] == 1
    assert body["actions"][0]["payload"]["ship_date"] == "2026-10-05"
    assert body["solver"]["inputs_hash"] and isinstance(body["solver"]["solve_ms"], int)


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_missing_or_malformed_authorization(client: TestClient, method: str, path: str) -> None:
    for headers in (
        {},
        {"Authorization": "Basic Zm9vOmJhcg=="},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer not-a-jwt"},
    ):
        r = client.request(method, path, headers=headers, json={})
        assert r.status_code == 401, headers
        assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")  # HS512 case
@pytest.mark.parametrize(
    "token_kwargs",
    [
        pytest.param({"secret": "a-different-secret-that-is-long-enough-0000"}, id="bad-signature"),
        pytest.param({"iss": "someone-else"}, id="wrong-issuer"),
        pytest.param({"aud": "keelson-engine"}, id="wrong-audience"),
        pytest.param({"exp": int(time.time()) - 30}, id="expired"),
        pytest.param({"drop": ("exp",)}, id="no-exp"),
        pytest.param({"drop": ("tenant",)}, id="no-tenant"),
        pytest.param({"tenant": ""}, id="empty-tenant"),
        pytest.param({"tenant": ["demo-retail"]}, id="tenant-not-string"),
        pytest.param({"algorithm": "HS512"}, id="wrong-alg"),
    ],
)
def test_bad_tokens_rejected(client: TestClient, small_raw: dict[str, Any], token_kwargs: dict[str, Any]) -> None:
    headers = {"Authorization": f"Bearer {make_token(**token_kwargs)}"}
    assert client.get("/v1/levers", headers=headers).status_code == 401
    assert client.post("/v1/impact", json=_impact_body(small_raw), headers=headers).status_code == 401


def test_unsigned_token_rejected(client: TestClient) -> None:
    import base64
    import json

    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {
        "iss": "keelson-engine",
        "aud": "keelson-optimizer",
        "tenant": "demo-retail",
        "exp": int(time.time()) + 60,
    }
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."
    assert client.get("/v1/levers", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_tenant_mismatch_forbidden(
    client: TestClient, auth: Callable[..., dict[str, str]], small_raw: dict[str, Any]
) -> None:
    r = client.post("/v1/impact", json=_impact_body(small_raw), headers=auth(tenant="northwind-test"))
    assert r.status_code == 403
    r = client.post("/v1/optimize", json=small_raw, headers=auth(tenant="northwind-test"))
    assert r.status_code == 403


def test_secret_value_is_not_leaked(client: TestClient) -> None:
    r = client.get("/v1/levers", headers={"Authorization": "Bearer x.y.z"})
    assert TEST_SECRET not in r.text


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda b: b["data"]["skus"][0].__setitem__("retail_price_cents", 25.0), id="float-money"),
        pytest.param(lambda b: b["data"]["demand"][0].__setitem__("units", True), id="bool-units"),
        pytest.param(lambda b: b["data"]["demand"][0].__setitem__("iso_week", "2026-41"), id="bad-week"),
        pytest.param(lambda b: b["data"]["receipts"][0].__setitem__("dc_code", "XX9"), id="unknown-dc"),
        pytest.param(lambda b: b["plan_spec"]["levers"][0].__setitem__("code", "magic"), id="unknown-lever"),
        pytest.param(lambda b: b["plan_spec"]["levers"].append(b["plan_spec"]["levers"][0]), id="duplicate-lever"),
        pytest.param(lambda b: b.__setitem__("surprise", 1), id="extra-field"),
    ],
)
def test_validation_errors_are_422(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    small: dict[str, Any],
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    mutate(small)
    r = client.post("/v1/optimize", json=small, headers=auth())
    assert r.status_code == 422, r.text


def test_validation_error_body_is_bounded_and_does_not_echo_input(
    client: TestClient, auth: Callable[..., dict[str, str]], small: dict[str, Any]
) -> None:
    """A model-level error on ``data`` must not echo the whole ProblemData back."""
    small["data"]["receipts"][0]["dc_code"] = "XX9"
    r = client.post("/v1/optimize", json=small, headers=auth())
    assert r.status_code == 422
    body = r.json()
    assert len(r.content) < 2000, len(r.content)
    assert all(set(e) == {"type", "loc", "msg"} for e in body["detail"])
    assert any("unknown dc XX9" in e["msg"] for e in body["detail"])


def test_unauthenticated_request_is_rejected_before_the_body_is_parsed(client: TestClient) -> None:
    """Without the early check FastAPI parses the JSON body first and answers 422 here."""
    for path in ("/v1/impact", "/v1/optimize"):
        r = client.post(path, content=b"{not json", headers={"content-type": "application/json"})
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")


def test_bad_token_with_valid_body_still_401_not_403(client: TestClient, small_raw: dict[str, Any]) -> None:
    headers = {"Authorization": f"Bearer {make_token(tenant='northwind-test', secret='x' * 40)}"}
    assert client.post("/v1/optimize", json=small_raw, headers=headers).status_code == 401


def test_body_size_limit(auth: Callable[..., dict[str, str]], small_raw: dict[str, Any]) -> None:
    import json

    body = json.dumps(small_raw).encode()
    limited = Settings(8200, TEST_SECRET, "WARNING", 0, max_body_bytes=len(body) - 1)
    with TestClient(create_app(limited)) as c:
        headers = {**auth(), "content-type": "application/json"}
        # Declared Content-Length over the limit.
        r = c.post("/v1/optimize", content=body, headers=headers)
        assert r.status_code == 413
        # Streamed (chunked, no Content-Length): counted while reading.
        r = c.post("/v1/optimize", content=iter([body[:1000], body[1000:]]), headers=headers)
        assert r.status_code == 413
    roomy = Settings(8200, TEST_SECRET, "WARNING", 0, max_body_bytes=len(body))
    with TestClient(create_app(roomy)) as c:
        r = c.post("/v1/optimize", content=body, headers={**auth(), "content-type": "application/json"})
        assert r.status_code == 200, r.text[:300]


def test_partial_issuer_rejected(client: TestClient) -> None:
    """Guards against substring issuer matching (PyJWT 2.10.0, CVE-2024-53861)."""
    for iss in ("keelson-engin", "keelson", "x-keelson-engine"):
        headers = {"Authorization": f"Bearer {make_token(iss=iss)}"}
        assert client.get("/v1/levers", headers=headers).status_code == 401, iss


def test_nonexistent_iso_week_is_422(
    client: TestClient, auth: Callable[..., dict[str, str]], small: dict[str, Any]
) -> None:
    small["data"]["demand"][0]["iso_week"] = "2025-W53"  # 2025 has 52 ISO weeks
    r = client.post("/v1/impact", json=_impact_body(small), headers=auth())
    assert r.status_code == 422
    assert any("not an ISO week" in e["msg"] for e in r.json()["detail"])


def test_compute_requests_are_serialised(
    monkeypatch: pytest.MonkeyPatch, auth: Callable[..., dict[str, str]], small_raw: dict[str, Any]
) -> None:
    """With max_concurrency=1 (default, sized for the 512 MB container) two simultaneous
    POSTs never compute at the same time; GET routes are not queued behind them."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from keelson_opt import main as main_mod
    from keelson_opt.service import run_impact as real_run_impact

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def slow_impact(req: Any) -> Any:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.3)
        try:
            return real_run_impact(req)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(main_mod, "run_impact", slow_impact)
    settings = Settings(8200, TEST_SECRET, "WARNING", 0)
    assert settings.max_concurrency == 1
    with TestClient(create_app(settings)) as c, ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(c.post, "/v1/impact", json=_impact_body(small_raw), headers=auth()) for _ in range(2)]
        time.sleep(0.1)
        assert c.get("/v1/levers", headers=auth()).status_code == 200  # not blocked
        assert [f.result().status_code for f in futures] == [200, 200]
    assert state["peak"] == 1
