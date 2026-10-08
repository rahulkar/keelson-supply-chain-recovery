"""Client for the optimizer service (``docs/contracts/40-optimizer.md``).

Auth: ``Authorization: Bearer <HS256 JWT>`` with ``{iss: keelson-engine, aud:
keelson-optimizer, tenant, exp}`` signed with ``OPTIMIZER_SHARED_SECRET``. The optimizer
runs one computation at a time, so calls are made sequentially by the nodes and the read
timeout allows for queueing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypeVar

import httpx
import jwt
import structlog
from keelson_contracts.optimizer import (
    ImpactRequest,
    ImpactResponse,
    OptimizeRequest,
    OptimizeResponse,
)
from pydantic import BaseModel, ValidationError

from .errors import OptimizerError

__all__ = ["OptimizerClient", "get_optimizer", "mint_service_token", "set_optimizer"]

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


def mint_service_token(tenant: str, secret: str, *, ttl_s: int = 120, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    return jwt.encode(
        {"iss": "keelson-engine", "aud": "keelson-optimizer", "tenant": tenant, "iat": issued, "exp": issued + ttl_s},
        secret,
        algorithm="HS256",
    )


class OptimizerClient:
    def __init__(
        self,
        *,
        base_url: str,
        secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        retries: int = 2,
    ) -> None:
        if not secret:
            raise OptimizerError("-", None, "OPTIMIZER_SHARED_SECRET is not configured")
        self._secret = secret
        self._retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout or httpx.Timeout(120.0, connect=5.0),
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def impact(self, req: ImpactRequest) -> ImpactResponse:
        return await self._post("/v1/impact", req, ImpactResponse)

    async def optimize(self, req: OptimizeRequest) -> OptimizeResponse:
        return await self._post("/v1/optimize", req, OptimizeResponse)

    async def _post(self, path: str, req: BaseModel, model: type[T]) -> T:
        tenant = str(getattr(getattr(req, "data", None), "tenant", ""))
        body: dict[str, Any] = req.model_dump(mode="json", exclude_none=True)
        attempt = 0
        while True:
            attempt += 1
            headers = {"authorization": f"Bearer {mint_service_token(tenant, self._secret)}"}
            try:
                resp = await self._client.post(path, json=body, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt <= self._retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise OptimizerError(path, None, f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code in (502, 503, 504) and attempt <= self._retries:
                await asyncio.sleep(0.5 * attempt)
                continue
            if resp.status_code != 200:
                raise OptimizerError(path, resp.status_code, resp.text[:500])
            try:
                return model.model_validate(resp.json())
            except (ValidationError, ValueError) as exc:
                raise OptimizerError(path, resp.status_code, f"invalid response: {exc}") from exc


_client: OptimizerClient | None = None


def get_optimizer() -> OptimizerClient:
    global _client
    if _client is None:
        from .settings import engine_settings  # noqa: PLC0415

        settings = engine_settings()
        _client = OptimizerClient(
            base_url=settings.optimizer_url, secret=settings.optimizer_shared_secret.get_secret_value()
        )
    return _client


def set_optimizer(client: OptimizerClient | None) -> None:
    global _client
    _client = client
