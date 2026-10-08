"""KeelsonLiteLlm headers/alias, sentinel default, FakeLlm streaming, runner cache LRU."""

from __future__ import annotations

from typing import Any

import pytest
from fakes.harness import PRIYA, Harness
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from keelson_contracts.runs import Actor

from keelson_engine.runtime.context import RunContext, bind_run
from keelson_engine.runtime.errors import RuntimeConfigError
from keelson_engine.runtime.llm import (
    FakeLlm,
    FakeScript,
    FakeTurn,
    KeelsonLiteLlm,
    SentinelModel,
    install_sentinel_default,
)
from keelson_engine.runtime.spec import AgentSpec


class CapturingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def acompletion(self, model: str, messages: Any, tools: Any, **kwargs: Any) -> Any:
        import litellm

        self.calls.append({"model": model, **kwargs})
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop", "index": 0}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _request() -> LlmRequest:
    return LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
                      config=types.GenerateContentConfig())


async def test_keelson_litellm_targets_proxy_alias_and_merges_run_headers(harness: Harness) -> None:
    model = KeelsonLiteLlm(alias="fast-default", api_base="http://litellm:4000/", api_key="sk-tenant")
    client = CapturingClient()
    model.llm_client = client  # type: ignore[assignment]
    run = RunContext(tenant="demo-retail", case_id="c", run_id="run-9", kind="chat", actor=Actor.model_validate(PRIYA),
                     payload={}, spec=AgentSpec(tenant="demo-retail"), deps=harness.core.deps(), invocation_id="e-run-9",
                     trace_id="trace-9")
    with bind_run(run):
        [response] = [r async for r in model.generate_content_async(_request())]
    call = client.calls[0]
    assert call["model"] == "litellm_proxy/fast-default" and model.alias == "fast-default"
    assert call["api_base"] == "http://litellm:4000" and call["api_key"] == "sk-tenant"
    assert call["extra_headers"] == {"x-litellm-trace-id": "trace-9", "x-litellm-customer-id": "demo-retail"}
    assert response.content.parts[0].text == "ok"
    # outside a run no per-run headers are added
    [_r] = [r async for r in model.generate_content_async(_request())]
    assert "extra_headers" not in client.calls[1]


async def test_model_requests_are_bounded_by_the_configured_timeout(harness: Harness) -> None:
    """Regression (M3 integration): a Gemini call that never answered stalled a run for 10 min
    (600 s client timeout before the retry). The factory passes the configured per-request
    timeout (``LLM_REQUEST_TIMEOUT_S``, default 180 s) to every model call."""
    from keelson_engine.config import Settings
    from keelson_engine.runtime.llm import LiteLlmModelFactory

    assert Settings.model_fields["llm_request_timeout_s"].default == 180.0
    model = LiteLlmModelFactory(api_base="http://litellm:4000", api_key="sk-tenant", timeout_s=42.0)(
        role="recommender", alias="reasoning-default"
    )
    client = CapturingClient()
    model.llm_client = client  # type: ignore[attr-defined]
    _ = [r async for r in model.generate_content_async(_request())]
    assert client.calls[0]["timeout"] == 42.0


@pytest.mark.parametrize("alias", ["", "gemini/gemini-3.8-flash", "openai/gpt", "a/b"])
def test_invalid_aliases_rejected(alias: str) -> None:
    with pytest.raises(RuntimeConfigError):
        KeelsonLiteLlm(alias=alias, api_base="http://x", api_key="k")


async def test_sentinel_default_model_raises() -> None:
    install_sentinel_default()
    agent = LlmAgent(name="orphan")
    assert isinstance(agent.canonical_model, SentinelModel)
    with pytest.raises(RuntimeConfigError):
        async for _ in agent.canonical_model.generate_content_async(_request()):
            pass


async def test_fake_llm_streams_partials_then_final() -> None:
    script = FakeScript({"a": [FakeTurn(thought="thinking hard", text="answer", total_tokens=10)]})
    llm = FakeLlm(agent="a", script=script)
    out = [r async for r in llm.generate_content_async(_request(), stream=True)]
    assert all(r.partial for r in out[:-1]) and not out[-1].partial
    assert out[0].content.parts[0].thought and out[-1].usage_metadata.total_token_count == 10
    with pytest.raises(AssertionError):
        [r async for r in llm.generate_content_async(_request())]


async def test_runner_cache_refcount_and_lru(harness: Harness) -> None:
    from google.adk.sessions import InMemorySessionService

    from keelson_engine.runtime.llm import FakeModelFactory
    from keelson_engine.runtime.runner_cache import RunnerCache
    from keelson_engine.runtime.workflow import WorkflowBindings

    built: list[tuple[str, str]] = []

    async def spec_loader(tenant: str, version: str) -> AgentSpec:
        built.append((tenant, version))
        return AgentSpec(tenant=tenant, version=version)

    async def bindings(tenant: str, spec: AgentSpec) -> WorkflowBindings:
        return WorkflowBindings(models=FakeModelFactory(FakeScript()))

    cache = RunnerCache(session_service=InMemorySessionService(), spec_loader=spec_loader, bindings=bindings, max_size=1)
    a = await cache.get("demo-retail", "v1")
    assert a.app.name == "t_demo_retail" and a.app.resumability_config.is_resumable
    assert [p.name for p in a.app.plugins] == ["keelson_policy", "keelson_budget", "keelson_usage", "keelson_audit",
                                              "keelson_evidence"]
    again = await cache.get("demo-retail", "v1")
    assert again.runner is a.runner and built == [("demo-retail", "v1")]
    closed: list[Any] = []
    original_close = a.runner.close

    async def tracking_close() -> None:
        closed.append(a.runner)
        await original_close()

    a.runner.close = tracking_close  # type: ignore[method-assign]
    b = await cache.get("northwind-test", None)  # evicts v1 logically, but it is still leased twice
    assert b.app.name == "t_northwind_test" and cache.size == 1 and not closed
    await a.release()
    assert not closed
    await again.release()
    assert closed == [a.runner]
    await b.release()
    await cache.close()
