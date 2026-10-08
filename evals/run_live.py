#!/usr/bin/env python3
"""Keelson live evals: golden + adversarial cases, black-box over HTTP (``docs/contracts/90`` §9).

Runs like ``e2e/api_thin_slice.py`` (whose HTTP helpers it imports): a throwaway Python
container on ``keelson_edge`` so ``*.localhost`` resolves to Caddy, real models behind LiteLLM.
Cases are data (``evals/cases/*.yaml``, see ``keelson_evals/cases.py``); for each case:

1. setup — Merxa golden reset (``merxa.admin``), demo injects (e.g. an adversarial supplier
   message), canonical-store resync (tenant admin ``sam``), temporary knowledge-base documents
   uploaded through ``/api/admin/kb`` (deleted again afterwards);
2. baselines — admin audit cursor, Merxa change sets, LiteLLM token usage, the Merxa tool
   catalogue (write / untrusted classification);
3. turns as planner ``priya`` — post a message (answer an approach card as the case says) or a
   case action (``promote`` / ``apply`` / ``confirm_assumptions``); every run is followed on the
   SSE stream; **approvals are never decided** (no commit without a human);
4. checks per turn and per case (``keelson_evals/evaluate.py``) and metrics: affected-entity
   recall vs golden, numeric grounding, latency, quarantine flags, tool calls, tokens/case.

Results: ``evals/results/<UTC stamp>.json`` (metrics) + ``.md`` (summary); both gitignored.

Modes: ``--dry-run`` validates the case files offline (no network, no httpx needed);
``--preflight`` only reads (logins, catalogue, audit, usage, KB list, Merxa change sets) to
check that the stack answers the way the runner expects.

Exit status: 0 every case passed; 1 a case failed or errored; 2 usage / environment error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from keelson_evals import assertions as A  # noqa: E402
from keelson_evals.assertions import Check  # noqa: E402
from keelson_evals.cases import Case, CaseError, Turn, is_soft, load_cases, validate_cases  # noqa: E402
from keelson_evals.evaluate import (  # noqa: E402
    CaseObservation,
    TurnObservation,
    evaluate_case,
    evaluate_turn,
    merge_metrics,
)
from keelson_evals.report import SCHEMA, CaseResult, TurnResult, render_markdown, summarise, write_results  # noqa: E402
from keelson_evals.suite import suite_hash  # noqa: E402

MAX_APPROACH_ROUNDS = 3
CONNECTOR = "merxa"
MODE_LABELS = ("replay", "record_or_replay", "record")
STOP_CODES = {
    "LLM_REPLAY_MISS": "replay_miss",
    "EVAL_BUDGET_EXHAUSTED": "budget_exhausted",
    "BUDGET_EXCEEDED": "budget_exhausted",
    "EVAL_CANCELLED": "cancelled",
}
"""``RUN_ERROR`` codes that end a case early (``keelson_contracts.evals.EVAL_STOP_CODES``): the
remaining turns could not be evaluated, so the case is ``error`` (``cancelled``) with this code
instead of a list of consequential check failures."""


@dataclass
class CaseHooks:
    """(M8) Callbacks of the in-app runner (``runner_service.py``); the CLI passes none.

    ``on_case_created`` runs right after ``POST /api/cases`` and before the first turn (the runner
    links the case to its eval run there, so the engine applies the run's LLM mode to every run of
    the case): an exception aborts the case before anything is posted. ``stop_reason`` is checked
    before each turn (``cancelled`` / ``budget_exhausted`` stop the case; the teardown still runs)."""

    on_case_created: Callable[[str], None] | None = None
    on_turn_started: Callable[[Turn], None] | None = None
    on_turn_finished: Callable[[Turn, TurnResult], None] | None = None
    stop_reason: Callable[[], str | None] | None = None


# ─────────────────────────────────── dry run ───────────────────────────────────


def dry_run(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(args.cases_dir, args.cases)
    except CaseError as exc:
        print(f"INVALID: {exc}")
        return 1
    problems = validate_cases(cases, args.repo_root)
    for case in cases:
        kinds = ", ".join(("message" if t.message is not None else f"action:{t.action.type}" if t.action is not None
                           else f"signal:{t.signal.inject.kind}") for t in case.turns)  # type: ignore[union-attr]
        print(f"  {case.id:32s} {len(case.turns)} turn(s) [{kinds}] tags={case.tags}")
    if problems:
        for p in problems:
            print(f"INVALID: {p}")
        return 1
    print(f"OK: {len(cases)} case file(s) valid")
    return 0


# ─────────────────────────────────── live helpers ───────────────────────────────────


def load_e2e(repo_root: Path) -> Any:
    """``e2e/api_thin_slice.py`` (HTTP helpers: logins, SSE follower, Merxa reset, scn resync)."""
    e2e_dir = Path(os.environ.get("KEELSON_E2E_DIR") or (repo_root / "e2e"))
    sys.path.insert(0, str(e2e_dir))
    import api_thin_slice as ts  # noqa: PLC0415

    return ts


class Live:
    """Per-suite HTTP state (lazy logins)."""

    def __init__(self, ts: Any, args: argparse.Namespace) -> None:
        self.ts = ts
        self.args = args
        self._sam: Any = None

    @property
    def sam(self) -> Any:
        if self._sam is None:
            self._sam = self.ts.keelson_login("sam")
        return self._sam

    # ── admin reads ──
    def audit_cursor(self) -> int:
        r = self.sam.client.get(f"{self.ts.KEELSON}/api/admin/audit", params={"limit": 1})
        self.ts.expect(r.status_code == 200, f"GET audit: HTTP {r.status_code}")
        items = r.json().get("items") or []
        return int(items[0]["id"]) if items else 0

    def audit_since(self, cursor: int, action: str = "untrusted_text") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        before: int | None = None
        for _ in range(50):
            params: dict[str, Any] = {"limit": 200, "action": action}
            if before is not None:
                params["before"] = before
            r = self.sam.client.get(f"{self.ts.KEELSON}/api/admin/audit", params=params)
            self.ts.expect(r.status_code == 200, f"GET audit: HTTP {r.status_code}")
            body = r.json()
            items = [i for i in body.get("items") or [] if int(i["id"]) > cursor]
            out.extend(items)
            nxt = body.get("next_before")
            if not nxt or len(items) < len(body.get("items") or []) or int(nxt) <= cursor:
                break
            before = int(nxt)
        return out

    def tool_catalog(self) -> list[dict[str, Any]] | None:
        r = self.sam.client.get(f"{self.ts.KEELSON}/api/admin/connectors/{CONNECTOR}/tools")
        if r.status_code != 200:
            self.ts.say(f"note: tool catalogue unavailable (HTTP {r.status_code})")
            return None
        return list(r.json().get("items") or [])

    def agents_catalog(self) -> dict[str, Any] | None:
        """``GET /api/admin/agents/catalog``: the tools bound to every LLM role (specialists,
        answer investigator) with the connectors' tool classifications."""
        r = self.sam.client.get(f"{self.ts.KEELSON}/api/admin/agents/catalog")
        if r.status_code != 200:
            self.ts.say(f"note: agents catalogue unavailable (HTTP {r.status_code})")
            return None
        return dict(r.json())

    def tokens_used(self) -> int | None:
        r = self.sam.client.get(f"{self.ts.KEELSON}/api/admin/gateway/usage", params={"days": 2})
        if r.status_code != 200:
            return None
        daily = r.json().get("daily") or []
        return sum(int(d.get("tokens_in") or 0) + int(d.get("tokens_out") or 0) for d in daily)

    # ── Merxa ──
    def merxa_admin(self) -> Any:
        return self.ts.merxa_login("merxa.admin")

    def change_sets(self) -> list[dict[str, Any]] | None:
        client = self.merxa_admin()
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = client.get(f"{self.ts.MERXA}/v1/change-sets", params=params)
            if r.status_code != 200:
                self.ts.say(f"note: Merxa change sets unavailable (HTTP {r.status_code})")
                return None
            body = r.json()
            out.extend(body.get("items") or [])
            cursor = body.get("next_cursor")
            if not cursor:
                break
        return out

    def inject(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = self.merxa_admin()
        r = client.post(
            f"{self.ts.MERXA}/v1/admin/scenario:inject",
            json={"kind": kind, "payload": payload},
            headers={"Origin": self.ts.MERXA, "Idempotency-Key": str(uuid.uuid4())},
            timeout=60.0,
        )
        self.ts.expect(r.status_code == 200, f"merxa inject {kind}: HTTP {r.status_code} {r.text[:200]}")
        return dict(r.json())

    # ── knowledge base ──
    def kb_id(self, name: str, description: str) -> str:
        base = f"{self.ts.KEELSON}/api/admin/kb"
        r = self.sam.client.get(base)
        self.ts.expect(r.status_code == 200, f"GET kb: HTTP {r.status_code} {r.text[:200]}")
        for kb in r.json().get("items") or []:
            if kb.get("name") == name:
                return str(kb["id"])
        r = self.sam.client.post(base, json={"name": name, "description": description}, headers=self.sam.headers())
        self.ts.expect(r.status_code == 201, f"POST kb: HTTP {r.status_code} {r.text[:200]}")
        return str(r.json()["id"])

    def kb_upload(self, kb_id: str, path: Path, created: list[tuple[str, str]]) -> dict[str, Any]:
        """Upload one document; its id is appended to ``created`` **before** a failure is raised
        (an embedding failure keeps the document as ``failed``: it must be cleaned up too)."""
        mime = mimetypes.guess_type(path.name)[0] or "text/markdown"
        if path.suffix.lower() == ".md":
            mime = "text/markdown"
        r = self.sam.client.post(
            f"{self.ts.KEELSON}/api/admin/kb/{kb_id}/documents",
            files={"file": (path.name, path.read_bytes(), mime)},
            headers=self.sam.headers(),
            timeout=180.0,
        )
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        doc_id = body.get("id") if r.status_code in (200, 201) else body.get("document_id")
        if doc_id:
            created.append((kb_id, str(doc_id)))
        self.ts.expect(r.status_code in (200, 201), f"upload {path.name}: HTTP {r.status_code} {r.text[:200]}")
        return body

    def kb_delete(self, kb_id: str, doc_id: str) -> bool:
        r = self.sam.client.delete(f"{self.ts.KEELSON}/api/admin/kb/{kb_id}/documents/{doc_id}",
                                   headers=self.sam.headers())
        return r.status_code in (204, 404)

    def kb_document_ids(self, kb_id: str) -> set[str] | None:
        r = self.sam.client.get(f"{self.ts.KEELSON}/api/admin/kb/{kb_id}/documents")
        if r.status_code != 200:
            return None
        return {str(d.get("id")) for d in r.json().get("items") or []}


# ─────────────────────────────────── turns ───────────────────────────────────


def _latest_block(case_snapshot: dict[str, Any], kind: str) -> dict[str, Any] | None:
    found = [b for b in case_snapshot.get("blocks") or [] if b.get("kind") == kind]
    return found[-1] if found else None


def _absorb(obs: TurnObservation, res: Any) -> None:
    obs.run_ids.append(res.run_id)
    obs.events.extend(res.events)
    obs.blocks.update(res.blocks)
    obs.final = res.final
    obs.interrupts_seen += [str(i.get("reason")) for i in res.interrupts]
    usage = (res.final.get("metadata") or {}).get("usage")
    obs.usages.append(usage if isinstance(usage, dict) else None)


def _follow_approach(live: Live, user: Any, case_id: str, turn: Turn, obs: TurnObservation, res: Any) -> Any:
    """Answer approach cards as the turn says; approvals are left to a human (never decided)."""
    ts = live.ts
    for _ in range(MAX_APPROACH_ROUNDS):
        if res.outcome.get("type") != "interrupt":
            return res
        approach = [i for i in res.interrupts if i.get("reason") == "approach"]
        if not approach:
            return res  # e.g. an approval: left open on purpose
        card = res.blocks.get("approach_card")
        obs.approach_card = dict((card or {}).get("props") or {})
        if turn.approach == "none":
            return res
        decision = "what_if" if turn.approach == "what_if" else "confirm"
        r = user.client.post(f"{ts.KEELSON}/api/interrupts/{approach[0]['id']}/decision",
                             json={"decision": decision}, headers=user.headers())
        ts.expect(r.status_code == 202, f"approach {decision}: HTTP {r.status_code} {r.text[:300]}")
        ts.expect(r.json().get("status") == "resumed", f"approach {decision}: {r.json()}")
        res = ts.follow_run(user, case_id, str(r.json()["run_id"]), res.last_seq, timeout_s=live.args.run_timeout)
        _absorb(obs, res)
    return res


def run_turn(live: Live, user: Any, case_id: str, turn: Turn, last_rec: dict[str, Any] | None) -> TurnObservation:
    ts = live.ts
    obs = TurnObservation(turn_id=turn.id, prompt=turn.message)
    ts.heading(f"turn {turn.id}")
    after = int(ts.get_case(user, case_id).get("last_event_seq") or 0)
    ts.CLOCK.marks.clear()
    t_post = ts.CLOCK.now()
    if turn.signal is not None:
        return _signal_turn(live, user, case_id, turn, obs, after, t_post)
    if turn.message is not None:
        r = user.client.post(f"{ts.KEELSON}/api/cases/{case_id}/messages", json={"text": turn.message},
                             headers=user.headers(idem=True))
        ts.expect(r.status_code == 202, f"post message: HTTP {r.status_code} {r.text[:200]}")
    else:
        spec = turn.action
        assert spec is not None
        ts.expect(last_rec is not None, f"action {spec.type}: no recommended_option yet")
        assert last_rec is not None
        state = A.action_state(last_rec, spec.type)
        if spec.if_enabled and (state is None or not state.get("enabled")):
            obs.skipped = f"{spec.type} not offered/enabled ({state})"
            ts.say(f"skip: {obs.skipped}")
            obs.case = ts.get_case(user, case_id)
            return obs
        option_id = str(last_rec["option"]["id"]) if spec.option == "recommended" else spec.option
        body: dict[str, Any] = {"type": spec.type, "option_id": option_id,
                                "plan_version": int(last_rec["plan_version"])}
        if spec.type == "confirm_assumptions":
            body["assumption_keys"] = [str(u.get("key")) for u in last_rec.get("unconfirmed") or []]
        r = user.client.post(f"{ts.KEELSON}/api/cases/{case_id}/actions", json=body, headers=user.headers(idem=True))
        ts.expect(r.status_code == 202, f"action {spec.type}: HTTP {r.status_code} {r.text[:300]}")
    res = ts.follow_run(user, case_id, str(r.json()["run_id"]), after, timeout_s=live.args.run_timeout)
    _absorb(obs, res)
    _follow_approach(live, user, case_id, turn, obs, res)
    marks = ts.CLOCK.marks
    obs.timings = {
        "first_stage": marks["first_stage"] - t_post if "first_stage" in marks else None,
        "exposure": marks["exposure"] - t_post if "exposure" in marks else None,
        "options": marks["options"] - t_post if "options" in marks else None,
        "total": ts.CLOCK.now() - t_post,
    }
    obs.case = ts.get_case(user, case_id)
    for kind, blk in obs.blocks.items():
        stored = next((b for b in obs.case.get("blocks") or [] if b.get("id") == blk.get("id")), blk)
        ref = str(stored.get("data_ref") or "")
        if ref.startswith("/api/blocks/"):
            d = user.client.get(f"{ts.KEELSON}{ref}")
            if d.status_code == 200:
                obs.data[kind] = d.json()
            else:
                ts.say(f"note: {kind} data_ref HTTP {d.status_code}")
    return obs


def _signal_turn(live: Live, user: Any, case_id: str, turn: Turn, obs: TurnObservation, after: int,
                 t_post: float) -> TurnObservation:
    """(M5) Merxa demo inject → the engine routes its webhook to this case as a ``signal`` run;
    follow that run like a turn (the first run that starts on the case after the inject)."""
    ts = live.ts
    spec = turn.signal
    assert spec is not None
    obs.change_sets_before = live.change_sets()
    out = live.inject(spec.inject.kind, dict(spec.inject.payload))
    ts.say(f"inject {spec.inject.kind}: {ts.ascii_text({k: out.get(k) for k in ('lane_code', 'factor_bp', 'weeks')})}")
    deadline = time.monotonic() + spec.start_timeout_s
    run_id: str | None = None
    try:
        for _seq, ev in ts.sse_events(user, case_id, after, deadline):
            if ev.get("type") == "RUN_STARTED" and ev.get("runId"):
                run_id = str(ev["runId"])
                break
    except ts.E2EFailure:
        run_id = None
    obs.signal_run = run_id
    if run_id is None:
        ts.say(f"no run reached the case within {spec.start_timeout_s} s of the inject")
        obs.case = ts.get_case(user, case_id)
        obs.change_sets_after = live.change_sets()
        obs.final = {"type": "NONE"}
        return obs
    ts.say(f"signal run {run_id} started")
    res = ts.follow_run(user, case_id, run_id, after, timeout_s=live.args.run_timeout)
    _absorb(obs, res)
    obs.timings = {"first_stage": None, "exposure": None, "options": None, "total": ts.CLOCK.now() - t_post}
    obs.case = ts.get_case(user, case_id)
    obs.change_sets_after = live.change_sets()
    return obs


# ─────────────────────────────────── cases ───────────────────────────────────


def _say_check(ts: Any, c: Check) -> None:
    tag = "ok  " if c.ok else ("soft" if c.soft else "FAIL")
    ts.say(f"check   {tag} {c.name}: {ts.ascii_text(c.detail, 200)}")


def _setup_check(case: Case, name: str, ok: bool, detail: str) -> Check:
    return Check(name, ok, detail, soft=is_soft(case, name))


def run_case(live: Live, case: Case, stamp: str, hooks: CaseHooks | None = None) -> CaseResult:
    ts = live.ts
    args = live.args
    hooks = hooks or CaseHooks()
    result = CaseResult(case.id, case.title, list(case.tags))
    t0 = time.monotonic()
    kb_docs: list[tuple[str, str]] = []
    setup_checks: list[Check] = []
    observations: list[TurnObservation] = []
    co: CaseObservation | None = None
    ts.heading(f"CASE {case.id}: {case.title}")
    if not args.reset and (case.setup.inject or case.teardown.reset_merxa or any(t.signal for t in case.turns)):
        # an inject (e.g. an adversarial supplier message) is only removed again by a Merxa reset:
        # never leave it behind in the ERP
        result.error = "needs Merxa resets (inject / teardown reset); not run with --no-reset"
        ts.say(f"SKIP: {result.error}")
        return result
    try:
        # 1. setup
        if case.setup.reset_merxa and args.reset:
            ts.merxa_reset()
        for inj in case.setup.inject:
            out = live.inject(inj.kind, dict(inj.payload))
            ts.say(f"inject  : {inj.kind} -> {json.dumps(out)[:200]}")
            setup_checks.append(_setup_check(case, "case.inject", True, f"{inj.kind} {out.get('id') or ''}"))
        if case.setup.resync and (args.reset or case.setup.inject):
            ts.keelson_resync(f"case {case.id}", timeout_s=args.sync_timeout)
        if case.setup.kb_upload is not None:
            up = case.setup.kb_upload
            kb_id = live.kb_id(up.kb_name, up.kb_description)
            for rel in up.files:
                doc = live.kb_upload(kb_id, (args.repo_root / rel).resolve(), kb_docs)
                status = str(doc.get("status"))
                ts.say(f"kb      : {rel} -> {doc['id']} status={status} chunks={doc.get('chunks')} "
                       f"duplicate={doc.get('duplicate')}")
                setup_checks.append(_setup_check(case, "case.kb_upload", status not in ("failed", "error"),
                                                 f"{rel}: status {status}, chunks {doc.get('chunks')}"))

        # 2. baselines
        audit_cursor = live.audit_cursor()
        catalog = live.tool_catalog()
        agents_catalog = live.agents_catalog()
        cs_before = live.change_sets()
        tokens_before = live.tokens_used()

        # 3. turns
        priya = ts.keelson_login("priya")
        r = priya.client.post(f"{ts.KEELSON}/api/cases", json={"title": f"Eval {case.id} {stamp}"},
                              headers=priya.headers())
        ts.expect(r.status_code == 201, f"create case: HTTP {r.status_code} {r.text[:200]}")
        case_id = str(r.json()["id"])
        result.case_id = case_id
        ts.say(f"case {case_id}")
        if hooks.on_case_created is not None:
            hooks.on_case_created(case_id)  # in-app runner: link to the eval run before the first turn
        co = CaseObservation(case_id=case_id, turns=observations, setup_checks=setup_checks,
                             agents_catalog=agents_catalog, connector_tools=catalog)
        last_rec: dict[str, Any] | None = None
        prompts: list[str] = []
        for turn in case.turns:
            reason = hooks.stop_reason() if hooks.stop_reason is not None else None
            if reason:
                result.error_code = reason
                result.error = f"stopped before turn {turn.id}: {reason.replace('_', ' ')}"
                ts.say(f"STOP: {result.error}")
                break
            if hooks.on_turn_started is not None:
                hooks.on_turn_started(turn)
            if turn.message:
                prompts.append(turn.message)
            obs = run_turn(live, priya, case_id, turn, last_rec)
            observations.append(obs)
            rec_blk = obs.blocks.get("recommended_option") or _latest_block(obs.case, "recommended_option")
            if rec_blk is not None:
                last_rec = dict(rec_blk.get("props") or {})
            prev = observations[-2] if len(observations) > 1 else None
            checks, metrics = evaluate_turn(case, turn, obs, prev, prompts)
            turn_result = TurnResult(turn.id, turn.kind, obs.run_ids, obs.timings, obs.plan_version, checks, metrics,
                                     obs.skipped, usage=A.usage_total(obs.usages), final_code=obs.final_code)
            result.turns.append(turn_result)
            for c in checks:
                _say_check(ts, c)
            if hooks.on_turn_finished is not None:
                hooks.on_turn_finished(turn, turn_result)
            stop = STOP_CODES.get(obs.final_code or "")
            if stop is not None:
                # the remaining turns would only report the consequences (e.g. "no recommended_option yet")
                result.error_code = stop
                result.error = f"turn {turn.id}: the run ended with {obs.final_code}"
                ts.say(f"STOP: {result.error}")
                break

        # 4. case-level observations
        if args.tokens_wait > 0:
            time.sleep(args.tokens_wait)  # LiteLLM writes spend logs in batches
        tokens_after = live.tokens_used()
        co.audit_rows = live.audit_since(audit_cursor)
        if catalog is not None:
            co.write_tools = A.write_tool_names(catalog)
            co.untrusted_tools = {str(t.get("name")) for t in catalog if t.get("content_trust") == "untrusted_text"}
        co.change_sets_before = cs_before
        co.change_sets_after = live.change_sets()
        if tokens_before is not None and tokens_after is not None:
            result.metrics["tokens"] = max(0, tokens_after - tokens_before)
        result.usage = A.usage_total(u for o in observations for u in o.usages)
    except (ts.E2EFailure, AssertionError) as exc:
        result.error = str(exc)
        ts.say(f"ERROR: {exc}")
    except Exception as exc:  # noqa: BLE001 — an HTTP / transport failure of one case must not stop the suite
        result.error = f"{type(exc).__name__}: {exc}"
        ts.say(f"ERROR: {result.error}")
    finally:
        if kb_docs:
            setup_checks.append(_setup_check(case, "case.kb_cleanup", *_kb_cleanup(live, kb_docs)))
        if case.teardown.reset_merxa and args.reset:
            try:
                ts.merxa_reset()
            except Exception as exc:  # noqa: BLE001
                ts.say(f"note: teardown reset failed: {exc}")

    if co is None:
        result.checks = setup_checks
    else:
        try:
            checks, metrics = evaluate_case(case, co)
        except Exception as exc:  # noqa: BLE001
            checks, metrics = setup_checks, {}
            result.error = result.error or f"evaluation failed: {type(exc).__name__}: {exc}"
        result.checks = checks
        result.metrics = merge_metrics([t.metrics for t in result.turns], {**metrics, **result.metrics})
        for c in result.checks:
            _say_check(ts, c)
    result.duration_s = time.monotonic() - t0
    ts.say(f"CASE {case.id}: {result.status.upper()} ({result.duration_s:.0f}s)")
    return result


def _kb_cleanup(live: Live, kb_docs: list[tuple[str, str]]) -> tuple[bool, str]:
    """Delete the temporary documents, then verify by listing that none of them is left."""
    problems: list[str] = []
    for kb_id, doc_id in kb_docs:
        try:
            if not live.kb_delete(kb_id, doc_id):
                problems.append(f"DELETE {doc_id} refused")
        except Exception as exc:  # noqa: BLE001 — keep cleaning up the others
            problems.append(f"DELETE {doc_id}: {type(exc).__name__}")
    for kb_id in sorted({k for k, _d in kb_docs}):
        try:
            left = live.kb_document_ids(kb_id)
        except Exception as exc:  # noqa: BLE001
            live.ts.say(f"note: KB listing failed: {type(exc).__name__}")
            left = None
        if left is None:
            problems.append(f"cannot list KB {kb_id} to verify the deletion")
            continue
        still = sorted(d for k, d in kb_docs if k == kb_id and d in left)
        if still:
            problems.append(f"still in KB {kb_id}: {still}")
    if problems:
        return False, "temporary KB document(s) not removed: " + "; ".join(problems)
    return True, f"{len(kb_docs)} document(s) deleted and verified gone"


# ─────────────────────────────────── preflight ───────────────────────────────────


def preflight(live: Live) -> int:
    """Read-only checks that the stack answers the way the runner expects."""
    ts = live.ts
    ts.heading("Preflight (read-only)")
    ts.keelson_login("priya")
    print(f"  audit cursor          {live.audit_cursor()}")
    catalog = live.tool_catalog()
    if catalog is not None:
        writes = sorted(A.write_tool_names(catalog))
        untrusted = sorted(str(t.get("name")) for t in catalog if t.get("content_trust") == "untrusted_text")
        print(f"  merxa tools           {len(catalog)} ({len(writes)} write: {writes[:8]}...)")
        print(f"  untrusted_text tools  {untrusted}")
    agents = live.agents_catalog()
    if agents is not None:
        bindings = {str(sp.get("slug")): len(sp.get("tools") or []) for sp in agents.get("specialists") or []}
        conn = {CONNECTOR: catalog} if catalog is not None else {}
        print(f"  specialist bindings   {bindings}")
        print(f"  bound-tool violations {A.bound_tool_violations(agents, conn)}")
    print(f"  tokens (2 days)       {live.tokens_used()}")
    r = live.sam.client.get(f"{ts.KEELSON}/api/admin/kb")
    print(f"  knowledge bases       HTTP {r.status_code} "
          f"{[k.get('name') for k in (r.json().get('items') or [])] if r.status_code == 200 else r.text[:120]}")
    cs = live.change_sets()
    print(f"  merxa change sets     {None if cs is None else len(cs)}")
    return 0


# ─────────────────────────────────── main ───────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="validate the case files offline and exit")
    parser.add_argument("--preflight", action="store_true", help="read-only stack check and exit")
    parser.add_argument("--case", dest="cases", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--cases-dir", type=Path, default=HERE / "cases")
    parser.add_argument("--results-dir", type=Path, default=HERE / "results")
    parser.add_argument("--repo-root", type=Path, default=HERE.parent)
    parser.add_argument("--no-reset", dest="reset", action="store_false",
                        help="never reset Merxa (cases expect the golden seed; use right after a reset only)")
    parser.add_argument("--run-timeout", type=float, default=600.0, help="seconds to wait for one run")
    parser.add_argument("--sync-timeout", type=float, default=240.0, help="seconds to wait for one scn sync")
    parser.add_argument("--tokens-wait", type=float, default=15.0,
                        help="seconds to wait before reading token usage after a case (0 = immediately)")
    parser.add_argument("--mode-label", choices=MODE_LABELS, default=None,
                        help="the engine's LLM_REPLAY_MODE for this run, recorded in the results file (the "
                             "Evals UI applies latency / token / cost targets to live modes only); unset = unknown")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    if args.dry_run:
        return dry_run(args)
    try:
        cases = load_cases(args.cases_dir, args.cases)
    except CaseError as exc:
        print(f"INVALID: {exc}")
        return 2
    problems = validate_cases(cases, args.repo_root)
    if problems:
        print("\n".join(f"INVALID: {p}" for p in problems))
        return 2
    try:
        ts = load_e2e(args.repo_root)
    except ImportError as exc:
        print(f"cannot import the e2e helpers (httpx installed? KEELSON_E2E_DIR?): {exc}", file=sys.stderr)
        return 2
    live = Live(ts, args)
    if args.preflight:
        try:
            return preflight(live)
        except ts.E2EFailure as exc:
            print(f"FAIL: {exc}")
            return 1
    started = dt.datetime.now(dt.UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    results = [run_case(live, case, stamp) for case in cases]
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "keelson_url": ts.KEELSON,
        "merxa_url": ts.MERXA,
        "mode": args.mode_label,
        "runner": "cli",
        "suite_hash": suite_hash(load_cases(args.cases_dir)),
        "cases": [r.to_json() for r in results],
    }
    doc["summary"] = summarise(results)
    json_path, md_path = write_results(args.results_dir, doc, stamp)
    print("\n" + render_markdown(doc))
    print(f"results: {json_path} / {md_path.name}")
    return 0 if all(r.status == "pass" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
