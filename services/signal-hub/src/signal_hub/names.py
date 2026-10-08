"""Organisation-name match keys (``124`` §1): exact, normalised, never fuzzy.

A global signal can name organisations (OFAC SDN entities) without ever carrying a tenant code: it
stores the public names' :func:`match_key` values (``signal.match_keys``), the hub stores the key of
each tenant node name (``network_node.name_key``, :meth:`signal_hub.pipeline.Pipeline.put_network`),
and the tenant's exposure query compares the two inside the tenant's RLS context
(:mod:`signal_hub.exposure`). Supplier codes never enter a global row.

:func:`match_key`:

1. NFKD, ASCII only, casefold; ``&`` → ``and``; periods and apostrophes dropped (``S.A.`` → ``sa``,
   ``Kenya's`` → ``kenyas``); any other punctuation → a space;
2. a leading ``the`` is dropped;
3. trailing legal-form tokens (:data:`LEGAL_FORMS`: ``co``, ``ltd``, ``limited``, ``gmbh``,
   ``sarl`` …, and the ``and`` of ``& Co.``) are dropped repeatedly;
4. ``None`` unless at least :data:`MIN_TOKENS` tokens and :data:`MIN_CHARS` characters remain (one
   generic word would match far too much).

``"Ningbo Shell Fabrics Co."`` and ``"NINGBO SHELL FABRICS COMPANY LIMITED"`` both give
``"ningbo shell fabrics"``; ``"Ningbo Shell Fabric"`` does not match them (no fuzzy matching).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Final

__all__ = ["LEGAL_FORMS", "MAX_NAME_CHARS", "MIN_CHARS", "MIN_TOKENS", "match_key", "match_keys"]

LEGAL_FORMS: Final = frozenset({
    "ltd", "limited", "co", "company", "corp", "corporation", "inc", "incorporated", "llc", "llp", "lp", "plc",
    "gmbh", "ag", "sa", "sas", "sarl", "srl", "spa", "bv", "nv", "jsc", "ojsc", "pjsc", "cjsc", "ooo", "oao",
    "zao", "fze", "fzco", "fzc", "pte", "pvt", "private", "sdn", "bhd", "tbk", "kk", "ab", "oy", "as", "sl", "se",
})
MIN_TOKENS: Final = 2
MIN_CHARS: Final = 6
MAX_NAME_CHARS: Final = 500
"""Longer inputs are not organisation names (and are not normalised at all)."""
_DROP_RE: Final = re.compile(r"[.'’`]")
_SPLIT_RE: Final = re.compile(r"[^a-z0-9]+")


def match_key(name: str | None) -> str | None:
    """The normalised key of an organisation name, or ``None`` when too short / generic."""
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        return None
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").casefold()
    text = _DROP_RE.sub("", text.replace("&", " and "))
    tokens = [t for t in _SPLIT_RE.split(text) if t]
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    while tokens and (tokens[-1] in LEGAL_FORMS or tokens[-1] == "and"):  # "… & Co." leaves a trailing "and"
        tokens.pop()
    key = " ".join(tokens)
    if len(tokens) < MIN_TOKENS or len(key) < MIN_CHARS:
        return None
    return key


def match_keys(names: Iterable[str | None], *, limit: int | None = None) -> list[str]:
    """Sorted unique keys of ``names`` (names without a key are skipped), at most ``limit``."""
    keys = sorted({k for k in (match_key(n) for n in names) if k is not None})
    return keys if limit is None else keys[:limit]
