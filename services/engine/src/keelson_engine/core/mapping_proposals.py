"""Proposed tier-A mappings: create, list, get, discard and the acceptance bookkeeping
(``docs/contracts/121-m8-mapping-compiler.md`` §3).

:func:`create` (``POST /api/admin/connectors/{slug}/mappings/proposals``) runs synchronously
within ``MAPPING_PROPOSE_TIMEOUT_S``: the approved tool catalogue (:mod:`~keelson_engine.core.mapping_profile`,
flagged vendor descriptions audited as ``untrusted_text.flagged``), sampling over the connector's
reader session (only with ``sample: true`` and an active connector, else 409 ``connector_inactive``;
``sample: false`` = schema only), the rule-based drafts (:mod:`~keelson_engine.core.mapping_heuristics`)
and — ``proposer: auto`` with ``MAPPING_PROPOSER=auto`` — the LLM refinement
(:mod:`keelson_engine.runtime.mapping_proposer`), which falls back to the rule-based drafts with a
note whenever the model is unavailable or fails. One proposal per tenant and two per process run at
a time (429 ``proposal_busy``); ``MAPPING_PROPOSER=off`` answers 409 ``proposer_disabled``.

A proposal is stored in ``connector_mapping_proposal`` (RLS; the last :data:`MAX_KEPT` per
connector) and **never activated**: the admin opens a draft in the mapping editor, previews it and
saves it with ``PUT …/mappings/{kind} {proposal_id}`` (:func:`keelson_engine.core.connector_mappings.put_mapping`),
which stores ``origin = proposal`` and marks the proposal ``accepted`` (:func:`mark_accepted`). An
action mapping saved from a proposal needs a second admin (change request ``connector_mapping``).
Audit: ``connector.mapping.proposal.created`` / ``.discarded``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import text

from keelson_contracts.changeset import OP_PAYLOAD_MODELS
from keelson_contracts.mapping import ActionMapping, ReadMapping
from keelson_contracts.mapping_proposal import MappingProposal, MappingProposeRequest, ProposalVersions
from keelson_contracts.scn import SCN_ENTITIES
from keelson_engine.core import connectors as reg
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.mapping_eval import canonical_json
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = ["MAX_KEPT", "acceptance_counts", "create", "discard", "for_accept", "get_proposal", "list_proposals",
           "mark_accepted"]

log = get_logger(__name__)

MAX_KEPT: Final = 20
"""Proposals kept per connector (older ones are deleted)."""
MAX_PER_TENANT: Final = 1
MAX_PER_PROCESS: Final = 2
_running: dict[str, int] = {}

_COLS: Final = ("id, connector, kinds, proposer, model_alias, status, inputs_hash, proposal, base_read_version, "
                "base_action_version, accepted_read_version, accepted_action_version, created_by, created_at, "
                "decided_by, decided_at")


def _settings() -> Any:
    from keelson_engine.config import get_settings

    return get_settings()


def _check_id(proposal_id: str) -> str:
    try:
        return str(uuid.UUID(str(proposal_id)))
    except (ValueError, TypeError):
        raise AdminError(404, "not_found", "proposal not found") from None


def _json(m: Mapping[str, Any]) -> dict[str, Any]:
    """The stored proposal with its current status and accepted versions."""
    body = dict(m["proposal"])
    accepted = None
    if m["accepted_read_version"] is not None or m["accepted_action_version"] is not None:
        accepted = {"read": m["accepted_read_version"], "action": m["accepted_action_version"]}
    body.update(status=m["status"], accepted=accepted)
    return MappingProposal.model_validate(body).model_dump(mode="json")


def _summary(m: Mapping[str, Any]) -> dict[str, Any]:
    p = m["proposal"] or {}
    read = p.get("read") or {}
    entities = read.get("entities") or []
    ops = (p.get("action") or {}).get("ops") or []
    return {
        "id": str(m["id"]), "connector": m["connector"], "status": m["status"], "proposer": m["proposer"],
        "model_alias": m["model_alias"], "kinds": list(m["kinds"] or []), "created_by": m["created_by"],
        "created_at": m["created_at"].isoformat() if m["created_at"] else None,
        "entities": len(entities), "complete_entities": sum(1 for e in entities if not e.get("missing_required")),
        "lookups": len(read.get("lookups") or []), "ops": len(ops),
        "notes": len(p.get("notes") or []),
        "accepted": ({"read": m["accepted_read_version"], "action": m["accepted_action_version"]}
                     if m["status"] == "accepted" else None),
    }


# ───────────────────────────── create ─────────────────────────────


async def _connector(tenant: str, slug: str) -> reg.ConnectorRow:
    try:
        conn = await reg.get_connector(tenant, slug)
    except reg.ConnectorNotFound:
        raise AdminError(404, "not_found", "connector not found") from None
    if conn.is_internal:
        raise AdminError(409, "provisioned_connector", "internal connectors take no mapping")
    return conn


async def _models(tenant: str) -> Any:
    """The tenant's LiteLLM model factory, or ``None`` without a key."""
    from keelson_engine.runtime.runner_cache import default_bindings
    from keelson_engine.runtime.spec import default_spec

    try:
        return (await default_bindings(tenant, default_spec(tenant))).models
    except Exception as exc:  # noqa: BLE001 - LlmKeyMissing, decryption, configuration: never the key
        log.info("mapping proposer has no model", tenant=tenant, error=type(exc).__name__)
        return None


async def _versions(tenant: str, connector: str) -> ProposalVersions:
    async with tenant_session(tenant) as s:
        rows = (await s.execute(text("SELECT kind, max(version) FROM connector_mapping WHERE tenant = :t AND "
                                     "connector = :c GROUP BY kind"), {"t": tenant, "c": connector})).all()
    got = {r[0]: int(r[1]) for r in rows}
    return ProposalVersions(read=got.get("read"), action=got.get("action"))


async def create(tenant: str, slug: str, request: MappingProposeRequest, actor: dict[str, Any], *,
                 models: Any = None) -> dict[str, Any]:
    """Propose read / action mappings for ``slug`` (see the module docstring); returns the stored
    ``MappingProposal`` JSON. ``models`` overrides the tenant's model factory (tests: FakeLlm)."""
    settings = _settings()
    if settings.mapping_proposer == "off":
        raise AdminError(409, "proposer_disabled", "mapping proposals are switched off in this installation")
    conn = await _connector(tenant, slug)
    if request.sample and not conn.usable:
        raise AdminError(409, "connector_inactive", f"the connector is {conn.status}: activate it to sample its read "
                         "tools, or propose from the tool catalogue alone (sample: false)")
    if _running.get(tenant, 0) >= MAX_PER_TENANT or sum(_running.values()) >= MAX_PER_PROCESS:
        raise AdminError(429, "proposal_busy", "a mapping proposal is already running; try again when it finished")
    _running[tenant] = _running.get(tenant, 0) + 1
    try:
        async with asyncio.timeout(settings.mapping_propose_timeout_s):
            proposal = await _propose(tenant, conn, request, actor, models=models)
    except TimeoutError:
        raise AdminError(504, "proposal_timeout", f"the proposal took longer than "
                         f"{int(settings.mapping_propose_timeout_s)} s (MAPPING_PROPOSE_TIMEOUT_S)") from None
    finally:
        _running[tenant] = max(0, _running.get(tenant, 1) - 1)
        if not _running[tenant]:
            _running.pop(tenant, None)
    return await _store(tenant, proposal, request, actor)


async def _propose(tenant: str, conn: reg.ConnectorRow, request: MappingProposeRequest, actor: dict[str, Any], *,
                   models: Any) -> MappingProposal:
    from keelson_engine.core import mapping_heuristics as mh
    from keelson_engine.core import mapping_profile as mp
    from keelson_engine.core.scn_crawl import CrawlError, session_caller

    settings = _settings()
    notes = mp.Notes()
    rows = [t for t in await reg.list_tools(tenant, conn.slug, include_removed=False) if t.status == "approved"]
    tools = mp.catalogue(rows, notes)
    if notes.flagged:
        await admin_audit(actor, "untrusted_text.flagged", f"connector:{conn.slug}/tools", None,
                          {"by": "pattern", "tools": notes.flagged[:20], "proposal": True}, tenant=tenant)
    entities = list(request.entities) if request.entities else list(SCN_ENTITIES)
    ops = list(request.ops) if request.ops else list(OP_PAYLOAD_MODELS)
    probe = mh.probe_variants(ops) if "action" in request.kinds else None
    if request.sample:
        try:
            async with session_caller(conn) as call:
                sampling = await mp.sample(call, tools, request, max_calls=settings.mapping_propose_max_calls,
                                           sample_size=settings.mapping_propose_sample_size, notes=notes, probe=probe)
        except CrawlError as exc:
            raise AdminError(502, exc.code, f"the connector could not be sampled: {exc.detail}"[:300]) from None
    else:
        notes.add("schema_only", None, "proposed from the tool catalogue alone (no records were sampled): column "
                  "mappings need samples or declared output schemas")
        sampling = await mp.sample(None, tools, request, max_calls=0, sample_size=0, notes=notes,
                                   raw_tools={t.name: t for t in rows})
    profiles = sampling.profiles
    draft = mh.propose(profiles, tools, kinds=request.kinds, entities=request.entities, ops=request.ops,
                       probes=sampling.probes, notes=notes)
    read, action = draft.read, draft.action
    proposer, alias, llm_calls = "heuristic", None, 0
    if request.proposer == "heuristic":
        notes.add("llm_disabled", None, "rule-based proposal requested (no model call)")
    elif settings.mapping_proposer != "auto":
        notes.add("llm_disabled", None, "AI refinement is switched off in this installation (MAPPING_PROPOSER="
                  f"{settings.mapping_proposer}): rule-based suggestions only")
    else:
        factory = models if models is not None else await _models(tenant)
        if factory is None:
            notes.add("llm_unavailable", None, "no AI model is configured for this tenant: rule-based suggestions "
                      "only", severity="warning")
        else:
            from keelson_engine.runtime import mapping_proposer as llm

            outcome = await llm.refine(
                tenant=tenant, actor=actor, profiles=profiles, tools=tools, draft=draft, entities=entities,
                ops=ops if "action" in request.kinds else [], models=factory, alias=settings.mapping_proposer_alias,
                timeout_s=settings.mapping_propose_llm_timeout_s,
                max_chars=settings.mapping_propose_max_profile_chars, notes=notes)
            llm_calls, alias = outcome.llm_calls, outcome.model_alias
            if outcome.output is not None:
                valid = llm.validate(outcome.output, profiles=profiles, refs=outcome.refs, tools=tools,
                                     entities=entities if "read" in request.kinds else [],
                                     ops=ops if "action" in request.kinds else [], notes=notes)
                if outcome.output.injection_suspected:
                    await admin_audit(actor, "untrusted_text.flagged", f"connector:{conn.slug}/mapping-proposal", None,
                                      {"by": "model", "proposal": True}, tenant=tenant)
                else:
                    read, action = llm.merge(draft, valid)
                    proposer = "llm+heuristic"
    inputs = mp.inputs_hash(profiles, tools, sampling.probes, request)
    return MappingProposal(
        id=str(uuid.uuid7()), connector=conn.slug, status="ready", proposer=proposer,  # type: ignore[arg-type]
        model_alias=alias, created_by=str(actor.get("username") or "")[:200] or None,
        created_at=dt.datetime.now(dt.UTC).replace(microsecond=0), inputs_hash=inputs,
        base_versions=await _versions(tenant, conn.slug), profiles=profiles, read=read, action=action,
        unmapped_entities=draft.unmapped_entities if "read" in request.kinds else [],  # type: ignore[arg-type]
        unmapped_ops=draft.unmapped_ops if "action" in request.kinds else [], notes=notes.items[:200],
        accepted=None, llm_calls=llm_calls,
    )


async def _store(tenant: str, proposal: MappingProposal, request: MappingProposeRequest,
                 actor: dict[str, Any]) -> dict[str, Any]:
    body = proposal.model_dump(mode="json")
    async with tenant_session(tenant) as s:
        await s.execute(text(
            "INSERT INTO connector_mapping_proposal (id, tenant, connector, kinds, proposer, model_alias, status, "
            "inputs_hash, proposal, base_read_version, base_action_version, created_by, created_at) "
            "VALUES (CAST(:id AS uuid), :t, :c, :k, :p, :a, 'ready', :h, CAST(:j AS jsonb), :br, :ba, :by, :at)"),
            {"id": proposal.id, "t": tenant, "c": proposal.connector, "k": list(request.kinds), "p": proposal.proposer,
             "a": proposal.model_alias, "h": proposal.inputs_hash, "j": dumps(body),
             "br": proposal.base_versions.read, "ba": proposal.base_versions.action, "by": proposal.created_by,
             "at": proposal.created_at})
        await s.execute(text(
            "DELETE FROM connector_mapping_proposal WHERE tenant = :t AND connector = :c AND id NOT IN ("
            "SELECT id FROM connector_mapping_proposal WHERE tenant = :t AND connector = :c "
            "ORDER BY created_at DESC, id DESC LIMIT :n)"), {"t": tenant, "c": proposal.connector, "n": MAX_KEPT})
    read = proposal.read
    await admin_audit(actor, "connector.mapping.proposal.created", f"connector:{proposal.connector}/proposal:{proposal.id}",
                      None, {"proposer": proposal.proposer, "inputs_hash": proposal.inputs_hash,
                             "kinds": list(request.kinds), "sample": request.sample,
                             "entities": len(read.entities) if read else 0,
                             "ops": len(proposal.action.ops) if proposal.action else 0,
                             "llm_calls": proposal.llm_calls,
                             "notes": sorted({n.code for n in proposal.notes})}, tenant=tenant)
    log.info("mapping proposal created", tenant=tenant, connector=proposal.connector, proposer=proposal.proposer,
             entities=len(read.entities) if read else 0)
    return body


# ───────────────────────────── read / discard ─────────────────────────────


async def _row(tenant: str, connector: str, proposal_id: str, *, lock: bool = False, s: Any = None) -> Any:
    sql = (f"SELECT {_COLS} FROM connector_mapping_proposal WHERE tenant = :t AND connector = :c "
           "AND id = CAST(:i AS uuid)" + (" FOR UPDATE" if lock else ""))
    params = {"t": tenant, "c": connector, "i": _check_id(proposal_id)}
    if s is not None:
        return (await s.execute(text(sql), params)).mappings().first()
    async with tenant_session(tenant) as session:
        return (await session.execute(text(sql), params)).mappings().first()


async def list_proposals(tenant: str, connector: str, *, limit: int = 20) -> list[dict[str, Any]]:
    await _connector(tenant, connector)
    async with tenant_session(tenant) as s:
        rows = (await s.execute(text(f"SELECT {_COLS} FROM connector_mapping_proposal WHERE tenant = :t AND "
                                     "connector = :c ORDER BY created_at DESC, id DESC LIMIT :n"),
                                {"t": tenant, "c": connector, "n": max(1, min(int(limit), MAX_KEPT))})).mappings().all()
    return [_summary(r) for r in rows]


async def get_proposal(tenant: str, connector: str, proposal_id: str) -> dict[str, Any]:
    await _connector(tenant, connector)
    m = await _row(tenant, connector, proposal_id)
    if m is None:
        raise AdminError(404, "not_found", "proposal not found")
    return _json(m)


async def discard(tenant: str, connector: str, proposal_id: str, actor: dict[str, Any]) -> dict[str, Any]:
    await _connector(tenant, connector)
    async with tenant_session(tenant) as s:
        m = await _row(tenant, connector, proposal_id, lock=True, s=s)
        if m is None:
            raise AdminError(404, "not_found", "proposal not found")
        if m["status"] != "ready":
            raise AdminError(409, "proposal_decided", f"this proposal is already {m['status']}", status_now=m["status"])
        await s.execute(text("UPDATE connector_mapping_proposal SET status = 'discarded', decided_by = :by, "
                             "decided_at = now() WHERE tenant = :t AND id = CAST(:i AS uuid)"),
                        {"by": actor.get("username"), "t": tenant, "i": str(m["id"])})
        updated = await _row(tenant, connector, proposal_id, s=s)
    await admin_audit(actor, "connector.mapping.proposal.discarded", f"connector:{connector}/proposal:{m['id']}",
                      {"status": "ready"}, {"status": "discarded"}, tenant=tenant)
    return _json(updated)


# ───────────────────────────── acceptance ─────────────────────────────


async def for_accept(tenant: str, connector: str, proposal_id: str, kind: str, *,
                     cosigned: bool = False) -> MappingProposal | None:
    """The proposal a mapping is saved from (``PUT …/mappings/{kind} {proposal_id}``): 404 when
    unknown, 409 ``proposal_discarded`` or ``proposal_kind`` when it cannot be accepted. An approved
    change request (``cosigned``) applies even if the proposal was pruned or discarded meanwhile
    (``None``)."""
    m = await _row(tenant, connector, proposal_id)
    if m is None:
        if cosigned:
            return None
        raise AdminError(404, "proposal_not_found", "the proposal does not exist (any more)")
    if kind not in (m["kinds"] or []):
        raise AdminError(409, "proposal_kind", f"this proposal has no {kind} mapping")
    if m["status"] == "discarded" and not cosigned:
        raise AdminError(409, "proposal_discarded", "this proposal was discarded")
    try:
        return MappingProposal.model_validate(_json(m))
    except ValidationError:
        return None


async def mark_accepted(tenant: str, connector: str, proposal_id: str, kind: str, version: int,
                        actor: dict[str, Any]) -> None:
    """The proposal (still ``ready`` / ``accepted``) records the version saved from it."""
    column = "accepted_read_version" if kind == "read" else "accepted_action_version"
    async with tenant_session(tenant) as s:
        await s.execute(text(
            f"UPDATE connector_mapping_proposal SET status = 'accepted', {column} = :v, decided_by = :by, "
            "decided_at = now() WHERE tenant = :t AND connector = :c AND id = CAST(:i AS uuid) "
            "AND status IN ('ready', 'accepted')"),
            {"v": int(version), "by": actor.get("username"), "t": tenant, "c": connector, "i": _check_id(proposal_id)})


def acceptance_counts(proposal: MappingProposal | None, kind: str, model: ReadMapping | ActionMapping) -> dict[str, int]:
    """How much of the saved mapping is the proposal's draft as-is: ``{accepted, edited}`` fields
    (read) or ops (action)."""
    if proposal is None:
        return {"accepted": 0, "edited": 0}
    if kind == "read" and isinstance(model, ReadMapping):
        draft = proposal.read_draft()
        drafted = {(em.entity, canonical_json(em.source.model_dump(mode="json")), c, e)
                   for em in (draft.entities if draft else []) for c, e in em.fields.items()}
        saved = [(em.entity, canonical_json(em.source.model_dump(mode="json")), c, e)
                 for em in model.entities for c, e in em.fields.items()]
        same = sum(1 for x in saved if x in drafted)
        return {"accepted": same, "edited": len(saved) - same}
    if isinstance(model, ActionMapping):
        adraft = proposal.action_draft()
        drafted_ops = {canonical_json(o.model_dump(mode="json")) for o in (adraft.ops if adraft else [])}
        same = sum(1 for o in model.ops if canonical_json(o.model_dump(mode="json")) in drafted_ops)
        return {"accepted": same, "edited": len(model.ops) - same}
    return {"accepted": 0, "edited": 0}
