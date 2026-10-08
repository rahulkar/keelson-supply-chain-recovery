#!/usr/bin/env python3
"""M1 thin slice (+ M3 options and canvas), end to end, through the real stack (Caddy, Keycloak, engine, Merxa, LiteLLM).

Runs in a throwaway Python container attached to ``keelson_edge`` so that ``*.localhost``
resolves to Caddy (see e2e/README.md). Uses the REAL Gemini models through LiteLLM.

Story:
 1. reset the Merxa demo scenario as ``merxa.admin`` (golden seed, so runs are repeatable), let
    tenant admin ``sam`` resync Keelson's canonical store (M3: cases are framed from the ``scn``
    snapshot) and disconnect ``marco``'s Merxa link in Keelson (``DELETE /api/connectors/merxa/link``)
 2. Keelson demo login-as ``priya`` → create a case → post the trim-delay message
 3. follow the SSE stream until RUN_FINISHED (timeline of stages / agents / tools / blocks)
 4. approach interrupt → ``confirm`` → follow the resume run (M4: the approach card lists the
    planner's checks and agents; ≥ 3 specialist agents consulted in parallel and the critic ran;
    values refine_scenario left unconfirmed are confirmed first → plan v+1)
 5. expect kpi_strip + recommended_option + plan_diff + narrative (no unfilled placeholders);
    M3: options A–D + Do nothing with scorecards, the canvas blocks (scenario_compare,
    network_map, impact_graph, stockout_timeline, plan_diff) and valid ``data_ref`` payloads
 6. action ``apply`` (option opt-A) → expect an approval interrupt for a human approver
 7. login-as ``marco`` → approve → 412 connect_required → Connect Merxa by scripting the
    Keycloak login form for ``marco.rossi`` → approve with change_set_hash + plan_version
 8. follow the resume run → commit_tracker ``applied`` then ``verified``
 9. Merxa REST (``merxa.admin``): change set applied, attribution names Marco Rossi via Keelson,
    transfer orders ``created_via == change_set:<code>``, audit rows present
10. M3: the worker resyncs the canonical store after the apply (stale mark / webhook)

Exit status 0 = pass; 1 = an expectation failed; 2 = usage / environment error.
Never prints secrets (``DEMO_USER_PASSWORD`` is read from the environment only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

KEELSON = os.environ.get("KEELSON_URL", "http://keelson.localhost").rstrip("/")
MERXA = os.environ.get("MERXA_URL", "http://merxa.localhost").rstrip("/")
PROMPT = (
    "Our trim supplier Meghna Trims says wadding and zips for the Bangladesh POs are delayed 7 days. "
    "What is the impact and what should we do?"
)
PLACEHOLDER_RE = re.compile(r"\{\{|\}\}")
FINAL_TYPES = {"RUN_FINISHED", "RUN_ERROR"}
LEAK_RE = re.compile(r"Traceback|File \"/|\b[A-Z][A-Za-z]+(Error|Exception)\b|postgresql://|redis://")
CONNECTOR = "merxa"
# M3 (docs/contracts/80 §4–§5): default candidates, "Do nothing", the canvas blocks
M3_OPTIONS = ("opt-A", "opt-B", "opt-C", "opt-D")
DO_NOTHING = "do-nothing"
SCORECARD_KEYS = {
    "fill_rate_bp", "lost_units", "lost_margin_cents", "incremental_cost_cents", "cost_breakdown",
    "net_benefit_cents", "shortage_days", "first_breach_shift_days", "tts_gain_days", "changes",
}
CANVAS_KINDS = ("scenario_compare", "network_map", "impact_graph", "stockout_timeline", "plan_diff")
DATA_KINDS = ("network_map", "impact_graph", "stockout_timeline")
# M4 (docs/contracts/90 §2–§5): planner + parallel specialists + critic
SPECIALISTS = ("inventory_demand", "sourcing", "logistics", "playbook")
MIN_SPECIALISTS = 3


class E2EFailure(Exception):
    """An expectation of the thin slice did not hold."""


def expect(cond: Any, message: str) -> None:
    if not cond:
        raise E2EFailure(message)


# ─────────────────────────────── output helpers ───────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.marks: dict[str, float] = {}

    def now(self) -> float:
        return time.monotonic() - self.t0

    def mark(self, name: str) -> float:
        value = self.now()
        self.marks.setdefault(name, value)
        return value


CLOCK = Clock()


def say(msg: str) -> None:
    print(f"[{CLOCK.now():7.1f}s] {msg}", flush=True)


def heading(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def ascii_text(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text.encode("ascii", "replace").decode("ascii")


# ─────────────────────────────── Keelson client ───────────────────────────────


@dataclass
class KeelsonUser:
    username: str
    client: httpx.Client
    csrf: str = ""
    me: dict[str, Any] = field(default_factory=dict)

    def headers(self, idem: bool = False) -> dict[str, str]:
        h = {"X-CSRF-Token": self.csrf, "Origin": KEELSON}
        if idem:
            h["Idempotency-Key"] = str(uuid.uuid4())
        return h


def new_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False)


def keelson_login(username: str) -> KeelsonUser:
    client = new_client()
    r = client.post(f"{KEELSON}/api/demo/login-as", json={"username": username}, headers={"Origin": KEELSON})
    expect(r.status_code == 200, f"login-as {username}: HTTP {r.status_code} {r.text[:300]}")
    expect("keelson_session" in client.cookies, f"login-as {username}: no keelson_session cookie")
    me = client.get(f"{KEELSON}/api/me")
    expect(me.status_code == 200, f"/api/me as {username}: HTTP {me.status_code}")
    body = me.json()
    expect(body["user"]["username"] == username, f"/api/me returned {body['user'].get('username')!r}")
    say(f"signed in to Keelson as {username} (roles {body['user']['roles']}, tenant {body['user']['active_tenant']})")
    return KeelsonUser(username, client, csrf=str(body["csrf_token"]), me=body)


def get_case(user: KeelsonUser, case_id: str) -> dict[str, Any]:
    r = user.client.get(f"{KEELSON}/api/cases/{case_id}")
    expect(r.status_code == 200, f"GET case: HTTP {r.status_code} {r.text[:300]}")
    return dict(r.json())


def blocks_by_kind(case: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for blk in case.get("blocks") or []:
        out.setdefault(str(blk.get("kind")), []).append(blk)
    return out


# ───────────────────────────────── SSE follower ─────────────────────────────────


@dataclass
class RunResult:
    run_id: str
    final: dict[str, Any]
    events: list[dict[str, Any]]
    blocks: dict[str, dict[str, Any]]
    text: str
    last_seq: int

    @property
    def outcome(self) -> dict[str, Any]:
        return dict(self.final.get("outcome") or {})

    @property
    def interrupts(self) -> list[dict[str, Any]]:
        return list(self.outcome.get("interrupts") or [])


def sse_events(user: KeelsonUser, case_id: str, after: int, deadline: float) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(seq, event)``; reconnects with ``Last-Event-ID`` if the stream drops."""
    last = after
    while time.monotonic() < deadline:
        try:
            with user.client.stream(
                "GET",
                f"{KEELSON}/api/cases/{case_id}/events",
                params={"after": last},
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(10.0, read=45.0),
            ) as resp:
                expect(resp.status_code == 200, f"SSE: HTTP {resp.status_code}")
                seq: int | None = None
                data: list[str] = []
                for line in resp.iter_lines():
                    if time.monotonic() > deadline:
                        return
                    if line == "":
                        if data:
                            event = json.loads("\n".join(data))
                            if seq is not None:
                                last = max(last, seq)
                            yield (seq or 0, event)
                        seq, data = None, []
                        continue
                    if line.startswith(":"):
                        continue
                    name, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if name == "id":
                        seq = int(value)
                    elif name == "data":
                        data.append(value)
        except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            say(f"(SSE reconnect after {type(exc).__name__})")
            continue
    raise E2EFailure("timed out waiting for the run to finish")


def follow_run(user: KeelsonUser, case_id: str, run_id: str, after: int, *, timeout_s: float) -> RunResult:
    """Print a readable timeline of one run; return when its RUN_FINISHED / RUN_ERROR arrives."""
    deadline = time.monotonic() + timeout_s
    events: list[dict[str, Any]] = []
    blocks: dict[str, dict[str, Any]] = {}
    text_parts: list[str] = []
    last_seq = after
    for seq, ev in sse_events(user, case_id, after, deadline):
        last_seq = max(last_seq, seq)
        etype = str(ev.get("type"))
        sc = ((ev.get("metadata") or {}).get("sc")) or {}
        ev_run = sc.get("runId") or ev.get("runId")
        if ev_run is not None and ev_run != run_id:
            continue  # an event of another run of the case (replay)
        if seq:  # persisted events of this run must name it (metadata.sc.runId)
            expect(sc.get("runId") == run_id, f"{etype} (seq {seq}) has metadata.sc.runId={sc.get('runId')!r}")
        events.append(ev)
        label = sc.get("label")
        if etype == "STEP_STARTED":
            CLOCK.mark("first_stage")
            say(f"stage   > {ev.get('stepName')}")
        elif etype == "SUBAGENT_STARTED":
            say(f"agent   > {ev.get('description') or ev.get('name')} started")
        elif etype == "SUBAGENT_FINISHED":
            say(f"agent   < {sc.get('agent') or ''} finished")
        elif etype == "SUBAGENT_ERROR":
            say(f"agent   ! {sc.get('agent') or ''} error {ev.get('code')}: {ascii_text(ev.get('message'))}")
            expect(not LEAK_RE.search(str(ev.get("message"))), "SUBAGENT_ERROR message leaks exception text")
        elif etype == "TOOL_CALL_START":
            say(f"tool    > {ascii_text(label or ev.get('toolCallName'))}")
        elif etype == "TOOL_CALL_RESULT":
            say(f"tool    < {ascii_text(ev.get('content'))}")
        elif etype == "ACTIVITY_SNAPSHOT":
            blk = dict(ev.get("content") or {})
            kind = str(blk.get("kind"))
            blocks[kind] = blk
            props = blk.get("props") or {}
            extra = ""
            if kind == "kpi_strip":
                CLOCK.mark("exposure")
                items = props.get("items") or []
                extra = ", ".join(f"{i.get('key')}={i.get('value')}" for i in items[:4])
                if any(i.get("preliminary") for i in items):
                    extra += " (preliminary)"
            elif kind == "recommended_option":
                CLOCK.mark("options")
                extra = f"{(props.get('option') or {}).get('id')} '{(props.get('option') or {}).get('title')}'"
            elif kind == "commit_tracker":
                extra = f"status={props.get('status')} ext_code={props.get('ext_code')}"
                CLOCK.mark(f"commit_{props.get('status')}")
            elif kind == "plan_diff":
                extra = f"lines={(props.get('totals') or {}).get('lines')} units={(props.get('totals') or {}).get('units')}"
            elif kind == "approval_card":
                extra = f"required_roles={props.get('required_roles')}"
            say(f"block   # {kind} [{blk.get('placement')}] {ascii_text(extra)}")
        elif etype == "TEXT_MESSAGE_CONTENT":
            text_parts.append(str(ev.get("delta") or ""))
        elif etype == "TEXT_MESSAGE_END":
            say(f"message : {ascii_text(''.join(text_parts), 300)}")
        elif etype == "CUSTOM":
            say(f"case    ~ {ev.get('name')} {ascii_text(json.dumps(ev.get('value')))}")
        elif etype == "RUN_ERROR":
            error_id = (ev.get("metadata") or {}).get("errorId")
            say(f"RUN_ERROR {ev.get('code')}: {ascii_text(ev.get('message'), 400)} (error_id={error_id})")
            expect(not LEAK_RE.search(str(ev.get("message"))), "RUN_ERROR message leaks exception text")
            record_usage(run_id, ev)
            return RunResult(run_id, ev, events, blocks, "".join(text_parts), last_seq)
        elif etype == "RUN_FINISHED":
            outcome = ev.get("outcome") or {}
            if outcome.get("type") == "interrupt":
                reasons = [f"{i.get('reason')}:{i.get('id')}" for i in outcome.get("interrupts") or []]
                say(f"RUN_FINISHED interrupt {reasons}")
            else:
                say("RUN_FINISHED success")
            record_usage(run_id, ev)
            return RunResult(run_id, ev, events, blocks, "".join(text_parts), last_seq)
    raise E2EFailure("SSE stream ended without RUN_FINISHED")


# ─────────────────────────────── token metering ───────────────────────────────

USAGE_RUNS: list[tuple[str, dict[str, Any]]] = []
"""``(run_id, metadata.usage)`` of every run this script followed (token diet metering)."""
_USAGE_COLS = ("calls", "replayed", "prompt_tokens", "completion_tokens", "reasoning_tokens", "latency_ms")


def _usage_line(name: str, row: dict[str, Any]) -> str:
    cost = row.get("cost_usd")
    cost_text = "     n/a" if cost is None else f"{float(cost):8.4f}"
    return (f"  {name:34s} {int(row.get('calls') or 0):5d} {int(row.get('replayed') or 0):6d} "
            f"{int(row.get('prompt_tokens') or 0):9d} "
            f"{int(row.get('completion_tokens') or 0):9d} {int(row.get('reasoning_tokens') or 0):9d} "
            f"{int(row.get('latency_ms') or 0) / 1000:8.1f} {cost_text}")


def _usage_header() -> str:
    return (f"  {'agent':34s} {'calls':>5s} {'replay':>6s} {'prompt':>9s} {'complet.':>9s} {'reason.':>9s} "
            f"{'wall s':>8s} {'cost $':>8s}")


def record_usage(run_id: str, final: dict[str, Any]) -> None:
    """Print the run's LLM usage (``metadata.usage`` of RUN_FINISHED / RUN_ERROR) and keep it."""
    usage = (final.get("metadata") or {}).get("usage")
    if not isinstance(usage, dict):
        say("usage   ~ (the run reported no metadata.usage)")
        return
    USAGE_RUNS.append((run_id, usage))
    total = usage.get("total") or {}
    cost = total.get("cost_usd")
    say(f"usage   ~ {int(total.get('calls') or 0)} calls ({int(total.get('replayed') or 0)} replayed), "
        f"prompt {int(total.get('prompt_tokens') or 0)}, "
        f"completion {int(total.get('completion_tokens') or 0)} (reasoning {int(total.get('reasoning_tokens') or 0)}), "
        f"cost {'n/a' if cost is None else f'${float(cost):.4f}'} (trace {usage.get('trace_id') or run_id})")


def print_usage_report() -> None:
    """Per-agent usage summed over every run of the case, then the case total."""
    if not USAGE_RUNS:
        return
    heading("LLM usage per agent (all runs of this case)")
    agents: dict[str, dict[str, Any]] = {}
    for _, usage in USAGE_RUNS:
        for name, row in (usage.get("agents") or {}).items():
            acc = agents.setdefault(name, {k: 0 for k in _USAGE_COLS} | {"cost_usd": None})
            for key in _USAGE_COLS:
                acc[key] += int(row.get(key) or 0)
            if row.get("cost_usd") is not None:
                acc["cost_usd"] = (acc["cost_usd"] or 0.0) + float(row["cost_usd"])
    print(_usage_header(), flush=True)
    total: dict[str, Any] = {k: 0 for k in _USAGE_COLS} | {"cost_usd": None}
    for name in sorted(agents, key=lambda n: -agents[n]["prompt_tokens"]):
        print(_usage_line(name, agents[name]), flush=True)
        for key in _USAGE_COLS:
            total[key] += agents[name][key]
        if agents[name]["cost_usd"] is not None:
            total["cost_usd"] = (total["cost_usd"] or 0.0) + agents[name]["cost_usd"]
    print(_usage_line(f"TOTAL ({len(USAGE_RUNS)} runs)", total), flush=True)


def expect_success(res: RunResult, what: str) -> None:
    expect(res.final.get("type") == "RUN_FINISHED", f"{what}: run failed: {res.final.get('code')} {res.final.get('message')}")


# ─────────────────────────────── Keycloak login form ───────────────────────────────


class _LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form" and self.action is None and ("kc-form-login" in a.get("id", "") or a.get("method", "").lower() == "post"):
            self.action = a.get("action")
            self._in_form = True
        elif tag == "input" and self._in_form and a.get("name"):
            self.fields[a["name"]] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def _localhost_secure_context(client: httpx.Client) -> None:
    """Behave like a browser for ``http://*.localhost`` (a secure context).

    Keycloak 26 marks its login cookies ``Secure; SameSite=None`` even over plain HTTP. Browsers
    send them to ``*.localhost`` (treated as potentially trustworthy); Python's cookie jar would
    not, and Keycloak then answers the login POST with 400 "cookie not found"."""
    for cookie in client.cookies.jar:
        if cookie.secure and (cookie.domain == "localhost" or cookie.domain.endswith(".localhost")):
            cookie.secure = False


def connect_merxa(user: KeelsonUser, merxa_username: str, password: str, return_to: str) -> None:
    """Script the delegated Connect-Merxa flow (Keycloak login form for the merxa realm)."""
    client = user.client
    r = client.get(f"{KEELSON}/api/connectors/merxa/connect", params={"return_to": return_to})
    expect(r.status_code == 302, f"connect: expected 302, got {r.status_code} {r.text[:200]}")
    authorize = r.headers["location"]
    expect(authorize.startswith("http://auth.localhost/realms/merxa/"), f"connect: unexpected redirect {authorize[:120]}")
    q = parse_qs(urlsplit(authorize).query)
    expect(q.get("client_id") == ["keelson-connect"], "connect: wrong client_id")
    expect(q.get("code_challenge_method") == ["S256"], "connect: PKCE S256 missing")
    expect("changes:apply" in (q.get("scope") or [""])[0], "connect: changes:apply scope not requested")
    say(f"connect > Keycloak merxa realm authorize (login_hint={q.get('login_hint', ['-'])[0]})")
    page = client.get(authorize, follow_redirects=True)
    expect(page.status_code == 200, f"connect: login page HTTP {page.status_code}")
    _localhost_secure_context(client)
    form = _LoginForm()
    form.feed(page.text)
    expect(form.action, "connect: no login form on the Keycloak page")
    data = dict(form.fields)
    data.update({"username": merxa_username, "password": password})
    data.setdefault("credentialId", "")
    action = urljoin(str(page.url), str(form.action).replace("&amp;", "&"))
    r = client.post(action, data=data)
    hops = 0
    while r.status_code in (301, 302, 303, 307, 308) and hops < 10:
        hops += 1
        _localhost_secure_context(client)
        loc = urljoin(str(r.request.url), r.headers["location"])
        parts = urlsplit(loc)
        if parts.netloc == urlsplit(KEELSON).netloc and parts.path == return_to:
            qs = parse_qs(parts.query)
            expect("connected" in qs, f"connect: callback reported {qs}")
            say(f"connect < linked ({parts.query})")
            return
        r = client.get(loc)
    if r.status_code == 200 and "kc-form-login" in r.text:
        raise E2EFailure("connect: Keycloak rejected the merxa credentials (login form shown again)")
    raise E2EFailure(f"connect: flow did not return to Keelson (HTTP {r.status_code} at {r.request.url})")


# ───────────────────────────────── Merxa REST ─────────────────────────────────


def merxa_login(username: str) -> httpx.Client:
    client = new_client()
    r = client.post(f"{MERXA}/v1/auth/demo/login-as", json={"username": username}, headers={"Origin": MERXA})
    expect(r.status_code == 200, f"merxa login-as {username}: HTTP {r.status_code} {r.text[:200]}")
    return client


def merxa_reset() -> None:
    """Re-seed Merxa from its golden seed (demo director, ``merxa.admin``).

    The seed itself carries the documented story (UK1/NL1 DC + store stock just reach W44), so
    no calibration happens here any more."""
    client = merxa_login("merxa.admin")
    r = client.post(
        f"{MERXA}/v1/admin/scenario:reset",
        headers={"Origin": MERXA, "Idempotency-Key": str(uuid.uuid4())},
        timeout=120.0,
    )
    expect(r.status_code == 200, f"merxa reset: HTTP {r.status_code} {r.text[:200]}")
    say(f"Merxa scenario reset ({r.json().get('profile')})")


def keelson_disconnect_merxa(username: str) -> None:
    """Demo reset on the Keelson side: drop ``username``'s delegated Merxa link (RFC 7009
    revocation at Keycloak + local revoke), so the approval always exercises 412 → Connect."""
    user = keelson_login(username)
    r = user.client.delete(f"{KEELSON}/api/connectors/merxa/link", headers=user.headers())
    expect(r.status_code == 200, f"disconnect merxa for {username}: HTTP {r.status_code} {r.text[:200]}")
    body = r.json()
    expect(body.get("status") == "missing", f"disconnect merxa: {body}")
    expect(body.get("idp_revocation") in ("revoked", "not_needed"), f"IdP revocation failed: {body}")
    link = user.client.get(f"{KEELSON}/api/connectors/merxa/link").json()
    expect(link.get("status") == "missing" and link.get("apply_ready") is False, f"link after disconnect: {link}")
    say(f"Keelson: {username}'s Merxa link disconnected (revoked={body.get('revoked')}, "
        f"idp_revocation={body.get('idp_revocation')})")


def verify_merxa(ext_code: str, approval_id: str | None) -> None:
    client = merxa_login("merxa.admin")
    r = client.get(f"{MERXA}/v1/change-sets/{ext_code}")
    expect(r.status_code == 200, f"merxa change set {ext_code}: HTTP {r.status_code}")
    cs = r.json()
    say(f"merxa   : {cs['code']} status={cs['status']} items={len(cs.get('items') or [])}")
    say(f"merxa   : attribution '{ascii_text(cs.get('attribution_text'))}'")
    expect(cs["status"] == "applied", f"merxa change set status is {cs['status']!r}")
    attribution = str(cs.get("attribution_text") or "")
    expect("Marco Rossi" in attribution and "via Keelson" in attribution,
           f"attribution does not name Marco Rossi via Keelson: {attribution!r}")
    if approval_id:
        expect(cs.get("approval_ref") == approval_id, f"approval_ref {cs.get('approval_ref')!r} != {approval_id!r}")
    expect(all(i.get("status") == "applied" for i in cs.get("items") or []), "not every change set item is applied")
    n_to_items = sum(1 for i in cs.get("items") or [] if i.get("op") == "transfer_order.create")

    tos: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{MERXA}/v1/transfer-orders", params=params)
        expect(r.status_code == 200, f"merxa transfer orders: HTTP {r.status_code}")
        body = r.json()
        tos.extend(body.get("items") or [])
        cursor = body.get("next_cursor")
        if not cursor:
            break
    mine = [t for t in tos if t.get("created_via") == f"change_set:{cs['code']}"]
    say(f"merxa   : {len(mine)} transfer order(s) created_via change_set:{cs['code']} "
        f"({', '.join(str(t.get('code')) for t in mine[:6])})")
    expect(mine, "no transfer orders created by the change set")
    expect(len(mine) == n_to_items, f"{len(mine)} transfer orders but {n_to_items} transfer_order.create items")

    r = client.get(f"{MERXA}/v1/audit-log", params={"change_set_code": cs["code"], "limit": 200})
    expect(r.status_code == 200, f"merxa audit log: HTTP {r.status_code}")
    rows = [row for row in r.json().get("items") or [] if row.get("approval_ref") == cs.get("approval_ref")]
    say(f"merxa   : {len(rows)} audit row(s) for {cs['code']} "
        f"(via={sorted({str(x.get('via')) for x in rows})}, actors={sorted({str(x.get('actor_display')) for x in rows})})")
    expect(rows, "no audit rows for the applied change set")
    expect(all(row.get("via") == "Keelson" for row in rows), "audit rows not marked via Keelson")
    expect(any(row.get("actor_display") == "Marco Rossi" for row in rows), "audit rows do not name Marco Rossi")


# ─────────────────────────── Keelson canonical store (scn sync) ───────────────────────────


def scn_status(admin: KeelsonUser) -> dict[str, Any]:
    r = admin.client.get(f"{KEELSON}/api/admin/connectors/{CONNECTOR}/snapshots", params={"limit": 10})
    expect(r.status_code == 200, f"GET snapshots: HTTP {r.status_code} {r.text[:200]}")
    return dict(r.json())


def current_snapshot_id(admin: KeelsonUser) -> str | None:
    cur = scn_status(admin).get("current") or {}
    return str(cur["id"]) if cur.get("id") else None


def uuid7_ms(value: str) -> int:
    """Creation time (Unix ms) embedded in a UUIDv7 (Keelson snapshot ids)."""
    return int(value.replace("-", "")[:12], 16)


def wait_for_snapshot(admin: KeelsonUser, *, newer_than: str | None, timeout_s: float, why: str,
                      started_after_ms: int | None = None) -> dict[str, Any]:
    """Wait until the connector's current snapshot is a ready one newer than ``newer_than``
    (snapshot ids are UUIDv7, so a later sync sorts after the earlier one) and, with
    ``started_after_ms``, one whose sync STARTED after that moment: a sync already running when
    Merxa was reset read the old data and must not count (the requested one follows it). A sync
    attempt that fails meanwhile fails the test with its stored error code."""
    t0 = time.monotonic()
    deadline = t0 + timeout_s
    while time.monotonic() < deadline:
        body = scn_status(admin)
        cur = body.get("current") or {}
        if cur.get("id") and cur.get("status") == "ready" and (newer_than is None or str(cur["id"]) > newer_than) and (
            started_after_ms is None or uuid7_ms(str(cur["id"])) >= started_after_ms
        ):
            say(f"scn     : snapshot {str(cur['id'])[:8]}... ready ({why}; reason={cur.get('reason')}, "
                f"rows={cur.get('rows')}, sync {int(cur.get('duration_ms') or 0) / 1000:.1f}s, "
                f"waited {time.monotonic() - t0:.0f}s)")
            return dict(cur)
        for item in body.get("items") or []:
            if item.get("status") == "failed" and (newer_than is None or str(item["id"]) > newer_than) and (
                started_after_ms is None or uuid7_ms(str(item["id"])) >= started_after_ms
            ):
                raise E2EFailure(f"scn sync failed ({why}): {item.get('error')}")
        time.sleep(2)
    state = scn_status(admin).get("state")
    raise E2EFailure(f"no new scn snapshot within {timeout_s:.0f}s ({why}); sync state {state}")


def keelson_resync(why: str, *, timeout_s: float = 240.0) -> dict[str, Any]:
    """Demo reset on the Keelson side: the canonical store must reflect the freshly reset Merxa
    data before a case is framed (a Merxa scenario reset is no webhook event; the tier-C poll
    would only notice it within 5 min). Tenant admin ``sam`` queues a sync and waits for it."""
    sam = keelson_login("sam")
    before = current_snapshot_id(sam)
    requested_ms = int(time.time() * 1000)  # after the reset returned: a sync starting now reads the new data
    r = sam.client.post(f"{KEELSON}/api/admin/connectors/{CONNECTOR}/sync", headers=sam.headers())
    expect(r.status_code == 202, f"POST sync: HTTP {r.status_code} {r.text[:200]}")
    expect(r.json().get("status") == "queued", f"POST sync: {r.json()}")
    return wait_for_snapshot(sam, newer_than=before, timeout_s=timeout_s, why=why, started_after_ms=requested_ms)


def verify_resync_after_apply(before: str | None, ext_code: str, *, timeout_s: float,
                              started_after_ms: int | None = None) -> dict[str, Any]:
    """After an apply the engine marks the snapshot stale (Merxa's ``change_set.applied``
    webhook does too); the worker resyncs after the 30 s debounce without anyone asking. Only a
    sync that started after the approval (``started_after_ms``) can contain the applied change."""
    sam = keelson_login("sam")
    t0 = time.monotonic()
    snap = wait_for_snapshot(sam, newer_than=before, timeout_s=timeout_s, why=f"resync after {ext_code}",
                             started_after_ms=started_after_ms)
    reason = str(snap.get("reason") or "")
    expect(not reason.startswith("admin:"), f"the resync after the apply was an admin request ({reason!r})")
    CLOCK.marks["resync_after_apply_s"] = time.monotonic() - t0
    r = sam.client.get(f"{KEELSON}/api/admin/connectors/{CONNECTOR}/events", params={"limit": 50})
    if r.status_code == 200:
        types = [str(e.get("event_type") or e.get("type")) for e in r.json().get("items") or []]
        say(f"webhook : recent connector events {sorted(set(types))}")
    return snap


# ─────────────────────────────── M3 analysis checks ───────────────────────────────


def _contracts() -> Any:
    """``keelson_contracts`` (packages/contracts-py on PYTHONPATH) validates the M3 blocks and
    their ``data_ref`` payloads with the same models the engine emits them with; without it only
    the structural checks below run."""
    try:
        import keelson_contracts.blocks as blocks  # noqa: PLC0415
    except ImportError:
        return None
    return blocks


def fetch_block_data(user: KeelsonUser, blk: dict[str, Any]) -> Any:
    ref = str(blk.get("data_ref") or "")
    expect(ref == f"/api/blocks/{blk['id']}/data", f"{blk['kind']}: data_ref {ref!r} is not /api/blocks/{blk['id']}/data")
    r = user.client.get(f"{KEELSON}{ref}")
    expect(r.status_code == 200, f"{blk['kind']}: GET {ref}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def _check_network_map(props: dict[str, Any], data: dict[str, Any]) -> str:
    nodes, links = data.get("nodes") or [], data.get("links") or []
    expect(nodes and links, "network_map: no nodes or links")
    ids = {n["id"] for n in nodes}
    expect(len(ids) == len(nodes), "network_map: duplicate node ids")
    expect(all(ln["from"] in ids and ln["to"] in ids for ln in links), "network_map: a link names an unknown node")
    expect(all(len(ln.get("path") or []) >= 2 for ln in links), "network_map: a link has fewer than 2 path points")
    w, s, e, n = props["bbox"]
    expect(all(w <= nd["lon_e6"] <= e and s <= nd["lat_e6"] <= n for nd in nodes), "network_map: a node lies outside bbox")
    expect(props["summary"]["nodes"] == len(nodes), "network_map: summary.nodes != len(nodes)")
    at_risk = [nd for nd in nodes if nd["status"] != "ok"]
    expect(at_risk, "network_map: no node is disrupted / impacted / at risk")
    kinds = sorted({ln["kind"] for ln in links})
    sea = [ln for ln in links if ln["kind"] in ("inbound", "sea_air") and len(ln["path"]) > 2]
    expect(sea, "network_map: no sea lane follows catalogued waypoints (data/geo missing in the engine image?)")
    return f"{len(nodes)} nodes ({len(at_risk)} not ok), {len(links)} links {kinds}, {len(sea)} waypoint lanes"


def _check_impact_graph(props: dict[str, Any], data: dict[str, Any]) -> str:
    nodes, edges = data.get("nodes") or [], data.get("edges") or []
    expect(0 < len(nodes) <= 300, f"impact_graph: {len(nodes)} nodes (want 1..300)")
    ids = {n["id"] for n in nodes}
    expect(all(e["source"] in ids and e["target"] in ids for e in edges), "impact_graph: an edge names an unknown node")
    statuses = {n["status"] for n in nodes}
    expect("disrupted" in statuses, f"impact_graph: no disrupted node ({sorted(statuses)})")
    expect(props["summary"]["nodes"] == len(nodes), "impact_graph: summary.nodes != len(nodes)")
    return f"{len(nodes)} nodes, {len(edges)} edges, statuses {sorted(statuses)}"


def _check_timeline(props: dict[str, Any], data: dict[str, Any], option_ids: set[str]) -> str:
    rows = data.get("rows") or []
    weeks = list(props.get("weeks") or [])
    expect(0 < len(rows) <= 40, f"stockout_timeline: {len(rows)} rows (want 1..40)")
    expect(weeks == sorted(weeks), "stockout_timeline: weeks out of order")
    expect({o["id"] for o in props.get("options") or []} <= option_ids, "stockout_timeline: unknown option id in props")
    for row in rows:
        expect({c["week"] for c in row["cells"]} <= set(weeks), f"stockout_timeline: row {row['id']} has a week outside props.weeks")
        expect(set(row.get("by_option") or {}) <= option_ids, f"stockout_timeline: row {row['id']} names an unknown option")
    short = sum(1 for r in rows for c in r["cells"] if c["status"] == "short")
    expect(short, "stockout_timeline: no short cell")
    return f"{len(rows)} rows x {len(weeks)} weeks ({weeks[0]}..{weeks[-1]}), {short} short cells"


def check_m3_analysis(user: KeelsonUser, case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """4 candidate options + Do nothing with scorecards, and the canvas blocks with valid
    ``data_ref`` payloads (80 §4–§5). Returns the scenario_compare options by id."""
    kinds = blocks_by_kind(case)
    for kind in CANVAS_KINDS:
        expect(kind in kinds, f"missing canvas block {kind} (have {sorted(kinds)})")
        expect(kinds[kind][-1].get("placement") == "canvas", f"{kind} placement {kinds[kind][-1].get('placement')!r}")
    for kind in ("kpi_strip", "recommended_option"):
        expect(kinds[kind][-1].get("placement") == "inline", f"{kind} placement {kinds[kind][-1].get('placement')!r}")
    contracts = _contracts()
    if contracts is None:
        say("note: keelson_contracts not importable; structural checks only")

    compare = kinds["scenario_compare"][-1]
    options = {str(o["id"]): o for o in compare["props"]["options"]}
    expect(set(M3_OPTIONS) | {DO_NOTHING} <= set(options), f"scenario_compare options {sorted(options)}")
    for oid, opt in options.items():
        card = opt.get("scorecard") or {}
        missing = SCORECARD_KEYS - set(card)
        expect(not missing, f"option {oid}: scorecard lacks {sorted(missing)}")
        flags = "".join((
            " RECOMMENDED" if opt.get("recommended") else "",
            " guided-only" if opt.get("guided_only") else "",
            " pareto" if opt.get("pareto") else "",
        ))
        say(f"option  : {oid:10s} {ascii_text(opt.get('title'), 32):32s} lost={card['lost_units']:>6} "
            f"cost={card['incremental_cost_cents'] / 100:>10.2f} net={card['net_benefit_cents'] / 100:>11.2f} "
            f"fill={card['fill_rate_bp'] / 100:.2f}% shift={card['first_breach_shift_days']}d "
            f"tts+{card['tts_gain_days']}d changes={card['changes']}{flags}")
    recommended = [oid for oid, o in options.items() if o.get("recommended")]
    expect(len(recommended) == 1, f"{len(recommended)} recommended options")
    rec = kinds["recommended_option"][-1]["props"]
    expect(rec["option"]["id"] == recommended[0], f"recommended_option {rec['option']['id']} != compare {recommended[0]}")
    dn = options[DO_NOTHING]["scorecard"]
    expect(dn["incremental_cost_cents"] == 0 and dn["changes"] == 0, f"do-nothing has cost/changes: {dn}")
    for oid in M3_OPTIONS:
        expect(options[oid]["scorecard"]["lost_units"] <= dn["lost_units"], f"{oid} loses more than doing nothing")

    if contracts is not None:
        for kind in ("scenario_compare", "kpi_strip", "recommended_option", "plan_diff"):
            contracts.parse_block(kinds[kind][-1])
    for kind in DATA_KINDS:
        blk = kinds[kind][-1]
        if contracts is not None:
            contracts.parse_block(blk)
        data = fetch_block_data(user, blk)
        if contracts is not None:
            contracts.parse_block_data(contracts.block_key(kind, int(blk.get("v") or 1)), data)
        if kind == "network_map":
            summary = _check_network_map(blk["props"], data)
        elif kind == "impact_graph":
            summary = _check_impact_graph(blk["props"], data)
        else:
            summary = _check_timeline(blk["props"], data, set(options))
        say(f"canvas  : {kind}: {summary}{' (contract-valid)' if contracts is not None else ''}")
    diff = kinds["plan_diff"][-1]["props"]
    say(f"canvas  : plan_diff {diff.get('option_id') or ''} totals {diff.get('totals')}")
    return options


# ─────────────────────────────────── the slice ───────────────────────────────────


@dataclass
class Analysis:
    case_id: str
    plan_version: int
    case: dict[str, Any]
    options: dict[str, dict[str, Any]]
    recommended: str
    t_msg: float
    last_seq: int
    agents: list[str] = field(default_factory=list)
    approach: dict[str, Any] = field(default_factory=dict)


def agents_started(*results: RunResult) -> list[str]:
    """Agent slugs (``SUBAGENT_STARTED.name``) of the runs, in order, without repeats."""
    out: list[str] = []
    for res in results:
        for ev in res.events:
            if ev.get("type") == "SUBAGENT_STARTED" and str(ev.get("name")) not in out:
                out.append(str(ev.get("name")))
    return out


def check_approach_card(card: dict[str, Any], *, mode: str | None = None) -> dict[str, Any]:
    """M4 approach card (90 §2): one plain sentence per plan task and the agents to be consulted."""
    props = dict(card.get("props") or {})
    checks = [c for c in props.get("checks") or [] if isinstance(c, dict)]
    agents = [a for a in props.get("agents") or [] if isinstance(a, dict)]
    say(f"approach: mode={props.get('mode')} checks={len(checks)} agents="
        f"{[a.get('display_name') for a in agents]} buttons={props.get('buttons')}")
    for c in checks:
        say(f"  check {c.get('task_id')}: {ascii_text(c.get('sentence'), 140)}")
    expect(checks, "approach card has no plan checks (planner output missing)")
    expect(all(str(c.get("sentence") or "").strip() for c in checks), "an approach check has no sentence")
    expect(agents, "approach card lists no agents to consult")
    expect(not PLACEHOLDER_RE.search(json.dumps(checks)), "approach checks contain placeholders")
    if mode is not None:
        expect(props.get("mode") == mode, f"approach card mode {props.get('mode')!r}, expected {mode!r}")
    return props


def reset_demo(args: argparse.Namespace) -> None:
    heading("Reset the demo")
    if args.reset_merxa:
        merxa_reset()
        CLOCK.mark("resync_requested")
        keelson_resync("after the Merxa reset", timeout_s=args.sync_timeout)
        CLOCK.marks["resync_after_reset_s"] = CLOCK.now() - CLOCK.marks["resync_requested"]
    else:
        say("Merxa reset skipped (--no-reset-merxa)")
    keelson_disconnect_merxa("marco")


def typed_edit_approach(args: argparse.Namespace, priya: KeelsonUser, case_id: str, res: RunResult,
                        props: dict[str, Any], mode: str | None) -> tuple[RunResult, dict[str, Any]]:
    """M4 typed **Edit** (contract ``ResumePayload``): change the card's first day / percent value
    with ``edits: [{key, value_int}]`` exactly as the web sends it, follow the re-framing run and
    expect a new approach card carrying the edited value."""
    heading("Planner: edit the approach (typed edit)")
    approach = [i for i in res.interrupts if i.get("reason") == "approach"]
    expect(approach, f"typed edit: no approach interrupt (have {res.interrupts})")
    editable = [e for e in props.get("editable") or []
                if e.get("kind") in ("days", "pct") and isinstance(e.get("value_int"), int)]
    expect(editable, f"the approach card offers no editable day / percent value (editable={props.get('editable')})")
    target = editable[0]
    new_value = int(target["value_int"]) + (7 if target["kind"] == "days" else 10)
    body = {"decision": "edit", "edits": [{"key": str(target["key"]), "value_int": new_value}]}
    say(f"edit {target['key']}: {target['value_int']} -> {new_value}")
    CLOCK.mark("edit_requested")
    r = priya.client.post(f"{KEELSON}/api/interrupts/{approach[0]['id']}/decision", json=body, headers=priya.headers())
    expect(r.status_code == 202, f"typed edit: HTTP {r.status_code} {r.text[:300]}")
    expect(r.json().get("status") == "resumed", f"typed edit: {r.json()}")
    res = follow_run(priya, case_id, str(r.json()["run_id"]), res.last_seq, timeout_s=args.run_timeout)
    expect_success(res, "typed edit resume run")
    CLOCK.mark("edit_done")
    expect("approach_card" in res.blocks, f"the typed edit produced no new approach card (have {sorted(res.blocks)})")
    edited = check_approach_card(res.blocks["approach_card"], mode=mode)
    value = next((e.get("value_int") for e in edited.get("editable") or [] if e.get("key") == target["key"]), None)
    expect(value == new_value, f"the new approach card shows {target['key']}={value}, expected {new_value}")
    return res, edited


def analyse(args: argparse.Namespace, *, title: str, prompt: str = PROMPT, mode: str | None = None,
            typed_edit: bool = False) -> Analysis:
    """Planner ``priya`` reports the disruption, confirms the approach and gets the options
    (``typed_edit``: first change one approach value with a typed Edit, then confirm)."""
    heading("Planner: report the disruption")
    priya = keelson_login("priya")
    r = priya.client.post(f"{KEELSON}/api/cases", json={"title": title}, headers=priya.headers())
    expect(r.status_code == 201, f"create case: HTTP {r.status_code} {r.text[:200]}")
    case_id = str(r.json()["id"])
    say(f"case {case_id}")
    after = int(get_case(priya, case_id).get("last_event_seq") or 0)
    t_msg = CLOCK.mark("message_posted")
    r = priya.client.post(f"{KEELSON}/api/cases/{case_id}/messages", json={"text": prompt}, headers=priya.headers(idem=True))
    expect(r.status_code == 202, f"post message: HTTP {r.status_code} {r.text[:200]}")
    res = follow_run(priya, case_id, str(r.json()["run_id"]), after, timeout_s=args.run_timeout)
    expect_success(res, "chat run")
    runs = [res]
    approach_props: dict[str, Any] = {}
    if "approach_card" in res.blocks:
        approach_props = check_approach_card(res.blocks["approach_card"], mode=mode)

    if typed_edit:
        expect(res.outcome.get("type") == "interrupt", "typed edit: the approach was confirmed automatically")
        res, approach_props = typed_edit_approach(args, priya, case_id, res, approach_props, mode)
        runs.append(res)

    if res.outcome.get("type") == "interrupt":
        approach = [i for i in res.interrupts if i.get("reason") == "approach"]
        expect(approach, f"unexpected interrupt(s): {res.interrupts}")
        expect("approach_card" in res.blocks, "approach interrupt without an approach_card block")
        heading("Planner: confirm the approach")
        r = priya.client.post(
            f"{KEELSON}/api/interrupts/{approach[0]['id']}/decision", json={"decision": "confirm"}, headers=priya.headers()
        )
        expect(r.status_code == 202, f"confirm approach: HTTP {r.status_code} {r.text[:300]}")
        expect(r.json().get("status") == "resumed", f"confirm approach: {r.json()}")
        res = follow_run(priya, case_id, str(r.json()["run_id"]), res.last_seq, timeout_s=args.run_timeout)
        expect_success(res, "approach resume run")
        runs.append(res)
    else:
        say("approach auto-confirmed (no interrupt)")
    expect(res.outcome.get("type") == "success", f"analysis run ended with {res.outcome}")
    CLOCK.mark("analysis_done")
    agents = agents_started(*runs)
    say(f"agents consulted: {agents}")

    heading("Check the analysis")
    case = get_case(priya, case_id)
    kinds = blocks_by_kind(case)
    for kind in ("kpi_strip", "recommended_option", "plan_diff", "narrative"):
        expect(kind in kinds, f"missing {kind} block (have {sorted(kinds)})")
    kpi_final = [b for b in kinds["kpi_strip"] if not any(i.get("preliminary") for i in b["props"]["items"])]
    expect(kpi_final, "no final (non-preliminary) kpi_strip block")
    say("kpi_strip: " + ", ".join(f"{i.get('key')}={i.get('value')}" for i in kpi_final[-1]["props"]["items"]))
    rec = kinds["recommended_option"][-1]["props"]
    plan_version = int(rec["plan_version"])
    option_ids = [rec["option"]["id"]] + [a["id"] for a in rec.get("alternatives") or []]
    say(f"plan v{plan_version}: recommended {rec['option']['id']} '{rec['option']['title']}', options {option_ids}")
    narrative = str(kinds["narrative"][-1]["props"]["markdown"])
    say(f"narrative: {ascii_text(narrative, 400)}")
    expect(not PLACEHOLDER_RE.search(narrative), "narrative contains unfilled placeholders")
    assistant = [m for m in case.get("messages") or [] if m.get("role") == "assistant"]
    expect(assistant, "no assistant message stored")
    expect(not PLACEHOLDER_RE.search(str(assistant[-1].get("text"))), "assistant message contains unfilled placeholders")
    if "unverified" in narrative.lower():
        say("note: the narrative contains a sentence marked unverified by the numeric linter")
    options = check_m3_analysis(priya, case)
    return Analysis(case_id, plan_version, case, options, str(rec["option"]["id"]), t_msg,
                    int(case.get("last_event_seq") or res.last_seq), agents, approach_props)


def check_m4_investigation(an: Analysis, *, min_specialists: int = MIN_SPECIALISTS) -> None:
    """Planner + parallel specialists + critic (90 §2–§5): the approach card listed the plan's
    checks, at least ``min_specialists`` specialist agents were consulted and the critic ran."""
    heading("Check the investigation (M4)")
    expect(an.approach, "no approach_card block in the analysis")
    planned = [str(a.get("slug")) for a in an.approach.get("agents") or []]
    consulted = [a for a in an.agents if a in SPECIALISTS]
    say(f"planned agents {planned}; specialists consulted {consulted}")
    expect(len(consulted) >= min_specialists,
           f"only {len(consulted)} specialist agent(s) consulted ({consulted}); expected >= {min_specialists}")
    expect(set(consulted) <= set(planned), f"consulted specialists {consulted} not on the approach card {planned}")
    checks = [c for c in an.approach.get("checks") or [] if isinstance(c, dict)]
    expect(len(checks) >= min_specialists, f"the approach card lists {len(checks)} checks")
    expect("critic" in an.agents, f"the critic did not run (agents {an.agents})")
    expect("recommender" in an.agents, f"the strategist did not run (agents {an.agents})")


def apply_action(rec: dict[str, Any]) -> dict[str, Any]:
    return next((a for a in rec.get("actions") or [] if a.get("type") == "apply"), {})


def confirm_pending_values(args: argparse.Namespace, an: Analysis) -> Analysis:
    """If refine_scenario left inference-backed values unconfirmed (90 §5: Apply disabled with
    "Confirm before applying"), the planner confirms them (case action ``confirm_assumptions``):
    impact + simulate re-run (no new investigation) and plan v+1 enables Apply."""
    priya = keelson_login("priya")
    case = get_case(priya, an.case_id)
    rec = blocks_by_kind(case)["recommended_option"][-1]["props"]
    pending = list(rec.get("unconfirmed") or [])
    apply = apply_action(rec)
    if not pending:
        if rec["option"]["id"] != DO_NOTHING:
            expect(apply.get("enabled") is True, f"Apply is disabled without unconfirmed values: {apply}")
        say("no unconfirmed values: Apply is enabled")
        return an
    heading("Planner: confirm the values the plan relies on")
    for u in pending:
        say(f"unconfirmed: {u.get('key')} = {ascii_text(u.get('value_text'))} ({ascii_text(u.get('source'))})")
    expect(apply.get("enabled") is False and "Confirm before applying" in str(apply.get("disabled_reason")),
           f"Apply should be disabled until the values are confirmed: {apply}")
    expect(any(a.get("type") == "confirm_assumptions" and a.get("enabled") for a in rec.get("actions") or []),
           "no enabled confirm_assumptions action offered")
    r = priya.client.post(
        f"{KEELSON}/api/cases/{an.case_id}/actions",
        json={"type": "confirm_assumptions", "option_id": rec["option"]["id"], "plan_version": an.plan_version,
              "assumption_keys": [str(u["key"]) for u in pending]},
        headers=priya.headers(idem=True),
    )
    expect(r.status_code == 202, f"confirm_assumptions: HTTP {r.status_code} {r.text[:300]}")
    res = follow_run(priya, an.case_id, str(r.json()["run_id"]), an.last_seq, timeout_s=args.run_timeout)
    expect_success(res, "confirm run")
    again = [a for a in agents_started(res) if a in SPECIALISTS]
    expect(not again, f"confirming values re-ran specialists {again} (only impact + simulate should run)")
    case = get_case(priya, an.case_id)
    rec2 = blocks_by_kind(case)["recommended_option"][-1]["props"]
    expect(int(rec2["plan_version"]) == an.plan_version + 1, f"plan v{rec2['plan_version']} after confirming")
    expect(not rec2.get("unconfirmed"), f"values still unconfirmed: {rec2.get('unconfirmed')}")
    if rec2["option"]["id"] != DO_NOTHING:
        expect(apply_action(rec2).get("enabled") is True, f"Apply still disabled after confirming: {apply_action(rec2)}")
    options = check_m3_analysis(priya, case)
    say(f"plan v{rec2['plan_version']}: recommended {rec2['option']['id']} after confirming")
    return Analysis(an.case_id, int(rec2["plan_version"]), case, options, str(rec2["option"]["id"]), an.t_msg,
                    int(case.get("last_event_seq") or res.last_seq), an.agents, an.approach)


@dataclass
class Approval:
    interrupt_id: str
    change_set_hash: str
    plan_version: int
    summary: Any


def request_apply(args: argparse.Namespace, an: Analysis, option_id: str) -> Approval:
    heading(f"Planner: apply option {option_id}")
    priya = keelson_login("priya")
    CLOCK.mark("apply_requested")
    r = priya.client.post(
        f"{KEELSON}/api/cases/{an.case_id}/actions",
        json={"type": "apply", "option_id": option_id, "plan_version": an.plan_version},
        headers=priya.headers(idem=True),
    )
    expect(r.status_code == 202, f"apply action: HTTP {r.status_code} {r.text[:300]}")
    res = follow_run(priya, an.case_id, str(r.json()["run_id"]), an.last_seq, timeout_s=args.run_timeout)
    expect_success(res, "apply run")
    approvals = [i for i in res.interrupts if i.get("reason") == "approval"]
    expect(approvals, f"expected a human approval interrupt, got outcome {res.outcome}")
    card = res.blocks.get("approval_card") or {}
    expect(card, "approval interrupt without an approval_card block")
    props = card["props"]
    expect("approver" in (props.get("required_roles") or []), f"approval card roles {props.get('required_roles')}")
    interrupt_id = str(approvals[0]["id"])
    expect(props.get("interrupt_id") == interrupt_id, "approval card interrupt id mismatch")
    say(f"approval needed: interrupt {interrupt_id} hash {str(props['change_set_hash'])[:12]}... "
        f"plan v{props['plan_version']} summary {ascii_text(props.get('summary'), 300)}")
    CLOCK.mark("approval_requested")
    return Approval(interrupt_id, str(props["change_set_hash"]), int(props["plan_version"]), props.get("summary"))


def approve_and_commit(args: argparse.Namespace, an: Analysis, approval: Approval) -> str:
    """Approver ``marco``: 412 connect_required -> Connect Merxa -> approve -> commit verified.
    Returns the Merxa change set code."""
    heading("Approver: connect Merxa and approve")
    password = os.environ.get("DEMO_USER_PASSWORD", "")
    marco = keelson_login("marco")
    body = {"decision": "approve", "plan_version": approval.plan_version, "change_set_hash": approval.change_set_hash}
    # The viewer's own link decides (the card's connect prop describes the initiator).
    before = marco.client.get(f"{KEELSON}/api/connectors/merxa/link").json()
    say(f"link before: status={before.get('status')} apply_ready={before.get('apply_ready')}")
    expect(before.get("apply_ready") is False, f"marco should be disconnected after the demo reset: {before}")
    r = marco.client.post(f"{KEELSON}/api/interrupts/{approval.interrupt_id}/decision", json=body, headers=marco.headers())
    expect(r.status_code == 412 and r.json().get("code") == "connect_required",
           f"approve before connecting: expected 412 connect_required, got {r.status_code} {r.text[:200]}")
    expect("/api/connectors/merxa/connect" in str(r.json().get("connect_url") or ""), "412 without a connect_url")
    say("approve -> 412 connect_required (approver has no Merxa link yet)")
    connect_merxa(marco, "marco.rossi", password, f"/cases/{an.case_id}")
    link = marco.client.get(f"{KEELSON}/api/connectors/merxa/link").json()
    say(f"link: status={link.get('status')} ext_username={link.get('ext_username')} scopes={link.get('scopes')}")
    expect(link.get("status") == "active" and link.get("apply_ready") is True, f"link not ready to approve: {link}")
    after = int(get_case(marco, an.case_id).get("last_event_seq") or an.last_seq)
    CLOCK.mark("approved")
    r = marco.client.post(f"{KEELSON}/api/interrupts/{approval.interrupt_id}/decision", json=body, headers=marco.headers())
    expect(r.status_code == 202, f"approve: HTTP {r.status_code} {r.text[:300]}")
    expect(r.json().get("status") == "resumed", f"approve: {r.json()}")
    res = follow_run(marco, an.case_id, str(r.json()["run_id"]), after, timeout_s=args.run_timeout)
    expect_success(res, "commit run")
    expect(res.outcome.get("type") == "success", f"commit run ended with {res.outcome}")
    statuses = [
        ((e.get("content") or {}).get("props") or {}).get("status")
        for e in res.events
        if e.get("type") == "ACTIVITY_SNAPSHOT" and (e.get("content") or {}).get("kind") == "commit_tracker"
    ]
    say(f"commit_tracker statuses: {statuses}")
    expect("applied" in statuses, "commit_tracker never reported applied")
    expect(statuses and statuses[-1] == "verified", "commit_tracker did not end verified")
    tracker = res.blocks["commit_tracker"]["props"]
    ext_code = str(tracker["ext_code"])
    expect(tracker.get("ext_url") == f"{MERXA}/change-batches/{ext_code}", f"ext_url {tracker.get('ext_url')}")
    CLOCK.mark("verified")
    case = get_case(marco, an.case_id)
    say(f"case status: {case['case']['status']}")
    expect(case["case"]["status"] == "committed", f"case status {case['case']['status']!r}, expected committed")
    expect(not case.get("open_interrupts"), "open interrupts remain after commit")
    return ext_code


def timings(an: Analysis) -> dict[str, float]:
    m = CLOCK.marks
    out: dict[str, float] = {}
    if "resync_after_reset_s" in m:
        out["resync_after_reset_s"] = m["resync_after_reset_s"]
    out.update({
        "first_stage_s": m.get("first_stage", 0) - an.t_msg,
        "exposure_s": m.get("exposure", 0) - an.t_msg,
        "options_s": m.get("options", 0) - an.t_msg,
        "analysis_total_s": m["analysis_done"] - an.t_msg,
        "apply_to_approval_card_s": m["approval_requested"] - m["apply_requested"],
        "commit_s": m["verified"] - m["approved"],
    })
    if "resync_after_apply_s" in m:
        out["resync_after_apply_s"] = m["resync_after_apply_s"]
    out["total_s"] = CLOCK.now()
    return out


def run_slice(args: argparse.Namespace) -> dict[str, float]:
    if not os.environ.get("DEMO_USER_PASSWORD", ""):
        print("DEMO_USER_PASSWORD is not set", file=sys.stderr)
        raise SystemExit(2)
    reset_demo(args)
    an = analyse(args, title="E2E trim delay")
    check_m4_investigation(an)
    an = confirm_pending_values(args, an)
    approval = request_apply(args, an, "opt-A")
    snap_before = current_snapshot_id(keelson_login("sam"))
    approved_ms = int(time.time() * 1000)
    ext_code = approve_and_commit(args, an, approval)
    heading("Merxa: read back")
    verify_merxa(ext_code, None)
    if args.check_resync:
        heading("Keelson: resync after the apply")
        verify_resync_after_apply(snap_before, ext_code, timeout_s=args.sync_timeout, started_after_ms=approved_ms)
    return timings(an)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-reset-merxa", dest="reset_merxa", action="store_false",
                        help="do not reset the Merxa demo scenario first (the story needs the golden seed)")
    parser.add_argument("--run-timeout", type=float, default=420.0, help="seconds to wait for one run")
    parser.add_argument("--sync-timeout", type=float, default=240.0, help="seconds to wait for one scn sync")
    parser.add_argument("--no-resync-check", dest="check_resync", action="store_false",
                        help="do not wait for the worker's resync after the apply")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_args(parser)
    args = parser.parse_args()
    try:
        result = run_slice(args)
    except E2EFailure as exc:
        print_usage_report()
        print(f"\nFAIL: {exc}", flush=True)
        return 1
    except httpx.HTTPError as exc:
        print_usage_report()
        print(f"\nFAIL: HTTP error {type(exc).__name__}: {exc}", flush=True)
        return 1
    print_usage_report()
    heading("PASS")
    for key, value in result.items():
        print(f"  {key:26s} {value:7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
