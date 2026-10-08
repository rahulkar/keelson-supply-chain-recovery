"""Verification of Keelson approval receipts (docs/contracts/10-auth.md).

A receipt is an ES256 JWT with header ``typ: approval-receipt+jwt`` signed by a key in
the Keelson JWKS (``KEELSON_JWKS_URL``). The JWKS is cached for ``RECEIPT_JWKS_TTL_S``
(default 5 min) and refetched when an unknown ``kid`` shows up (at most every 10 s).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt.algorithms import ECAlgorithm

from merxa_api.config import Settings
from merxa_api.errors import Problem
from merxa_api.logs import get_logger
from merxa_api.security.principal import (
    AUTOCOMMIT_CLIENT,
    ROLE_OPS_DIRECTOR,
    SCOPE_APPLY,
    SCOPE_APPLY_AUTO,
    Principal,
)

log = get_logger(__name__)

RECEIPT_TYP = "approval-receipt+jwt"
_MIN_REFETCH_INTERVAL_S = 10.0
# M5 (docs/contracts/95 §1): the other members of a four-eyes quorum, ``user:<username>``.
_CO_APPROVER_RE = re.compile(r"^user:[^\s:@]+$")
MAX_CO_APPROVERS = 9
MAX_NAME_CHARS = 200
_REQUIRED_CLAIMS = (
    "iss",
    "aud",
    "exp",
    "iat",
    "jti",
    "approval_id",
    "change_set_ref",
    "change_set_hash",
    "approved_by",
)


class ReceiptError(Problem):
    """Receipt rejected. Always HTTP 403 with code ``invalid_receipt`` + a reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(403, "invalid_receipt", detail, extra={"reason": reason})
        self.reason = reason


@dataclass(frozen=True, slots=True)
class VerifiedReceipt:
    claims: dict[str, Any]

    @property
    def jti(self) -> str:
        return str(self.claims["jti"])

    @property
    def approval_id(self) -> str:
        return str(self.claims["approval_id"])

    @property
    def approved_by(self) -> str:
        return str(self.claims["approved_by"])

    @property
    def initiated_by(self) -> str | None:
        value = self.claims.get("initiated_by")
        return str(value) if value is not None else None

    @property
    def approver_name(self) -> str | None:
        value = self.claims.get("approver_name")
        return str(value) if value is not None else None

    @property
    def is_policy(self) -> bool:
        return self.approved_by.startswith("policy:")

    @property
    def co_approved_by(self) -> tuple[str, ...]:
        """Validated ``co_approved_by`` claim (``()`` when absent); see :func:`check_co_approvers`."""
        value = self.claims.get("co_approved_by")
        return tuple(value) if isinstance(value, list) else ()

    @property
    def co_approvers(self) -> list[dict[str, str]]:
        """``[{ref, name}]`` for attribution. The display name comes from the optional
        ``co_approver_names`` claim (parallel to ``co_approved_by``) when it is well formed,
        else from the username in the reference (``user:lena`` → ``lena``)."""
        refs = self.co_approved_by
        raw = self.claims.get("co_approver_names")
        names: list[str] = [ref.removeprefix("user:") for ref in refs]
        if isinstance(raw, list) and len(raw) == len(refs) and all(isinstance(n, str) and _display_ok(n) for n in raw):
            names = [str(n).strip() for n in raw]
        return [{"ref": ref, "name": name} for ref, name in zip(refs, names, strict=True)]


def _display_ok(name: str) -> bool:
    stripped = name.strip()
    return 0 < len(stripped) <= MAX_NAME_CHARS and all(ch.isprintable() for ch in stripped)


def check_co_approvers(claims: dict[str, Any]) -> None:
    """``co_approved_by`` (optional, M5): a list of at most 9 distinct ``user:<username>`` refs
    (compared case-insensitively), only on user approvals, never repeating ``approved_by`` (an
    empty list means none).
    Anything else rejects the receipt."""
    if "co_approved_by" not in claims:
        return
    value = claims["co_approved_by"]
    if not isinstance(value, list) or len(value) > MAX_CO_APPROVERS:
        raise ReceiptError(
            "bad_co_approved_by", f"co_approved_by must be a list of at most {MAX_CO_APPROVERS} approvers"
        )
    if not value:
        return
    if not all(isinstance(v, str) and _CO_APPROVER_RE.match(v) for v in value):
        raise ReceiptError("bad_co_approved_by", "co_approved_by entries must look like 'user:<username>'")
    # Usernames are case-insensitive in the IdP (Keycloak lower-cases them), so "user:Lena" and
    # "user:lena" are the same person: never count one person twice in a quorum.
    folded = [v.casefold() for v in value]
    if len(set(folded)) != len(folded):
        raise ReceiptError("bad_co_approved_by", "co_approved_by must not repeat an approver")
    approved_by = str(claims.get("approved_by"))
    if not approved_by.startswith("user:"):
        raise ReceiptError("bad_co_approved_by", "co_approved_by is only allowed on user approvals")
    if approved_by.casefold() in folded:
        raise ReceiptError("bad_co_approved_by", "co_approved_by must not include approved_by")


class KeelsonJwks:
    """Small async JWKS cache keyed by ``kid``."""

    def __init__(self, url: str, ttl_s: int, http_client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self.ttl_s = ttl_s
        self._client = http_client
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self) -> None:
        if self._client is not None:
            resp = await self._client.get(self.url)
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(self.url)
        resp.raise_for_status()
        data = resp.json()
        keys: dict[str, Any] = {}
        for jwk in data.get("keys", []):
            if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or not jwk.get("kid"):
                continue
            if jwk.get("use") not in (None, "sig"):
                continue
            try:
                keys[str(jwk["kid"])] = ECAlgorithm.from_jwk(json.dumps(jwk))
            except Exception as exc:
                log.warning("skipping unusable keelson jwk", extra={"kid": jwk.get("kid"), "error": str(exc)})
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def get_key(self, kid: str) -> Any:
        now = time.monotonic()
        fresh = now - self._fetched_at < self.ttl_s
        if fresh and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            stale = now - self._fetched_at >= self.ttl_s
            unknown = kid not in self._keys
            if stale or (unknown and now - self._fetched_at >= _MIN_REFETCH_INTERVAL_S):
                try:
                    await self._fetch()
                except (httpx.HTTPError, ValueError) as exc:
                    # Transient: 503 so the caller retries (same idempotency key) instead of
                    # treating the approval as rejected. Never a reason to accept stale keys.
                    raise Problem(
                        503,
                        "receipt_jwks_unavailable",
                        f"cannot fetch the receipt issuer's JWKS ({type(exc).__name__}); retry later",
                        headers={"Retry-After": "5"},
                    ) from exc
        key = self._keys.get(kid)
        if key is None:
            raise ReceiptError("unknown_kid", f"no Keelson signing key with kid {kid!r}")
        return key


@dataclass(frozen=True, slots=True)
class ApplyContext:
    """What the receipt is checked against."""

    change_set_id: str
    change_set_code: str
    preview_hash: str
    approval_ref: str
    principal: Principal


class ReceiptVerifier:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.jwks = KeelsonJwks(settings.keelson_jwks_url, settings.receipt_jwks_ttl_s, http_client)

    async def verify(self, token: str, ctx: ApplyContext) -> VerifiedReceipt:
        """Verify signature + claims; raise :class:`ReceiptError` on any failure.

        Auto-commit caps are enforced separately by the change-set engine (they depend
        on the items), see ``services.changesets``.
        """
        if not token or not isinstance(token, str):
            raise ReceiptError("missing", "approval_receipt is required")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise ReceiptError("malformed", f"receipt is not a JWT: {exc}") from exc
        if header.get("typ") != RECEIPT_TYP:
            raise ReceiptError("wrong_typ", f"receipt typ must be {RECEIPT_TYP!r}")
        if header.get("alg") != "ES256":
            raise ReceiptError("wrong_alg", "receipt must be signed with ES256")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ReceiptError("missing_kid", "receipt header has no kid")
        key = await self.jwks.get_key(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["ES256"],
                audience=self.settings.mcp_resource,
                issuer=self.settings.trusted_receipt_issuers,
                leeway=self.settings.receipt_leeway_s,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError as exc:
            raise ReceiptError("expired", "approval receipt has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise ReceiptError("wrong_audience", "receipt audience is not this resource") from exc
        except jwt.InvalidIssuerError as exc:
            raise ReceiptError("untrusted_issuer", "receipt issuer is not trusted") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise ReceiptError("missing_claim", str(exc)) from exc
        except jwt.InvalidSignatureError as exc:
            raise ReceiptError("bad_signature", "receipt signature is invalid") from exc
        except jwt.PyJWTError as exc:
            raise ReceiptError("invalid", f"receipt rejected: {exc}") from exc

        self._check_binding(claims, ctx)
        check_co_approvers(claims)
        return VerifiedReceipt(claims=claims)

    def _check_binding(self, claims: dict[str, Any], ctx: ApplyContext) -> None:
        if claims.get("change_set_hash") != ctx.preview_hash:
            raise ReceiptError("hash_mismatch", "receipt change_set_hash does not match preview_hash")
        if str(claims.get("change_set_ref")) not in {ctx.change_set_id, ctx.change_set_code}:
            raise ReceiptError("change_set_mismatch", "receipt change_set_ref does not match this change set")
        if str(claims.get("approval_id")) != ctx.approval_ref:
            raise ReceiptError("approval_ref_mismatch", "receipt approval_id does not match approval_ref")
        ext_iss = claims.get("ext_iss")
        if ext_iss is not None and str(ext_iss).rstrip("/") != self.settings.oidc_issuer.rstrip("/"):
            raise ReceiptError("ext_iss_mismatch", "receipt ext_iss is not the merxa realm")

        approved_by = str(claims.get("approved_by"))
        principal = ctx.principal
        if approved_by.startswith("user:"):
            if not claims.get("ext_sub") or str(claims["ext_sub"]) != principal.sub:
                raise ReceiptError("ext_sub_mismatch", "receipt ext_sub does not match the access token subject")
            if not principal.has_scope(SCOPE_APPLY):
                raise ReceiptError("insufficient_scope", f"delegated apply requires scope {SCOPE_APPLY}")
            if not principal.has_role(ROLE_OPS_DIRECTOR):
                raise ReceiptError("missing_role", f"delegated apply requires role {ROLE_OPS_DIRECTOR}")
        elif approved_by.startswith("policy:"):
            if principal.client_id != AUTOCOMMIT_CLIENT:
                raise ReceiptError("wrong_client", f"policy receipts are only accepted from {AUTOCOMMIT_CLIENT}")
            if not principal.has_scope(SCOPE_APPLY_AUTO):
                raise ReceiptError("insufficient_scope", f"policy apply requires scope {SCOPE_APPLY_AUTO}")
        else:
            raise ReceiptError("bad_approved_by", "approved_by must start with 'user:' or 'policy:'")
