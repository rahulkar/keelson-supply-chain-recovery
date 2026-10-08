"""Deterministic integer apportionment shared by the seed generator and story checks."""

from __future__ import annotations

from collections.abc import Sequence


def largest_remainder(total: int, weights: Sequence[float]) -> list[int]:
    """Split ``total`` into non-negative ints proportional to ``weights`` summing to ``total``.

    Ties on the fractional remainder go to the lower index, so results are stable.
    """
    if total < 0:
        raise ValueError("total must be >= 0")
    wsum = float(sum(weights))
    if total == 0 or wsum <= 0:
        return [0] * len(weights)
    raw = [total * w / wsum for w in weights]
    base = [int(x) for x in raw]
    short = total - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[:short]:
        base[i] += 1
    return base


def sku_split(units: int, curve_bp: Sequence[int]) -> list[int]:
    """Split style-level units into sizes using a size curve in basis points."""
    return largest_remainder(units, [float(bp) for bp in curve_bp])


def share_split(units: int, curve_bp: Sequence[int]) -> list[int]:
    """Alternative size split: floor of the pro-rata share, leftover units one each to the
    largest shares (ties to the lower index).

    Consumers may split style forecasts this way (the Keelson engine does); the seed uses
    it only to size buffers so the story holds under either rounding.
    """
    total = sum(curve_bp)
    if units <= 0 or total <= 0:
        return [0] * len(curve_bp)
    out = [units * bp // total for bp in curve_bp]
    order = sorted(range(len(curve_bp)), key=lambda i: (-curve_bp[i], i))
    for i in order[: units - sum(out)]:
        out[i] += 1
    return out
