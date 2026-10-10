"""64-bit SimHash fingerprints of news text (``docs/contracts/123`` §2) — pure and deterministic.

* :func:`tokens`: NFKC + casefold, URLs dropped, everything that is not a letter or digit becomes a
  space, a small English stopword set is removed (digits are kept: "day 2" and "day 3" differ).
* Features: every token and every pair of adjacent tokens (weight 1 each). Each feature is hashed
  with keyed BLAKE2b (8 bytes, ``person=kslSimH1``), so a fingerprint never depends on
  ``PYTHONHASHSEED``, the platform or the Python version.
* :func:`simhash64`: per bit, the weighted vote of the features (+w when the feature's bit is 1,
  −w otherwise); the bit is 1 when the sum is positive. Texts with fewer than :data:`MIN_TOKENS`
  tokens get no fingerprint (short headlines collide too easily).
* Storage: a **signed** ``bigint`` (:func:`to_signed`) and eight 8-bit bands
  (:func:`bands`, ``(band index << 8) | 8 bits``). Two fingerprints within Hamming distance 7 share
  at least one band (pigeonhole: 7 differing bits cannot touch all eight bands), so the GIN band
  index finds every pair within ``max_hamming_bits <= 7`` exactly.

Calibration (headline + snippet, 20-30 tokens; ``tests/test_m8_simhash.py``): a one-word edit flips
about 3 bits, a changed number 5, a dropped tail of three words or an outlet prefix about 6;
unrelated items are 24-39 bits apart (binomial around 32). The default ``max_hamming_bits`` 6 catches
syndicated edits; an unrelated pair within 6 bits has a probability below 1e-10.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "BANDS",
    "BAND_BITS",
    "MAX_EXACT_BITS",
    "MIN_TOKENS",
    "Fingerprint",
    "bands",
    "features",
    "fingerprint",
    "hamming",
    "simhash64",
    "to_signed",
    "to_unsigned",
    "tokens",
]

MIN_TOKENS: Final = 6
BANDS: Final = 8
BAND_BITS: Final = 8
MAX_EXACT_BITS: Final = BANDS - 1
"""Largest Hamming distance the band index finds without misses."""
_MASK64: Final = (1 << 64) - 1
_BAND_MASK: Final = (1 << BAND_BITS) - 1
_PERSON: Final = b"kslSimH1"
_URL_RE: Final = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_NON_WORD_RE: Final = re.compile(r"[\W_]+", re.UNICODE)
STOPWORDS: Final = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from", "has", "have", "in", "into",
    "is", "it", "its", "of", "on", "or", "that", "the", "their", "this", "to", "was", "were", "will", "with",
})


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A stored fingerprint: the signed 64-bit value and its band keys."""

    value: int
    bands: tuple[int, ...]

    @property
    def unsigned(self) -> int:
        return to_unsigned(self.value)


def tokens(text: str) -> list[str]:
    """Normalised word tokens of ``text`` (see the module docstring)."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    cleaned = _NON_WORD_RE.sub(" ", _URL_RE.sub(" ", folded))
    return [t for t in cleaned.split() if t not in STOPWORDS]


def features(words: Iterable[str]) -> dict[str, int]:
    """Weighted features: unigrams and adjacent bigrams (1 each; a repeat adds up)."""
    seq = list(words)
    out: dict[str, int] = {}
    for w in seq:
        out[w] = out.get(w, 0) + 1
    for a, b in itertools.pairwise(seq):
        key = f"{a} {b}"
        out[key] = out.get(key, 0) + 1
    return out


def _hash64(feature: str) -> int:
    return int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8, person=_PERSON).digest(), "big")


def simhash64(text: str) -> int | None:
    """The unsigned 64-bit SimHash of ``text``, or ``None`` below :data:`MIN_TOKENS` tokens."""
    words = tokens(text)
    if len(words) < MIN_TOKENS:
        return None
    votes = [0] * 64
    for feature, weight in sorted(features(words).items()):
        h = _hash64(feature)
        for bit in range(64):
            votes[bit] += weight if (h >> bit) & 1 else -weight
    value = 0
    for bit, vote in enumerate(votes):
        if vote > 0:
            value |= 1 << bit
    return value


def to_signed(value: int) -> int:
    """Unsigned 64-bit → Postgres ``bigint``."""
    value &= _MASK64
    return value - (1 << 64) if value >= 1 << 63 else value


def to_unsigned(value: int) -> int:
    return value & _MASK64


def bands(value: int) -> tuple[int, ...]:
    """Band keys of a fingerprint (signed or unsigned): ``(i << 8) | bits[8 i : 8 i + 8]``."""
    u = to_unsigned(value)
    return tuple((i << BAND_BITS) | ((u >> (BAND_BITS * i)) & _BAND_MASK) for i in range(BANDS))


def hamming(a: int, b: int) -> int:
    """Differing bits of two fingerprints (signed or unsigned)."""
    return ((a ^ b) & _MASK64).bit_count()


def fingerprint(text: str) -> Fingerprint | None:
    """Signed value + bands of ``text``, or ``None`` when it is too short to fingerprint."""
    value = simhash64(text)
    if value is None:
        return None
    return Fingerprint(to_signed(value), bands(value))
