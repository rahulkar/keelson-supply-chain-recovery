"""Secret lookup: ``/run/secrets/<name>`` (compose ``secrets:``) then env ``NAME``.

Values are never logged. A missing secret raises :class:`SecretNotFound` naming only the
secret, not any value.
"""

from __future__ import annotations

import os
from pathlib import Path

from keelson_engine.config import get_settings

__all__ = ["SecretNotFound", "read_secret"]


class SecretNotFound(LookupError):
    """Neither ``/run/secrets/<name>`` nor env ``<NAME>`` is set."""


def read_secret(name: str) -> str:
    """Return the secret ``name`` from ``<secrets_dir>/<name>`` or env ``NAME.upper()``.

    File contents are returned with surrounding whitespace stripped (PEM files and
    base64 keys are insensitive to the trailing newline).
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid secret name")
    path = Path(get_settings().secrets_dir) / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    except (IsADirectoryError, PermissionError) as exc:
        raise SecretNotFound(f"secret {name!r} is not readable at {path}") from exc
    if value:
        return value
    env_value = os.environ.get(name.upper(), "").strip()
    if env_value:
        return env_value
    raise SecretNotFound(f"secret {name!r} not found ({path} or env {name.upper()})")
