"""SimHash fingerprints (docs/contracts/123 §2): golden values, normalisation, edit distances of
syndicated copies vs unrelated items, the exact band index (pigeonhole) and hash-seed independence."""

from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from signal_hub.simhash import (
    BAND_BITS,
    BANDS,
    MAX_EXACT_BITS,
    MIN_TOKENS,
    bands,
    features,
    fingerprint,
    hamming,
    simhash64,
    to_signed,
    to_unsigned,
    tokens,
)

HAMBURG = ("Dockworkers strike halts container handling at the Port of Hamburg\nUnion says the walkout at all four "
           "terminals will last at least 48 hours; vessels are waiting at anchor.")
SUEZ = ("Suez Canal transits suspended after Red Sea security incident\nSouthbound and northbound transits of the Suez "
        "Canal were halted early on Monday after a security incident in the southern Red Sea.")
KESTREL = ("Typhoon KESTREL-26 forces Cat Lai terminal to suspend operations\nHo Chi Minh City's main container terminal "
           "stops vessel operations as the storm approaches.")


@pytest.mark.parametrize(("text", "golden"), [
    (HAMBURG, 0xBE9FE25E00EE410A),
    (SUEZ, 0x314FD59D70511CAC),
    (KESTREL, 0x4DB5A1C3AE32EB28),
])
def test_golden_fingerprints(text: str, golden: int) -> None:
    assert simhash64(text) == golden
    fp = fingerprint(text)
    assert fp is not None and fp.unsigned == golden and fp.value == to_signed(golden)
    assert fp.bands == bands(golden) == bands(fp.value)


def test_normalisation_ignores_case_punctuation_urls_whitespace_and_stopwords() -> None:
    variant = ("DOCKWORKERS' STRIKE HALTS   CONTAINER HANDLING AT PORT OF HAMBURG — the union says the walkout, at all "
               "four terminals, will last at least 48 hours: vessels are waiting at anchor https://wire.example/a?b=1")
    assert tokens(variant) == tokens(HAMBURG)
    assert simhash64(variant) == simhash64(HAMBURG)
    assert tokens("Hà Nội port") == ["hà", "nội", "port"]  # NFKC + casefold, non-ASCII letters kept
    assert features(["a1", "b2", "a1"]) == {"a1": 2, "b2": 1, "a1 b2": 1, "b2 a1": 1}


@pytest.mark.parametrize(("edit", "max_bits"), [
    (HAMBURG.replace("halts", "stops"), 3),  # one word
    (HAMBURG.replace("48 hours", "72 hours"), 5),  # a number (digits are kept)
    (HAMBURG.rsplit(" ", 3)[0], 6),  # the snippet cut three words earlier
    ("(Simulated Shipping Wire) " + HAMBURG, 6),  # an outlet prefix
])
def test_syndicated_edits_stay_within_the_default(edit: str, max_bits: int) -> None:
    a, b = simhash64(HAMBURG), simhash64(edit)
    assert a is not None and b is not None
    assert hamming(a, b) <= max_bits <= 6


def test_unrelated_items_are_far_apart() -> None:
    values = [simhash64(t) for t in (HAMBURG, SUEZ, KESTREL)]
    assert all(v is not None for v in values)
    pairs = [(a, b) for i, a in enumerate(values) for b in values[i + 1:]]
    assert all(hamming(a, b) > 20 for a, b in pairs if a is not None and b is not None)


def test_short_texts_get_no_fingerprint() -> None:
    assert MIN_TOKENS == 6
    assert simhash64("Port strike in Hamburg") is None
    assert simhash64("The port strike at the Hamburg docks") is None  # 4 tokens after stopwords
    assert fingerprint("") is None
    assert simhash64("Port strike in Hamburg closes six terminals") is not None


def test_signed_round_trip() -> None:
    for value in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1, 0xBE9FE25E00EE410A):
        signed = to_signed(value)
        assert -(1 << 63) <= signed < (1 << 63)
        assert to_unsigned(signed) == value
        assert bands(signed) == bands(value)
    assert to_signed((1 << 64) - 1) == -1


def test_band_index_is_exact_up_to_seven_bits() -> None:
    """Pigeonhole: with 8 bands of 8 bits, any two fingerprints within 7 bits share a band; at 8
    bits a pair can share none (one flip per band), which is why max_hamming_bits <= 7."""
    assert (BANDS, BAND_BITS, MAX_EXACT_BITS) == (8, 8, 7)
    rng = random.Random(20261008)
    for _ in range(2000):
        h = rng.getrandbits(64)
        flips = rng.sample(range(64), rng.randint(0, MAX_EXACT_BITS))
        g = h
        for bit in flips:
            g ^= 1 << bit
        assert hamming(h, g) == len(flips)
        assert set(bands(h)) & set(bands(g)), (hex(h), flips)
    h = rng.getrandbits(64)
    g = h
    for band in range(BANDS):
        g ^= 1 << (band * BAND_BITS)  # one differing bit in every band
    assert hamming(h, g) == 8 and not set(bands(h)) & set(bands(g))


def test_fingerprints_do_not_depend_on_the_hash_seed() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    code = "from signal_hub.simhash import simhash64; import sys; print(simhash64(sys.argv[1]))"
    outs = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": os.pathsep.join([str(src), os.environ.get("PYTHONPATH", "")])}
        out = subprocess.run([sys.executable, "-c", code, HAMBURG], capture_output=True, text=True, env=env, check=True)
        outs.add(out.stdout.strip())
    assert outs == {str(0xBE9FE25E00EE410A)}
