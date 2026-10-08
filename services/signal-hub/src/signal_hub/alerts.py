"""Per-tenant alerts (``100`` §5): rule evaluation, hysteresis, auto-resolve, change feed.

* Rules: the tenant's ``alert_rule_set`` or :data:`~keelson_contracts.signals.DEFAULT_ALERT_RULES`
  (alert at severity >= 4000 bp with >= 1 exposed node; ``alert_and_draft_case`` at >= 7000 bp with
  an exposed supplier/factory/port). Evaluated with ``decide_alert_action``.
* One alert per (tenant, event), id ``event_id("alert", tenant, event)`` (deterministic).
* Hysteresis: an update edits the alert (severity, title, why, exposure) and only **re-notifies**
  — status back to ``new`` (unless ``opened``) and a ``renotified`` change — when severity rose by at
  least the rule's ``hysteresis_bp`` since the last notification, or when the action escalates from
  ``alert`` to ``alert_and_draft_case``.
* Auto-resolve: an alert whose event is resolved/expired becomes ``resolved``.
* Nothing is ever committed; ``alert_and_draft_case`` only tells the engine to open a draft case.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Final

import asyncpg

from keelson_contracts.signals import (
    DEFAULT_ALERT_RULES,
    Alert,
    AlertRule,
    ExposureSummary,
    NodeCounts,
    alert_severity_for,
    decide_alert_action,
    should_renotify,
)
from keelson_contracts.signals import (
    event_id as make_event_id,
)
from signal_hub.logs import get_logger

__all__ = [
    "AlertChange",
    "alert_from_row",
    "alert_id_for",
    "evaluate_alert",
    "load_rules",
    "why_sentence",
]

log = get_logger(__name__)
_ACTIVE: Final = ("new", "seen", "snoozed", "dismissed", "opened")
_RESET_ON_RENOTIFY: Final = ("seen", "snoozed", "dismissed")
_LABELS: Final[dict[str, tuple[str, str]]] = {
    "supplier": ("supplier", "suppliers"),
    "factory": ("factory", "factories"),
    "port": ("port", "ports"),
    "dc": ("DC", "DCs"),
    "store": ("store cluster", "store clusters"),
    "lane": ("lane", "lanes"),
}


def alert_id_for(tenant: str, event_id: str) -> str:
    return make_event_id("alert", tenant, event_id)


@dataclass(frozen=True, slots=True)
class AlertChange:
    alert_id: str
    change: str  # raised | renotified | updated | resolved
    action: str | None


async def load_rules(conn: asyncpg.Connection, tenant: str) -> tuple[AlertRule, ...]:
    raw = await conn.fetchval("SELECT rules FROM alert_rule_set WHERE tenant = $1", tenant)
    if not raw:
        return DEFAULT_ALERT_RULES
    try:
        return tuple(AlertRule.model_validate(r) for r in raw)
    except ValueError:
        log.exception("stored alert rules invalid; using defaults", extra={"tenant": tenant})
        return DEFAULT_ALERT_RULES


def why_sentence(rule: AlertRule, severity_bp: int, counts: NodeCounts) -> str:
    """Deterministic explanation from the rule and the exposure counts (no event text)."""
    parts = []
    for node_type in ("port", "factory", "supplier", "dc", "store", "lane"):
        n = counts.get(node_type)
        if n:
            one, many = _LABELS[node_type]
            parts.append(f"{n} {one if n == 1 else many}")
    exposed = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else "no network nodes"
    action = "draft case suggested" if rule.action == "alert_and_draft_case" else "alert"
    return (
        f"Severity {severity_bp:,} bp meets rule {rule.id} (≥ {rule.when.min_severity_bp:,} bp, "
        f"≥ {rule.when.min_exposed_nodes} exposed node{'s' if rule.when.min_exposed_nodes != 1 else ''}); "
        f"exposed: {exposed} — {action}."
    )[:500]


def alert_from_row(row: asyncpg.Record | dict[str, Any]) -> Alert:
    return Alert.model_validate({
        "id": str(row["id"]),
        "tenant": row["tenant"],
        "event_id": str(row["event_id"]),
        "status": row["status"],
        "severity": row["severity"],
        "title": row["title"],
        "why": row["why"],
        "exposure_summary": row["exposure_summary"],
        "first_raised_at": row["first_raised_at"],
        "updated_at": row["updated_at"],
        "case_id": row["case_id"],
        "simulated": row["simulated"],
    })


async def _log_change(conn: asyncpg.Connection, tenant: str, alert_id: str, change: str, action: str | None,
                      at: dt.datetime) -> AlertChange:
    await conn.execute(
        "INSERT INTO alert_change (tenant, alert_id, change, action, at) VALUES ($1, $2, $3, $4, $5)",
        tenant, alert_id, change, action, at,
    )
    return AlertChange(alert_id, change, action)


async def evaluate_alert(
    conn: asyncpg.Connection,
    *,
    tenant: str,
    event: asyncpg.Record | dict[str, Any],
    counts: NodeCounts,
    now: dt.datetime,
    rules: tuple[AlertRule, ...] | None = None,
) -> AlertChange | None:
    """Create/update/resolve the tenant's alert for ``event`` (``conn`` in the tenant's context)."""
    ev_id = str(event["id"])
    aid = alert_id_for(tenant, ev_id)
    existing = await conn.fetchrow("SELECT * FROM alert WHERE tenant = $1 AND event_id = $2 FOR UPDATE", tenant, ev_id)
    severity_bp = int(event["severity_bp"])
    if event["status"] == "resolved":
        if existing is not None and existing["status"] != "resolved":
            await conn.execute(
                "UPDATE alert SET status = 'resolved', updated_at = greatest($2, first_raised_at) WHERE id = $1",
                existing["id"], now,
            )
            return await _log_change(conn, tenant, aid, "resolved", existing["action"], now)
        return None
    rules = rules if rules is not None else await load_rules(conn, tenant)
    decision = decide_alert_action(
        rules, severity_bp=severity_bp, confidence_bp=int(event["confidence_bp"]), kind=event["kind"], counts=counts
    )
    summary = ExposureSummary(nodes_by_type=counts, open_po_lines=None, units_inbound=None).model_dump(mode="json")
    title = str(event["title"])[:300]
    band = alert_severity_for(severity_bp)
    if decision is None or decision[0] == "ignore":
        if existing is not None and existing["status"] in _ACTIVE and (
            existing["severity_bp"] != severity_bp or existing["title"] != title
            or existing["exposure_summary"] != summary
        ):
            # Below the rules now (or suppressed): keep the alert, refresh its facts quietly (and only
            # log a change when a fact changed — a network push or rule edit must not spam the feed).
            await conn.execute(
                """UPDATE alert SET severity = $2, severity_bp = $3, title = $4, exposure_summary = $5,
                   updated_at = greatest($6, first_raised_at) WHERE id = $1""",
                existing["id"], band, severity_bp, title, summary, now,
            )
            return await _log_change(conn, tenant, aid, "updated", existing["action"], now)
        return None
    action, rule = decision
    why = why_sentence(rule, severity_bp, counts)
    simulated = bool(event["simulated"])
    if existing is None:
        await conn.execute(
            """
            INSERT INTO alert (id, tenant, event_id, status, severity, severity_bp, title, why, exposure_summary,
                               first_raised_at, updated_at, case_id, simulated, rule_id, action,
                               notified_severity_bp, notified_at)
            VALUES ($1, $2, $3, 'new', $4, $5, $6, $7, $8, $9, $9, NULL, $10, $11, $12, $5, $9)
            """,
            aid, tenant, ev_id, band, severity_bp, title, why, summary, now, simulated, rule.id, action,
        )
        return await _log_change(conn, tenant, aid, "raised", action, now)
    if existing["status"] == "resolved":
        # The event is active again (e.g. a new advisory after expiry): raise afresh.
        await conn.execute(
            """UPDATE alert SET status = 'new', severity = $2, severity_bp = $3, title = $4, why = $5,
               exposure_summary = $6, updated_at = greatest($7, first_raised_at), rule_id = $8, action = $9,
               notified_severity_bp = $3, notified_at = $7 WHERE id = $1""",
            existing["id"], band, severity_bp, title, why, summary, now, rule.id, action,
        )
        return await _log_change(conn, tenant, aid, "raised", action, now)
    escalated = action == "alert_and_draft_case" and existing["action"] == "alert"
    renotify = escalated or should_renotify(int(existing["notified_severity_bp"]), severity_bp, rule.hysteresis_bp)
    changed = (
        existing["severity_bp"] != severity_bp or existing["title"] != title or existing["why"] != why
        or existing["exposure_summary"] != summary or existing["action"] != action
    )
    if not renotify and not changed:
        return None
    new_action = "alert_and_draft_case" if "alert_and_draft_case" in (action, existing["action"]) else "alert"
    status = existing["status"]
    if renotify and status in _RESET_ON_RENOTIFY:
        status = "new"
    await conn.execute(
        """UPDATE alert SET status = $2, severity = $3, severity_bp = $4, title = $5, why = $6,
           exposure_summary = $7, updated_at = greatest($8, first_raised_at), rule_id = $9, action = $10,
           notified_severity_bp = CASE WHEN $11 THEN $4 ELSE notified_severity_bp END,
           notified_at = CASE WHEN $11 THEN $8 ELSE notified_at END
           WHERE id = $1""",
        existing["id"], status, band, severity_bp, title, why, summary, now, rule.id, new_action, renotify,
    )
    return await _log_change(conn, tenant, aid, "renotified" if renotify else "updated", new_action, now)
