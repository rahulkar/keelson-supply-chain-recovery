"""SSE endpoint over a real HTTP server (uvicorn): replay with Last-Event-ID / ?after,
live tail, keep-alive, auth/tenant/cursor errors, disconnect never cancels the run."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn

from e1_browser import Browser
from e1_env import TENANT_A
from keelson_contracts.agui import ScMetadata, Stage, step_started
from keelson_engine.core import runs
from keelson_engine.core.events import get_event_bus

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest_asyncio.fixture(loop_scope="session")
async def server(platform: None, api: httpx.AsyncClient) -> AsyncIterator[str]:
    from keelson_engine.api.main import create_app

    port = _free_port()
    config = uvicorn.Config(create_app(manage_resources=False), host="127.0.0.1", port=port, log_level="warning",
                            lifespan="off")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        await asyncio.wait_for(task, 10)


def _step(case_id: str, label: str) -> dict[str, Any]:
    sc = ScMetadata(tenant_id=TENANT_A, case_id=case_id, stage=Stage.CHECKING_SYSTEMS, prov="fact", label=label)
    return step_started(sc)


class Frames:
    """Incremental SSE parser over an httpx streaming response."""

    def __init__(self, resp: httpx.Response) -> None:
        self._lines = resp.aiter_lines()

    async def next(self, timeout: float = 5.0) -> dict[str, Any]:
        async def read() -> dict[str, Any]:
            frame: dict[str, Any] = {"comments": []}
            async for line in self._lines:
                if line == "":
                    if set(frame) - {"comments"} or frame["comments"]:
                        return frame
                    continue
                if line.startswith(":"):
                    frame["comments"].append(line[1:].strip())
                    continue
                key, _, value = line.partition(":")
                frame[key] = value[1:] if value.startswith(" ") else value
            raise EOFError

        return await asyncio.wait_for(read(), timeout)

    async def next_event(self, timeout: float = 5.0) -> dict[str, Any]:
        while True:
            frame = await self.next(timeout)
            if "data" in frame:
                return frame


async def test_replay_with_last_event_id_then_live(server: str, browser_factory: Any) -> None:
    marco: Browser = await browser_factory("marco")
    case_id = (await marco.post("/api/cases", json={"title": "sse"})).json()["id"]
    bus = get_event_bus()
    seqs = [await bus.append(tenant=TENANT_A, case_id=case_id, run_id="r1", event=_step(case_id, f"e{i}"))
            for i in range(3)]
    headers = {"Cookie": f"keelson_session={marco.cookie}", "Last-Event-ID": str(seqs[0])}
    async with httpx.AsyncClient(base_url=server, timeout=10) as http:
        async with http.stream("GET", f"/api/cases/{case_id}/events", headers=headers) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["cache-control"] == "no-cache"
            frames = Frames(resp)
            first = await frames.next()
            assert first.get("retry") == "3000"
            got = [await frames.next_event(), await frames.next_event()]
            assert [int(f["id"]) for f in got] == seqs[1:]
            assert all(f["event"] == "message" for f in got)
            payload = json.loads(got[0]["data"])
            assert payload["type"] == "STEP_STARTED" and payload["metadata"]["sc"]["seq"] == seqs[1]
            assert payload["metadata"]["sc"]["label"] == "e1"
            live_seq = await bus.append(tenant=TENANT_A, case_id=case_id, run_id="r1", event=_step(case_id, "live"))
            live = await frames.next_event()
            assert int(live["id"]) == live_seq and json.loads(live["data"])["metadata"]["sc"]["label"] == "live"


async def test_after_query_and_full_replay(server: str, browser_factory: Any) -> None:
    marco: Browser = await browser_factory("marco")
    case_id = (await marco.post("/api/cases", json={})).json()["id"]
    bus = get_event_bus()
    seqs = [await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=_step(case_id, f"e{i}"))
            for i in range(2)]
    async with httpx.AsyncClient(base_url=server, timeout=10) as http:
        async with http.stream("GET", f"/api/cases/{case_id}/events", params={"after": seqs[0]},
                               headers={"Cookie": f"keelson_session={marco.cookie}"}) as resp:
            frames = Frames(resp)
            assert int((await frames.next_event())["id"]) == seqs[1]
        async with http.stream("GET", f"/api/cases/{case_id}/events",
                               headers={"Cookie": f"keelson_session={marco.cookie}"}) as resp:
            frames = Frames(resp)
            assert [int((await frames.next_event())["id"]) for _ in range(2)] == seqs


async def test_keepalive_comment(server: str, browser_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import fastapi.routing

    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.2)
    marco: Browser = await browser_factory("marco")
    case_id = (await marco.post("/api/cases", json={})).json()["id"]
    async with httpx.AsyncClient(base_url=server, timeout=10) as http:
        async with http.stream("GET", f"/api/cases/{case_id}/events",
                               headers={"Cookie": f"keelson_session={marco.cookie}"}) as resp:
            frames = Frames(resp)
            await frames.next()  # retry hint
            ping = await frames.next(timeout=3)
            assert ping["comments"] == ["ping"]


async def test_stream_errors_are_http_errors(server: str, browser_factory: Any) -> None:
    marco: Browser = await browser_factory("marco")
    nora: Browser = await browser_factory("nora")
    case_id = (await marco.post("/api/cases", json={})).json()["id"]
    async with httpx.AsyncClient(base_url=server, timeout=10) as http:
        assert (await http.get(f"/api/cases/{case_id}/events")).status_code == 401
        resp = await http.get(f"/api/cases/{case_id}/events", headers={"Cookie": f"keelson_session={nora.cookie}"})
        assert resp.status_code == 404
        resp = await http.get(f"/api/cases/{case_id}/events",
                              headers={"Cookie": f"keelson_session={marco.cookie}", "Last-Event-ID": "-5"})
        assert resp.status_code == 400 and resp.json()["code"] == "invalid_cursor"
        resp = await http.get(f"/api/cases/{case_id}/events", params={"after": "99999999999999999999"},
                              headers={"Cookie": f"keelson_session={marco.cookie}"})
        assert resp.status_code == 400


async def test_disconnect_does_not_cancel_run(server: str, browser_factory: Any) -> None:
    priya: Browser = await browser_factory("priya")
    case_id = (await priya.post("/api/cases", json={})).json()["id"]
    run_id = (await priya.post(f"/api/cases/{case_id}/messages", json={"text": "hello"},
                               headers={"Idempotency-Key": "sse-1"})).json()["run_id"]
    async with httpx.AsyncClient(base_url=server, timeout=10) as http:
        async with http.stream("GET", f"/api/cases/{case_id}/events",
                               headers={"Cookie": f"keelson_session={priya.cookie}"}) as resp:
            await Frames(resp).next()
    await asyncio.sleep(0.2)
    assert await runs.is_cancel_requested(run_id) is False
    run = await runs.get_run(TENANT_A, run_id)
    assert run is not None and run.status == "queued" and not run.cancel_requested


async def test_stream_ends_after_logout(server: str, browser_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: an open stream stops delivering tenant events once its session is gone."""
    from keelson_engine.api import routes_cases

    monkeypatch.setattr(routes_cases, "SESSION_RECHECK_S", 0.0)
    marco: Browser = await browser_factory("marco")
    case_id = (await marco.post("/api/cases", json={})).json()["id"]
    bus = get_event_bus()
    async with httpx.AsyncClient(base_url=server, timeout=10) as http:
        async with http.stream("GET", f"/api/cases/{case_id}/events",
                               headers={"Cookie": f"keelson_session={marco.cookie}"}) as resp:
            frames = Frames(resp)
            await frames.next()  # retry hint
            await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=_step(case_id, "before"))
            assert json.loads((await frames.next_event())["data"])["metadata"]["sc"]["label"] == "before"
            assert (await marco.post("/api/auth/logout")).status_code == 200
            await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=_step(case_id, "after"))
            with pytest.raises(EOFError):
                await frames.next_event(timeout=5)


async def test_open_streams_end_promptly_on_shutdown(platform: None, api: httpx.AsyncClient,
                                                     browser_factory: Any) -> None:
    """Regression: uvicorn waits for open connections *before* the lifespan shutdown, so an SSE
    stream that only stopped on lifespan shutdown held the process until
    --timeout-graceful-shutdown cancelled it. The SIGTERM hook ends streams at once."""
    import time

    from keelson_engine.api import shutdown
    from keelson_engine.api.main import create_app

    marco: Browser = await browser_factory("marco")
    case_id = (await marco.post("/api/cases", json={})).json()["id"]
    port = _free_port()
    config = uvicorn.Config(create_app(manage_resources=False), host="127.0.0.1", port=port, log_level="warning",
                            lifespan="on", timeout_graceful_shutdown=8)
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    try:
        for _ in range(100):
            if srv.started:
                break
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            async with http.stream("GET", f"/api/cases/{case_id}/events",
                                   headers={"Cookie": f"keelson_session={marco.cookie}"}) as resp:
                frames = Frames(resp)
                await frames.next()  # retry hint: the stream is open and idle (tailing)
                started = time.monotonic()
                shutdown.notify_shutdown()  # what the chained SIGTERM handler does
                srv.should_exit = True  # ... before uvicorn's own handler
                with pytest.raises(EOFError):
                    await frames.next(timeout=3)
        await asyncio.wait_for(task, 5)
        assert time.monotonic() - started < 3, "server waited for the open stream"
    finally:
        srv.should_exit = True
        if not task.done():
            task.cancel()
        shutdown.reset_shutdown()


async def test_signal_hook_chains_to_the_previous_handler() -> None:
    import signal

    from keelson_engine.api import shutdown

    shutdown.reset_shutdown()
    seen: list[int] = []
    original = signal.signal(signal.SIGUSR1, lambda signum, frame: seen.append(signum))
    try:
        uninstall = shutdown.install_signal_hook(signals=(signal.SIGUSR1,))
        assert not shutdown.is_shutting_down()
        signal.raise_signal(signal.SIGUSR1)
        for _ in range(20):
            if shutdown.is_shutting_down():
                break
            await asyncio.sleep(0.01)
        assert shutdown.is_shutting_down() and seen == [signal.SIGUSR1]
        uninstall()
        signal.raise_signal(signal.SIGUSR1)
        assert seen == [signal.SIGUSR1, signal.SIGUSR1]  # previous handler restored
    finally:
        signal.signal(signal.SIGUSR1, original)
        shutdown.reset_shutdown()


async def test_until_shutdown_stops_a_blocked_iterator() -> None:
    from keelson_engine.api import shutdown

    shutdown.reset_shutdown()
    closed: list[bool] = []

    async def source() -> Any:
        try:
            yield 1
            await asyncio.sleep(3600)  # an idle tail
            yield 2
        finally:
            closed.append(True)

    got: list[int] = []

    async def consume() -> None:
        async for item in shutdown.until_shutdown(source()):
            got.append(item)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    shutdown.notify_shutdown()
    await asyncio.wait_for(task, 2)
    assert got == [1] and closed == [True]
    shutdown.reset_shutdown()
