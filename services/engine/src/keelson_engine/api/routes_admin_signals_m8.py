"""M8 Signals admin — clustering settings (``docs/contracts/123-m8-signal-clustering.md`` §7), under
``/api/admin``. Same rules as :mod:`keelson_engine.api.routes_admin_m7` (session + CSRF + Origin on
mutations, ``tenant-admin`` / ``platform-admin``).

==============================================  ==================================================
``GET  /signals/clustering``                    status: settings, backends, 7-day counts (+ ``editable``)
``PUT  /signals/clustering``                    ``ClusteringConfigUpdate`` → ``ClusteringConfig`` (platform
                                                admin only: 403 ``platform_admin_required``; 409
                                                ``stale_version``; 422 out of bounds)
``POST /signals/clustering/run``                one cluster maintenance round now (platform admin only)
==============================================  ==================================================
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from keelson_contracts.signals import ClusteringConfigUpdate
from keelson_engine.api.admin_deps import AdminRead, AdminWrite, require_platform
from keelson_engine.core import signals_clustering

__all__ = ["router"]

router = APIRouter(prefix="/api/admin")


@router.get("/signals/clustering")
async def clustering_status(ctx: AdminRead) -> dict[str, Any]:
    """Every admin may read the platform-wide settings; only a platform admin may edit them."""
    return await signals_clustering.get_clustering(ctx.tenant, editable=ctx.platform)


@router.put("/signals/clustering")
async def clustering_put(body: ClusteringConfigUpdate, ctx: AdminWrite) -> dict[str, Any]:
    require_platform(ctx)
    return await signals_clustering.put_clustering(ctx.tenant, body, ctx.actor())


@router.post("/signals/clustering/run")
async def clustering_run(ctx: AdminWrite) -> dict[str, Any]:
    require_platform(ctx)
    return await signals_clustering.run_clustering(ctx.tenant, ctx.actor())
