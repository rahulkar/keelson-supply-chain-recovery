"""Service secrets at rest (``hub_secret``): AES-256-GCM with a key-encryption key derived by
HKDF-SHA256 from ``SIGNALS_DATA_KEY`` (preferred, a dedicated secret) or, when that is unset, from
``SIGNAL_HUB_SHARED_SECRET`` (a separate HKDF ``info``, so the derived key is unrelated to the JWT
signing use of the same secret).

Stored as ``v1.<base64url(nonce || ciphertext+tag)>`` with the secret's name as associated data, so
a row copied under another name does not decrypt. ``kek_id`` (``data:<8 hex>`` / ``shared:<8 hex>``,
a fingerprint of the *derived* key) tells a rotated secret apart from a corrupt row. Plaintexts are
never logged; errors never include them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

import asyncpg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = ["Kek", "SecretBoxError", "derive_kek", "load_secret", "open_box", "seal", "store_secret"]

_SALT = b"keelson-signal-hub/hub_secret"
_INFO = b"signal-hub hub_secret aes-256-gcm v1"
_PREFIX = "v1."


class SecretBoxError(RuntimeError):
    """A secret cannot be sealed/opened (no key configured, other key, tampered row)."""


@dataclass(frozen=True, slots=True)
class Kek:
    key: bytes
    kek_id: str

    def __repr__(self) -> str:  # never print key material
        return f"Kek(kek_id={self.kek_id!r})"


def derive_kek(*, data_key: str | None, shared_secret: str | None) -> Kek | None:
    """The key-encryption key, or ``None`` when neither secret is configured."""
    source, secret = ("data", data_key) if data_key else ("shared", shared_secret)
    if not secret:
        return None
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(secret.encode("utf-8"))
    fingerprint = hashlib.sha256(b"kek-id:" + key).hexdigest()[:8]
    return Kek(key, f"{source}:{fingerprint}")


def _aad(name: str) -> bytes:
    return f"hub_secret:{name}".encode()


def seal(kek: Kek, name: str, plaintext: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(kek.key).encrypt(nonce, plaintext.encode("utf-8"), _aad(name))
    return _PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def open_box(kek: Kek, name: str, token: str) -> str:
    if not token.startswith(_PREFIX):
        raise SecretBoxError("unknown secret format")
    try:
        raw = base64.urlsafe_b64decode(token[len(_PREFIX):].encode("ascii"))
        if len(raw) < 12 + 16:
            raise SecretBoxError("secret too short")
        return AESGCM(kek.key).decrypt(raw[:12], raw[12:], _aad(name)).decode("utf-8")
    except (InvalidTag, ValueError, UnicodeError) as exc:
        raise SecretBoxError(f"secret {name!r} does not decrypt with the configured key ({type(exc).__name__})") from None


async def store_secret(conn: asyncpg.Connection, kek: Kek, name: str, plaintext: str, meta: dict[str, Any]) -> None:
    """Upsert one secret (``conn`` must be in the system context)."""
    await conn.execute(
        """INSERT INTO hub_secret (name, ciphertext, kek_id, meta, updated_at) VALUES ($1, $2, $3, $4::text::jsonb, now())
           ON CONFLICT (name) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, kek_id = EXCLUDED.kek_id,
             meta = EXCLUDED.meta, updated_at = now()""",
        name, seal(kek, name, plaintext), kek.kek_id, json.dumps(meta, sort_keys=True),
    )


async def load_secret(conn: asyncpg.Connection, kek: Kek, name: str) -> str | None:
    """The decrypted secret, ``None`` when there is no row. :class:`SecretBoxError` when the row was
    sealed with another key (secret rotated) or does not decrypt."""
    row = await conn.fetchrow("SELECT ciphertext, kek_id FROM hub_secret WHERE name = $1", name)
    if row is None:
        return None
    if row["kek_id"] != kek.kek_id:
        raise SecretBoxError(f"secret {name!r} was sealed with another key ({row['kek_id']}, configured {kek.kek_id})")
    return open_box(kek, name, str(row["ciphertext"]))
