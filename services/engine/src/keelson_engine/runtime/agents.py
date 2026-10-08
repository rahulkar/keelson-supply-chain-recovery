"""LLM agent construction: every agent is ``single_turn`` + ``include_contents='none'`` + an
``InstructionProvider`` (tenant text is passed as *data* in the turn input, never templated),
an explicit model from the factory, and either tools **or** an ``output_schema`` (never both).

:class:`KeelsonLlmAgent` works around an ADK 2.11 behaviour: the agent's structured output is
dumped with ``exclude_none=True`` and then re-validated against the same ``output_schema``,
which rejects our strict schemas whose nullable fields are *required* (``x: T | None`` with no
default). The override restores the omitted nulls before validating and returns the full
JSON dict.
"""

from __future__ import annotations

import types as pytypes
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Union, get_args, get_origin

from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.workflow import RetryConfig
from pydantic import BaseModel

from .errors import RuntimeConfigError
from .llm import ModelFactory
from .toolsets import require_toolset_ok

__all__ = ["KeelsonLlmAgent", "LLM_RETRY", "fill_nullable", "make_agent"]

LLM_RETRY = RetryConfig(
    max_attempts=2, initial_delay=0.2, max_delay=1.0, jitter=0.0, exceptions=["ValidationError", "JSONDecodeError"]
)
"""One retry for malformed structured output (plan §7)."""


def _model_in(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        found = _model_in(arg)
        if found is not None:
            return found
    return None


def _nullable(annotation: Any) -> bool:
    origin = get_origin(annotation)
    return origin in (Union, pytypes.UnionType) and type(None) in get_args(annotation)


def fill_nullable(model: type[BaseModel], data: Any) -> Any:
    """Re-insert ``None`` for omitted nullable fields, recursively (lists and nested models)."""
    if not isinstance(data, Mapping):
        return data
    out = dict(data)
    for name, info in model.model_fields.items():
        key = info.alias or name
        annotation = info.annotation
        if key not in out:
            if _nullable(annotation):
                out[key] = None
            continue
        value = out[key]
        nested = _model_in(annotation)
        if nested is None or value is None:
            continue
        if isinstance(value, list):
            out[key] = [fill_nullable(nested, v) for v in value]
        else:
            out[key] = fill_nullable(nested, value)
    return out


class KeelsonLlmAgent(LlmAgent):
    def _validate_output_data(self, data: Any) -> Any:
        schema = self.output_schema
        if data is None or not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            return super()._validate_output_data(data)
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="json")
        return schema.model_validate(fill_nullable(schema, data)).model_dump(mode="json")


Instruction = Callable[[ReadonlyContext], Any]


def make_agent(
    *,
    name: str,
    alias: str,
    models: ModelFactory,
    instruction: Instruction,
    output_schema: type[BaseModel] | None = None,
    tools: Sequence[BaseTool | BaseToolset] = (),
    description: str = "",
) -> KeelsonLlmAgent:
    if output_schema is not None and tools:
        raise RuntimeConfigError(f"agent {name}: tools and output_schema must never be combined")
    return KeelsonLlmAgent(
        name=name,
        description=description,
        model=models(role=name, alias=alias),
        mode="single_turn",
        include_contents="none",
        instruction=instruction,
        output_schema=output_schema,
        tools=list(tools),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        retry_config=LLM_RETRY if output_schema is not None else None,
        before_model_callback=require_toolset_ok if tools else None,
    )
