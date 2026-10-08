"""Fake LiteLLM ``POST /embeddings`` (respx) with deterministic vectors.

``vector_for(text, dims)``: a hashed bag of words (each lower-case word adds ±1 at
``sha256(word) % dims``), L2-normalised — texts sharing words have a higher cosine similarity, so
search ranking is testable without a model. ``forced`` overrides: ``wrong_dims`` returns vectors of
another size, ``status`` answers with that HTTP status.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

import httpx
import respx

_WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset({"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "are", "be", "by", "with",
                  "at", "it", "this", "that", "from", "as", "we", "our", "if", "not", "no", "any"})


def vector_for(text: str, dims: int) -> list[float]:
    vec = [0.0] * dims
    for word in _WORD.findall(text.lower()):
        if word in STOP:
            continue
        digest = hashlib.sha256(word.encode()).digest()
        idx = int.from_bytes(digest[:4], "big") % dims
        vec[idx] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [x / norm for x in vec]


class FakeEmbeddings:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.requests: list[dict[str, Any]] = []
        self.auth: list[str] = []
        self.urls: list[str] = []
        self.wrong_dims: int | None = None
        self.status: int | None = None
        self.fail_after: int | None = None
        """Answer 500 once this many requests succeeded (mid-job failure)."""

    def install(self, router: respx.Router) -> None:
        router.post(f"{self.base}/embeddings", name="litellm-embeddings").mock(side_effect=self._embeddings)

    def reset(self) -> None:
        self.requests.clear()
        self.auth.clear()
        self.urls.clear()
        self.wrong_dims = None
        self.status = None
        self.fail_after = None

    @property
    def inputs(self) -> list[str]:
        return [t for r in self.requests for t in r["input"]]

    def _embeddings(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.urls.append(str(req.url))
        self.auth.append(req.headers.get("authorization", ""))
        if self.status is not None:
            return httpx.Response(self.status, json={"error": {"message": "fake failure"}})
        if self.fail_after is not None and len(self.requests) >= self.fail_after:
            return httpx.Response(500, json={"error": {"message": "fake failure"}})
        self.requests.append(body)
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        dims = int(body.get("dimensions") or 8)
        out_dims = self.wrong_dims or dims
        data = [{"object": "embedding", "index": i, "embedding": vector_for(t, out_dims)} for i, t in enumerate(inputs)]
        data.reverse()  # LiteLLM does not promise order; the client sorts by index
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"],
                                         "usage": {"prompt_tokens": 1, "total_tokens": 1}})
