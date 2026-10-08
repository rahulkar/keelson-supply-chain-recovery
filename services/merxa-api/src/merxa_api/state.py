"""Process-wide service container shared by the REST app and the MCP servers."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine

from merxa_api.config import Settings
from merxa_api.db import create_engine
from merxa_api.security.oidc import OidcClient
from merxa_api.security.receipts import ReceiptVerifier
from merxa_api.security.tokens import DiscoveringJWTVerifier, build_token_verifier
from merxa_api.services.admin import AdminService
from merxa_api.services.apikeys import ApiKeyService
from merxa_api.services.changesets import ChangeSetService
from merxa_api.services.clients import ClientsSeen
from merxa_api.services.manual import ManualChangeService
from merxa_api.services.snapshots import SnapshotService
from merxa_api.services.webhooks import WebhookService


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    token_verifier: DiscoveringJWTVerifier
    receipts: ReceiptVerifier
    oidc: OidcClient
    changesets: ChangeSetService
    manual: ManualChangeService
    admin: AdminService
    apikeys: ApiKeyService
    webhooks: WebhookService
    snapshots: SnapshotService
    clients_seen: ClientsSeen

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        engine: AsyncEngine | None = None,
        idp_http_client: httpx2.AsyncClient | None = None,
        http_client: httpx.AsyncClient | None = None,
        webhook_http_client: httpx.AsyncClient | None = None,
    ) -> AppState:
        """Wire services. Tests inject HTTP clients with mock transports for the IdP
        (merxa realm JWKS, via ``httpx2`` as used by FastMCP's ``JWTVerifier``), for
        Keelson's receipt JWKS / OIDC endpoints (``httpx``) and for webhook receivers."""
        engine = engine or create_engine(settings.database_url)
        receipts = ReceiptVerifier(settings, http_client)
        return cls(
            settings=settings,
            engine=engine,
            token_verifier=build_token_verifier(settings, idp_http_client),
            receipts=receipts,
            oidc=OidcClient(settings, http_client),
            changesets=ChangeSetService(engine, settings, receipts),
            manual=ManualChangeService(engine, settings),
            admin=AdminService(engine, settings),
            apikeys=ApiKeyService(engine),
            webhooks=WebhookService(engine, settings, webhook_http_client),
            snapshots=SnapshotService(engine, settings),
            clients_seen=ClientsSeen(engine),
        )
