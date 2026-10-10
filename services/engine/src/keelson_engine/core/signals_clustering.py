"""Signal clustering settings (``docs/contracts/123-m8-signal-clustering.md`` §7) — the engine side of the
Signals admin "Clustering" section.

The settings are **platform-wide** on the Signal Hub (global feeds cluster once for every tenant):

* every admin (tenant-admin or platform-admin) may read them, with the backends and the counts of
  the last 7 days in the hub's RLS scope of the admin's tenant (``GET /v1/clustering``, tenant token);
* only a platform admin may change them or run one maintenance round: the engine checks the role
  first, then calls the hub with a **platform-scoped** service token (``scope: "platform"``,
  :func:`keelson_engine.core.signal_hub.mint_hub_token`). The hub stores a new version (``409`` on a
  stale ``base_version``, ``422`` out of bounds) and the engine writes a platform ``admin_audit``
  row ``signals.clustering`` with the before/after diff.

Threshold edits are not a plan §17 guarded change (no four-eyes): they are platform-admin only and
audited. Switching the embedding profile (guarded, ``embedding_profile``) is deferred (deploy env).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from keelson_contracts.signals import ClusteringConfig, ClusteringConfigUpdate, ClusteringStatus
from keelson_engine.core.change_requests import json_diff
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit

__all__ = ["get_clustering", "put_clustering", "run_clustering"]

log = get_logger(__name__)


def _hub_error(exc: Exception) -> AdminError:
    from keelson_engine.core import signal_hub

    if isinstance(exc, signal_hub.HubNotConfigured):
        return AdminError(503, exc.code, "the Signal Hub is not configured")
    if isinstance(exc, signal_hub.HubNotFound):
        return AdminError(503, "signal_hub_unsupported", "the Signal Hub does not support clustering settings yet")
    if isinstance(exc, signal_hub.HubRejected):
        body = exc.body if isinstance(exc.body, dict) else {}
        detail = body.get("detail")
        if exc.status == 409:
            current = detail.get("current_version") if isinstance(detail, dict) else None
            return AdminError(409, "stale_version", "the clustering settings were changed by someone else; reload them",
                              current_version=current if isinstance(current, int) else None)
        if exc.status == 422:
            return AdminError(422, "clustering_invalid", "the Signal Hub refused these clustering settings")
        if exc.status == 403:
            return AdminError(403, "platform_admin_required", "the Signal Hub refused the platform scope")
        return AdminError(502, exc.code, f"the Signal Hub refused the request (HTTP {exc.status})")
    assert isinstance(exc, signal_hub.HubError)
    return AdminError(503, exc.code, "the Signal Hub could not be reached")


async def get_clustering(tenant: str, *, editable: bool) -> dict[str, Any]:
    """``ClusteringStatus`` (validated) + ``editable`` (the caller is a platform admin)."""
    from keelson_engine.core import signal_hub

    try:
        body = await signal_hub.request_json("GET", "/v1/clustering", tenant=tenant)
    except signal_hub.HubError as exc:
        raise _hub_error(exc) from None
    try:
        status = ClusteringStatus.model_validate(body)
    except ValidationError:
        log.warning("signal hub clustering status does not match the contract", tenant=tenant)
        raise AdminError(502, "signal_hub_unavailable", "the Signal Hub answered with an invalid clustering status") from None
    return {**status.model_dump(mode="json"), "editable": editable}


async def put_clustering(tenant: str, update: ClusteringConfigUpdate, actor: dict[str, Any]) -> dict[str, Any]:
    """Store new platform settings (caller: a platform admin) and audit the diff."""
    from keelson_engine.core import signal_hub

    username = str(actor.get("username") or actor.get("sub") or "platform-admin")[:200]
    body = update.model_copy(update={"updated_by": username}).model_dump(mode="json")
    try:
        before_raw = await signal_hub.request_json("GET", "/v1/clustering", tenant=tenant)
        saved_raw = await signal_hub.request_json("PUT", "/v1/clustering/config", tenant=tenant, json_body=body,
                                                  platform=True)
    except signal_hub.HubError as exc:
        raise _hub_error(exc) from None
    try:
        saved = ClusteringConfig.model_validate(saved_raw)
    except ValidationError:
        raise AdminError(502, "signal_hub_unavailable", "the Signal Hub answered with invalid clustering settings") from None
    before = before_raw.get("config") if isinstance(before_raw, dict) else None
    plain = saved.model_dump(mode="json", exclude={"updated_at", "updated_by"})
    old = {k: v for k, v in before.items() if k not in ("updated_at", "updated_by")} if isinstance(before, dict) else None
    await admin_audit(actor, "signals.clustering", "signals:clustering", old,
                      {**plain, "diff": json_diff(old, plain), "requested_by_tenant": tenant}, platform=True)
    return saved.model_dump(mode="json")


async def run_clustering(tenant: str, actor: dict[str, Any]) -> dict[str, Any]:
    """One cluster maintenance round on the hub now (merge pass + embedding jobs)."""
    from keelson_engine.core import signal_hub

    try:
        out = await signal_hub.request_json("POST", "/v1/clustering/run", tenant=tenant, json_body={}, platform=True)
    except signal_hub.HubError as exc:
        raise _hub_error(exc) from None
    merged = out.get("merged") if isinstance(out, dict) else None
    result = {
        "merged": [m for m in merged if isinstance(m, dict)][:500] if isinstance(merged, list) else [],
        "embedded": out.get("embedded", 0) if isinstance(out, dict) and isinstance(out.get("embedded"), int) else 0,
    }
    await admin_audit(actor, "signals.clustering_run", "signals:clustering", None,
                      {"merged": len(result["merged"]), "embedded": result["embedded"], "requested_by_tenant": tenant},
                      platform=True)
    return result
