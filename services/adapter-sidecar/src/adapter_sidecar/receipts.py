"""Verification of Keelson approval receipts (``docs/contracts/10-auth.md``; contract 122 §5.4).

Same algorithm as Merxa's verifier, bound to **this** sidecar:

* header ``typ: approval-receipt+jwt``, ``alg: ES256``, a ``kid`` from the Keelson JWKS
  (``KEELSON_JWKS_URL``; cached ``RECEIPT_JWKS_TTL_S``, refetched for an unknown ``kid`` at most
  every 10 s; a fetch failure is the transient ``receipt_jwks_unavailable``);
* ``aud`` == ``MCP_RESOURCE`` (Keelson mints non-platform receipts for the connector's MCP URL, so a
  receipt for Merxa — or for another sidecar — is refused: ``wrong_audience``), ``iss`` ∈
  ``TRUSTED_RECEIPT_ISSUERS``, the required claims, ``exp`` with ``RECEIPT_LEEWAY_S``;
* bindings: ``change_set_hash`` == ``preview_hash``, ``change_set_ref`` ∈ {id, code},
  ``approval_id`` == ``approval_ref``, ``ext_iss`` (when present) == the configured issuer,
  ``tenant`` ∈ ``receipts.tenants``, ``ext_sub`` == ``receipts.expected_ext_sub``;
  ``co_approved_by`` checked like Merxa (distinct ``user:<name>`` refs, user approvals only);
* a ``user:`` approval needs the caller's scope ``changes:apply``; a ``policy:`` approval needs
  ``changes:apply:auto`` and passes the sidecar's own auto-commit caps
  (:func:`check_autocommit_caps`) — defence in depth on top of Keelson's policy;
* the ``jti`` is single-use across apply and rollback (``used_receipt``; checked by the caller
  inside its write transaction).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt.algorithms import ECAlgorithm

from adapter_sidecar.config import SCOPE_APPLY, SCOPE_APPLY_AUTO, AutocommitSpec, ReceiptsSpec
from adapter_sidecar.errors import Problem
from adapter_sidecar.logs import get_logger

log = get_logger(__name__)

RECEIPT_TYP = "approval-receipt+jwt"
_MIN_REFETCH_INTERVAL_S = 10.0
_CO_APPROVER_RE = re.compile(r"^user:[^\s:@]+$")
MAX_CO_APPROVERS = 9
MAX_NAME_CHARS = 200
_REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "jti", "approval_id", "change_set_ref", "change_set_hash",
                    "approved_by", "tenant")


class ReceiptError(Problem):
    """Receipt rejected: HTTP 403, code ``invalid_receipt`` and a ``reason``."""

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
    def approved_by(self) -> str:
        return str(self.claims["approved_by"])

    @property
    def approver_name(self) -> str | None:
        value = self.claims.get("approver_name")
        return str(value) if isinstance(value, str) and _display_ok(value) else None

    @property
    def initiated_by(self) -> str | None:
        value = self.claims.get("initiated_by")
        return str(value) if isinstance(value, str) and _display_ok(value) else None

    @property
    def is_policy(self) -> bool:
        return self.approved_by.startswith("policy:")

    @property
    def co_approved_by(self) -> tuple[str, ...]:
        value = self.claims.get("co_approved_by")
        return tuple(value) if isinstance(value, list) else ()

    @property
    def co_approvers(self) -> list[dict[str, str]]:
        """``[{ref, name}]`` (names from the optional parallel ``co_approver_names`` claim)."""
        refs = self.co_approved_by
        raw = self.claims.get("co_approver_names")
        names = [ref.removeprefix("user:") for ref in refs]
        if isinstance(raw, list) and len(raw) == len(refs) and all(isinstance(n, str) and _display_ok(n) for n in raw):
            names = [str(n).strip() for n in raw]
        return [{"ref": ref, "name": name} for ref, name in zip(refs, names, strict=True)]


def _display_ok(name: str) -> bool:
    stripped = name.strip()
    return 0 < len(stripped) <= MAX_NAME_CHARS and all(ch.isprintable() for ch in stripped)


def check_co_approvers(claims: dict[str, Any]) -> None:
    if "co_approved_by" not in claims:
        return
    value = claims["co_approved_by"]
    if not isinstance(value, list) or len(value) > MAX_CO_APPROVERS:
        raise ReceiptError("bad_co_approved_by", f"co_approved_by must be a list of at most {MAX_CO_APPROVERS} approvers")
    if not value:
        return
    if not all(isinstance(v, str) and _CO_APPROVER_RE.match(v) for v in value):
        raise ReceiptError("bad_co_approved_by", "co_approved_by entries must look like 'user:<username>'")
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
        self._fetched_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _fetch(self) -> None:
        if self._client is not None:
            resp = await self._client.get(self.url)
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False, trust_env=False) as c:
                resp = await c.get(self.url)
        resp.raise_for_status()
        data = resp.json()
        keys: dict[str, Any] = {}
        for jwk in data.get("keys", []) if isinstance(data, dict) else []:
            if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or not jwk.get("kid"):
                continue
            if jwk.get("use") not in (None, "sig"):
                continue
            try:
                keys[str(jwk["kid"])] = ECAlgorithm.from_jwk(json.dumps(jwk))
            except Exception as exc:  # noqa: BLE001 - one bad key never blocks the others
                log.warning("skipping unusable keelson jwk", extra={"kid": jwk.get("kid"), "error": str(exc)})
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def get_key(self, kid: str) -> Any:
        now = time.monotonic()
        if now - self._fetched_at < self.ttl_s and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            stale = now - self._fetched_at >= self.ttl_s
            unknown = kid not in self._keys
            if stale or (unknown and now - self._fetched_at >= _MIN_REFETCH_INTERVAL_S):
                try:
                    await self._fetch()
                except (httpx.HTTPError, ValueError) as exc:
                    raise Problem(503, "receipt_jwks_unavailable",
                                  f"cannot fetch the receipt issuer's JWKS ({type(exc).__name__}); retry later",
                                  headers={"Retry-After": "5"}) from exc
        key = self._keys.get(kid)
        if key is None:
            raise ReceiptError("unknown_kid", f"no Keelson signing key with kid {kid!r}")
        return key


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    change_set_id: str
    change_set_code: str
    preview_hash: str
    approval_ref: str
    scopes: frozenset[str]
    """Scopes of the caller's credential."""


class ReceiptVerifier:
    def __init__(self, *, audience: str, issuers: Sequence[str], ext_iss: str, rules: ReceiptsSpec,
                 jwks_url: str, jwks_ttl_s: int = 300, leeway_s: int = 30,
                 http_client: httpx.AsyncClient | None = None) -> None:
        self.audience = audience
        self.issuers = list(issuers)
        self.ext_iss = ext_iss.rstrip("/")
        self.rules = rules
        self.leeway_s = leeway_s
        self.jwks = KeelsonJwks(jwks_url, jwks_ttl_s, http_client)

    async def verify(self, token: str, ctx: ReceiptContext) -> VerifiedReceipt:
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
                token, key, algorithms=["ES256"], audience=self.audience, issuer=self.issuers,
                leeway=self.leeway_s, options={"require": list(_REQUIRED_CLAIMS)},
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

    def _check_binding(self, claims: dict[str, Any], ctx: ReceiptContext) -> None:
        if claims.get("change_set_hash") != ctx.preview_hash:
            raise ReceiptError("hash_mismatch", "receipt change_set_hash does not match preview_hash")
        if str(claims.get("change_set_ref")) not in {ctx.change_set_id, ctx.change_set_code}:
            raise ReceiptError("change_set_mismatch", "receipt change_set_ref does not match this change set")
        if str(claims.get("approval_id")) != ctx.approval_ref:
            raise ReceiptError("approval_ref_mismatch", "receipt approval_id does not match approval_ref")
        ext_iss = claims.get("ext_iss")
        if ext_iss is not None and str(ext_iss).rstrip("/") != self.ext_iss:
            raise ReceiptError("ext_iss_mismatch", "receipt ext_iss is not this system")
        if self.rules.tenants and str(claims.get("tenant")) not in self.rules.tenants:
            raise ReceiptError("wrong_tenant", "receipt tenant is not served by this sidecar")
        if self.rules.expected_ext_sub is not None and str(claims.get("ext_sub")) != self.rules.expected_ext_sub:
            raise ReceiptError("ext_sub_mismatch", "receipt ext_sub does not name this connector")
        approved_by = str(claims.get("approved_by"))
        if approved_by.startswith("user:"):
            if SCOPE_APPLY not in ctx.scopes:
                raise ReceiptError("insufficient_scope", f"a delegated apply requires scope {SCOPE_APPLY}")
        elif approved_by.startswith("policy:"):
            if SCOPE_APPLY_AUTO not in ctx.scopes:
                raise ReceiptError("insufficient_scope", f"a policy apply requires scope {SCOPE_APPLY_AUTO}")
            if self.rules.autocommit is None:
                raise ReceiptError("policy_not_accepted", "this sidecar accepts no auto-policy receipts")
        else:
            raise ReceiptError("bad_approved_by", "approved_by must start with 'user:' or 'policy:'")


def check_autocommit_caps(caps: AutocommitSpec | None, ops: Iterable[str], items: int, units: int) -> None:
    """The sidecar's own caps for policy receipts (403 ``auto_cap_exceeded``)."""
    if caps is None:
        raise Problem(403, "auto_cap_exceeded", "this sidecar accepts no auto-policy applies")
    allowed = set(caps.ops)
    used = set(ops)
    reasons = []
    if not used <= allowed:
        reasons.append(f"ops {sorted(used - allowed)} are not allowed for auto-commit")
    if items > caps.max_items:
        reasons.append(f"{items} items exceeds the cap of {caps.max_items}")
    if units > caps.max_units:
        reasons.append(f"{units} units exceeds the cap of {caps.max_units}")
    if reasons:
        raise Problem(403, "auto_cap_exceeded", "; ".join(reasons),
                      extra={"caps": {"ops": sorted(allowed), "max_items": caps.max_items, "max_units": caps.max_units}})
