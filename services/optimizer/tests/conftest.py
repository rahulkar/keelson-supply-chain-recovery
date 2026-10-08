from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from keelson_opt.config import Settings
from keelson_opt.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
TEST_SECRET = "test-only-optimizer-secret-0123456789abcdef"  # not a real secret


@pytest.fixture(scope="session")
def small_raw() -> dict[str, Any]:
    """The golden OptimizeRequest (data + perturbations + plan_spec + solver)."""
    return json.loads((FIXTURES / "small_problem.json").read_text(encoding="utf-8"))


@pytest.fixture
def small(small_raw: dict[str, Any]) -> dict[str, Any]:
    """A deep copy tests may mutate."""
    return copy.deepcopy(small_raw)


@pytest.fixture
def settings() -> Settings:
    return Settings(port=8200, shared_secret=TEST_SECRET, log_level="WARNING", jwt_leeway_s=0)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def make_token(
    *,
    secret: str = TEST_SECRET,
    algorithm: str = "HS256",
    drop: tuple[str, ...] = (),
    **overrides: Any,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "keelson-engine",
        "aud": "keelson-optimizer",
        "tenant": "demo-retail",
        "sub": "engine-worker",
        "iat": now,
        "exp": now + 60,
    }
    claims.update(overrides)
    for key in drop:
        claims.pop(key, None)
    return jwt.encode(claims, secret, algorithm=algorithm)


@pytest.fixture
def auth() -> Callable[..., dict[str, str]]:
    def _auth(**kw: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(**kw)}"}

    return _auth
