"""Approval receipt claims (Keelson → Merxa), exactly per ``10-auth.md``.

M5 (``95-m5-remediation.md`` §1) adds one optional claim, ``co_approved_by``
(``["user:<username>", …]``): the other members of a four-eyes quorum, in approval order.
It is omitted from the claim set while empty, so M1 receipts are byte-identical, and the
M1 verification rules are unchanged (Merxa ignores claims it does not know).

The engine signs these claims as a compact JWS (ES256, kid ``ar-1``, typ
``approval-receipt+jwt``) with ``/run/secrets/keelson_receipt_key.pem``; Merxa
verifies them against Keelson's JWKS before ``apply_change_set``. This module only
defines the claim set and header — signing lives in the engine (no JWT dependency
here).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import Field, StrictInt, model_validator

from .common import ContractModel, Sha256Hex, TenantSlug

__all__ = [
    "APPROVED_BY_PATTERN",
    "CO_APPROVED_BY_PATTERN",
    "MAX_CO_APPROVERS",
    "ApprovalReceiptClaims",
    "CLIENT_ASSERTION_KID",
    "DEFAULT_RECEIPT_TTL_S",
    "MERXA_ISSUER",
    "MERXA_MCP_RESOURCE",
    "RECEIPT_ALG",
    "RECEIPT_AUDIENCE",
    "RECEIPT_ISSUER",
    "RECEIPT_KID",
    "RECEIPT_TYP",
    "approved_by_policy",
    "approved_by_user",
    "receipt_header",
]

RECEIPT_TYP: Final = "approval-receipt+jwt"
RECEIPT_KID: Final = "ar-1"
RECEIPT_ALG: Final = "ES256"
CLIENT_ASSERTION_KID: Final = "ca-1"
RECEIPT_ISSUER: Final = "http://keelson.localhost"
MERXA_MCP_RESOURCE: Final = "http://merxa.localhost/mcp"
RECEIPT_AUDIENCE: Final = MERXA_MCP_RESOURCE
MERXA_ISSUER: Final = "http://auth.localhost/realms/merxa"
DEFAULT_RECEIPT_TTL_S: Final = 300

APPROVED_BY_PATTERN: Final = r"^(user:[^\s:@]+|policy:[^\s@]+@[^\s@]+)$"
CO_APPROVED_BY_PATTERN: Final = r"^user:[^\s:@]+$"
MAX_CO_APPROVERS: Final = 9
"""A quorum has at most 10 members (``policy.MAX_QUORUM_COUNT``): the executor + 9 co-approvers."""


def _is_empty(value: Any) -> bool:
    return not value


def approved_by_user(username: str) -> str:
    """``approved_by`` for a delegated human approval: ``"user:<keelson username>"``."""
    return f"user:{username}"


def approved_by_policy(rule_id: str, policy_version: str) -> str:
    """``approved_by`` for an auto-policy approval: ``"policy:<rule_id>@<policy_version>"``."""
    return f"policy:{rule_id}@{policy_version}"


def receipt_header() -> dict[str, str]:
    """JOSE header for approval receipts."""
    return {"alg": RECEIPT_ALG, "kid": RECEIPT_KID, "typ": RECEIPT_TYP}


class ApprovalReceiptClaims(ContractModel):
    """JWT claim set of an approval receipt."""

    iss: Annotated[str, Field(min_length=1)] = RECEIPT_ISSUER
    aud: Annotated[str, Field(min_length=1)] = RECEIPT_AUDIENCE
    iat: StrictInt
    exp: StrictInt
    jti: Annotated[str, Field(min_length=1)]
    approval_id: Annotated[str, Field(min_length=1)]
    change_set_ref: Annotated[str, Field(min_length=1)]
    """Merxa ``change_set_id``."""
    change_set_hash: Sha256Hex
    """``H`` — must equal Merxa's ``preview_hash``."""
    tenant: TenantSlug
    keelson_sub: Annotated[str, Field(min_length=1)]
    ext_iss: Annotated[str, Field(min_length=1)] = MERXA_ISSUER
    ext_sub: Annotated[str, Field(min_length=1)]
    approved_by: Annotated[str, Field(pattern=APPROVED_BY_PATTERN)]
    approver_name: str | None
    """Display name of the human approver; required for ``user:`` approvals, ``None``
    allowed for ``policy:`` approvals (Merxa reads it as optional)."""
    initiated_by: Annotated[str, Field(min_length=1)]
    policy_version: Annotated[str, Field(min_length=1)]
    co_approved_by: Annotated[
        list[Annotated[str, Field(pattern=CO_APPROVED_BY_PATTERN)]], Field(max_length=MAX_CO_APPROVERS)
    ] = Field(default=[], exclude_if=_is_empty)
    """M5, additive: the other quorum members (``user:<username>``), omitted while empty.
    Only on ``user:`` approvals; never repeats ``approved_by`` or a name."""

    @model_validator(mode="after")
    def _consistent(self) -> ApprovalReceiptClaims:
        if self.exp <= self.iat:
            raise ValueError("exp must be after iat")
        if self.co_approved_by:
            if not self.approved_by.startswith("user:"):
                raise ValueError("co_approved_by is only allowed on user approvals")
            if len(set(self.co_approved_by)) != len(self.co_approved_by):
                raise ValueError("co_approved_by must not repeat an approver")
            if self.approved_by in self.co_approved_by:
                raise ValueError("co_approved_by must not include approved_by")
        if self.approved_by.startswith("policy:"):
            # "policy:<rule_id>@<policy_version>" must name the same policy version
            # as the policy_version claim, or the receipt attributes the apply to a
            # rule set that did not take the decision.
            stated = self.approved_by.rsplit("@", 1)[1]
            if stated != self.policy_version:
                raise ValueError(
                    f"approved_by policy version {stated!r} != policy_version {self.policy_version!r}"
                )
        elif not self.approver_name:
            raise ValueError("a user approval (approved_by 'user:…') requires approver_name")
        return self

    @property
    def approval_kind(self) -> Literal["user", "policy"]:
        return "user" if self.approved_by.startswith("user:") else "policy"

    @classmethod
    def issue(
        cls,
        *,
        approval_id: str,
        change_set_ref: str,
        change_set_hash: str,
        tenant: str,
        keelson_sub: str,
        ext_sub: str,
        approved_by: str,
        approver_name: str | None,
        initiated_by: str,
        policy_version: str,
        ttl_s: int = DEFAULT_RECEIPT_TTL_S,
        now: int | None = None,
        jti: str | None = None,
        co_approved_by: Sequence[str] = (),
        aud: str = RECEIPT_AUDIENCE,
        ext_iss: str = MERXA_ISSUER,
    ) -> ApprovalReceiptClaims:
        """Claims with ``iat=now``, ``exp=now+ttl_s`` and a fresh ``jti`` (uuid4).

        ``co_approved_by`` (M5) lists the other quorum members (``user:<username>``).

        ``aud`` / ``ext_iss`` (M8, ``122-m8-adapter-sidecar.md`` §8): the system the receipt is for.
        The defaults are Merxa's, so platform receipts stay byte-identical; a non-platform connector
        (e.g. an adapter sidecar) gets its own MCP URL and issuer, so a receipt minted for one
        system is refused by every other (audience binding)."""
        iat = int(time.time()) if now is None else now
        return cls(
            aud=aud,
            ext_iss=ext_iss,
            iat=iat,
            exp=iat + ttl_s,
            jti=jti or str(uuid.uuid4()),
            approval_id=approval_id,
            change_set_ref=change_set_ref,
            change_set_hash=change_set_hash,
            tenant=tenant,
            keelson_sub=keelson_sub,
            ext_sub=ext_sub,
            approved_by=approved_by,
            approver_name=approver_name,
            initiated_by=initiated_by,
            policy_version=policy_version,
            co_approved_by=list(co_approved_by),
        )

    def to_claims(self) -> dict[str, Any]:
        """Claims dict for the JWT encoder (every M1 key present, ``approver_name`` may be null;
        ``co_approved_by`` only when non-empty)."""
        return self.model_dump(mode="json")
