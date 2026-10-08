"""Test doubles: a scripted LLM (no network, no Gemini), a static clock, a LiteLLM proxy double and
deterministic embeddings (``vector_for``, as the engine's ``fakes/e1_embeddings.py``)."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from keelson_contracts.llm import GuardVerdict, NewsExtraction
from signal_hub.quarantine import LlmBadOutput, LlmUnavailable


@dataclass
class FakeLlm:
    """Scripted guard/extractor. ``extract_fn(text) -> dict`` builds the extraction; ``unavailable``
    raises :class:`LlmUnavailable` (e.g. 402 credits depleted) on every call."""

    extract_fn: Callable[[str], dict[str, Any]] | None = None
    guard_flags: Callable[[str], bool] = lambda _t: False
    unavailable: int | None = None
    bad_output: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def extract_alias(self) -> str:
        return "extract-fast"

    async def guard(self, text: str) -> GuardVerdict:
        self.calls.append(("guard", text))
        if self.unavailable is not None:
            raise LlmUnavailable(f"gateway HTTP {self.unavailable}", status=self.unavailable)
        flagged = self.guard_flags(text)
        return GuardVerdict(injection_suspected=flagged, reason="fake guard" if flagged else "")

    async def extract(self, text: str) -> NewsExtraction:
        self.calls.append(("extract", text))
        if self.unavailable is not None:
            raise LlmUnavailable(f"gateway HTTP {self.unavailable}", status=self.unavailable)
        if self.bad_output:
            raise LlmBadOutput("news_extraction: invalid model output (ValidationError)")
        data = self.extract_fn(text) if self.extract_fn else {}
        base = {"kind": "logistics", "subtype": "other", "places": [], "companies": [], "ports": [],
                "chokepoints": [], "dates_text": [], "injection_suspected": False}
        return NewsExtraction.model_validate({**base, **data})


_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "are", "be", "by", "with",
                   "at", "it", "this", "that", "from", "as"})


def vector_for(text: str, dims: int) -> list[float]:
    """Hashed bag of words (±1 at ``sha256(word) % dims``), L2-normalised: texts sharing words are
    more similar, so similarity is testable without a model."""
    vec = [0.0] * dims
    for word in _WORD.findall(text.lower()):
        if word in _STOP:
            continue
        digest = hashlib.sha256(word.encode()).digest()
        vec[int.from_bytes(digest[:4], "big") % dims] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [x / norm for x in vec]


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + dt.timedelta(**delta)


class MockLiteLlm:
    """A LiteLLM proxy double (``httpx.MockTransport``): the management routes the bootstrap uses
    (master key only) and ``/v1/chat/completions`` that answers ``chat_status`` (402 = credits
    depleted) for a valid virtual key restricted to the requested model. No network, no model."""

    def __init__(self, master_key: str = "sk-master-test", *, ready: bool = True, chat_status: int = 402,
                 embed_status: int = 200, embed_wrong_dims: int | None = None) -> None:
        import hashlib

        self._sha = lambda k: hashlib.sha256(k.encode()).hexdigest()
        self.master_key = master_key
        self.ready = ready
        self.chat_status = chat_status
        self.embed_status = embed_status
        self.embed_wrong_dims = embed_wrong_dims
        self.embed_inputs: list[str] = []
        self.keys: dict[str, dict[str, Any]] = {}  # sha256 → info
        self.raw: dict[str, str] = {}  # sha256 → raw key (to answer chat calls)
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Any] = []
        self.counter = 0

    def transport(self) -> Any:
        import httpx

        return httpx.MockTransport(self.handle)

    def _json(self, request: Any) -> Any:
        import json

        return json.loads(request.content or b"{}")

    def handle(self, request: Any) -> Any:
        import httpx

        path = request.url.path
        self.calls.append((request.method, path))
        auth = request.headers.get("authorization", "")
        if path == "/health/readiness":
            return httpx.Response(200 if self.ready else 503, json={"status": "ok" if self.ready else "down"})
        if path == "/v1/chat/completions":
            key = auth.removeprefix("Bearer ")
            info = self.keys.get(self._sha(key))
            body = self._json(request)
            self.bodies.append(body)
            if info is None or info.get("status") != "active":
                return httpx.Response(401, json={"error": {"message": "invalid key"}})
            if body.get("model") not in info["models"]:
                return httpx.Response(401, json={"error": {"message": "model not allowed"}})
            return httpx.Response(self.chat_status, json={"error": {"message": "budget exceeded / credits depleted"}})
        if path == "/v1/embeddings":
            key = auth.removeprefix("Bearer ")
            info = self.keys.get(self._sha(key))
            body = self._json(request)
            if info is None or info.get("status") != "active" or body.get("model") not in info["models"]:
                return httpx.Response(401, json={"error": {"message": "invalid key or model not allowed"}})
            if self.embed_status != 200:
                return httpx.Response(self.embed_status, json={"error": {"message": "budget exceeded / credits depleted"}})
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            self.embed_inputs += inputs
            dims = self.embed_wrong_dims or int(body.get("dimensions") or 8)
            data = [{"object": "embedding", "index": i, "embedding": vector_for(t, dims)} for i, t in enumerate(inputs)]
            data.reverse()  # LiteLLM does not promise order; the client sorts by index
            return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"]})
        if auth != f"Bearer {self.master_key}":
            return httpx.Response(401, json={"error": {"message": "not a master key"}})
        if path == "/key/info":
            info = self.keys.get(request.url.params.get("key", ""))
            if info is None:
                return httpx.Response(404, json={"error": {"message": "Key not found in database"}})
            return httpx.Response(200, json={"key": request.url.params["key"], "info": dict(info)})
        if path == "/key/generate":
            body = self._json(request)
            self.bodies.append(body)
            self.counter += 1
            raw = f"sk-virtual-{self.counter:04d}-SECRETVALUE"
            digest = self._sha(raw)
            self.keys[digest] = {"key_alias": body.get("key_alias"), "models": list(body.get("models") or []),
                                 "max_budget": body.get("max_budget"), "budget_duration": body.get("budget_duration"),
                                 "metadata": body.get("metadata") or {}, "key_type": body.get("key_type"),
                                 # LiteLLM's handle_key_type: llm_api → allowed_routes [llm_api_routes]
                                 "allowed_routes": ["llm_api_routes"] if body.get("key_type") == "llm_api" else [],
                                 "status": "active", "spend": 0.0}
            self.raw[digest] = raw
            return httpx.Response(200, json={"key": raw, "key_alias": body.get("key_alias"), "token_id": digest})
        if path == "/key/update":
            body = self._json(request)
            self.bodies.append(dict(body))
            digest = body.pop("key")
            if digest not in self.keys:
                return httpx.Response(404, json={"error": {"message": f"Key not found {digest}"}})
            self.keys[digest].update(body)
            return httpx.Response(200, json={"key": digest, **self.keys[digest]})
        if path == "/key/delete":
            body = self._json(request)
            aliases = set(body.get("key_aliases") or [])
            gone = [d for d, i in self.keys.items() if i.get("key_alias") in aliases]
            for d in gone:
                del self.keys[d]
            return httpx.Response(200 if gone else 404, json={"deleted_keys": gone})
        return httpx.Response(404, json={"error": {"message": f"no route {path}"}})

    def active_keys(self, alias: str) -> list[dict[str, Any]]:
        return [i for i in self.keys.values() if i.get("key_alias") == alias and i.get("status") == "active"]
