#!/usr/bin/env python3
"""Assertions for infra/caddy/Caddyfile against stub upstreams (see run.sh).

Usage: ``python check_routes.py <caddy-host> [default|kc-admin-allowed]``.
Prints one JSON line per check; exit status = number of failed checks.
Requests are sent with raw paths (http.client) so path tricks reach Caddy unmodified.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import time
from collections.abc import Callable
from typing import Any

CADDY = sys.argv[1] if len(sys.argv) > 1 else "caddy"
MODE = sys.argv[2] if len(sys.argv) > 2 else "default"
CADDY_IP = socket.gethostbyname(CADDY)
MY_IP = socket.gethostbyname(socket.gethostname())


def req(host: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
    conn = http.client.HTTPConnection(CADDY_IP, 80, timeout=15)
    conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", host)
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    conn.endheaders()
    r = conn.getresponse()
    raw = r.read()
    try:
        body: Any = json.loads(raw)
    except ValueError:
        body = raw.decode(errors="replace")
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


failures = 0


def check(name: str, fn: Callable[[], dict[str, Any]]) -> None:
    global failures
    try:
        detail = fn()
        ok = bool(detail.pop("ok"))
    except Exception as exc:  # noqa: BLE001 - any error is a failed check
        ok, detail = False, {"error": f"{type(exc).__name__}: {exc}"}
    failures += 0 if ok else 1
    print(json.dumps({"check": name, "ok": ok, **detail}), flush=True)


def upstream_port(host: str, path: str) -> int | None:
    status, _, body = req(host, path)
    return body.get("port") if status == 200 and isinstance(body, dict) else None


def routes() -> dict[str, Any]:
    table = {
        ("keelson.localhost", "/"): 3000,
        ("keelson.localhost", "/cases/1"): 3000,
        ("keelson.localhost", "/apix"): 3000,
        ("keelson.localhost", "/api"): 8000,
        ("keelson.localhost", "/api/me"): 8000,
        ("keelson.localhost", "/api/.well-known/jwks.json"): 8000,
        ("merxa.localhost", "/"): 3001,
        ("merxa.localhost", "/purchase-orders"): 3001,
        ("merxa.localhost", "/v1/auth/me"): 8100,
        ("merxa.localhost", "/mcp"): 8100,
        ("merxa.localhost", "/mcp/messages"): 8100,
        ("merxa.localhost", "/mcp-lite"): 8100,
        ("merxa.localhost", "/mcp-lite/x"): 8100,
        ("merxa.localhost", "/.well-known/oauth-protected-resource/mcp"): 8100,
        ("fernhill.localhost", "/mcp"): 8400,
        ("fernhill.localhost", "/v1/snapshots/x/allocation/part-0001.parquet"): 8400,
        ("fernhill.localhost", "/v1/admin/stats"): 8400,
        ("fernhill.localhost", "/healthz"): 8400,
        ("auth.localhost", "/realms/keelson/.well-known/openid-configuration"): 8080,
        ("auth.localhost", "/resources/x/login/keycloak.v2/css/login.css"): 8080,
    }
    got = {f"{h}{p}": upstream_port(h, p) for (h, p) in table}
    bad = {f"{h}{p}": got[f"{h}{p}"] for (h, p), want in table.items() if got[f"{h}{p}"] != want}
    unknown = req("unknown.localhost", "/")[0]
    return {"ok": not bad and unknown == 404, "mismatches": bad, "unknown_host_status": unknown}


def forwarded_headers() -> dict[str, Any]:
    spoof = {
        "X-Forwarded-For": "6.6.6.6",
        "X-Forwarded-Host": "evil.example",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Port": "8443",
        "X-Real-IP": "6.6.6.6",
    }
    status, resp_headers, body = req("auth.localhost", "/realms/keelson/x", spoof)
    h = {k.lower(): v for k, v in body["headers"].items()}
    want = {
        "host": "auth.localhost",
        "x-forwarded-host": "auth.localhost",
        "x-forwarded-proto": "http",
        "x-forwarded-port": "80",
        "x-forwarded-for": MY_IP,
        "x-real-ip": MY_IP,
    }
    wrong = {k: h.get(k) for k, v in want.items() if h.get(k) != v}
    return {
        "ok": status == 200 and not wrong and "server" not in resp_headers
        and resp_headers.get("x-content-type-options") == "nosniff",
        "wrong_upstream_headers": wrong,
        "server_header": resp_headers.get("server"),
    }


ADMIN_PATHS = [
    "/admin",
    "/admin/",
    "/admin/master/console/",
    "/admin/realms/keelson/users",
    "//admin/master/console/",
    "/%61dmin/master/console/",
    "/ADMIN/master/console/",
    "/./admin/realms",
    "/realms/keelson/../../admin/realms",
    "/realms/master",
    "/realms/master/.well-known/openid-configuration",
    "/realms//master/protocol/openid-connect/token",
    "/realms/%6Daster/.well-known/openid-configuration",
    "/realms/keelson/../master/.well-known/openid-configuration",
    "/REALMS/MASTER/.well-known/openid-configuration",
]


def kc_admin_blocked() -> dict[str, Any]:
    got = {p: req("auth.localhost", p)[0] for p in ADMIN_PATHS}
    leaked = {p: s for p, s in got.items() if s != 403}
    return {"ok": not leaked, "not_blocked": leaked, "checked": len(got)}


def kc_admin_allowed() -> dict[str, Any]:
    got = {p: upstream_port("auth.localhost", p) for p in ("/admin/master/console/", "/realms/master/x")}
    return {"ok": all(v == 8080 for v in got.values()), "upstream_ports": got}


def forward_auth(host: str, port: int) -> Callable[[], dict[str, Any]]:
    def run() -> dict[str, Any]:
        anon = req(host, "/ui/", {"X-Auth-User": "evil", "X-Auth-Roles": "platform-admin"})
        wrong = req(host, "/ui/", {"Cookie": "keelson_session=planner"})
        ok_status, _, ok_body = req(host, "/ui/?x=1", {"Cookie": "keelson_session=admin"})
        nr_status, _, nr_body = req(
            host, "/ui/", {"Cookie": "keelson_session=admin-noroles", "X-Auth-Roles": "spoofed"}
        )
        ok_h = {k.lower(): v for k, v in ok_body["headers"].items()} if ok_status == 200 else {}
        nr_h = {k.lower(): v for k, v in nr_body["headers"].items()} if nr_status == 200 else {}
        return {
            "ok": anon[0] == 401
            and wrong[0] == 401
            and ok_status == 200
            and ok_body["port"] == port
            and ok_body["path"] == "/ui/?x=1"
            and ok_h.get("host") == host
            and ok_h.get("x-auth-user") == "ava"
            and ok_h.get("x-auth-roles") == "platform-admin"
            and nr_status == 200
            and "x-auth-roles" not in nr_h,
            "anonymous": anon[0],
            "non_admin": wrong[0],
            "admin": ok_status,
            "admin_upstream_port": ok_body.get("port") if isinstance(ok_body, dict) else None,
            "client_spoofed_roles_stripped": "x-auth-roles" not in nr_h,
        }

    return run


def streaming(host: str, path: str) -> Callable[[], dict[str, Any]]:
    """First chunk must arrive long before the stub finishes (2 s later)."""

    def run() -> dict[str, Any]:
        conn = http.client.HTTPConnection(CADDY_IP, 80, timeout=15)
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        conn.putheader("Accept", "text/event-stream")
        conn.endheaders()
        start = time.perf_counter()
        r = conn.getresponse()
        first = r.readline()
        ttfb = time.perf_counter() - start
        r.read()
        total = time.perf_counter() - start
        return {"ok": first.startswith(b"data: 0") and ttfb < 1.0 and total >= 1.9,
                "first_chunk_s": round(ttfb, 2), "total_s": round(total, 2)}

    return run


def log_probe() -> dict[str, Any]:
    """Send credential-bearing values; run.sh asserts none of them reach Caddy's log."""
    status, _, _ = req(
        "keelson.localhost",
        "/api/auth/callback?code=CODE-LOGPROBE&state=state-ok&session_state=SS-LOGPROBE",
        {
            "Cookie": "keelson_session=COOKIE-LOGPROBE",
            "Authorization": "Bearer BEARER-LOGPROBE",
            "X-Demo-Access": "DEMO-LOGPROBE",
            "X-CSRF-Token": "CSRF-LOGPROBE",
        },
    )
    return {"ok": status == 200, "status": status}


def main() -> int:
    if MODE == "kc-admin-allowed":
        check("kc_admin_allowed_when_DEV_ALLOW_KC_ADMIN_1", kc_admin_allowed)
        return failures
    check("routes", routes)
    check("forwarded_headers_and_security_headers", forwarded_headers)
    check("kc_admin_blocked", kc_admin_blocked)
    check("forward_auth_gateway", forward_auth("gateway.localhost", 4000))
    check("forward_auth_traces", forward_auth("traces.localhost", 6006))
    check("sse_unbuffered_keelson_api", streaming("keelson.localhost", "/api/runs/1/stream"))
    check("sse_unbuffered_merxa_mcp", streaming("merxa.localhost", "/mcp/stream"))
    # Regression guard for streamed (unknown-length) responses of any content type. Caddy
    # 2.11 already flushes these immediately; flush_interval -1 keeps that explicit.
    check("ndjson_unbuffered_keelson_api", streaming("keelson.localhost", "/api/runs/1/ndjson"))
    check("ndjson_unbuffered_merxa_mcp", streaming("merxa.localhost", "/mcp/ndjson"))
    check("ndjson_unbuffered_merxa_v1", streaming("merxa.localhost", "/v1/export/ndjson"))
    check("sse_unbuffered_fernhill_mcp", streaming("fernhill.localhost", "/mcp/stream"))
    check("log_probe_sent", log_probe)
    return failures


if __name__ == "__main__":
    sys.exit(main())
