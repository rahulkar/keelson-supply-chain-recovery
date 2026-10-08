"""Alerts inbox, engine side (``docs/contracts/100-m6-signals.md`` §5, §6).

The Signal Hub owns alerts (rules, exposure, hysteresis, auto-resolve); the engine proxies them to
the web (``api/routes_alerts.py``), opens cases from them and fans out live updates.

**One case per alert** (:func:`open_case`): ``alert_case`` (primary key ``(tenant, alert_id)``,
advisory-locked) maps an alert to its case; the case, its queued ``signal`` run
(``{event_id, alert_id}``, idempotency key ``alert:<id>``) and the mapping are written in one
transaction. The hub is then told ``opened {case_id}``; if that call fails the mapping stays
``hub_synced = false`` and the watcher repeats it. The case title is built deterministically from
the event's structured subtype and the exposure counts — never from the (possibly news-derived,
untrusted) alert title.

**Live channel — choice: the engine polls the hub.** The contract defines no hub → engine callback,
and a poller keeps the hub free of any engine address or reverse credential. :class:`AlertWatcher`
(one task per engine-api process, started by the app lifespan) polls ``GET /v1/alerts`` every
``ALERTS_POLL_SECONDS`` for each tenant with an **active** internal Signal Hub connector. A Valkey
lock per tenant makes one replica the poller per interval. Differences against the per-tenant state
(Valkey hash) become AG-UI ``CUSTOM`` events appended to the Valkey stream
``keelson:alerts:<tenant>`` (MAXLEN ~ ``ALERTS_STREAM_MAXLEN``, TTL 24 h), which
``GET /api/alerts/events`` tails (``id`` = stream entry id, so ``Last-Event-ID`` resumes):

* ``sc.alert.raised`` ``{alert, renotify}`` — a new alert, or (``renotify: true``) an existing one
  whose severity band rose (the hub's hysteresis decides when severity changes at all);
* ``sc.alert.updated`` ``{alert}`` — status, severity, case or ``updated_at`` changed;
* ``sc.alert.removed`` ``{alert_id}`` — gone from the hub's (complete) list, e.g. a hub demo reset;
* ``sc.alert.count`` ``{new}`` — the number of ``new`` alerts changed (Home pill);
* ``sc.alert.case_opened`` ``{alert_id, case_id, run_id, auto}``.

The first poll after an empty state (process start with a flushed Valkey, demo reset) only primes
the state: no burst of ``raised`` events for old alerts.

**Auto-open** (``alert_and_draft_case``): the hub's ``Alert`` carries no action, so the engine
re-evaluates the tenant's rules deterministically (``keelson_contracts.signals.decide_alert_action``
on the event's ``severity_bp``/``confidence_bp``/``kind`` and the alert's exposure counts; rules from
the tenant config ``alert_rules``, else ``DEFAULT_ALERT_RULES``). Only a ``new`` alert without a
case is considered, once per alert version (``updated_at``); a dismissed/snoozed/seen alert is never
auto-opened, and neither is an event backed only by news inferences (:func:`auto_open_allowed`:
untrusted text never starts a run on its own). The case is opened as ``system:signal-hub`` (no
roles: never auto-approved) and its run stops at the approach card like any non-chat origin. Nothing is ever committed automatically.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from keelson_contracts.agui import validate_event
from keelson_contracts.signals import (
    DEFAULT_ALERT_RULES,
    Alert,
    AlertDetail,
    AlertRule,
    AlertStatusUpdate,
    decide_alert_action,
)
from keelson_engine.config import get_settings
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.signal_hub import is_hub_id

__all__ = [
    "ALERT_CASE_OPENED",
    "ALERT_COUNT",
    "ALERT_EVENT_NAMES",
    "ALERT_RAISED",
    "ALERT_REMOVED",
    "ALERT_UPDATED",
    "AUTO_ACTOR",
    "AlertNotFound",
    "AlertWatcher",
    "OpenCaseResult",
    "alert_stream_key",
    "alert_valkey_keys",
    "apply_local",
    "auto_open_allowed",
    "case_title",
    "cases_for",
    "custom_event",
    "custom_rules",
    "new_count",
    "open_case",
    "poke",
    "poll_tenant",
    "publish",
    "rules_for",
    "stream_tip",
    "tail",
    "wants_draft_case",
]

log = get_logger(__name__)

ALERT_RAISED: Final = "sc.alert.raised"
ALERT_UPDATED: Final = "sc.alert.updated"
ALERT_REMOVED: Final = "sc.alert.removed"
ALERT_COUNT: Final = "sc.alert.count"
ALERT_CASE_OPENED: Final = "sc.alert.case_opened"
ALERT_EVENT_NAMES: Final = (ALERT_RAISED, ALERT_UPDATED, ALERT_REMOVED, ALERT_COUNT, ALERT_CASE_OPENED)

AUTO_ACTOR: Final[dict[str, Any]] = {"sub": "system:signal-hub", "username": "signal-hub", "name": "Signal Hub",
                                     "roles": []}
"""The principal an auto-opened case/run belongs to: no roles (never auto-approved)."""

STREAM_TTL_S: Final = 24 * 3600
_SEVERITY_RANK: Final = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_STREAM_ID_RE: Final = re.compile(r"^\d{1,20}-\d{1,20}$")
_COUNT_FIELD: Final = "__count"


class AlertNotFound(LookupError):
    pass


# ───────────────────────────── Valkey keys / events ─────────────────────────────


def alert_stream_key(tenant: str) -> str:
    return f"keelson:alerts:{tenant}"


def _state_key(tenant: str) -> str:
    return f"keelson:alerts:{tenant}:state"


def _primed_key(tenant: str) -> str:
    return f"keelson:alerts:{tenant}:primed"


def _poll_lock_key(tenant: str) -> str:
    return f"keelson:alerts:{tenant}:poll"


def alert_valkey_keys(tenant: str) -> list[str]:
    """Every Valkey key of the tenant's alert channel (demo reset deletes them)."""
    return [alert_stream_key(tenant), _state_key(tenant), _primed_key(tenant), _poll_lock_key(tenant)]


def custom_event(name: str, value: Any) -> dict[str, Any]:
    """AG-UI 1.0 ``CUSTOM {name, value, timestamp}`` (tenant-level: no ``metadata.sc``, which names a case)."""
    event = {"type": "CUSTOM", "name": name, "value": value, "timestamp": time.time_ns() // 1_000_000}
    validate_event(event)
    return event


async def publish(tenant: str, name: str, value: Any) -> str | None:
    """Append one event to the tenant's alert stream; returns the entry id (``None`` when Valkey is down)."""
    from keelson_engine.core.valkey import get_valkey

    event = custom_event(name, value)
    key = alert_stream_key(tenant)
    try:
        pipe = get_valkey().pipeline(transaction=False)
        pipe.xadd(key, {"data": json.dumps(event, ensure_ascii=False, allow_nan=False)},
                  maxlen=get_settings().alerts_stream_maxlen, approximate=True)
        pipe.expire(key, STREAM_TTL_S)
        res = await pipe.execute()
    except (RedisError, OSError) as exc:
        log.warning("alert event not published", tenant=tenant, event=name, error=type(exc).__name__)
        return None
    return str(res[0])


async def stream_tip(tenant: str) -> str | None:
    """The id of the stream's newest entry (``"0-0"`` when empty; ``None`` when Valkey is down)."""
    from keelson_engine.core.valkey import get_valkey

    try:
        tip = await get_valkey().xrevrange(alert_stream_key(tenant), count=1)
    except (RedisError, OSError):
        return None
    return str(tip[0][0]) if tip else "0-0"


async def tail(tenant: str, after: str | None) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Follow the tenant's alert stream from ``after`` (a stream entry id; default: the current tip).
    Never returns on its own; Valkey outages are retried."""
    from keelson_engine.core.valkey import get_valkey

    key = alert_stream_key(tenant)
    last: str | None = after if after and _STREAM_ID_RE.match(after) else None
    while True:
        try:
            if last is None:
                tip = await get_valkey().xrevrange(key, count=1)
                last = str(tip[0][0]) if tip else "0-0"
            resp: Any = await get_valkey().xread({key: last}, count=100, block=5000)
        except (RedisError, OSError) as exc:
            log.warning("alert stream read failed", tenant=tenant, error=type(exc).__name__)
            await asyncio.sleep(get_settings().alerts_poll_seconds)
            continue
        for stream_name, items in resp or []:
            if str(stream_name) != key:
                continue
            for entry_id, fields in items:
                last = str(entry_id)
                try:
                    yield last, json.loads(dict(fields).get("data") or "{}")
                except json.JSONDecodeError:
                    log.warning("dropping malformed alert event", tenant=tenant, entry=last)


# ───────────────────────────── alert ↔ case ─────────────────────────────


async def cases_for(tenant: str, alert_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """``alert_id → {case_id, run_id, auto, hub_synced}`` for the given alerts (local mapping)."""
    ids = sorted({a for a in alert_ids if is_hub_id(a)})
    if not ids:
        return {}
    async with tenant_session(tenant) as s:
        rows = (await s.execute(
            text("SELECT alert_id::text AS alert_id, event_id::text AS event_id, case_id, run_id, auto, hub_synced "
                 "FROM alert_case WHERE alert_id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )).mappings().all()
    return {str(r["alert_id"]): dict(r) for r in rows}


def apply_local(alert: Alert, mapping: Mapping[str, Any] | None) -> Alert:
    """The alert as the inbox shows it: a case opened here wins over a hub status that was not
    updated yet (``opened`` + ``case_id``)."""
    if mapping is None or alert.case_id is not None:
        return alert  # the hub knows the case: its status (e.g. resolved later) is authoritative
    case_id = str(mapping["case_id"])
    if alert.status == "resolved":
        return alert.model_copy(update={"case_id": case_id})
    return alert.model_copy(update={"status": "opened", "case_id": case_id})


def new_count(alerts: Iterable[Alert]) -> int:
    return sum(1 for a in alerts if a.status == "new")


# ───────────────────────────── rules / auto-open ─────────────────────────────


async def rules_for(tenant: str) -> tuple[AlertRule, ...]:
    """The tenant's alert rules: ``tenant.config.alert_rules`` (tenants.yaml), else the defaults.
    An invalid rule set falls back to the defaults (logged) — never to "no rules"."""
    custom = await custom_rules(tenant)
    return custom or DEFAULT_ALERT_RULES


async def custom_rules(tenant: str) -> tuple[AlertRule, ...]:
    """The tenant's own valid ``alert_rules`` (``()`` = none, or an invalid set: the defaults apply).
    Also pushed to the hub with the tenant network (``core.network``) so both sides use one set."""
    async with system_session() as s:
        cfg = (await s.execute(text("SELECT config FROM tenant WHERE slug = :t"), {"t": tenant})).scalar_one_or_none()
    raw = cfg.get("alert_rules") if isinstance(cfg, dict) else None
    if not raw:
        return ()
    try:
        if not isinstance(raw, list):
            raise TypeError("alert_rules must be a list")
        rules = tuple(AlertRule.model_validate(r) for r in raw)
        if len({r.id for r in rules}) != len(rules):
            raise ValueError("alert rule ids must be unique")
        return rules
    except (ValidationError, TypeError, ValueError) as exc:
        log.warning("invalid tenant alert rules; using the defaults", tenant=tenant, error=type(exc).__name__)
        return ()


def wants_draft_case(detail: AlertDetail, rules: Iterable[AlertRule]) -> bool:
    """True when the tenant's rules give ``alert_and_draft_case`` for this alert's event + exposure."""
    decision = decide_alert_action(rules, severity_bp=detail.event.severity_bp,
                                   confidence_bp=detail.event.confidence_bp, kind=detail.event.kind,
                                   counts=detail.alert.exposure_summary.nodes_by_type)
    return decision is not None and decision[0] == "alert_and_draft_case"


def auto_open_allowed(detail: AlertDetail) -> bool:
    """Only an event backed by a structured signal may open a case on its own: at least one member
    signal with provenance ``fact`` (feeds, connector-event fields) or ``simulated`` (demo packs) —
    or, when the hub sent no member signals, an authoritative anchor. A news-only event (every
    signal an LLM ``inference`` from untrusted text) still raises its alert, but a person opens the
    case: untrusted text never starts a run by itself."""
    if detail.signals:
        return any(s.provenance in ("fact", "simulated") for s in detail.signals)
    return detail.event.anchor_signal_id is not None


def case_title(detail: AlertDetail) -> str:
    """Deterministic case title from structured fields only (``subtype`` is ``[a-z0-9_]``, counts are
    ints): e.g. "Tropical cyclone alert — 3 sites, 4 lanes exposed". The alert title may carry a
    news headline (untrusted text) and is shown by the web from the alert itself, not copied here."""
    label = detail.event.subtype.replace("_", " ").strip().capitalize() or "Disruption"
    counts = detail.alert.exposure_summary.nodes_by_type
    sites, lanes = counts.sites, counts.lane
    parts = [f"{sites} site{'s' if sites != 1 else ''}"]
    if lanes:
        parts.append(f"{lanes} lane{'s' if lanes != 1 else ''}")
    title = f"{label} alert — {', '.join(parts)} exposed"
    return (title + (" (simulated)" if detail.alert.simulated else ""))[:200]


@dataclass(frozen=True, slots=True)
class OpenCaseResult:
    alert_id: str
    event_id: str
    case_id: str
    run_id: str | None
    created: bool
    auto: bool

    def to_json(self) -> dict[str, Any]:
        return {"alert_id": self.alert_id, "event_id": self.event_id, "case_id": self.case_id,
                "run_id": self.run_id, "created": self.created, "auto": self.auto}


async def _mapping(tenant: str, alert_id: str) -> dict[str, Any] | None:
    return (await cases_for(tenant, [alert_id])).get(alert_id)


async def _sync_hub_opened(tenant: str, alert_id: str, case_id: str) -> bool:
    """Tell the hub ``opened {case_id}``; marks the mapping synced on success."""
    from keelson_engine.core import signal_hub

    try:
        await signal_hub.set_alert_status(tenant, alert_id, AlertStatusUpdate(status="opened", case_id=case_id))
    except signal_hub.HubNotFound:
        synced = True  # the hub forgot the alert (e.g. demo reset): nothing left to sync
    except signal_hub.HubRejected as exc:
        # a refusal (e.g. 409: the hub already names another case for this alert) is final: a
        # retry every poll would only repeat it. The local mapping stays the engine's truth.
        log.warning("signal hub refused the alert's opened status (not retried)", tenant=tenant, alert=alert_id,
                    status=exc.status)
        synced = True
    except signal_hub.HubError as exc:
        log.info("alert opened status not synced to the hub yet", tenant=tenant, alert=alert_id, error=exc.code)
        return False
    else:
        synced = True
    async with tenant_session(tenant) as s:
        await s.execute(text("UPDATE alert_case SET hub_synced = :v WHERE alert_id = CAST(:a AS uuid)"),
                        {"v": synced, "a": alert_id})
    return synced


async def open_case(tenant: str, alert_id: str, *, actor: dict[str, Any], auto: bool = False,
                    detail: AlertDetail | None = None) -> OpenCaseResult:
    """Open (or return) the one case of ``alert_id`` and queue its ``signal`` run (module docstring).

    Raises :class:`AlertNotFound` for an unknown/foreign alert and
    :class:`~keelson_engine.core.signal_hub.HubError` when the hub cannot be asked."""
    from keelson_engine.core import signal_hub
    from keelson_engine.core.repo import admin_audit
    from keelson_engine.core.runs import insert_run, wake_workers

    if not is_hub_id(alert_id):
        raise AlertNotFound(alert_id)
    existing = await _mapping(tenant, alert_id)
    if existing is not None:
        if not existing["hub_synced"]:
            await _sync_hub_opened(tenant, alert_id, str(existing["case_id"]))
        return OpenCaseResult(alert_id, str(existing["event_id"]), str(existing["case_id"]), existing["run_id"],
                              created=False, auto=bool(existing["auto"]))
    if detail is None:
        try:
            detail = await signal_hub.get_alert(tenant, alert_id)
        except signal_hub.HubNotFound as exc:
            raise AlertNotFound(alert_id) from exc
    if detail.alert.id != alert_id or detail.alert.tenant != tenant:
        raise AlertNotFound(alert_id)
    event_id = detail.event.id
    sub = str(actor.get("sub") or "")
    name = str(actor.get("name") or actor.get("username") or "")
    created = False
    try:
        async with tenant_session(tenant) as s:
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                            {"k": f"keelson.alert_case|{tenant}|{alert_id}"})
            row = (await s.execute(
                text("SELECT case_id, run_id, auto FROM alert_case WHERE alert_id = CAST(:a AS uuid)"),
                {"a": alert_id},
            )).first()
            if row is not None:
                case_id, run_id, was_auto = str(row[0]), row[1], bool(row[2])
            else:
                case_id = str((await s.execute(
                    text('INSERT INTO "case" (tenant, title, mode, created_by, created_by_name) '
                         "VALUES (:t, :title, 'actual', :sub, :name) RETURNING id"),
                    {"t": tenant, "title": case_title(detail), "sub": sub, "name": name},
                )).scalar_one())
                run_id, _ = await insert_run(
                    s, tenant=tenant, case_id=case_id, kind="signal",
                    payload={"event_id": event_id, "alert_id": alert_id}, actor=actor,
                    idempotency_key=f"alert:{alert_id}",
                )
                await s.execute(
                    text("INSERT INTO alert_case (tenant, alert_id, event_id, case_id, run_id, auto, opened_by) "
                         "VALUES (:t, CAST(:a AS uuid), CAST(:e AS uuid), :c, :r, :auto, :by)"),
                    {"t": tenant, "a": alert_id, "e": event_id, "c": case_id, "r": run_id, "auto": auto,
                     "by": sub},
                )
                created, was_auto = True, auto
    except IntegrityError:
        found = await _mapping(tenant, alert_id)  # a concurrent opener (other process) won
        if found is None:
            raise
        return OpenCaseResult(alert_id, str(found["event_id"]), str(found["case_id"]), found["run_id"],
                              created=False, auto=bool(found["auto"]))
    result = OpenCaseResult(alert_id, event_id, case_id, run_id, created=created, auto=was_auto)
    if not created:
        return result
    if run_id:
        await wake_workers(run_id, tenant)
    await _sync_hub_opened(tenant, alert_id, case_id)
    await publish(tenant, ALERT_CASE_OPENED, {"alert_id": alert_id, "case_id": case_id, "run_id": run_id,
                                              "auto": auto})
    await admin_audit(actor, "alert.open_case", f"alert:{alert_id}", None,
                      {"case_id": case_id, "run_id": run_id, "event_id": event_id, "auto": auto,
                       "simulated": detail.alert.simulated}, tenant=tenant)
    log.info("case opened from alert", tenant=tenant, alert=alert_id, case=case_id, auto=auto)
    return result


# ───────────────────────────── watcher ─────────────────────────────


def _fingerprint(alert: Alert) -> dict[str, Any]:
    return {"s": alert.status, "v": alert.severity, "u": alert.updated_at.isoformat(), "c": alert.case_id}


async def _demo_reset_running(tenant: str) -> bool:
    from keelson_engine.core.valkey import get_valkey

    try:
        return bool(await get_valkey().exists(f"keelson:demo_reset:{tenant}"))
    except (RedisError, OSError):
        return False


async def poll_tenant(tenant: str, *, force: bool = False) -> dict[str, Any] | None:
    """One poll of the tenant's alerts (module docstring). ``None`` when another replica holds the
    poll lock (or a demo reset runs). Raises :class:`~keelson_engine.core.signal_hub.HubError` when
    the hub cannot be read."""
    from keelson_engine.core import signal_hub
    from keelson_engine.core.valkey import get_valkey

    st = get_settings()
    v = get_valkey()
    if await _demo_reset_running(tenant):
        return None
    if not force:
        lock_ms = max(500, int(st.alerts_poll_seconds * 900))
        if not await v.set(_poll_lock_key(tenant), "1", nx=True, px=lock_ms):
            return None
    limit = max(1, min(st.alerts_list_limit, signal_hub.MAX_LIST_LIMIT))
    alerts = await signal_hub.list_alerts(tenant, limit=limit)
    mapping = await cases_for(tenant, [a.id for a in alerts])
    state_raw = cast(dict[str, str], await v.hgetall(_state_key(tenant)) or {})
    primed = bool(await v.exists(_primed_key(tenant)))
    rules: tuple[AlertRule, ...] | None = None
    events: list[tuple[str, Any]] = []
    new_state: dict[str, str] = {}
    views: list[Alert] = []
    opened = 0
    for alert in reversed(alerts):  # oldest first: events in raise order
        view = apply_local(alert, mapping.get(alert.id))
        prev = _load(state_raw.get(alert.id))
        fp = _fingerprint(view)
        checked = prev.get("a") if prev else None
        if (view.status == "new" and view.case_id is None and alert.id not in mapping
                and checked != fp["u"]):
            try:
                detail = await signal_hub.get_alert(tenant, alert.id)
                if rules is None:
                    rules = await rules_for(tenant)
                if wants_draft_case(detail, rules) and auto_open_allowed(detail):
                    if await _demo_reset_running(tenant):
                        return None  # a reset started mid-poll: never open a case for a pre-reset alert
                    result = await open_case(tenant, alert.id, actor=AUTO_ACTOR, auto=True, detail=detail)
                    opened += int(result.created)
                    view = view.model_copy(update={"status": "opened", "case_id": result.case_id})
                    fp = _fingerprint(view)
                checked = fp["u"]
            except AlertNotFound:
                checked = fp["u"]
            except signal_hub.HubError as exc:  # retried on the next poll
                log.info("alert auto-open check deferred", tenant=tenant, alert=alert.id, error=exc.code)
        fp["a"] = checked
        if prev is None:
            if primed:
                events.append((ALERT_RAISED, {"alert": view.model_dump(mode="json"), "renotify": False}))
        elif (prev.get("s"), prev.get("v"), prev.get("u"), prev.get("c")) != (fp["s"], fp["v"], fp["u"], fp["c"]):
            rose = _SEVERITY_RANK[view.severity] > _SEVERITY_RANK.get(str(prev.get("v")), 99)
            events.append((ALERT_RAISED if rose else ALERT_UPDATED,
                           {"alert": view.model_dump(mode="json"), **({"renotify": True} if rose else {})}))
        new_state[alert.id] = json.dumps(fp, separators=(",", ":"))
        views.append(view)
    if len(alerts) < limit:
        for gone in sorted(k for k in state_raw if k != _COUNT_FIELD and k not in new_state):
            if primed:
                events.append((ALERT_REMOVED, {"alert_id": gone}))
    count = new_count(views)
    if not primed or state_raw.get(_COUNT_FIELD) != str(count):
        events.append((ALERT_COUNT, {"new": count}))
    new_state[_COUNT_FIELD] = str(count)
    if await _demo_reset_running(tenant):
        return None  # the reset deletes this state: do not write back pre-reset alerts
    pipe = v.pipeline(transaction=True)
    pipe.delete(_state_key(tenant))
    pipe.hset(_state_key(tenant), mapping=cast(Any, new_state))
    pipe.expire(_state_key(tenant), 7 * 24 * 3600)
    pipe.set(_primed_key(tenant), "1", ex=7 * 24 * 3600)
    await pipe.execute()
    for name, value in events:
        await publish(tenant, name, value)
    # mappings whose "opened" status never reached the hub (hub was down when the case opened)
    for alert_id, m in mapping.items():
        if not m["hub_synced"]:
            await _sync_hub_opened(tenant, alert_id, str(m["case_id"]))
    return {"alerts": len(alerts), "events": len(events), "opened": opened, "new": count}


def _load(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


_watcher: AlertWatcher | None = None


def poke() -> None:
    """Wake this process's watcher now (e.g. right after a demo pack step)."""
    if _watcher is not None:
        _watcher.wake()


class AlertWatcher:
    """engine-api background task: per-tenant alert polls, forward retries, internal tool snapshots."""

    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self._stopped = False
        self._tools_due: dict[str, float] = {}
        self._failing: set[str] = set()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    async def run(self) -> None:
        global _watcher
        _watcher = self
        log.info("alert watcher started", interval_s=get_settings().alerts_poll_seconds)
        try:
            while not self._stopped:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the loop never dies
                    log.warning("alert watcher tick failed", error=type(exc).__name__)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=get_settings().alerts_poll_seconds)
                except TimeoutError:
                    pass
                self._wake.clear()
        finally:
            if _watcher is self:
                _watcher = None

    async def tick(self) -> None:
        from keelson_engine.core import connectors, hub_ingest, signal_hub

        if not signal_hub.is_configured():
            return
        st = get_settings()
        tenants = await connectors.list_tenants_with_connector(st.signal_hub_connector_slug)
        for tenant in tenants:
            await self._refresh_tools(tenant)
            try:
                await poll_tenant(tenant)
            except signal_hub.HubError as exc:
                if tenant not in self._failing:  # log once per outage, not every 5 s
                    log.warning("signal hub alerts unavailable", tenant=tenant, error=exc.code, status=exc.status)
                    self._failing.add(tenant)
                continue
            if tenant in self._failing:
                log.info("signal hub alerts available again", tenant=tenant)
                self._failing.discard(tenant)
        await hub_ingest.retry_pending()

    async def _refresh_tools(self, tenant: str) -> None:
        from keelson_engine.core import connectors

        now = time.monotonic()
        if self._tools_due.get(tenant, 0.0) > now:
            return
        slug = get_settings().signal_hub_connector_slug
        try:
            await connectors.refresh_tool_snapshot(tenant, slug)
        except Exception as exc:  # noqa: BLE001 - retried a minute later
            log.info("signal hub tool snapshot deferred", tenant=tenant, error=type(exc).__name__)
            self._tools_due[tenant] = now + 60.0
            return
        self._tools_due[tenant] = now + get_settings().alerts_tools_refresh_seconds
