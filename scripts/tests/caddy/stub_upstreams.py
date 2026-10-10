#!/usr/bin/env python3
"""Stub upstreams for the Caddy routing test (stdlib only).

Listens on every port the Caddyfile proxies to and answers each request with JSON
``{"port", "method", "path", "headers"}`` so the test can assert which upstream got the
request and with which headers. Special paths:

* ``/api/internal/forward-auth`` (port 8000) - forward_auth decision: 200 + ``X-Auth-User``
  (+ ``X-Auth-Roles`` unless the cookie says ``noroles``) when the Cookie carries
  ``keelson_session=admin...``; 401 otherwise; 400 when the query is not
  ``role=platform-admin`` or the X-Forwarded-Method/-Uri headers are missing.
* ``*/stream`` (text/event-stream) and ``*/ndjson`` (application/x-ndjson) - send a first
  chunk, wait 2 s, then the rest (checks unbuffered proxying).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PORTS = (8000, 3000, 8100, 3001, 8080, 4000, 6006, 8400)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep test output clean
        pass

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", "stub")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        port = self.server.server_address[1]
        url = urlsplit(self.path)
        if port == 8000 and url.path == "/api/internal/forward-auth":
            self._forward_auth(url.query)
            return
        if url.path.endswith("/stream"):
            self._stream("text/event-stream")
            return
        if url.path.endswith("/ndjson"):
            self._stream("application/x-ndjson")
            return
        body = json.dumps(
            {"port": port, "method": self.command, "path": self.path, "headers": dict(self.headers.items())}
        ).encode()
        self._send(200, body)

    def _forward_auth(self, query: str) -> None:
        if (
            query != "role=platform-admin"
            or not self.headers.get("X-Forwarded-Method")
            or not self.headers.get("X-Forwarded-Uri")
        ):
            self._send(400, b'{"error":"bad forward-auth request"}')
            return
        cookie = self.headers.get("Cookie", "")
        if "keelson_session=admin" not in cookie:
            self._send(401, b'{"error":"no platform-admin session"}')
            return
        extra = {"X-Auth-User": "ava"}
        if "noroles" not in cookie:
            extra["X-Auth-Roles"] = "platform-admin"
        self._send(200, b"{}", extra)

    def _stream(self, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i, delay in enumerate((0.0, 2.0)):
            time.sleep(delay)
            data = f"data: {i}\n\n".encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main() -> None:
    servers = [ThreadingHTTPServer(("0.0.0.0", p), Handler) for p in PORTS]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    print(json.dumps({"event": "stub_ready", "ports": PORTS}), flush=True)
    threads[0].join()


if __name__ == "__main__":
    main()
