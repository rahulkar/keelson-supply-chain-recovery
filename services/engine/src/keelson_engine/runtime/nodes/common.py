"""Helpers shared by the workflow nodes.

Nodes communicate with the UI only through *ephemeral* ADK events (``partial=True``, never
persisted to the ADK session, so they never disturb HITL/replay bookkeeping) whose
``custom_metadata`` the translator turns into AG-UI events:

* ``{"sc_block": <envelope>}`` → ``ACTIVITY_SNAPSHOT`` (the block is saved first via
  ``repo.save_block``);
* ``{"sc_text": {"message_id", "text"}}`` → ``TEXT_MESSAGE_*`` (only ``compose``-style nodes);
* ``{"sc_case": {"mode", "status", "title"}}`` → ``CUSTOM sc.case.updated``;
* ``{"sc_progress": {"label", "prov", "evidence_refs"}}`` → a progress label on the stage;
* ``{"lifecycle": "agent_started", "agent": <slug>}`` → ``SUBAGENT_STARTED`` (specialist marker).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from google.adk.events.event import Event
from google.genai import types
from keelson_contracts.blocks import (
    BlockProvenance,
    block_data_to_wire,
    block_key,
    block_to_wire,
    make_block,
    parse_block_data,
)
from keelson_contracts.common import Provenance, stable_block_id

from ..context import RunContext

__all__ = [
    "ENGINE_COMMIT",
    "ENGINE_COMPOSE",
    "ENGINE_FRAME",
    "ENGINE_OPT",
    "as_of",
    "block_event",
    "case_event",
    "case_status",
    "input_dict",
    "marker_event",
    "progress_event",
    "routed",
    "say",
    "text_event",
]

ENGINE_OPT = "keelson-opt@0.1"
ENGINE_FRAME = "keelson-frame@0.1"
ENGINE_COMPOSE = "keelson-compose@0.1"
ENGINE_COMMIT = "keelson-commit@0.1"


def as_of(run: RunContext) -> dt.date:
    return run.spec.planning.as_of or dt.date.today()


def _ephemeral(**metadata: Any) -> Event:
    return Event(partial=True, custom_metadata=dict(metadata))


async def block_event(
    run: RunContext,
    kind: str,
    props: Any,
    *,
    title: str,
    prov: Provenance | str,
    engine: str,
    key: Sequence[str | int],
    inputs_hash: str | None = None,
    placement: str | None = None,
    data: Any = None,
) -> Event:
    """Validate, persist and announce a render block (stable id → re-emits replace).

    ``data`` (M3 canvas visuals) is the large payload: validated against the block's data model,
    stored with the block and served at its ``data_ref`` (``/api/blocks/{id}/data``); the event
    carries only the envelope (props = summary)."""
    block = make_block(
        kind,
        props,
        title=title,
        provenance=BlockProvenance(
            kind=Provenance(prov), engine=engine, inputs_hash=inputs_hash, as_of=as_of(run)
        ),
        placement=placement,  # type: ignore[arg-type]
        block_id=stable_block_id(run.case_id, kind, *key),
    )
    envelope = block_to_wire(block)
    if data is None:
        await run.deps.repo.save_block(tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, envelope=envelope)
    else:
        model = parse_block_data(block_key(kind, 1), data.model_dump(mode="json", by_alias=True)
                                 if hasattr(data, "model_dump") else data)
        await run.deps.repo.save_block(
            tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, envelope=envelope,
            data=block_data_to_wire(model),
        )
    return _ephemeral(sc_block=envelope)


def text_event(message_id: str, text: str) -> Event:
    return _ephemeral(sc_text={"message_id": message_id, "text": text})


async def say(run: RunContext, key: str, text: str) -> Event:
    """Persist an assistant message for the case and announce it as a TEXT_MESSAGE.

    Only deterministic composing nodes call this (numbers already resolved)."""
    await run.deps.repo.add_message(
        tenant=run.tenant, case_id=run.case_id, role="assistant", text=text, run_id=run.run_id
    )
    return text_event(f"msg_{run.run_id}_{key}", text)


def case_event(*, mode: str, status: str, title: str) -> Event:
    return _ephemeral(sc_case={"mode": mode, "status": status, "title": title})


async def case_status(run: RunContext, status: str) -> Event:
    """Persist the case status (case list / reload) and announce it (``sc.case.updated``)."""
    await run.deps.repo.set_case_status(tenant=run.tenant, case_id=run.case_id, status=status)
    return case_event(mode=run.case_mode, status=status, title=run.case_title)


def progress_event(label: str, *, prov: str = "fact", evidence_refs: Sequence[str] = ()) -> Event:
    return _ephemeral(sc_progress={"label": label, "prov": prov, "evidence_refs": list(evidence_refs)})


def marker_event(agent: str) -> Event:
    """Specialist wrapper start marker (``50-engine-workflow.md`` specialist pattern)."""
    return _ephemeral(lifecycle="agent_started", agent=agent)


def routed(output: Any, route: str) -> Event:
    """Final node event carrying both the output and the route for conditional edges."""
    return Event(output=output, route=route)


def input_dict(node_input: Any) -> dict[str, Any]:
    """Node inputs are JSON dicts; ``types.Content`` (START) is parsed from its text."""
    if isinstance(node_input, Mapping):
        return dict(node_input)
    if isinstance(node_input, types.Content):
        import json  # noqa: PLC0415

        text = "".join(p.text or "" for p in node_input.parts or [] if not p.thought)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("run envelope must be a JSON object")
        return data
    if hasattr(node_input, "model_dump"):
        return dict(node_input.model_dump(mode="json"))
    raise ValueError(f"unexpected node input type {type(node_input).__name__}")
