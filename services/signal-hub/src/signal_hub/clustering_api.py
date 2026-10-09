"""Clustering REST routes (``docs/contracts/123`` §7), service JWT:

==================================  ==========================================================
``GET  /v1/clustering``             ``ClusteringStatus``: settings, similarity backends, counts of
                                    the last 7 days (global rows + the token tenant's own rows)
``PUT  /v1/clustering/config``      ``ClusteringConfigUpdate`` → ``ClusteringConfig`` (version + 1);
                                    platform-scoped token only (403), stale ``base_version`` → 409,
                                    out-of-bounds values → 422
``POST /v1/clustering/run``         one maintenance round now (merge pass + embedding jobs);
                                    platform-scoped token only
==================================  ==========================================================

The settings are platform-wide (global feeds cluster once for every tenant): a tenant admin may
read them through the engine but only a platform admin's token (``scope: "platform"``, minted by
the engine after its own platform-admin check) may change them. Writes run in the system context.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, HTTPException, Request, status

from keelson_contracts.signals import ClusteringConfigUpdate, ClusteringStatus
from signal_hub import maintenance, membership, vectors
from signal_hub.auth import Principal, ServicePrincipal
from signal_hub.clustering_config import ConfigConflict, load_config, save_config
from signal_hub.db import tx
from signal_hub.logs import get_logger

if TYPE_CHECKING:
    from signal_hub.app import HubState

__all__ = ["STATUS_WINDOW_DAYS", "build_clustering_router", "run_maintenance"]

log = get_logger(__name__)
STATUS_WINDOW_DAYS: Final = 7


def _hub(request: Request) -> HubState:
    hub: HubState = request.app.state.hub
    return hub


def _require_platform(principal: ServicePrincipal) -> None:
    if not principal.platform:
        log.warning("clustering change refused: not a platform-scoped token", extra={"tenant": principal.tenant})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "changing the clustering settings needs a platform admin")


async def run_maintenance(hub: HubState) -> dict[str, Any]:
    """One maintenance round with the hub's embedder (the API loop and ``POST /v1/clustering/run``)."""
    return await maintenance.run(hub.pipeline, now=hub.clock(), embedder=hub.embedder, batch=hub.settings.embed_batch,
                                 max_attempts=hub.settings.extraction_max_attempts,
                                 include_headline=hub.settings.embed_include_headline)


def build_clustering_router() -> APIRouter:
    r = APIRouter()

    @r.get("/v1/clustering")
    async def clustering_status(request: Request, principal: Principal) -> dict[str, Any]:
        hub = _hub(request)
        async with tx(hub.pool, None) as conn:
            config = await load_config(conn)
        async with tx(hub.pool, principal.tenant) as conn:  # pending jobs like the counts: global + own
            pgvector, remote, reason = await vectors.remote_status(conn, hub.settings)
            counts = await membership.counts(conn, hub.clock() - dt.timedelta(days=STATUS_WINDOW_DAYS))
        out = ClusteringStatus.model_validate({
            "config": config.model_dump(mode="json"),
            "vector_backend": {"pgvector": pgvector, "local_profile": "local-terms-v1", "remote": remote,
                               "remote_reason": reason},
            "window_days": STATUS_WINDOW_DAYS,
            "counts": counts,
        })
        return out.model_dump(mode="json")

    @r.put("/v1/clustering/config")
    async def put_clustering_config(request: Request, principal: Principal, body: ClusteringConfigUpdate) -> dict[str, Any]:
        _require_platform(principal)
        hub = _hub(request)
        try:
            async with tx(hub.pool, None) as conn:
                saved = await save_config(conn, body, hub.clock())
        except ConfigConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc), "current_version": exc.current}) from None
        return saved.model_dump(mode="json")

    @r.post("/v1/clustering/run")
    async def run_clustering(request: Request, principal: Principal) -> dict[str, Any]:
        _require_platform(principal)
        report = await run_maintenance(_hub(request))
        return {"merged": [{"source_event_id": a, "target_event_id": b} for a, b in report["merged"]],
                "embedded": report["embedded"], "embedding_failed": report["embedding_failed"]}

    return r
