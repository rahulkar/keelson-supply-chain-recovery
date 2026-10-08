from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fakes.harness import Harness  # noqa: E402


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    h = await Harness(tmp=tmp_path).setup()
    try:
        yield h
    finally:
        await h.close()
