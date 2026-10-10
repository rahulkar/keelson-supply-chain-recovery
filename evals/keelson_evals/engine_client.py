"""(M8) Client of the engine's internal eval-runner API (``/api/internal/evals/runner``,
``docs/contracts/128-m8-evals-ui.md`` §4).

Bearer ``EVALS_RUNNER_TOKEN`` on every call, plus ``Origin: <KEELSON_URL>`` (the engine checks the
Origin of every non-GET ``/api/*`` call). The base URL comes from the environment only: a job never
names a URL. Transport errors, 5xx and 429 are retried with back-off; any other non-2xx answer is an
:class:`EngineError` carrying the engine's ``code`` (``run_not_running`` = the lease was lost or
the run was finished elsewhere: stop working on it).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

__all__ = ["EngineClient", "EngineError"]

RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0)
PREFIX = "/api/internal/evals/runner"


class EngineError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"HTTP {status} {code}: {message}"[:500])
        self.status = status
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status == 429 or self.status >= 500


class EngineClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        retry_delays: Sequence[float] = RETRY_DELAYS_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("EVALS_RUNNER_TOKEN is empty")
        self.base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Origin": self.base}
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False)
        self._delays = tuple(retry_delays)
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    # ── transport ──
    def _once(self, method: str, path: str, body: Any) -> httpx.Response:
        try:
            return self._client.request(method, f"{self.base}{PREFIX}{path}", json=body, headers=self._headers)
        except httpx.TransportError as exc:
            raise EngineError(0, "transport_error", type(exc).__name__) from None

    def _call(self, method: str, path: str, body: Any = None, *, retry: bool = True) -> httpx.Response:
        attempts = (0.0, *self._delays) if retry else (0.0,)
        last: EngineError | None = None
        for delay in attempts:
            if delay:
                self._sleep(delay)
            try:
                resp = self._once(method, path, body)
            except EngineError as exc:
                last = exc
                continue
            if resp.status_code < 300:
                return resp
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            code = str(payload.get("code") or "error") if isinstance(payload, Mapping) else "error"
            message = str(payload.get("message") or "") if isinstance(payload, Mapping) else ""
            last = EngineError(resp.status_code, code, message)
            if not last.retryable:
                raise last
        assert last is not None
        raise last

    # ── protocol ──
    def heartbeat(self, runner_id: str, version: str, suite: Mapping[str, Any]) -> dict[str, Any]:
        r = self._call("POST", "/heartbeat", {"runner_id": runner_id, "version": version, "suite": suite})
        return dict(r.json())

    def claim(self, runner_id: str) -> dict[str, Any] | None:
        """The next queued run (``EvalRunnerJob``) or ``None`` (204)."""
        r = self._call("POST", "/claim", {"runner_id": runner_id})
        if r.status_code == 204:
            return None
        return dict(r.json()["job"])

    def events(self, run_id: str, runner_id: str, events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Report progress (an empty list only renews the lease); answers the run's flags
        ``{cancel_requested, budget_exhausted, lease_expires_at}``."""
        r = self._call("POST", f"/runs/{run_id}/events", {"runner_id": runner_id, "events": list(events)})
        return dict(r.json())

    def attach(self, run_id: str, runner_id: str, eval_case_id: str, keelson_case_id: str) -> None:
        """Link the Keelson case to the run (before its first message: the engine applies the run's
        LLM mode and budget to every run of a linked case)."""
        self._call("POST", f"/runs/{run_id}/cases/{eval_case_id}/attach",
                   {"runner_id": runner_id, "keelson_case_id": keelson_case_id})

    def put_result(self, run_id: str, runner_id: str, eval_case_id: str, case_doc: Mapping[str, Any]) -> dict[str, Any]:
        """The case's ``keelson-evals/2`` result (idempotent: a retry replaces it)."""
        r = self._call("PUT", f"/runs/{run_id}/cases/{eval_case_id}/result",
                       {"runner_id": runner_id, "result": case_doc})
        return dict(r.json())

    def finish(self, run_id: str, runner_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        r = self._call("POST", f"/runs/{run_id}/finish", {"runner_id": runner_id, "status": status, "error": error})
        return dict(r.json())
