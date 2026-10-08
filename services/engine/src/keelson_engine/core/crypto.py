"""Keys and signatures.

* **Envelope encryption** of tokens/keys at rest: AES-256-GCM with the data key
  ``keelson_data_key`` (base64 of 32 bytes, key id ``dk-1``). Token format
  ``"dk-1:" + base64url(nonce(12) || ciphertext||tag)``; the caller-supplied AAD binds a
  ciphertext to its row (e.g. ``"bff|<session_id>"``), so a ciphertext copied into
  another row does not decrypt.
* **JWKS** (``/api/.well-known/jwks.json``): public EC P-256 keys ``ca-1`` (client
  assertions, ``keelson_client_assertion_key``) and ``ar-1`` (approval receipts,
  ``keelson_receipt_key``).
* **Client assertion** (RFC 7523 ``private_key_jwt``) and **approval receipt** signing (ES256).
* **Quorum receipts** (M5, ``95-m5-remediation.md`` §1): :func:`quorum_receipt_claims` /
  :func:`sign_quorum_receipt` make the quorum member counted under ``approver`` the executor
  (``approved_by``, ``approver_name``, ``keelson_sub``) whatever the approval order, and list the
  other members as the additive claim ``co_approved_by: ["user:<username>", …]`` (approval order).
"""

from __future__ import annotations

import base64
import binascii
import os
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jwt.algorithms import ECAlgorithm

from keelson_contracts.receipts import (
    CLIENT_ASSERTION_KID,
    DEFAULT_RECEIPT_TTL_S,
    RECEIPT_ALG,
    RECEIPT_KID,
    ApprovalReceiptClaims,
    approved_by_user,
    receipt_header,
)
from keelson_engine.core.secrets import read_secret

__all__ = [
    "CLIENT_ASSERTION_SECRET",
    "DATA_KEY_ID",
    "DATA_KEY_SECRET",
    "DecryptionError",
    "RECEIPT_SECRET",
    "decrypt",
    "decrypt_str",
    "encrypt",
    "encrypt_str",
    "jwks",
    "quorum_receipt_claims",
    "reset_keys",
    "sign_approval_receipt",
    "sign_client_assertion",
    "sign_quorum_receipt",
    "split_quorum",
]

DATA_KEY_ID = "dk-1"
DATA_KEY_SECRET = "keelson_data_key"
CLIENT_ASSERTION_SECRET = "keelson_client_assertion_key"
RECEIPT_SECRET = "keelson_receipt_key"
CLIENT_ASSERTION_TTL_S = 60


class DecryptionError(ValueError):
    """Ciphertext is malformed, uses an unknown key id, or fails authentication."""


@dataclass(frozen=True, slots=True)
class _SigningKey:
    kid: str
    private: ec.EllipticCurvePrivateKey

    def public_jwk(self) -> dict[str, Any]:
        jwk = ECAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": "ES256"})
        return dict(jwk)


_lock = threading.Lock()
_data_keys: dict[str, AESGCM] = {}
_signing: dict[str, _SigningKey] = {}


def reset_keys() -> None:
    """Forget cached keys (tests / rotation)."""
    with _lock:
        _data_keys.clear()
        _signing.clear()


def _data_key(key_id: str = DATA_KEY_ID) -> AESGCM:
    if key_id != DATA_KEY_ID:
        raise DecryptionError(f"unknown data key id {key_id!r}")
    with _lock:
        cached = _data_keys.get(key_id)
        if cached is None:
            raw = read_secret(DATA_KEY_SECRET)
            try:
                key = base64.b64decode(raw, validate=True)
            except binascii.Error as exc:
                raise RuntimeError("keelson_data_key is not valid base64") from exc
            if len(key) != 32:
                raise RuntimeError("keelson_data_key must be 32 bytes (AES-256)")
            cached = AESGCM(key)
            _data_keys[key_id] = cached
        return cached


def encrypt(plaintext: bytes, aad: str) -> str:
    """AES-256-GCM encrypt; returns ``"dk-1:<b64url nonce|ct>"``."""
    if not aad:
        raise ValueError("aad is required")
    nonce = os.urandom(12)
    ct = _data_key().encrypt(nonce, plaintext, aad.encode("utf-8"))
    return f"{DATA_KEY_ID}:" + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(token: str, aad: str) -> bytes:
    key_id, sep, body = token.partition(":")
    if not sep or not body:
        raise DecryptionError("malformed ciphertext")
    aes = _data_key(key_id)
    try:
        blob = base64.urlsafe_b64decode(body.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise DecryptionError("malformed ciphertext") from exc
    if len(blob) < 12 + 16:
        raise DecryptionError("malformed ciphertext")
    try:
        return aes.decrypt(blob[:12], blob[12:], aad.encode("utf-8"))
    except Exception as exc:  # cryptography raises InvalidTag
        raise DecryptionError("ciphertext authentication failed") from exc


def encrypt_str(plaintext: str, aad: str) -> str:
    return encrypt(plaintext.encode("utf-8"), aad)


def decrypt_str(token: str, aad: str) -> str:
    return decrypt(token, aad).decode("utf-8")


def _signing_key(kid: str, secret: str) -> _SigningKey:
    with _lock:
        cached = _signing.get(kid)
        if cached is None:
            pem = read_secret(secret).encode("utf-8")
            key = serialization.load_pem_private_key(pem, password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                raise RuntimeError(f"{secret} must be an EC P-256 private key")
            cached = _SigningKey(kid, key)
            _signing[kid] = cached
        return cached


def _assertion_key() -> _SigningKey:
    return _signing_key(CLIENT_ASSERTION_KID, CLIENT_ASSERTION_SECRET)


def _receipt_key() -> _SigningKey:
    return _signing_key(RECEIPT_KID, RECEIPT_SECRET)


def jwks() -> dict[str, Any]:
    """Public JWKS with kids ``ca-1`` and ``ar-1``."""
    return {"keys": [_assertion_key().public_jwk(), _receipt_key().public_jwk()]}


def sign_client_assertion(client_id: str, audience: str, *, now: int | None = None) -> str:
    """RFC 7523 client assertion for ``private_key_jwt`` (``kid ca-1``, 60 s lifetime)."""
    if not client_id or not audience:
        raise ValueError("client_id and audience are required")
    iat = int(time.time()) if now is None else now
    claims = {
        "iss": client_id,
        "sub": client_id,
        "aud": audience,
        "jti": str(uuid.uuid4()),
        "iat": iat,
        "exp": iat + CLIENT_ASSERTION_TTL_S,
    }
    key = _assertion_key()
    return jwt.encode(claims, key.private, algorithm="ES256", headers={"kid": key.kid, "typ": "JWT"})


def sign_approval_receipt(claims: ApprovalReceiptClaims) -> str:
    """Compact JWS (ES256, ``kid ar-1``, ``typ approval-receipt+jwt``) over the receipt claims."""
    header = receipt_header()
    key = _receipt_key()
    return jwt.encode(
        claims.to_claims(),
        key.private,
        algorithm=RECEIPT_ALG,
        headers={"kid": header["kid"], "typ": header["typ"]},
    )


def _member(d: Any) -> dict[str, Any]:
    if isinstance(d, Mapping):
        return {"sub": str(d.get("sub") or ""), "username": str(d.get("username") or d.get("sub") or ""),
                "name": str(d.get("name") or ""), "role": d.get("role")}
    return {"sub": str(d.sub), "username": str(d.username), "name": str(d.name or ""), "role": getattr(d, "role", None)}


def split_quorum(decided_by: Sequence[Any], *, executor_role: str = "approver"
                 ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(executor, co_approvers)`` of a resolved approval: the member counted under
    ``executor_role`` (else the first one) and the others in approval order (one per person).
    ``decided_by`` = ``ResumePayload.decided_by`` (``Decider`` models or their JSON)."""
    members = [_member(d) for d in decided_by]
    if not members:
        raise ValueError("decided_by is empty")
    executor = next((m for m in members if m["role"] == executor_role), members[0])
    others: list[dict[str, Any]] = []
    seen = {executor["sub"], executor["username"]}
    for m in members:
        if m["sub"] in seen or m["username"] in seen:
            continue
        seen.update((m["sub"], m["username"]))
        others.append(m)
    return executor, others


def quorum_receipt_claims(
    *,
    approval_id: str,
    change_set_ref: str,
    change_set_hash: str,
    tenant: str,
    decided_by: Sequence[Any],
    ext_sub: str,
    initiated_by: str,
    policy_version: str,
    executor_role: str = "approver",
    ttl_s: int = DEFAULT_RECEIPT_TTL_S,
    now: int | None = None,
    jti: str | None = None,
    aud: str | None = None,
    ext_iss: str | None = None,
) -> ApprovalReceiptClaims:
    """Receipt claims for a (four-eyes) quorum approval: the executor (see :func:`split_quorum`)
    is ``approved_by``/``approver_name``/``keelson_sub`` (``ext_sub`` must be the executor's
    linked Merxa subject); the other members become ``co_approved_by``. A single approver yields
    exactly the M1 claim set (``co_approved_by`` omitted). ``aud`` / ``ext_iss`` (M8): a
    non-platform target system (``None`` = Merxa's)."""
    executor, others = split_quorum(decided_by, executor_role=executor_role)
    target = {k: v for k, v in (("aud", aud), ("ext_iss", ext_iss)) if v is not None}
    return ApprovalReceiptClaims.issue(
        approval_id=approval_id,
        change_set_ref=change_set_ref,
        change_set_hash=change_set_hash,
        tenant=tenant,
        keelson_sub=executor["sub"],
        ext_sub=ext_sub,
        approved_by=approved_by_user(executor["username"]),
        approver_name=executor["name"] or executor["username"],
        initiated_by=initiated_by,
        policy_version=policy_version,
        ttl_s=ttl_s,
        now=now,
        jti=jti,
        co_approved_by=[approved_by_user(m["username"]) for m in others],
        **target,
    )


def sign_quorum_receipt(**kwargs: Any) -> tuple[str, ApprovalReceiptClaims]:
    """:func:`quorum_receipt_claims` + :func:`sign_approval_receipt` → ``(jws, claims)``."""
    claims = quorum_receipt_claims(**kwargs)
    return sign_approval_receipt(claims), claims
