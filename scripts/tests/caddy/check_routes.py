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


def raw(host: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(CADDY_IP, 80, timeout=15)
    conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", host)
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    conn.endheaders()
    r = conn.getresponse()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()


BASEMAP_ARCHIVE = "/basemap/v4/test.pmtiles"  # 4096 bytes, written by make_basemap_fixture.py


def same_origin_static(h: dict[str, str]) -> bool:
    return (
        h.get("x-content-type-options") == "nosniff"
        and h.get("cross-origin-resource-policy") == "same-origin"
        and "access-control-allow-origin" not in h
        and "server" not in h
    )


def basemap_range() -> dict[str, Any]:
    """The archive is read with HTTP Range (pmtiles://): 206, exact bytes, never encoded."""
    status, h, body = raw("keelson.localhost", BASEMAP_ARCHIVE, {"Range": "bytes=0-126", "Accept-Encoding": "gzip, zstd"})
    tail_status, tail_h, tail = raw("keelson.localhost", BASEMAP_ARCHIVE, {"Range": "bytes=4000-"})
    return {
        "ok": status == 206
        and h.get("content-range") == "bytes 0-126/4096"
        and len(body) == 127
        and body.startswith(b"PMTiles\x03")
        and "content-encoding" not in h
        and h.get("cache-control") == "public, max-age=31536000, immutable"
        and same_origin_static(h)
        and tail_status == 206
        and tail_h.get("content-range") == "bytes 4000-4095/4096"
        and len(tail) == 96,
        "status": status,
        "content_range": h.get("content-range"),
        "content_encoding": h.get("content-encoding"),
        "cache_control": h.get("cache-control"),
        "tail": [tail_status, tail_h.get("content-range")],
    }


def basemap_files() -> dict[str, Any]:
    full_status, full_h, full = raw("keelson.localhost", BASEMAP_ARCHIVE, {"Accept-Encoding": "gzip, zstd"})
    m_status, m_h, m_body = raw("keelson.localhost", "/basemap/manifest.json")
    g_status, g_h, _ = raw("keelson.localhost", "/basemap/fonts/Noto%20Sans%20Regular/0-255.pbf", {"Accept-Encoding": "gzip"})
    s_status, s_h, _ = raw("keelson.localhost", "/basemap/sprites/v4/light.json")
    return {
        "ok": full_status == 200
        and full_h.get("accept-ranges") == "bytes"
        and len(full) == 4096
        and "content-encoding" not in full_h
        and m_status == 200
        and m_h.get("content-type", "").startswith("application/json")
        and m_h.get("cache-control") == "no-cache"
        and json.loads(m_body)["schema"] == "keelson.basemap/1"
        and same_origin_static(m_h)
        and g_status == 200
        and g_h.get("content-type") == "application/x-protobuf"
        and g_h.get("content-encoding") == "gzip"
        and g_h.get("cache-control") == "public, max-age=86400"
        and s_status == 200
        and s_h.get("cache-control") == "public, max-age=86400",
        "archive": [full_status, full_h.get("accept-ranges"), full_h.get("content-encoding")],
        "manifest": [m_status, m_h.get("content-type"), m_h.get("cache-control")],
        "glyphs": [g_status, g_h.get("content-type"), g_h.get("content-encoding"), g_h.get("cache-control")],
        "sprite": [s_status, s_h.get("cache-control")],
    }


BASEMAP_NOT_SERVED = [
    "/basemap/",
    "/basemap/v4/",
    "/basemap/.staging-1-2/partial.pmtiles",
    "/basemap/%2estaging-1-2/partial.pmtiles",
    "/basemap/.hidden.json",
    "/basemap/v4/.keelson-partial.pmtiles",
    "/basemap/missing.json",
    "/basemap//etc/passwd",
]
# Dot segments (plain or percent-encoded) are decoded and cleaned before matching: the request
# leaves /basemap/* and is an ordinary page request for keelson-web (never a file outside the root).
BASEMAP_TRAVERSAL = [
    "/basemap/../Caddyfile",
    "/basemap/%2e%2e/etc/passwd",
    "/basemap/v4/%2e%2e/%2e%2e/etc/passwd",
    "/basemap/..%2f..%2fetc%2fpasswd",
]


def from_web_stub(status: int, body: bytes) -> bool:
    try:
        return status == 200 and json.loads(body).get("port") == 3000
    except ValueError:
        return False


def basemap_hidden() -> dict[str, Any]:
    """No listing, no dotfiles (staging), no traversal; /basemap/* never reaches keelson-web."""
    got = {p: raw("keelson.localhost", p) for p in BASEMAP_NOT_SERVED}
    leaked = {p: s for p, (s, _, _) in got.items() if s != 404}
    traversal = {p: raw("keelson.localhost", p) for p in BASEMAP_TRAVERSAL}
    escaped = {p: s for p, (s, _, b) in traversal.items() if not from_web_stub(s, b)}
    return {"ok": not leaked and not escaped, "not_404": leaked, "traversal_not_to_web": escaped, "checked": len(got) + len(traversal)}


def basemap_not_installed() -> dict[str, Any]:
    """Empty volume: the manifest answers 204 (map falls back, no console 404), files 404."""
    m_status, m_h, m_body = raw("keelson.localhost", "/basemap/manifest.json")
    a_status = raw("keelson.localhost", BASEMAP_ARCHIVE)[0]
    return {
        "ok": m_status == 204 and m_body == b"" and a_status == 404 and m_h.get("cache-control") == "no-cache",
        "manifest": m_status,
        "archive": a_status,
    }


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
        check("basemap_not_installed", basemap_not_installed)
        return failures
    if MODE == "basemap-dotted-root":
        # Native mode serves BASEMAP_ROOT=<repo>/.native/data/basemap: a dot directory above the
        # root must not hide the base map (only dotfiles inside it are hidden).
        check("basemap_dotted_root_byte_ranges", basemap_range)
        check("basemap_dotted_root_files_headers", basemap_files)
        check("basemap_dotted_root_hidden_and_no_traversal", basemap_hidden)
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
    check("basemap_archive_byte_ranges", basemap_range)
    check("basemap_files_headers", basemap_files)
    check("basemap_hidden_and_no_traversal", basemap_hidden)
    check("sse_unbuffered_fernhill_mcp", streaming("fernhill.localhost", "/mcp/stream"))
    check("log_probe_sent", log_probe)
    return failures


if __name__ == "__main__":
    sys.exit(main())
