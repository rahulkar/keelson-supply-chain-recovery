"""Merxa-issued API keys (``mx_<prefix>_<secret>``), docs/contracts/22-merxa-m2.md §6.

Only ``sha256(key)`` is stored; the key is returned once at mint time. Keys authenticate
``/mcp-lite`` (header ``X-Merxa-Api-Key`` or ``Authorization: Bearer mx_…``) and REST; the
key's scopes are its grant (no realm roles). ``last_used_at`` is updated at most once a
minute per key.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from merxa_api import tables as t
from merxa_api.errors import Problem, not_found
from merxa_api.security.principal import READ_SCOPES, SCOPE_APPLY, SCOPE_PREVIEW, Principal
from merxa_api.services import audit
from merxa_api.services.audit import Attribution
from merxa_api.services.common import jsonable, row_dict

KEY_RE = re.compile(r"^mx_([a-z0-9]{8})_([A-Za-z0-9]{32})$")
ALLOWED_SCOPES: tuple[str, ...] = (*READ_SCOPES, SCOPE_PREVIEW, SCOPE_APPLY)
_PREFIX_ALPHABET = string.ascii_lowercase + string.digits
_SECRET_ALPHABET = string.ascii_letters + string.digits
TOUCH_INTERVAL_S = 60


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def looks_like_key(value: str | None) -> bool:
    return bool(value) and str(value).startswith("mx_")


@dataclass(frozen=True, slots=True)
class ApiKeyInfo:
    id: uuid.UUID
    name: str
    prefix: str
    scopes: tuple[str, ...]

    def principal(self) -> Principal:
        return Principal(
            sub=f"apikey:{self.id}",
            kind="api_key",
            username=self.name,
            display=f"API key {self.name}",
            client_id=f"apikey:{self.prefix}",
            scopes=frozenset(self.scopes),
            roles=frozenset(),
            claims={},
        )


class ApiKeyService:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def mint(self, principal: Principal, *, name: Any, scopes: Any) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or "\x00" in name:
            raise Problem(400, "invalid_parameter", "name must be 1..100 characters")
        if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
            raise Problem(400, "invalid_parameter", "scopes must be a non-empty array of strings")
        bad = sorted(set(scopes) - set(ALLOWED_SCOPES))
        if bad:
            raise Problem(
                400, "invalid_scope", f"scopes {bad} cannot be granted to API keys; allowed: {list(ALLOWED_SCOPES)}"
            )
        scopes = [s for s in ALLOWED_SCOPES if s in set(scopes)]
        ak = t.api_key
        async with self.engine.begin() as conn:
            for _ in range(5):
                prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(8))
                taken = (await conn.execute(select(ak.c.id).where(ak.c.prefix == prefix))).first()
                if taken is None:
                    break
            else:  # pragma: no cover - 36^8 space
                raise Problem(503, "try_again", "could not allocate a key prefix")
            key = f"mx_{prefix}_" + "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(32))
            row = (
                await conn.execute(
                    ak.insert()
                    .values(
                        name=name.strip(),
                        prefix=prefix,
                        sha256=hash_key(key),
                        scopes=scopes,
                        created_by=principal.username or principal.sub,
                    )
                    .returning(ak.c.id, ak.c.created_at)
                )
            ).one()
            await audit.write(
                conn,
                Attribution.of(principal),
                entity_type="api_key",
                entity_ref=prefix,
                action="create",
                after={"name": name.strip(), "prefix": prefix, "scopes": scopes},
            )
        return {
            "id": str(row.id),
            "name": name.strip(),
            "prefix": prefix,
            "scopes": scopes,
            "created_by": principal.username or principal.sub,
            "created_at": jsonable(row.created_at),
            "key": key,
            "key_shown_once": True,
        }

    async def list(self, *, include_revoked: bool = True) -> dict[str, Any]:
        ak = t.api_key
        q = select(
            ak.c.id,
            ak.c.name,
            ak.c.prefix,
            ak.c.scopes,
            ak.c.created_by,
            ak.c.created_at,
            ak.c.last_used_at,
            ak.c.revoked_at,
            ak.c.version,
        ).order_by(ak.c.created_at, ak.c.id)
        if not include_revoked:
            q = q.where(ak.c.revoked_at.is_(None))
        async with self.engine.connect() as conn:
            items = [row_dict(r) for r in await conn.execute(q)]
        for it in items:
            it["status"] = "revoked" if it["revoked_at"] else "active"
        return {"items": items}

    async def revoke(self, principal: Principal, key_id: str) -> dict[str, Any]:
        ak = t.api_key
        try:
            kid = uuid.UUID(str(key_id))
        except ValueError as exc:
            raise not_found("api key", key_id) from exc
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(ak).where(ak.c.id == kid).with_for_update())).first()
            if row is None:
                raise not_found("api key", key_id)
            if row.revoked_at is None:
                row = (
                    await conn.execute(update(ak).where(ak.c.id == kid).values(revoked_at=func.now()).returning(ak))
                ).one()
                await audit.write(
                    conn,
                    Attribution.of(principal),
                    entity_type="api_key",
                    entity_ref=row.prefix,
                    action="revoke",
                    after={"name": row.name},
                )
        out = row_dict(row, exclude=("sha256", "updated_at"))
        out["status"] = "revoked"
        return out

    async def verify(self, key: str | None) -> ApiKeyInfo | None:
        """The active key matching ``key`` (constant-time hash compare), else ``None``."""
        m = KEY_RE.match(key or "")
        if not m:
            return None
        ak = t.api_key
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(ak.c.id, ak.c.name, ak.c.prefix, ak.c.sha256, ak.c.scopes, ak.c.revoked_at).where(
                        ak.c.prefix == m.group(1)
                    )
                )
            ).first()
        if row is None or not hmac.compare_digest(row.sha256, hash_key(str(key))) or row.revoked_at is not None:
            return None
        await self._touch(row.id)
        return ApiKeyInfo(row.id, row.name, row.prefix, tuple(row.scopes or ()))

    async def _touch(self, key_id: uuid.UUID) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE merxa.api_key SET last_used_at = now() WHERE id = :id "
                    "AND (last_used_at IS NULL OR last_used_at < now() - make_interval(secs => :s))"
                ),
                {"id": key_id, "s": TOUCH_INTERVAL_S},
            )
