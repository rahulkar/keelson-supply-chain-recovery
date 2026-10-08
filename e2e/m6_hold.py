"""Hold the engine worker without stopping it, so a queued run never reaches a model (M6 e2e).

Why: ``POST /api/alerts/{id}/open-case`` creates a case *and* queues its ``signal`` run in one
transaction and wakes the worker, so the M5 trick (a seeded ``running`` gate run on the case) cannot
be put in front of it. The worker runs one run at a time (``concurrency: 1``), so instead this
occupies the worker's only slot *before* the case exists:

1. take an ``ACCESS EXCLUSIVE`` lock on the ADK ``sessions`` table (database ``adk``; only the worker
   reads it, at the very start of a run, before any workflow node or model call);
2. queue a deterministic *gate* run (an ``action`` rollback of an unknown change set, like the M5
   gates) on a throwaway case and wake the worker: it claims the gate and blocks in
   ``get_session`` holding its slot;
3. now every newly queued run stays ``queued`` (the claim also skips ``cancel_requested`` runs);
   the caller cancels them (``POST /api/runs/{id}/cancel``) while they wait;
4. release: cancel any run still queued since the hold began, request cancel of the gate, wait
   for the worker's cancel watch (2 s poll) to abort it, then drop the lock. The gate then ends
   ``cancelled`` (or, at worst, fails as a rollback of an unknown change set: no model either).

If this process dies, the lock goes with its connection: :func:`cancel_queued` is therefore called
right after each run the caller creates, not only at release.

CLI (Playwright): ``python /e2e/m6_hold.py --max-s 900`` holds until SIGTERM / SIGINT (``docker
stop``) or the deadline, printing ``HOLDING gate=<run> since=<iso>`` once the worker is held and
``RELEASED cancelled=<n>`` at the end. Runs inside the engine image (``docker compose run engine-api``).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import signal
import time
import uuid
from typing import Any

TENANT = "demo-retail"
GATE_OWNER_PREFIX = "e2e-m6-hold"
CANCEL_POLL_GRACE_S = 6.0  # the worker's cancel watch polls every 2 s


class HoldError(RuntimeError):
    pass


async def _sql(query: str, params: dict[str, Any] | None = None, *, fetch: bool = True) -> list[dict[str, Any]]:
    from keelson_engine.core.db import tenant_session
    from sqlalchemy import text

    async with tenant_session(TENANT) as s:
        res = await s.execute(text(query), params or {})
        return [dict(m) for m in res.mappings().all()] if fetch else []


def _adk_dsn() -> str:
    url = os.environ.get("ADK_DATABASE_URL", "")
    if not url:
        raise HoldError("ADK_DATABASE_URL is not set (run inside the engine image)")
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


class WorkerHold:
    """``async with WorkerHold(creator_sub, actor) as hold:`` — see the module docstring."""

    def __init__(self, created_by: str, actor: dict[str, Any], *, claim_timeout_s: float = 90.0) -> None:
        self.created_by = created_by
        self.actor = actor
        self.claim_timeout_s = claim_timeout_s
        self.gate_run: str | None = None
        self.gate_case: str | None = None
        self.since: dt.datetime | None = None
        self._conn: Any = None
        self._tx: Any = None

    async def __aenter__(self) -> WorkerHold:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()

    async def start(self) -> None:
        import asyncpg

        from keelson_engine.core.runs import wake_workers

        busy = await _sql("SELECT id, kind FROM run WHERE status = 'running'")
        if busy:
            raise HoldError(f"the worker is busy ({busy[:3]}); hold needs an idle worker")
        self._conn = await asyncpg.connect(_adk_dsn())
        self._tx = self._conn.transaction()
        await self._tx.start()
        await self._conn.execute("SET LOCAL lock_timeout = '15s'")
        await self._conn.execute("LOCK TABLE sessions IN ACCESS EXCLUSIVE MODE")
        rows = await _sql("SELECT clock_timestamp() AS now")
        self.since = rows[0]["now"]
        case_id = str(
            (
                await _sql(
                    'INSERT INTO "case" (tenant, title, mode, created_by, created_by_name) '
                    "VALUES (:t, 'M6 e2e: worker hold (gate)', 'actual', :by, 'e2e') RETURNING id",
                    {"t": TENANT, "by": self.created_by},
                )
            )[0]["id"]
        )
        rid = f"{GATE_OWNER_PREFIX}-{uuid.uuid4().hex[:12]}"
        await _sql(
            """
            INSERT INTO run (id, tenant, case_id, kind, origin_kind, status, payload, actor, session_id, created_by)
            VALUES (:id, :t, :c, 'action', 'action', 'queued', CAST(:p AS jsonb), CAST(:a AS jsonb), :id, :by)
            """,
            {
                "id": rid,
                "t": TENANT,
                "c": case_id,
                "p": json.dumps({"type": "rollback", "change_set_id": "e2e-gate"}),
                "a": json.dumps(self.actor),
                "by": self.created_by,
            },
            fetch=False,
        )
        self.gate_run, self.gate_case = rid, case_id
        await wake_workers(rid, TENANT)
        deadline = time.monotonic() + self.claim_timeout_s
        while True:
            row = (await _sql("SELECT status, lease_owner FROM run WHERE id = :i", {"i": rid}))[0]
            if row["status"] == "running" and row["lease_owner"]:
                return
            if row["status"] != "queued" or time.monotonic() > deadline:
                await self.release()
                raise HoldError(f"the worker did not claim the gate run ({row})")
            await asyncio.sleep(0.5)

    async def cancel_queued(self) -> list[str]:
        """Cancel every run queued since the hold began (never the gate). Returns their ids."""
        if self.since is None:
            return []
        rows = await _sql(
            """
            UPDATE run SET status = 'cancelled', cancel_requested = true, finished_at = now(),
                   error = 'cancelled by e2e (worker hold)', updated_at = now()
             WHERE status = 'queued' AND created_at >= :since AND id <> :gate
            RETURNING id
            """,
            {"since": self.since, "gate": self.gate_run or ""},
        )
        return [str(r["id"]) for r in rows]

    async def queued_runs(self) -> list[dict[str, Any]]:
        if self.since is None:
            return []
        return await _sql(
            "SELECT id, case_id, kind, status, payload, actor FROM run WHERE created_at >= :since AND id <> :gate "
            "ORDER BY created_at",
            {"since": self.since, "gate": self.gate_run or ""},
        )

    async def release(self) -> int:
        cancelled = 0
        try:
            cancelled = len(await self.cancel_queued())
            if self.gate_run:
                await _sql(
                    "UPDATE run SET cancel_requested = true, updated_at = now() WHERE id = :i",
                    {"i": self.gate_run},
                    fetch=False,
                )
                await asyncio.sleep(CANCEL_POLL_GRACE_S)  # the worker's cancel watch aborts the gate
        finally:
            if self._tx is not None:
                try:
                    await self._tx.rollback()
                finally:
                    await self._conn.close()
                self._tx = self._conn = None
        if self.gate_run:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                row = (await _sql("SELECT status FROM run WHERE id = :i", {"i": self.gate_run}))[0]
                if row["status"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.5)
            # a gate the worker never finished (it should) is cancelled here, never left to the reaper
            await _sql(
                "UPDATE run SET status = 'cancelled', finished_at = now(), lease_owner = NULL, "
                "error = 'e2e gate', updated_at = now() WHERE id = :i AND status IN ('queued', 'running')",
                {"i": self.gate_run},
                fetch=False,
            )
            self.gate_run = None
        return cancelled


async def _cli(max_s: float) -> int:
    from keelson_engine.core.db import init_db

    await init_db()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    hold = WorkerHold("e2e-m6-hold", {"sub": "e2e-m6-hold", "username": "e2e", "name": "e2e", "roles": []})
    await hold.start()
    print(f"HOLDING gate={hold.gate_run} since={hold.since.isoformat() if hold.since else ''}", flush=True)
    try:
        await asyncio.wait_for(stop.wait(), timeout=max_s)
    except TimeoutError:
        print("deadline reached", flush=True)
    finally:
        n = await hold.release()
        print(f"RELEASED cancelled={n}", flush=True)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-s", type=float, default=900.0)
    raise SystemExit(asyncio.run(_cli(ap.parse_args().max_s)))
