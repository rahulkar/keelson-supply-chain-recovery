"""Read side of ``admin_audit`` for the back-office (tenant-scoped through RLS; platform rows —
``tenant IS NULL`` — only via ``platform=True``)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import text

from keelson_engine.core.db import system_session, tenant_session

__all__ = ["ACTIVITY_CATEGORIES", "AUDIT_CATEGORY_PREFIXES", "audit_category", "evals_dir", "list_activity", "list_audit",
           "list_evals"]


AUDIT_CATEGORY_PREFIXES: dict[str, tuple[str, ...]] = {
    "approvals": ("approval.", "interrupt.", "decision.", "change_request."),
    "change_sets": ("change_set.", "commit.", "rollback.", "checklist.", "ledger."),
    "runs": ("run.", "case.", "message.", "demo.reset"),
    "signals": ("alert.", "signal.", "signals.", "watch_region.", "alert_rule.", "demo.pack"),
    "admin": ("agent_spec.", "agents.", "policy.", "policies.", "connector", "tool.", "gateway.", "kb.", "tenant.",
              "user.", "console.", "mapping.", "admin.", "auth.", "demo.login_as", "untrusted_text."),
}
"""``admin_audit`` action prefix → audit category (``110`` §6; first match in this order wins, so
``change_request.*`` is an approval). Mirrors ``apps/keelson-web`` ``lib/admin/audit.ts``."""


def audit_category(action: str) -> str | None:
    for cat, prefixes in AUDIT_CATEGORY_PREFIXES.items():
        if action.startswith(prefixes):
            return cat
    return None


def _like(prefix: str) -> str:
    return prefix.replace("\\", "").replace("%", "").replace("_", r"\_") + "%"


async def list_audit(tenant: str | None, *, platform: bool = False, limit: int = 50, before: int | None = None,
                     action: str | None = None, actor: str | None = None,
                     target: str | None = None, category: str | None = None) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    where = ["tenant IS NULL" if platform else "tenant = :t"]
    params: dict[str, Any] = {"t": tenant, "n": limit + 1}
    if category is not None:
        cats = list(AUDIT_CATEGORY_PREFIXES)
        if category not in cats:
            raise ValueError("unknown audit category")
        # in category: matches one of its prefixes and none of an earlier category's
        own = [f"action LIKE :c_{category}_{i}" for i in range(len(AUDIT_CATEGORY_PREFIXES[category]))]
        for i, p in enumerate(AUDIT_CATEGORY_PREFIXES[category]):
            params[f"c_{category}_{i}"] = _like(p)
        clause = "(" + " OR ".join(own) + ")"
        for earlier in cats[: cats.index(category)]:
            for i, p in enumerate(AUDIT_CATEGORY_PREFIXES[earlier]):
                params[f"c_{earlier}_{i}"] = _like(p)
                clause += f" AND action NOT LIKE :c_{earlier}_{i}"
        where.append(clause)
    if before is not None:
        where.append("id < :before")
        params["before"] = int(before)
    if action:
        where.append("action LIKE :action")
        params["action"] = action.replace("%", "").replace("_", r"\_") + "%"
    if actor:
        where.append("actor_username = :actor")
        params["actor"] = actor
    if target:
        where.append("target LIKE :target")
        params["target"] = "%" + target.replace("%", "").replace("_", r"\_") + "%"
    sql = text(
        "SELECT id, tenant, at, actor_sub, actor_username, action, target, before, after, trace_id "
        f"FROM admin_audit WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT :n"
    )
    if platform:
        async with system_session() as s:
            rows = (await s.execute(sql, params)).mappings().all()
    else:
        assert tenant is not None
        async with tenant_session(tenant) as s:
            rows = (await s.execute(sql, params)).mappings().all()
    items = [
        {"id": int(r["id"]), "tenant": r["tenant"], "at": r["at"].isoformat(), "actor_sub": r["actor_sub"],
         "actor_username": r["actor_username"], "action": r["action"], "target": r["target"],
         "before": r["before"], "after": r["after"], "trace_id": r["trace_id"],
         "category": audit_category(str(r["action"]))}
        for r in rows[:limit]
    ]
    return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit and items else None}


# ───────────────────────────── M7: activity across categories (110 §6) ─────────────────────────────

ACTIVITY_CATEGORIES = ("runs", "approvals", "change_sets", "admin", "signals")

_ACTIVITY_SQL: dict[str, str] = {
    "runs": """
        SELECT 'run:' || r.id AS key, r.created_at AS at, coalesce(r.actor->>'username', r.created_by) AS actor,
               'run.' || r.kind || '.' || r.status AS action, 'case:' || r.case_id AS target, r.case_id,
               jsonb_build_object('run_id', r.id, 'kind', r.kind, 'status', r.status,
                                  'agent_spec_version', r.agent_spec_version, 'error', left(r.error, 300),
                                  'finished_at', r.finished_at) AS detail
          FROM run r WHERE r.tenant = :t""",
    "approvals": """
        SELECT 'approval:' || d.id AS key, d.created_at AS at, d.approver_username AS actor,
               'approval.' || d.decision AS action, 'interrupt:' || d.interrupt_id AS target, i.case_id,
               jsonb_build_object('interrupt_id', d.interrupt_id, 'role', d.role, 'decision', d.decision,
                                  'reason', left(d.reason, 500), 'plan_version', d.plan_version,
                                  'change_set_hash', d.change_set_hash) AS detail
          FROM approval_decision d JOIN interrupt i ON i.id = d.interrupt_id
         WHERE d.tenant = :t""",
    "change_sets": """
        SELECT 'change_set:' || c.id AS key, c.updated_at AS at, NULL::text AS actor,
               'change_set.' || c.status AS action, 'change_set:' || c.id AS target, c.case_id,
               jsonb_build_object('change_set_id', c.id, 'connector', c.connector, 'status', c.status,
                                  'revision', c.revision, 'ext_code', c.ext_code, 'plan_version', c.plan_version,
                                  'lines', jsonb_array_length(CASE WHEN jsonb_typeof(c.items) = 'array'
                                                                   THEN c.items ELSE '[]'::jsonb END)) AS detail
          FROM change_set c WHERE c.tenant = :t""",
    "admin": """
        SELECT 'admin:' || a.id AS key, a.at AS at, a.actor_username AS actor, a.action AS action,
               a.target AS target, NULL::text AS case_id,
               jsonb_build_object('before', a.before, 'after', a.after, 'trace_id', a.trace_id) AS detail
          FROM admin_audit a WHERE a.tenant = :t""",
    "signals": """
        SELECT 'alert_case:' || ac.alert_id::text AS key, ac.created_at AS at, ac.opened_by AS actor,
               CASE WHEN ac.auto THEN 'signal.alert_case_auto' ELSE 'signal.alert_case' END AS action,
               'alert:' || ac.alert_id::text AS target, ac.case_id,
               jsonb_build_object('alert_id', ac.alert_id, 'event_id', ac.event_id, 'run_id', ac.run_id,
                                  'auto', ac.auto) AS detail
          FROM alert_case ac WHERE ac.tenant = :t
        UNION ALL
        SELECT 'case_signal:' || cs.id AS key, cs.created_at AS at, 'webhook:' || cs.connector AS actor,
               'signal.' || cs.kind AS action, 'event:' || cs.event_id AS target, cs.case_id,
               jsonb_build_object('connector', cs.connector, 'event_type', cs.event_type, 'run_id', cs.run_id)
                 AS detail
          FROM case_signal cs WHERE cs.tenant = :t""",
}


def _encode_cursor(at: Any, key: str) -> str:
    """Opaque, URL-safe ``next_before`` (base64url of ``<iso at>|<key>``)."""
    import base64

    return base64.urlsafe_b64encode(f"{at.isoformat()}|{key}".encode()).decode().rstrip("=")


def _cursor(before: str | None) -> tuple[Any, str] | None:
    import base64
    import binascii
    import datetime as dt

    if not before:
        return None
    try:
        raw = base64.urlsafe_b64decode(before + "=" * (-len(before) % 4)).decode("utf-8")
        at_text, sep, key = raw.partition("|")
        at = dt.datetime.fromisoformat(at_text)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ValueError("invalid cursor") from None
    if not sep or at.tzinfo is None:
        raise ValueError("invalid cursor")
    return at, key


async def list_activity(tenant: str, *, categories: list[str] | None = None, limit: int = 50,
                        before: str | None = None, actor: str | None = None, case_id: str | None = None,
                        action: str | None = None, since: Any = None, until: Any = None) -> dict[str, Any]:
    """One newest-first timeline across runs, approvals, change sets, admin changes and signals of
    the tenant: ``{items: [{key, category, at, actor, action, target, case_id, detail}], next_before}``
    (``next_before`` = an opaque URL-safe cursor). Filters: ``categories``, exact ``actor``,
    ``case_id``, ``action`` prefix, ``since`` / ``until`` (aware datetimes). Raises ``ValueError``
    for a malformed cursor."""
    cats = [c for c in (categories or list(ACTIVITY_CATEGORIES)) if c in ACTIVITY_CATEGORIES]
    limit = max(1, min(int(limit), 200))
    cursor = _cursor(before)
    rows: list[dict[str, Any]] = []
    fetch = limit + 25
    for cat in cats:
        conds: list[str] = []
        params: dict[str, Any] = {"t": tenant, "n": fetch}
        if cursor is not None:
            conds.append("(at, key COLLATE \"C\") < (CAST(:before_at AS timestamptz), CAST(:before_key AS text))")
            params["before_at"], params["before_key"] = cursor
        if since is not None:
            conds.append("at >= :since")
            params["since"] = since
        if until is not None:
            conds.append("at < :until")
            params["until"] = until
        if actor:
            conds.append("actor = :actor")
            params["actor"] = actor
        if case_id:
            conds.append("case_id = :case_id")
            params["case_id"] = case_id
        if action:
            conds.append("action LIKE :action")
            params["action"] = action.replace("%", "").replace("_", "\\_") + "%"
        where = (" AND " + " AND ".join(conds)) if conds else ""
        # exact keyset: (at, key) in byte order ("C"), the same order Python compares the keys in
        sql = (f"SELECT * FROM ({_ACTIVITY_SQL[cat]}) q WHERE true{where} "
               'ORDER BY at DESC, key COLLATE "C" DESC LIMIT :n')
        async with tenant_session(tenant) as s:
            for m in (await s.execute(text(sql), params)).mappings().all():
                rows.append({**dict(m), "category": cat})
    rows.sort(key=lambda r: (r["at"], r["key"]), reverse=True)
    if cursor is not None:
        rows = [r for r in rows if (r["at"], r["key"]) < cursor]
    page = rows[:limit]
    items = [{"key": r["key"], "category": r["category"], "at": r["at"].isoformat(), "actor": r["actor"],
              "action": r["action"], "target": r["target"], "case_id": r["case_id"], "detail": r["detail"]}
             for r in page]
    more = len(rows) > limit
    nxt = _encode_cursor(page[-1]['at'], page[-1]['key']) if more and page else None
    return {"items": items, "next_before": nxt, "categories": cats}


# ───────────────────────────── M7: evals (110 §6) ─────────────────────────────

MAX_EVAL_FILE_BYTES = 8 * 1024 * 1024
_EVAL_FILE_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z\.json")


def evals_dir() -> Path:
    from keelson_engine.config import get_settings

    configured = get_settings().evals_results_dir
    if configured is not None:
        return Path(configured)
    image = Path("/app/evals/results")
    if image.is_dir():
        return image
    return Path(__file__).resolve().parents[5] / "evals" / "results"


def _num(value: Any) -> Any:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _eval_case(c: Any) -> dict[str, Any] | None:
    if not isinstance(c, dict):
        return None
    checks = [x for x in (c.get("checks") or []) if isinstance(x, dict)]
    for t in c.get("turns") or []:
        if isinstance(t, dict):
            checks += [x for x in (t.get("checks") or []) if isinstance(x, dict)]
    metrics = c.get("metrics") if isinstance(c.get("metrics"), dict) else {}
    scores = {str(k)[:64]: v for k, v in metrics.items() if _num(v) is not None}
    return {"id": str(c.get("id") or "")[:120], "title": str(c.get("title") or "")[:200],
            "tags": [str(t)[:40] for t in (c.get("tags") or []) if isinstance(t, str)][:20],
            "status": str(c.get("status") or "unknown")[:20], "duration_s": _num(c.get("duration_s")),
            "checks": len(checks),
            "checks_failed": sum(1 for x in checks if x.get("ok") is False and not x.get("soft")),
            "scores": scores}


def list_evals(limit: int | None = None) -> dict[str, Any]:
    """The latest ``evals/results/<UTC stamp>.json`` files (newest first) with per-case scores.
    Read-only; a file that is too large or not JSON is listed with an ``error``."""
    from keelson_engine.config import get_settings

    directory = evals_dir()
    n = limit or get_settings().evals_list_limit
    if not directory.is_dir():
        return {"items": [], "directory_available": False}
    files = sorted((p for p in directory.iterdir() if p.is_file() and _EVAL_FILE_RE.fullmatch(p.name)),
                   key=lambda p: p.name, reverse=True)[:n]
    items: list[dict[str, Any]] = []
    for path in files:
        entry: dict[str, Any] = {"run": path.stem, "file": path.name, "error": None}
        try:
            if path.stat().st_size > MAX_EVAL_FILE_BYTES:
                raise ValueError("file too large")
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not a results document")
            summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
            entry.update({
                "schema": str(doc.get("schema") or "")[:40], "started_at": doc.get("started_at"),
                "finished_at": doc.get("finished_at"),
                "summary": {k: v for k, v in summary.items() if _num(v) is not None},
                "cases": [c for c in (_eval_case(x) for x in doc.get("cases") or []) if c is not None],
            })
        except (OSError, ValueError) as exc:
            entry["error"] = f"unreadable: {type(exc).__name__}"
        items.append(entry)
    return {"items": items, "directory_available": True}
