"""LLM plumbing: the LiteLLM proxy model, the sentinel default and an offline fake.

* :class:`KeelsonLiteLlm` — ``model="litellm_proxy/<alias>"``, ``api_base=LITELLM_BASE_URL``,
  ``api_key=<tenant virtual key>``; per-run headers (``x-litellm-trace-id``,
  ``x-litellm-customer-id``) are merged from the run contextvar inside
  ``generate_content_async`` so cached Runners stay run-independent. Every call goes through
  the engine-level record/replay layer (:mod:`.replay`, env ``LLM_REPLAY_MODE``; ``off`` by
  default = a plain pass-through).
* :class:`SentinelModel` — installed as the process-global ADK default model; any agent
  built without an explicit model fails loudly instead of silently calling a default Gemini.
* :class:`FakeLlm` / :class:`FakeScript` — scripted responses keyed by agent name (text,
  structured JSON, function calls, thought parts) so the whole workflow runs offline.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import BaseModel, PrivateAttr

from .context import maybe_current_run
from .errors import RuntimeConfigError
from .replay import ReplayMode, ReplayStore, generate_with_replay, replay_mode

__all__ = [
    "FakeLlm",
    "FakeScript",
    "FakeTurn",
    "KeelsonLiteLlm",
    "LiteLlmModelFactory",
    "ModelFactory",
    "SentinelModel",
    "install_sentinel_default",
]

PROXY_PREFIX = "litellm_proxy/"


class KeelsonLiteLlm(LiteLlm):
    """LiteLLM-proxy model bound to one alias and one tenant virtual key."""

    _alias: str = PrivateAttr(default="")
    _replay_mode: ReplayMode | None = PrivateAttr(default=None)
    _replay_store: ReplayStore | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        alias: str,
        api_base: str,
        api_key: str,
        timeout_s: float = 600.0,
        replay_mode: ReplayMode | None = None,
        replay_store: ReplayStore | None = None,
        **kwargs: Any,
    ) -> None:
        """``replay_mode``/``replay_store``: ``None`` = ``LLM_REPLAY_MODE`` (read per call) and
        the Postgres ``llm_replay`` table."""
        if not alias or alias.startswith(("gemini/", "openai/")) or "/" in alias:
            raise RuntimeConfigError(f"invalid LiteLLM alias {alias!r}")
        super().__init__(
            model=f"{PROXY_PREFIX}{alias}",
            api_base=api_base.rstrip("/"),
            api_key=api_key,
            timeout=timeout_s,
            **kwargs,
        )
        self._alias = alias
        self._replay_mode = _check_mode(replay_mode)
        self._replay_store = replay_store

    @property
    def alias(self) -> str:
        return self._alias

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        run = maybe_current_run()
        if run is not None:
            config = llm_request.config
            http_options = config.http_options or types.HttpOptions()
            headers = dict(http_options.headers or {})
            headers.update(run.llm_headers)
            config.http_options = http_options.model_copy(update={"headers": headers})
        live = super().generate_content_async
        async for response in generate_with_replay(
            live=lambda: live(llm_request, stream=stream),
            llm_request=llm_request,
            stream=stream,
            alias=self._alias,
            mode=self._replay_mode,
            store=self._replay_store,
        ):
            yield response


def _check_mode(mode: ReplayMode | None) -> ReplayMode | None:
    return None if mode is None else replay_mode(mode)


class SentinelModel(BaseLlm):
    """Default model that refuses to run: every agent must name its model explicitly."""

    model: str = "keelson-sentinel"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        raise RuntimeConfigError(
            "an LLM agent ran without an explicit model; Keelson agents must use KeelsonLiteLlm"
        )
        yield  # pragma: no cover - keeps this an async generator


def install_sentinel_default() -> None:
    """Make the process-global ADK default model the sentinel (idempotent)."""
    LlmAgent.set_default_model(SentinelModel())


class ModelFactory(Protocol):
    def __call__(self, *, role: str, alias: str) -> BaseLlm: ...


class LiteLlmModelFactory:
    """Production factory: one :class:`KeelsonLiteLlm` per alias for a tenant key."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        timeout_s: float = 600.0,
        replay_mode: ReplayMode | None = None,
        replay_store: ReplayStore | None = None,
    ) -> None:
        if not api_key:
            raise RuntimeConfigError("tenant LLM virtual key is empty")
        self._api_base = api_base
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._replay_mode = _check_mode(replay_mode)
        self._replay_store = replay_store
        self._cache: dict[str, KeelsonLiteLlm] = {}

    def __call__(self, *, role: str, alias: str) -> BaseLlm:
        model = self._cache.get(alias)
        if model is None:
            model = KeelsonLiteLlm(
                alias=alias, api_base=self._api_base, api_key=self._api_key, timeout_s=self._timeout_s,
                replay_mode=self._replay_mode, replay_store=self._replay_store,
            )
            self._cache[alias] = model
        return model


# ─────────────────────────────────── fake ────────────────────────────────────


@dataclass
class FakeTurn:
    """One scripted model turn."""

    text: str | None = None
    json: Mapping[str, Any] | BaseModel | None = None
    function_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    thought: str | None = None
    total_tokens: int = 100
    error_code: str | None = None
    delay_s: float = 0.0
    """Simulated model latency (timing hooks: parallel waves, latency budgets)."""

    def parts(self, ids: Callable[[], str]) -> list[types.Part]:
        parts: list[types.Part] = []
        if self.thought:
            parts.append(types.Part(text=self.thought, thought=True))
        if self.json is not None:
            data = self.json.model_dump(mode="json") if isinstance(self.json, BaseModel) else dict(self.json)
            parts.append(types.Part(text=json.dumps(data)))
        if self.text is not None:
            parts.append(types.Part(text=self.text))
        for name, args in self.function_calls:
            parts.append(types.Part(function_call=types.FunctionCall(id=ids(), name=name, args=args)))
        return parts


TurnSource = FakeTurn | Callable[[LlmRequest], FakeTurn]


class FakeScript:
    """Scripted turns per agent name, consumed in order; records every request."""

    def __init__(self, turns: Mapping[str, list[TurnSource]] | None = None) -> None:
        self._turns: dict[str, list[TurnSource]] = defaultdict(list)
        for agent, items in (turns or {}).items():
            self._turns[agent].extend(items)
        self.requests: dict[str, list[LlmRequest]] = defaultdict(list)
        self._ids = itertools.count(1)
        self.timeline: list[tuple[str, float, float]] = []
        """Timing hook: ``(agent, start, end)`` per model call (``time.monotonic``)."""
        self.default_delay_s: float = 0.0
        """Latency added to every turn without its own ``delay_s``."""

    def calls_overlap(self, agents: Iterable[str]) -> bool:
        """True when at least two calls of *different* ``agents`` ran at the same time."""
        wanted = set(agents)
        spans = [(a, s, e) for a, s, e in self.timeline if a in wanted]
        return any(
            a1 != a2 and s1 < e2 and s2 < e1
            for i, (a1, s1, e1) in enumerate(spans)
            for (a2, s2, e2) in spans[i + 1 :]
        )

    def add(self, agent: str, *turns: TurnSource) -> FakeScript:
        self._turns[agent].extend(turns)
        return self

    def remaining(self, agent: str) -> int:
        return len(self._turns.get(agent, []))

    def next_turn(self, agent: str, request: LlmRequest) -> FakeTurn:
        self.requests[agent].append(request)
        queue = self._turns.get(agent)
        if not queue:
            raise AssertionError(f"FakeLlm: no scripted turn left for agent {agent!r}")
        source = queue.pop(0)
        return source(request) if callable(source) else source

    def new_call_id(self) -> str:
        return f"fc-{next(self._ids)}"


class FakeLlm(BaseLlm):
    """Offline model: answers from a :class:`FakeScript` under the calling agent's name."""

    model: str = "fake"
    agent: str
    script: Any
    """The shared :class:`FakeScript` (typed ``Any`` for pydantic)."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        script: FakeScript = self.script
        started = time.monotonic()
        turn = script.next_turn(self.agent, llm_request)
        delay = turn.delay_s or script.default_delay_s
        if delay:
            await asyncio.sleep(delay)
        script.timeline.append((self.agent, started, time.monotonic()))
        usage = types.GenerateContentResponseUsageMetadata(
            total_token_count=turn.total_tokens, prompt_token_count=turn.total_tokens // 2,
            candidates_token_count=turn.total_tokens - turn.total_tokens // 2,
        )
        if turn.error_code:
            yield LlmResponse(error_code=turn.error_code, error_message="scripted error", usage_metadata=usage)
            return
        if stream:
            if turn.thought:
                for chunk in _chunks(turn.thought):
                    yield LlmResponse(
                        content=types.Content(role="model", parts=[types.Part(text=chunk, thought=True)]),
                        partial=True,
                    )
            visible = turn.text if turn.text is not None else None
            if visible:
                for chunk in _chunks(visible):
                    yield LlmResponse(
                        content=types.Content(role="model", parts=[types.Part(text=chunk)]), partial=True
                    )
        yield LlmResponse(
            content=types.Content(role="model", parts=turn.parts(script.new_call_id)),
            usage_metadata=usage,
            partial=False,
            turn_complete=True,
        )


def _chunks(text: str, size: int = 24) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]


class FakeModelFactory:
    """Test factory: one :class:`FakeLlm` per agent name over a shared script."""

    def __init__(self, script: FakeScript) -> None:
        self.script = script

    def __call__(self, *, role: str, alias: str) -> BaseLlm:
        return FakeLlm(agent=role, script=self.script, model=f"fake/{alias}")
