"""(M8) The in-app runner loop (``runner_service.py``) against the scripted fake stack of
``test_runner.py`` and a fake engine job API; the engine client's retry rules over a mock transport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import runner_service as rs
from keelson_evals.cases import load_cases
from keelson_evals.engine_client import EngineClient, EngineError
from keelson_evals.suite import suite_hash, suite_info
from test_runner import ANALYSIS_BLOCKS, FakeRun, FakeStack, fake_ts

EVALS = Path(__file__).resolve().parents[1]
RUN = "0199c1a2-7b3c-7d4e-8f50-0123456789ab"

CASES = {
    "alpha": "id: alpha\ntitle: Alpha\ntags: [golden]\nturns:\n  - id: a1\n    message: first\n  - id: a2\n    message: second\n",
    "beta": "id: beta\ntitle: Beta\nturns:\n  - id: b1\n    message: hello\n",
    "kbcase": ("id: kbcase\ntitle: KB\ntags: [adversarial]\nsetup:\n  kb_upload:\n    kb_name: Temp\n"
               "    files: [data/kb/adversarial/carrier-rate-update-injection.md]\nturns:\n  - id: k1\n    message: hi\n"),
}


class FakeEngine:
    """The engine's internal runner API: records every call into the stack's call log (one global
    order), answers flags, can fail result PUTs or refuse events."""

    def __init__(self, stack: FakeStack, job: dict[str, Any] | None = None) -> None:
        self.log = stack.calls
        self.job = job
        self.flags = {"cancel_requested": False, "budget_exhausted": False}
        self.triggers: dict[tuple[str, str | None], dict[str, bool]] = {}
        self.put_failures = 0
        self.refuse_events = False
        self.results: dict[str, dict[str, Any]] = {}
        self.finished: tuple[str, str | None] | None = None

    def heartbeat(self, runner_id: str, version: str, suite: dict[str, Any]) -> dict[str, Any]:
        self.log.append(("ENGINE", "heartbeat", suite["suite_hash"]))
        return {"poll_after_s": 5}

    def claim(self, runner_id: str) -> dict[str, Any] | None:
        job, self.job = self.job, None
        return job

    def events(self, run_id: str, runner_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        if self.refuse_events:
            raise EngineError(409, "run_not_running", "the run is not running")
        for e in events:
            self.log.append(("ENGINE", f"event:{e['type']}", e.get("turn_id") or e.get("case_id")))
            self.flags.update(self.triggers.get((e["type"], e.get("turn_id") or e.get("case_id")), {}))
        return dict(self.flags)

    def attach(self, run_id: str, runner_id: str, eval_case_id: str, keelson_case_id: str) -> None:
        self.log.append(("ENGINE", "attach", f"{eval_case_id}->{keelson_case_id}"))

    def put_result(self, run_id: str, runner_id: str, eval_case_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        if self.put_failures:
            self.put_failures -= 1
            raise EngineError(503, "unavailable", "try later")
        self.results[eval_case_id] = doc
        self.log.append(("ENGINE", "result", f"{eval_case_id}:{doc['status']}"))
        return {"stored": True}

    def finish(self, run_id: str, runner_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        self.finished = (status, error)
        self.log.append(("ENGINE", "finish", status))
        return {"status": status}


def _cases_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cases"
    d.mkdir()
    for name, body in CASES.items():
        (d / f"{name}.yaml").write_text(body, encoding="utf-8")
    return d


def _job(mode: str, case_ids: list[str]) -> dict[str, Any]:
    return {"run_id": RUN, "mode": mode, "case_ids": case_ids, "budget": None, "lease_expires_at": "2026-10-08T10:00:00Z"}


def _runner(stack: FakeStack, engine: FakeEngine, tmp_path: Path) -> rs.Runner:
    return rs.Runner(engine, fake_ts(stack), cases_dir=_cases_dir(tmp_path), repo_root=EVALS.parent,  # type: ignore[arg-type]
                     runner_id="runner-1")


def _success(stack: FakeStack, *run_ids: str) -> None:
    for rid in run_ids:
        stack.script[rid] = FakeRun(rid, {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
                                    blocks=dict(ANALYSIS_BLOCKS))


def _engine_log(stack: FakeStack) -> list[str]:
    out = []
    for c in stack.calls:
        if c[0] == "ENGINE":
            out.append(f"{c[1]}:{c[2]}")
        elif c[0] == "POST" and str(c[1]).endswith("/messages"):
            out.append("MESSAGE")
        elif c[0] == "POST" and str(c[1]).endswith("/api/cases"):
            out.append("CREATE")
    return out


def test_suite_catalogue_and_hash_are_stable() -> None:
    cases = load_cases(EVALS / "cases")
    info = suite_info(cases)
    assert info == suite_info(list(reversed(cases))) and info["suite_hash"] == suite_hash(cases)
    by_id = {c["id"]: c for c in info["cases"]}
    assert len(by_id) == len(cases) >= 7
    assert by_id["adversarial_kb_doc"]["needs_live_embeddings"] is True
    assert by_id["adversarial_supplier_message"]["injects"] is True and by_id["signal_origin_never_auto"]["injects"]
    assert by_id["suez_whatif"]["turn_kinds"] == ["message", "action"]
    changed = [c.model_copy(update={"title": c.title + "!"}) if c.id == "suez_whatif" else c for c in cases]
    assert suite_hash(changed) != info["suite_hash"]


def test_claim_attach_before_first_message_report_and_finish(tmp_path: Path) -> None:
    stack = FakeStack()
    _success(stack, "run-1", "run-2")
    engine = FakeEngine(stack, _job("replay", ["alpha"]))
    runner = _runner(stack, engine, tmp_path)
    assert runner.run_once() is True
    log = _engine_log(stack)
    assert log[0].startswith("heartbeat:") and log[-1] == "finish:succeeded"
    assert log.index("attach:alpha->case-1") == log.index("CREATE") + 1 < log.index("MESSAGE")
    assert [x for x in log if x.startswith("event:")] == [
        "event:run_started:None", "event:case_started:alpha", "event:turn_started:a1", "event:turn_finished:a1",
        "event:turn_started:a2", "event:turn_finished:a2", "event:case_finished:alpha", "event:run_finished:None"]
    assert engine.results["alpha"]["status"] == "pass" and engine.results["alpha"]["turns"][0]["timings_ms"]
    assert runner.run_once() is False  # nothing queued any more


def test_replay_mode_skips_cases_that_need_live_embeddings(tmp_path: Path) -> None:
    stack = FakeStack()
    engine = FakeEngine(stack, _job("replay", ["kbcase"]))
    _runner(stack, engine, tmp_path).run_once()
    doc = engine.results["kbcase"]
    assert doc["status"] == "skipped" and doc["error_code"] == "needs_live_embeddings"
    assert "CREATE" not in _engine_log(stack) and not stack.kb_docs  # nothing uploaded, no case
    assert engine.finished == ("succeeded", None)


def test_live_mode_runs_the_kb_case(tmp_path: Path) -> None:
    stack = FakeStack()
    _success(stack, "run-1")
    engine = FakeEngine(stack, _job("record_or_replay", ["kbcase"]))
    _runner(stack, engine, tmp_path).run_once()
    assert engine.results["kbcase"]["status"] in ("pass", "fail")
    assert "attach:kbcase->case-1" in _engine_log(stack) and stack.kb_docs == {}  # uploaded, then cleaned up


def test_cancel_between_turns_stops_and_still_tears_down(tmp_path: Path) -> None:
    stack = FakeStack()
    _success(stack, "run-1", "run-2", "run-3")
    engine = FakeEngine(stack, _job("replay", ["alpha", "beta"]))
    engine.triggers[("turn_finished", "a1")] = {"cancel_requested": True}
    _runner(stack, engine, tmp_path).run_once()
    assert engine.results["alpha"]["status"] == "cancelled" and len(engine.results["alpha"]["turns"]) == 1
    assert engine.results["beta"]["status"] == "cancelled" and engine.results["beta"]["turns"] == []
    assert _engine_log(stack).count("MESSAGE") == 1
    assert [c[0] for c in stack.calls].count("RESET") == 1  # alpha's setup reset; beta never started
    assert engine.finished == ("cancelled", None)


def test_budget_exhausted_skips_the_remaining_cases(tmp_path: Path) -> None:
    stack = FakeStack()
    _success(stack, "run-1", "run-2", "run-3")
    engine = FakeEngine(stack, _job("record_or_replay", ["beta", "alpha"]))
    engine.triggers[("case_finished", "beta")] = {"budget_exhausted": True}
    _runner(stack, engine, tmp_path).run_once()
    assert engine.results["beta"]["status"] == "pass"
    assert engine.results["alpha"]["status"] == "skipped" and engine.results["alpha"]["error_code"] == "budget_exhausted"
    assert engine.finished == ("succeeded", None)


def test_a_result_the_engine_could_not_store_is_retried_before_finishing(tmp_path: Path) -> None:
    stack = FakeStack()
    _success(stack, "run-1")
    engine = FakeEngine(stack, _job("replay", ["beta"]))
    engine.put_failures = 1
    _runner(stack, engine, tmp_path).run_once()
    assert engine.results["beta"]["status"] == "pass" and engine.finished == ("succeeded", None)
    log = _engine_log(stack)
    assert log.index("result:beta:pass") < log.index("finish:succeeded")


def test_a_lost_lease_aborts_without_finishing(tmp_path: Path) -> None:
    stack = FakeStack()
    engine = FakeEngine(stack, _job("replay", ["beta"]))
    engine.refuse_events = True
    assert _runner(stack, engine, tmp_path).execute(_job("replay", ["beta"])) == ""
    assert engine.finished is None and "CREATE" not in _engine_log(stack)


def test_unknown_cases_fail_the_run(tmp_path: Path) -> None:
    stack = FakeStack()
    engine = FakeEngine(stack)
    assert _runner(stack, engine, tmp_path).execute(_job("replay", ["nope"])) == "failed"
    assert engine.finished is not None and engine.finished[0] == "failed" and "nope" in str(engine.finished[1])


def test_flags_are_sticky_and_ordered() -> None:
    flags = rs.Flags()
    assert flags.stop_reason() is None
    flags.update({"budget_exhausted": True})
    flags.update({"cancel_requested": False, "budget_exhausted": False})
    assert flags.stop_reason() == "budget_exhausted"
    flags.update({"cancel_requested": True})
    assert flags.stop_reason() == "cancelled"


# ───────────────────────────── engine client ─────────────────────────────


def _client(handler: Any, sleeps: list[float]) -> EngineClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return EngineClient("http://keelson.test", "tok", client=http, sleep=sleeps.append)


def test_engine_client_sends_bearer_and_origin_and_retries_5xx() -> None:
    seen: list[httpx.Request] = []
    answers = iter([httpx.Response(503, json={"code": "busy"}), httpx.Response(200, json={"stored": True})])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(answers)

    sleeps: list[float] = []
    client = _client(handler, sleeps)
    assert client.put_result(RUN, "r", "alpha", {"id": "alpha"}) == {"stored": True}
    assert sleeps == [1.0] and len(seen) == 2
    req = seen[0]
    assert req.headers["authorization"] == "Bearer tok" and req.headers["origin"] == "http://keelson.test"
    assert req.url.path == f"/api/internal/evals/runner/runs/{RUN}/cases/alpha/result" and req.method == "PUT"


def test_engine_client_does_not_retry_4xx_and_reads_204() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/claim"):
            return httpx.Response(204)
        return httpx.Response(409, json={"code": "run_not_running", "message": "no"})

    sleeps: list[float] = []
    client = _client(handler, sleeps)
    assert client.claim("r") is None
    with pytest.raises(EngineError) as err:
        client.events(RUN, "r", [])
    assert err.value.code == "run_not_running" and not err.value.retryable and sleeps == []


def test_engine_client_gives_up_after_the_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    sleeps: list[float] = []
    with pytest.raises(EngineError) as err:
        _client(handler, sleeps).finish(RUN, "r", "succeeded")
    assert err.value.status == 0 and err.value.retryable and sleeps == [1.0, 2.0, 4.0, 8.0]
    with pytest.raises(ValueError):
        EngineClient("http://keelson.test", "")
