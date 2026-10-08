#!/usr/bin/env python3
"""(M8) The in-app eval runner: compose service ``evals-runner`` (profile ``evals``), see
``docs/contracts/128-m8-evals-ui.md`` §4.

Platform admins start runs in ``/admin/evals``; the engine queues them. This loop:

1. **heartbeat** — posts its case catalogue (``keelson_evals.suite.suite_info``: ids, titles, tags,
   turn kinds, whether a case needs live embeddings or injects into Merxa, the suite hash);
2. **claim** — takes the queued run (one active run platform-wide) with a lease, renewed by a
   background thread every ``EVALS_RUNNER_LEASE_RENEW_S`` while it works (a turn can take minutes);
3. **per case** — the CLI's own ``run_live.run_case`` (black box over HTTP as the demo personas, Merxa
   resets, apply probes that never approve) with hooks: the new Keelson case is **attached** to the
   eval run right after ``POST /api/cases`` and before its first message, so the engine applies the
   run's LLM mode (``replay`` = recorded answers only, no provider call) and live budget to every run
   of that case; turn progress is reported as events; the cancel / budget flags returned by every
   events call are checked before each turn; the case's ``keelson-evals/2`` result is PUT (retried:
   a result is never lost to a 5xx). In ``replay`` mode, cases that upload knowledge-base documents
   are reported ``skipped`` (``needs_live_embeddings``): embeddings are not part of LLM replay;
4. **finish** — the engine computes the summary and thresholds from the stored results.

Cancelling a running eval in the UI also cancels the in-flight Keelson run of the current case
(engine side), so the turn ends and the loop stops before the next one; the case's teardown (Merxa
reset) still runs.

Configuration comes from the environment only (a job never names a URL): ``KEELSON_URL``,
``MERXA_URL`` (read by ``e2e/api_thin_slice.py``), ``EVALS_RUNNER_TOKEN``, ``EVALS_RUNNER_POLL_S``
(default 5), ``EVALS_RUNNER_LEASE_RENEW_S`` (default 30), ``KEELSON_E2E_DIR`` (``/app/e2e``).
``--once`` handles at most one job and exits (tests, smoke checks).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_live  # noqa: E402
from keelson_evals.cases import Case, CaseError, load_cases  # noqa: E402
from keelson_evals.engine_client import EngineClient, EngineError  # noqa: E402
from keelson_evals.report import CaseResult  # noqa: E402
from keelson_evals.suite import suite_info  # noqa: E402

VERSION = "m8.1"
DEFAULT_POLL_S = 5.0
DEFAULT_RENEW_S = 30.0


def log(msg: str) -> None:
    print(f"[evals-runner {dt.datetime.now(dt.UTC):%H:%M:%S}] {msg}", flush=True)


class RunAborted(Exception):
    """The engine no longer accepts reports for the run (lease lost / finished elsewhere)."""


class Flags:
    """The run's cancel / budget flags as the engine last answered them (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cancel_requested = False
        self.budget_exhausted = False

    def update(self, answer: dict[str, Any]) -> None:
        with self._lock:
            self.cancel_requested = self.cancel_requested or bool(answer.get("cancel_requested"))
            self.budget_exhausted = self.budget_exhausted or bool(answer.get("budget_exhausted"))

    def stop_reason(self) -> str | None:
        with self._lock:
            if self.cancel_requested:
                return "cancelled"
            if self.budget_exhausted:
                return "budget_exhausted"
            return None


class LeaseKeeper(threading.Thread):
    """Renews the run's lease (an empty events call) every ``interval_s`` while a case runs, and
    picks up the cancel / budget flags meanwhile. Uses its own HTTP client (thread safety)."""

    def __init__(self, engine: EngineClient, run_id: str, runner_id: str, flags: Flags, interval_s: float) -> None:
        super().__init__(name=f"lease-{run_id[:8]}", daemon=True)
        self.engine, self.run_id, self.runner_id, self.flags, self.interval_s = engine, run_id, runner_id, flags, interval_s
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval_s):
            try:
                self.flags.update(self.engine.events(self.run_id, self.runner_id, []))
            except EngineError as exc:
                log(f"lease renewal failed: {exc}")

    def stop(self) -> None:
        self._stop_event.set()


class Runner:
    def __init__(
        self,
        engine: EngineClient,
        ts: Any,
        *,
        cases_dir: Path,
        repo_root: Path,
        runner_id: str,
        keeper_factory: Callable[[str, Flags], LeaseKeeper | None] | None = None,
        live_args: argparse.Namespace | None = None,
    ) -> None:
        self.engine = engine
        self.ts = ts
        self.cases_dir = cases_dir
        self.repo_root = repo_root
        self.runner_id = runner_id
        self.keeper_factory = keeper_factory
        self.cases = load_cases(cases_dir)
        self.suite = suite_info(self.cases)
        self.live_args = live_args or self._default_live_args()

    def _default_live_args(self) -> argparse.Namespace:
        args = run_live.parse_args([])
        args.repo_root = self.repo_root
        args.cases_dir = self.cases_dir
        args.tokens_wait = 0.0  # in-app runs use the exact per-run usage; the gateway delta is only indicative
        return args

    # ── loop ──
    def heartbeat(self) -> dict[str, Any]:
        return self.engine.heartbeat(self.runner_id, VERSION, self.suite)

    def run_once(self) -> bool:
        """Heartbeat, then claim and execute at most one run. ``True`` when a run was executed."""
        self.heartbeat()
        job = self.engine.claim(self.runner_id)
        if job is None:
            return False
        self.execute(job)
        return True

    # ── one run ──
    def _events(self, run_id: str, events: list[dict[str, Any]], flags: Flags) -> None:
        try:
            flags.update(self.engine.events(run_id, self.runner_id, events))
        except EngineError as exc:
            if exc.code == "run_not_running" or exc.status in (403, 404):
                raise RunAborted(str(exc)) from None
            log(f"progress event not recorded: {exc}")

    def _report(self, run_id: str, case: Case, result: CaseResult, flags: Flags, pending: list[tuple[str, dict[str, Any]]]
                ) -> None:
        doc = result.to_json()
        try:
            self.engine.put_result(run_id, self.runner_id, case.id, doc)
        except EngineError as exc:
            if exc.code == "run_not_running":
                raise RunAborted(str(exc)) from None
            if exc.retryable:
                log(f"case {case.id}: result not stored yet ({exc}); retrying before the run finishes")
                pending.append((case.id, doc))
            else:
                log(f"case {case.id}: the engine refused the result: {exc}")
        self._events(run_id, [{"type": "case_finished", "case_id": case.id, "data": {
            "status": result.status, "error_code": result.error_code}}], flags)

    def _hooks(self, run_id: str, case: Case, flags: Flags) -> run_live.CaseHooks:
        def created(keelson_case_id: str) -> None:
            # attach failure must abort the case: an unlinked case would run in the process's LLM mode
            self.engine.attach(run_id, self.runner_id, case.id, keelson_case_id)

        def started(turn: Any) -> None:
            self._events(run_id, [{"type": "turn_started", "case_id": case.id, "turn_id": turn.id,
                                   "data": {"kind": turn.kind}}], flags)

        def finished(turn: Any, tr: Any) -> None:
            failed = sum(1 for c in tr.checks if not c.ok and not c.soft)
            self._events(run_id, [{"type": "turn_finished", "case_id": case.id, "turn_id": turn.id, "data": {
                "checks": len(tr.checks), "checks_failed": failed, "final_code": tr.final_code}}], flags)

        return run_live.CaseHooks(on_case_created=created, on_turn_started=started, on_turn_finished=finished,
                                  stop_reason=flags.stop_reason)

    def execute(self, job: dict[str, Any]) -> str:
        """Run every case of ``job``; returns the status sent to ``finish`` (``""`` = aborted)."""
        run_id, mode = str(job["run_id"]), str(job["mode"])
        flags = Flags()
        log(f"run {run_id}: mode {mode}, cases {job['case_ids']}")
        try:
            order = {cid: i for i, cid in enumerate(job["case_ids"])}
            cases = sorted(load_cases(self.cases_dir, list(job["case_ids"])), key=lambda c: order[c.id])
        except CaseError as exc:
            self.engine.finish(run_id, self.runner_id, "failed", f"this runner cannot run the cases: {exc}"[:500])
            return "failed"
        keeper = self.keeper_factory(run_id, flags) if self.keeper_factory is not None else None
        if keeper is not None:
            keeper.start()
        pending: list[tuple[str, dict[str, Any]]] = []
        try:
            live = run_live.Live(self.ts, self.live_args)
            self._events(run_id, [{"type": "run_started", "data": {"mode": mode, "cases": len(cases)}}], flags)
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            for case in cases:
                reason = flags.stop_reason()
                if reason is not None:
                    result = CaseResult(case.id, case.title, list(case.tags), error_code=reason)
                    if reason == "budget_exhausted":
                        result.skipped = "the run's live budget was used up before this case"
                    else:
                        result.error = "the run was cancelled before this case"
                    self._report(run_id, case, result, flags, pending)
                    continue
                self._events(run_id, [{"type": "case_started", "case_id": case.id,
                                       "data": {"title": case.title[:300], "turns": len(case.turns)}}], flags)
                if mode == "replay" and case.setup.kb_upload is not None:
                    result = CaseResult(case.id, case.title, list(case.tags), error_code="needs_live_embeddings",
                                        skipped="uploads knowledge-base documents: embeddings are not replayed")
                    self._report(run_id, case, result, flags, pending)
                    continue
                result = run_live.run_case(live, case, stamp, self._hooks(run_id, case, flags))
                if hasattr(self.ts, "USAGE_RUNS"):
                    self.ts.USAGE_RUNS.clear()  # the e2e helpers keep every run's usage: a long-lived loop must not
                self._report(run_id, case, result, flags, pending)
            for case_id, doc in pending:
                self.engine.put_result(run_id, self.runner_id, case_id, doc)  # raises: the run fails below
            status = "cancelled" if flags.cancel_requested else "succeeded"
            self._events(run_id, [{"type": "run_finished", "data": {"status": status}}], flags)
            self.engine.finish(run_id, self.runner_id, status)
            log(f"run {run_id}: {status}")
            return status
        except RunAborted as exc:
            log(f"run {run_id}: aborted ({exc})")
            return ""
        except Exception as exc:  # noqa: BLE001 - report the failure; the loop goes on with the next run
            log(f"run {run_id}: failed ({type(exc).__name__}: {exc})")
            try:
                self.engine.finish(run_id, self.runner_id, "failed", f"{type(exc).__name__}: {exc}"[:500])
            except EngineError as finish_exc:
                log(f"run {run_id}: could not report the failure ({finish_exc}); the lease will expire")
            return "failed"
        finally:
            if keeper is not None:
                keeper.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--once", action="store_true", help="handle at most one run, then exit")
    parser.add_argument("--cases-dir", type=Path, default=HERE / "cases")
    parser.add_argument("--repo-root", type=Path, default=HERE.parent)
    return parser.parse_args(argv)


def _float_env(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("EVALS_RUNNER_TOKEN", "").strip()
    if not token:
        print("EVALS_RUNNER_TOKEN is not set: the in-app runner is off", file=sys.stderr)
        return 2
    try:
        ts = run_live.load_e2e(args.repo_root.resolve())
    except ImportError as exc:
        print(f"cannot import the e2e helpers (KEELSON_E2E_DIR?): {exc}", file=sys.stderr)
        return 2
    poll_s = _float_env("EVALS_RUNNER_POLL_S", DEFAULT_POLL_S, 1.0, 300.0)
    renew_s = _float_env("EVALS_RUNNER_LEASE_RENEW_S", DEFAULT_RENEW_S, 5.0, 120.0)
    runner_id = f"{socket.gethostname()[:40]}-{os.getpid()}"
    engine = EngineClient(ts.KEELSON, token)

    def keeper(run_id: str, flags: Flags) -> LeaseKeeper:
        return LeaseKeeper(EngineClient(ts.KEELSON, token), run_id, runner_id, flags, renew_s)

    runner = Runner(engine, ts, cases_dir=args.cases_dir.resolve(), repo_root=args.repo_root.resolve(),
                    runner_id=runner_id, keeper_factory=keeper)
    log(f"runner {runner_id} ({VERSION}): {len(runner.cases)} cases, suite {runner.suite['suite_hash'][:12]}, "
        f"engine {ts.KEELSON}")
    while True:
        try:
            worked = runner.run_once()
        except EngineError as exc:
            log(f"engine unavailable: {exc}")
            worked = False
        if args.once:
            return 0
        if not worked:
            time.sleep(poll_s)


if __name__ == "__main__":
    sys.exit(main())
