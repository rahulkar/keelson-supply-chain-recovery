"""Keyed integer draws for robustness method ``mc/1`` (``docs/contracts/126-m8-co2-robustness.md`` §5).

A draw never depends on iteration order, on Python's per-process string hash seed or on the
platform: sample ``n`` of ``(seed, stream, key)`` comes from

    blake2b(f"mc/1|{seed}|{stream}|{key}|{n // 8}", digest_size=64)

read as eight little-endian unsigned 64-bit words; word ``n % 8`` gives the uniform basis point
``u = ((word >> 32) * 10000) >> 32`` in ``0..9999``. Streams: ``value:{i}`` / ``window:{i}``
(perturbation ``i``'s value / duration), ``lt`` (transit noise, keyed by the arrival) and ``dem``
(demand factor, keyed ``{cluster}|{style}``). The same ``(seed, stream, key)`` gives every
option of a plan the same draws (common random numbers).

:func:`quantile` maps ``u`` to a value of a p10 / p50 / p90 band: integer piecewise-linear
through ``(1000, p10)``, ``(5000, p50)``, ``(9000, p90)``; the tails continue at the adjacent
slope; results round half up.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

__all__ = ["DRAW_SCHEME", "IntArray", "quantile", "uniform_bp"]

DRAW_SCHEME = "mc/1"
IntArray = npt.NDArray[np.int64]
_PER_DIGEST = 8
_SHIFT = np.uint64(32)
_BP = np.uint64(10_000)


def uniform_bp(seed: int, stream: str, key: str, n: int) -> IntArray:
    """``n`` uniform basis points (``0..9999``) of ``(seed, stream, key)``, as int64."""
    words: list[npt.NDArray[np.uint64]] = []
    for chunk in range((n + _PER_DIGEST - 1) // _PER_DIGEST):
        digest = hashlib.blake2b(f"{DRAW_SCHEME}|{seed}|{stream}|{key}|{chunk}".encode(), digest_size=64).digest()
        words.append(np.frombuffer(digest, dtype="<u8"))
    if not words:
        return np.zeros(0, dtype=np.int64)
    u = np.concatenate(words)[:n]
    return (((u >> _SHIFT) * _BP) >> _SHIFT).astype(np.int64)


def quantile(u_bp: IntArray, band: Sequence[int], *, minimum: int | None = None) -> IntArray:
    """Values of ``band = (p10, p50, p90)`` at the uniform basis points ``u_bp`` (int64)."""
    p10, p50, p90 = (int(v) for v in band)
    d = u_bp.astype(np.int64) - 5000
    num = np.where(d <= 0, (p50 - p10) * d, (p90 - p50) * d)
    out = p50 + np.floor_divide(2 * num + 4000, 8000)
    if minimum is not None:
        out = np.maximum(out, minimum)
    return out.astype(np.int64)
