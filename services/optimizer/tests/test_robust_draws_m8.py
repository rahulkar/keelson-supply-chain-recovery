"""Robustness ``mc/1`` draws (126 §5): keyed, integer, order- and process-independent; the
piecewise-linear quantile through the band's anchors."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from keelson_opt.robust.draws import quantile, uniform_bp

LINE_KEY = "PO-BD-0001#1|UK1|PUF-W-BLK-M|2026-10-29"


def test_pinned_draws_for_seed_7() -> None:
    """The scheme is part of the contract: a change here changes every stored band."""
    assert uniform_bp(7, "lt", LINE_KEY, 10).tolist() == [9630, 1107, 6049, 7318, 8787, 2147, 8668, 529, 1458, 8427]
    assert uniform_bp(7, "value:0", "", 10).tolist() == [5543, 8598, 3576, 3541, 4287, 5664, 915, 4265, 8049, 8905]
    assert quantile(uniform_bp(7, "value:0", "", 10), (5, 7, 14)).tolist() == [8, 13, 6, 6, 7, 8, 5, 7, 12, 14]


def test_draws_are_prefix_stable_keyed_and_in_range() -> None:
    long = uniform_bp(7, "lt", LINE_KEY, 100)
    assert long[:13].tolist() == uniform_bp(7, "lt", LINE_KEY, 13).tolist()
    assert long.dtype == np.int64 and long.min() >= 0 and long.max() <= 9999
    assert uniform_bp(8, "lt", LINE_KEY, 16).tolist() != uniform_bp(7, "lt", LINE_KEY, 16).tolist()
    assert uniform_bp(7, "dem", LINE_KEY, 16).tolist() != uniform_bp(7, "lt", LINE_KEY, 16).tolist()
    assert uniform_bp(7, "lt", "other", 0).tolist() == []


def test_draws_are_roughly_uniform() -> None:
    u = uniform_bp(42, "lt", "uniformity", 4000)
    hist, _ = np.histogram(u, bins=10, range=(0, 10_000))
    assert hist.min() > 320 and hist.max() < 480


def test_quantile_anchors_tails_and_rounding() -> None:
    u = np.array([0, 1000, 3000, 5000, 7000, 9000, 9999], dtype=np.int64)
    # u=0: 7 - 2 x 5000 / 4000 = 4.5 → 5 (half up); u=9999: 7 + 7 x 4999 / 4000 = 15.75 → 16
    assert quantile(u, (5, 7, 14)).tolist() == [5, 5, 6, 7, 11, 14, 16]
    # anchors are exact for any band; the tails continue at the adjacent slope
    for band in ((-2, 0, 5), (9000, 10000, 11000), (0, 0, 0), (7, 10, 14)):
        got = quantile(np.array([1000, 5000, 9000], dtype=np.int64), band).tolist()
        assert got == list(band)
    # monotone in u, integer only
    grid = np.arange(0, 10_000, dtype=np.int64)
    vals = quantile(grid, (5, 7, 14))
    assert vals.dtype == np.int64 and bool((np.diff(vals) >= 0).all())
    assert quantile(grid, (1, 2, 3), minimum=2).min() == 2


def test_quantile_rounds_half_up() -> None:
    # slope (p50 - p10) / 4000 per bp: 1 / 4000 → u = 3000 is exactly half a unit below p50
    assert quantile(np.array([3000], dtype=np.int64), (0, 1, 2)).tolist() == [1]
    assert quantile(np.array([2999], dtype=np.int64), (0, 1, 2)).tolist() == [0]
    assert quantile(np.array([7000], dtype=np.int64), (0, 1, 2)).tolist() == [2]


_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
from keelson_opt.robust.draws import uniform_bp
keys = ["a", "b|c", "PO-1#1|UK1|SKU|2026-10-05"]
print(";".join(",".join(map(str, uniform_bp(7, "lt", k, 24).tolist())) for k in keys))
"""


@pytest.mark.parametrize("hash_seed", ["1", "4242"])
def test_identical_across_processes_and_hash_seeds(hash_seed: str) -> None:
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    out = subprocess.run([sys.executable, "-c", _CHILD, src], capture_output=True, text=True, env=env, check=True)
    here = ";".join(
        ",".join(map(str, uniform_bp(7, "lt", k, 24).tolist())) for k in ["a", "b|c", "PO-1#1|UK1|SKU|2026-10-05"]
    )
    assert out.stdout.strip() == here
