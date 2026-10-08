"""Guide mode (``docs/contracts/95-m5-remediation.md`` §2): manual remediation with verified progress.

Guide lines (``remediation.partition_actions``) become one ``remediation_checklist@1`` per
connector. Steps come from the connector's ``get_operator_guide`` when it has that tool approved
(Merxa: a draft change set holding the guide lines is created with the **preparer** principal —
never validated or applied — and its guide is read with the **reader**; deep links are kept only
when safe), else they are generated deterministically from the action payloads, optionally with a
screened excerpt of the tenant's SOPs (knowledge base via :func:`quarantine.sop_snippets`;
the checklist is then labelled ``inference``).

Every step names what read-back must find (``expected {entity_type, entity_ref, match}``,
:func:`expected_for`). :func:`verify_checklist` reads the expected entities back with the
connector's read tools (reader principal) and marks matching steps ``verified`` with an evidence
row; the worker runs :class:`GuideVerifier` on new webhook events and every 60 s while a
checklist is open. People may tick ``done`` / ``skipped`` by hand (case action
``checklist_step``, audited). When every step is ``verified`` or ``skipped`` the checklist is
completed and the case says "Guide completed".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from keelson_contracts.agui import ScMetadata, Stage, activity_snapshot, text_message_content, text_message_end, \
    text_message_start
from keelson_contracts.blocks import (
    BlockProvenance,
    ChecklistStep,
    RemediationChecklistProps,
    block_to_wire,
    is_safe_deep_link,
    make_block,
)
from keelson_contracts.changeset import CHANGE_SET_ITEM_ADAPTER
from keelson_contracts.common import Provenance, stable_block_id

from .errors import McpToolError, McpTransportError
from .mcp_client import get_gateway
from .quarantine import sop_snippets

__all__ = [
    "ENGINE_GUIDE",
    "GUIDE_TOOL",
    "GuideVerifier",
    "build_checklist",
    "checklist_block",
    "checklist_id_for",
    "checklist_props",
    "expected_for",
    "generated_step",
    "is_completed",
    "matches",
    "publish_checklist",
    "verify_checklist",
    "verify_open_checklists",
]

log = structlog.get_logger(__name__)

ENGINE_GUIDE: Final = "keelson-guide@0.1"
GUIDE_TOOL: Final = "get_operator_guide"
READBACK_ACTOR: Final = "system:readback"
MAX_GUIDE_STEPS: Final = 100
_FINAL = frozenset({"verified", "skipped"})


def checklist_id_for(*, tenant: str, case_id: str, plan_version: int, option_id: str, connector: str,
                     nonce: str) -> str:
    """Deterministic id (a crash-retried node re-creates the same checklist); ``nonce`` = the
    invocation, so a later Guide action gets a new checklist."""
    raw = f"{tenant}|{case_id}|{plan_version}|{option_id}|{connector}|{nonce}"
    return "CL-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


# ─────────────────────────────── expected entities ───────────────────────────────


def _lines(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(({"sku": str(x["sku"]), "qty": int(x["qty"])} for x in payload.get("lines") or []),
                  key=lambda x: x["sku"])


def _json_only(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_only(v) for k, v in value.items() if not isinstance(v, float)}
    if isinstance(value, list):
        return [_json_only(v) for v in value if not isinstance(v, float)]
    return value


def expected_for(action: Mapping[str, Any]) -> dict[str, Any]:
    """``ChecklistExpected`` JSON: what read-back must find once the step is done."""
    op = str(action["op"])
    p = dict(action.get("payload") or {})
    ref = action.get("target_ref")
    ref = str(ref) if ref else None
    if op == "transfer_order.create":
        return {"entity_type": "transfer_order", "entity_ref": None,
                "match": {"from_dc": p["from_dc"], "to_dc": p["to_dc"], "lines": _lines(p)}}
    if op == "allocation.set_qty":
        return {"entity_type": "allocation", "entity_ref": ref, "match": {"qty": int(p["qty"])}}
    if op == "po_line.reschedule":
        return {"entity_type": "po_line", "entity_ref": ref, "match": {"eta_dc": str(p["eta_dc"])}}
    if op == "po_line.change_qty":
        return {"entity_type": "po_line", "entity_ref": ref, "match": {"qty_ordered": int(p["qty"])}}
    if op == "po_line.cancel":
        return {"entity_type": "po_line", "entity_ref": ref, "match": {"status": "cancelled"}}
    if op == "po_line.split":
        return {"entity_type": "shipment", "entity_ref": None,
                "match": {"po_line_ref": ref, "mode": p.get("mode"), "qty": int(p["qty"])}}
    if op == "capacity_booking.create":
        return {"entity_type": "capacity_booking", "entity_ref": None,
                "match": {"lane_code": p["lane_code"], "iso_week": p["iso_week"], "qty_units": int(p["qty_units"])}}
    if op == "capacity_booking.cancel":
        return {"entity_type": "capacity_booking", "entity_ref": ref, "match": {"status": "released"}}
    if op == "po.create":
        return {"entity_type": "purchase_order", "entity_ref": None,
                "match": {"supplier_code": p["supplier_code"], "factory_code": p["factory_code"],
                          "dest_dc": p["dest_dc"], "lines": _lines(p)}}
    if op == "production_order.resequence":
        return {"entity_type": "factory_line", "entity_ref": str(p.get("line_code") or ref or ""),
                "match": {"order_codes": list(p.get("order_codes") or [])}}
    return {"entity_type": op.split(".")[0], "entity_ref": ref, "match": _json_only(p)}


# ─────────────────────────────── generated steps ───────────────────────────────


def _qty(n: Any) -> str:
    return f"{int(n):,}"


def generated_step(action: Mapping[str, Any], *, vendor: str) -> tuple[str, str]:
    """``(title, instructions_md)`` written from the payload (values only from the plan)."""
    op = str(action["op"])
    p = dict(action.get("payload") or {})
    ref = str(action.get("target_ref") or "")
    if op == "transfer_order.create":
        lines = "\n".join(f"- {x['sku']} × {_qty(x['qty'])}" for x in _lines(p))
        return (f"Create transfer order {p['from_dc']} → {p['to_dc']}",
                f"In {vendor}, create a transfer order from **{p['from_dc']}** to **{p['to_dc']}** shipping "
                f"**{p['ship_date']}** with these lines:\n{lines}")
    if op == "allocation.set_qty":
        return (f"Set allocation {ref} to {_qty(p['qty'])} units",
                f"In {vendor}, open allocation **{ref}** and set its quantity to **{_qty(p['qty'])}** units.")
    if op == "po_line.reschedule":
        return (f"Reschedule PO line {ref}",
                f"In {vendor}, change the DC arrival date of PO line **{ref}** to **{p['eta_dc']}**.")
    if op == "po_line.change_qty":
        return (f"Change quantity of PO line {ref}",
                f"In {vendor}, set the ordered quantity of PO line **{ref}** to **{_qty(p['qty'])}** units.")
    if op == "po_line.cancel":
        return (f"Cancel PO line {ref}", f"In {vendor}, cancel PO line **{ref}**.")
    if op == "po_line.split":
        mode = str(p.get("mode") or "").replace("_", "-")
        return (f"Move {_qty(p['qty'])} units of {ref} to {mode}",
                f"In {vendor}, split **{_qty(p['qty'])}** units of PO line **{ref}** onto the {mode} lane "
                f"**{p['lane_code']}**.")
    if op == "capacity_booking.create":
        return (f"Book capacity on {p['lane_code']} in {p['iso_week']}",
                f"In {vendor}, book **{_qty(p['qty_units'])}** units of capacity on lane **{p['lane_code']}** "
                f"in week **{p['iso_week']}**.")
    if op == "capacity_booking.cancel":
        return (f"Release capacity booking {ref}", f"In {vendor}, release (cancel) capacity booking **{ref}**.")
    if op == "po.create":
        lines = "\n".join(f"- {x['sku']} × {_qty(x['qty'])}" for x in _lines(p))
        return (f"Create a purchase order with {p['supplier_code']}",
                f"In {vendor}, create a purchase order with supplier **{p['supplier_code']}** (factory "
                f"**{p['factory_code']}**) for DC **{p['dest_dc']}**, arriving **{p['eta_dc']}**, with these "
                f"lines:\n{lines}")
    if op == "production_order.resequence":
        codes = " → ".join(str(c) for c in p.get("order_codes") or [])
        return (f"Resequence factory line {p.get('line_code')}",
                f"In {vendor}, put the production orders on line **{p.get('line_code')}** in this order: {codes}.")
    fields = "; ".join(f"{k} = {v}" for k, v in sorted(p.items()) if not isinstance(v, (dict, list)))
    return (f"{op} {ref}".strip(), f"In {vendor}, apply `{op}`" + (f" to **{ref}**" if ref else "")
            + (f" with {fields}." if fields else "."))


def _link_value(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(f"{x.get('sku')}:{x.get('qty')}" if isinstance(x, Mapping) else str(x) for x in value)
    return str(value)


def templated_link(action: Mapping[str, Any], templates: Mapping[str, Any]) -> str | None:
    """A deep link from the connector config's ``deep_links`` ``{op: template}`` (``{field}``
    placeholders from the payload / ``target_ref``, URL-encoded); unsafe results are dropped."""
    template = templates.get(str(action["op"]))
    if not isinstance(template, str):
        return None
    values = {k: urllib.parse.quote(_link_value(v), safe=":,") for k, v in dict(action.get("payload") or {}).items()}
    values["target_ref"] = urllib.parse.quote(str(action.get("target_ref") or ""), safe="")
    try:
        link = template.format(**values)
    except (KeyError, IndexError, ValueError):
        return None
    return link if is_safe_deep_link(link) else None


# ─────────────────────────────── building ───────────────────────────────


@dataclass
class BuiltChecklist:
    checklist_id: str
    connector: str
    steps: list[dict[str, Any]]
    meta: dict[str, Any] = field(default_factory=dict)


def _tool_items(actions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for seq, action in enumerate(actions, start=1):
        item = CHANGE_SET_ITEM_ADAPTER.validate_python({
            "seq": seq, "op": action["op"], "target_ref": action.get("target_ref"),
            "expected_version": action.get("expected_version"), "payload": action.get("payload") or {},
        })
        items.append(item.tool_item())
    return items


async def _operator_guide(tenant: str, connector: str, checklist_id: str, actions: Sequence[Mapping[str, Any]],
                          *, title: str, origin: Mapping[str, Any]) -> dict[int, list[dict[str, Any]]]:
    """``get_operator_guide`` steps by item seq (1-based, our order), via a never-applied draft."""
    gateway = get_gateway()
    async with gateway.session(tenant=tenant, connector=connector, principal="preparer") as s:
        created = await s.call("create_change_set", {"title": title, "origin": dict(origin),
                                                     "idempotency_key": f"{checklist_id}:guide:create"})
        ext_id = str(created["change_set_id"])
        await s.call("add_change_set_items", {"change_set_id": ext_id, "items": _tool_items(actions),
                                              "idempotency_key": f"{checklist_id}:guide:add"})
    async with gateway.session(tenant=tenant, connector=connector, principal="reader") as s:
        guide = await s.call(GUIDE_TOOL, {"change_set_id": ext_id, "locale": "en"})
    out: dict[int, list[dict[str, Any]]] = {}
    for step in guide.get("steps") or []:
        try:
            out.setdefault(int(step.get("item_seq") or step.get("seq")), []).append(dict(step))
        except (TypeError, ValueError):
            continue
    return out


async def _guide_tool_trusted(deps: Any, tenant: str, connector: str) -> bool:
    """The operator guide's text is shown verbatim, so it is used only while its tool is an approved
    read classified ``trusted_data`` (an ``untrusted_text`` guide falls back to generated steps)."""
    try:
        tools = await deps.connectors.list_approved_tools(tenant=tenant, connector=connector)
    except Exception as exc:  # unknown classification: generate the steps instead
        log.warning("operator guide classification unavailable", connector=connector, error=type(exc).__name__)
        return False
    tool = next((t for t in tools if t.name == GUIDE_TOOL), None)
    return tool is not None and getattr(tool, "access", "read") == "read" \
        and getattr(tool, "content_trust", "trusted_data") == "trusted_data"


async def _baseline(tenant: str, connector: str, tools: set[str], steps: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Refs of entities that already exist for the steps that create something (never matched)."""
    out: dict[str, list[str]] = {}
    pending = [st for st in steps if st["expected"]["entity_ref"] is None]
    if not pending:
        return out
    try:
        async with get_gateway().session(tenant=tenant, connector=connector, principal="reader") as s:
            for st in pending:
                found = await _candidates(s, tools, st["expected"])
                out[str(st["seq"])] = sorted({ref for ref, _ in found if ref})
    except (McpToolError, McpTransportError, OSError) as exc:
        log.warning("guide baseline read failed", connector=connector, error=type(exc).__name__)
    return out


async def build_checklist(
    deps: Any, *, tenant: str, case_id: str, run_id: str | None, connector: str, vendor: str,
    actions: Sequence[Mapping[str, Any]], checklist_id: str, plan_version: int, option_id: str,
    tools: set[str], config: Mapping[str, Any], sop: bool = True, kb_ids: Sequence[str] | None = None,
) -> BuiltChecklist:
    """Steps for ``actions`` on ``connector`` (operator guide, else generated + SOP excerpts)."""
    actions = list(actions)[:MAX_GUIDE_STEPS]
    guide: dict[int, list[dict[str, Any]]] = {}
    source = "generated"
    if GUIDE_TOOL in tools and {"create_change_set", "add_change_set_items"} <= tools \
            and await _guide_tool_trusted(deps, tenant, connector):
        try:
            guide = await _operator_guide(
                tenant, connector, checklist_id, actions, title=f"Guided steps — plan v{plan_version}",
                origin={"system": "Keelson", "case_ref": case_id, "plan_version": plan_version, "purpose": "guide"},
            )
            source = "operator_guide"
        except (McpToolError, McpTransportError, KeyError, ValueError) as exc:
            log.warning("operator guide unavailable; generating the steps", connector=connector,
                        error=type(exc).__name__)
    templates = dict(config.get("deep_links") or {}) if isinstance(config.get("deep_links"), Mapping) else {}
    sop_cache: dict[str, list[dict[str, Any]]] = {}
    evidence: list[str] = []
    steps: list[dict[str, Any]] = []
    generated = 0
    for seq, action in enumerate(actions, start=1):
        title, instructions = generated_step(action, vendor=vendor)
        link = templated_link(action, templates)
        from_guide = guide.get(seq) or []
        if from_guide:
            first = from_guide[0]
            title = str(first.get("title") or title)[:200]
            texts = [str(g.get("instructions") or "").strip() for g in from_guide if str(g.get("instructions") or "").strip()]
            if texts:
                instructions = "\n\n".join(texts)[:4000]
            links = [str(g.get("deep_link")) for g in from_guide if g.get("deep_link")]
            link = next((x for x in links if is_safe_deep_link(x)), link)
        else:
            generated += 1
            if sop:
                op = str(action["op"])
                if op not in sop_cache:
                    sop_cache[op] = await sop_snippets(
                        deps, tenant=tenant, case_id=case_id, run_id=run_id,
                        query=f"procedure {op.replace('.', ' ').replace('_', ' ')} {vendor}", kb_ids=kb_ids, top_k=1,
                    )
                for snip in sop_cache[op]:
                    where = snip["document_title"] + (f" › {snip['heading']}" if snip["heading"] else "")
                    instructions += f"\n\n_Your procedure ({where}; unverified excerpt):_ {snip['excerpt']}"
                    evidence.append(snip["evidence_id"])
        step = ChecklistStep(
            seq=seq, title=title, instructions_md=instructions, deep_link=link, expected=expected_for(action),
            status="pending", verified_at=None, evidence_id=None,
        )
        steps.append(step.model_dump(mode="json"))
    prov = "fact" if source == "operator_guide" and generated == 0 else "inference"
    meta = {"source": source, "prov": prov, "vendor": vendor, "sop_evidence_ids": sorted(set(evidence)),
            "lines": [dict(a) for a in actions]}
    meta["baseline"] = await _baseline(tenant, connector, tools, steps)
    return BuiltChecklist(checklist_id=checklist_id, connector=connector, steps=steps, meta=meta)


# ─────────────────────────────── blocks ───────────────────────────────


def is_completed(checklist: Mapping[str, Any]) -> bool:
    steps = checklist.get("steps") or []
    return bool(steps) and all(str(s.get("status")) in _FINAL for s in steps)


def checklist_props(checklist: Mapping[str, Any]) -> RemediationChecklistProps:
    steps = [ChecklistStep.model_validate(s) for s in checklist.get("steps") or []]
    return RemediationChecklistProps(
        checklist_id=str(checklist["id"]), connector=str(checklist["connector"]), steps=steps,
        progress=RemediationChecklistProps.progress_of(steps),
    )


def checklist_block(checklist: Mapping[str, Any], *, case_id: str, as_of: dt.date | None = None) -> dict[str, Any]:
    meta = checklist.get("meta") or {}
    vendor = str(meta.get("vendor") or checklist["connector"])
    block = make_block(
        "remediation_checklist", checklist_props(checklist),
        title=f"Guided steps in {vendor}",
        provenance=BlockProvenance(kind=Provenance(str(meta.get("prov") or "inference")), engine=ENGINE_GUIDE,
                                   as_of=as_of or dt.date.today()),
        block_id=stable_block_id(case_id, "remediation_checklist", str(checklist["id"])),
    )
    return block_to_wire(block)


def _sc(tenant: str, case_id: str, label: str, prov: str = "fact") -> ScMetadata:
    return ScMetadata(tenant_id=tenant, case_id=case_id, stage=Stage.VERIFIED, prov=prov, label=label)  # type: ignore[arg-type]


async def publish_checklist(deps: Any, *, tenant: str, checklist: Mapping[str, Any], label: str,
                            run_id: str | None = None) -> dict[str, Any]:
    """Outside a run (worker verify loop): save the block and append its ``ACTIVITY_SNAPSHOT``."""
    case_id = str(checklist["case_id"])
    envelope = checklist_block(checklist, case_id=case_id)
    await deps.repo.save_block(tenant=tenant, case_id=case_id, run_id=run_id, envelope=envelope)
    await deps.bus.append(tenant=tenant, case_id=case_id, run_id=run_id,
                          event=activity_snapshot(_sc(tenant, case_id, label, str(envelope["provenance"]["kind"])),
                                                  block=envelope))
    return envelope


def completion_text(checklist: Mapping[str, Any]) -> str:
    steps = checklist.get("steps") or []
    verified = sum(1 for s in steps if s.get("status") == "verified")
    skipped = sum(1 for s in steps if s.get("status") == "skipped")
    vendor = str((checklist.get("meta") or {}).get("vendor") or checklist.get("connector"))
    tail = f" ({skipped} skipped)" if skipped else ""
    return f"Guide completed: {verified} of {len(steps)} steps in {vendor} are verified by reading them back{tail}."


async def announce_completion(deps: Any, *, tenant: str, checklist: Mapping[str, Any], run_id: str | None = None) -> None:
    """Persist + stream the "Guide completed" message (outside a run)."""
    case_id = str(checklist["case_id"])
    text = completion_text(checklist)
    await deps.repo.add_message(tenant=tenant, case_id=case_id, role="assistant", text=text, run_id=run_id)
    message_id = f"msg_guide_{checklist['id']}_done"
    sc = _sc(tenant, case_id, "Guide completed", "committed")
    for event in (text_message_start(sc, message_id=message_id),
                  text_message_content(sc, message_id=message_id, delta=text),
                  text_message_end(sc, message_id=message_id)):
        await deps.bus.append(tenant=tenant, case_id=case_id, run_id=run_id, event=event)


# ─────────────────────────────── read-back ───────────────────────────────


def _same_lines(want: Sequence[Mapping[str, Any]], have: Any) -> bool:
    if not isinstance(have, list):
        return False
    got: dict[str, int] = {}
    for line in have:
        if not isinstance(line, Mapping) or "sku" not in line:
            return False
        qty = line.get("qty", line.get("qty_ordered"))
        got[str(line["sku"])] = got.get(str(line["sku"]), 0) + int(qty or 0)
    return got == {str(x["sku"]): int(x["qty"]) for x in want}


def matches(entity: Mapping[str, Any], match: Mapping[str, Any]) -> bool:
    """Every ``match`` field equals the entity's (``lines`` compared as a sku → qty multiset)."""
    for key, value in match.items():
        if key == "lines":
            if not _same_lines(value, entity.get("lines")):
                return False
        elif isinstance(value, list):
            if [str(v) for v in value] != [str(v) for v in entity.get(key) or []]:
                return False
        elif str(entity.get(key)) != str(value) or entity.get(key) is None:
            return False
    return True


async def _candidates(session: Any, tools: set[str], expected: Mapping[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    """``[(ref, entity)]`` read back for an expected entity (reader principal, read tools only)."""
    etype = str(expected["entity_type"])
    ref = expected.get("entity_ref")
    match = expected.get("match") or {}
    if etype == "transfer_order" and "list_transfer_orders" in tools:
        data = await session.call("list_transfer_orders", {"from_dc": match.get("from_dc"), "to_dc": match.get("to_dc")})
        return [(str(t.get("code") or ""), dict(t)) for t in data.get("items") or []
                if not str(t.get("created_via") or "").startswith("change_set:")]
    if etype == "allocation" and "list_allocations" in tools:
        data = await session.call("list_allocations", {})
        return [(str(a.get("code")), dict(a)) for a in data.get("items") or [] if ref is None or a.get("code") == ref]
    if etype == "po_line" and ref and "#" in str(ref) and "get_purchase_order" in tools:
        po, _, line_no = str(ref).rpartition("#")
        data = await session.call("get_purchase_order", {"code": po})
        return [(str(ref), dict(line)) for line in data.get("lines") or [] if str(line.get("line_no")) == line_no]
    if etype == "shipment" and "list_shipments" in tools:
        po = str(match.get("po_line_ref") or "").rpartition("#")[0]
        data = await session.call("list_shipments", {"po_code": po} if po else {})
        out = []
        for shp in data.get("items") or []:
            for line in shp.get("lines") or []:
                out.append((str(shp.get("code") or ""), {"po_line_ref": line.get("po_line_ref"), "qty": line.get("qty"),
                                                         "mode": shp.get("mode")}))
        return out
    if etype == "purchase_order" and "list_purchase_orders" in tools and "get_purchase_order" in tools:
        data = await session.call("list_purchase_orders", {"supplier_code": match.get("supplier_code")})
        out = []
        for po in (data.get("items") or [])[:20]:
            full = await session.call("get_purchase_order", {"code": po.get("code")})
            out.append((str(po.get("code")), {**dict(full), "lines": [
                {"sku": ln.get("sku"), "qty": ln.get("qty_ordered", ln.get("qty"))} for ln in full.get("lines") or []]}))
        return out
    lister = f"list_{etype}s"
    if ref is None and lister in tools:
        args = {k: v for k, v in match.items() if isinstance(v, (str, int)) and not isinstance(v, bool)}
        try:
            data = await session.call(lister, args)
        except McpToolError:
            data = await session.call(lister, {})
        return [(str(x.get("code") or ""), dict(x)) for x in data.get("items") or []]
    if ref and "get_entity" in tools:
        try:
            data = await session.call("get_entity", {"type": etype, "code": str(ref)})
        except McpToolError:
            return []
        entity = dict(data.get("data") or {})
        return [(str(ref), entity)]
    return []


async def _claimed(deps: Any, tenant: str, checklist: Mapping[str, Any]) -> set[str]:
    ids = [str(s["evidence_id"]) for s in checklist.get("steps") or [] if s.get("evidence_id")]
    if not ids:
        return set()
    rows = await deps.repo.get_evidence(tenant=tenant, ids=ids)
    return {str((r.get("args") or {}).get("matched_ref")) for r in rows if (r.get("args") or {}).get("matched_ref")}


async def verify_checklist(deps: Any, *, tenant: str, checklist: Mapping[str, Any],
                           now: dt.datetime | None = None) -> tuple[dict[str, Any], list[int]]:
    """Read back every step that is not verified/skipped; mark matches ``verified`` (evidence row,
    ``verified_at``). Returns the (possibly updated) checklist and the newly verified seqs."""
    now = now or dt.datetime.now(dt.UTC)
    current = dict(checklist)
    open_steps = [s for s in current.get("steps") or [] if str(s.get("status")) not in _FINAL]
    if not open_steps or deps.checklists is None:
        return current, []
    connector = str(current["connector"])
    tools = {t.name for t in await deps.connectors.list_approved_tools(tenant=tenant, connector=connector)
             if getattr(t, "access", "read") == "read" and getattr(t, "content_trust", "trusted_data") == "trusted_data"}
    baseline = {k: set(v) for k, v in ((current.get("meta") or {}).get("baseline") or {}).items()}
    claimed = await _claimed(deps, tenant, current)
    verified: list[int] = []
    async with get_gateway().session(tenant=tenant, connector=connector, principal="reader") as session:
        for step in open_steps:
            expected = step["expected"]
            try:
                found = await _candidates(session, tools, expected)
            except McpToolError as exc:
                log.info("guide read-back failed for a step", checklist_id=current["id"], seq=step["seq"],
                         error=exc.code)
                continue
            seen = baseline.get(str(step["seq"]), set())
            hit = next(((ref, ent) for ref, ent in found
                        if matches(ent, expected.get("match") or {})
                        and (expected.get("entity_ref") is not None or (ref and ref not in seen and ref not in claimed))),
                       None)
            if hit is None:
                continue
            ref, entity = hit
            evidence_id = await deps.repo.save_evidence(
                tenant=tenant, case_id=str(current["case_id"]), run_id=None, connector=connector,
                tool=f"readback:{expected['entity_type']}",
                args={"checklist_id": current["id"], "seq": int(step["seq"]), "matched_ref": ref,
                      "entity_type": expected["entity_type"]},
                result=entity, prov="fact",
            )
            updated = await deps.checklists.set_step(
                tenant=tenant, checklist_id=str(current["id"]), seq=int(step["seq"]), status="verified",
                verified_at=now.isoformat(), evidence_id=evidence_id, actor=READBACK_ACTOR,
            )
            if updated is not None:
                current = dict(updated)
            if ref:
                claimed.add(ref)
            verified.append(int(step["seq"]))
    return current, verified


async def finish_if_completed(deps: Any, *, tenant: str, checklist: Mapping[str, Any], run_id: str | None = None,
                              announce: bool = True) -> bool:
    """Close a completed open checklist (status ``completed`` + "Guide completed" message)."""
    if not is_completed(checklist) or str(checklist.get("status") or "open") != "open":
        return False
    await deps.checklists.set_status(tenant=tenant, checklist_id=str(checklist["id"]), status="completed")
    if announce:
        await announce_completion(deps, tenant=tenant, checklist=checklist, run_id=run_id)
    return True


async def verify_open_checklists(deps: Any, *, tenant: str | None = None, connector: str | None = None,
                                 now: dt.datetime | None = None) -> int:
    """Verify every open checklist (optionally of one tenant / connector); returns steps verified."""
    if deps.checklists is None:
        return 0
    total = 0
    for row in await deps.checklists.list_open_checklists(tenant=tenant):
        t = str(row.get("tenant") or tenant or "")
        if not t or (connector is not None and str(row.get("connector")) != connector):
            continue
        try:
            updated, seqs = await verify_checklist(deps, tenant=t, checklist=row, now=now)
        except (McpToolError, McpTransportError, OSError) as exc:
            log.warning("guide verification failed", checklist_id=row.get("id"), error=type(exc).__name__)
            continue
        if not seqs:
            continue
        total += len(seqs)
        await publish_checklist(deps, tenant=t, checklist=updated,
                                label=f"Verified {len(seqs)} guided step{'s' if len(seqs) != 1 else ''}")
        await finish_if_completed(deps, tenant=t, checklist=updated)
    return total


@dataclass
class GuideVerifier:
    """The worker's auto-tick: per ``(tenant, connector)`` with open checklists, verify when a new
    webhook event arrived (``signals.latest_event_id``) or ``poll_interval_s`` passed."""

    deps: Any
    poll_interval_s: float = 60.0
    clock: Any = time.monotonic
    _last_event: dict[tuple[str, str], int | None] = field(default_factory=dict)
    _last_poll: dict[tuple[str, str], float] = field(default_factory=dict)

    async def tick(self) -> int:
        if self.deps.checklists is None:
            return 0
        targets = sorted({(str(r.get("tenant") or ""), str(r.get("connector") or ""))
                          for r in await self.deps.checklists.list_open_checklists(tenant=None)})
        verified = 0
        now = self.clock()
        for tenant, connector in targets:
            if not tenant or not connector:
                continue
            key = (tenant, connector)
            event_id: int | None = None
            if self.deps.signals is not None:
                try:
                    event_id = await self.deps.signals.latest_event_id(tenant=tenant, connector=connector)
                except Exception as exc:  # the poll still runs on its interval
                    log.debug("latest webhook event unavailable", tenant=tenant, error=type(exc).__name__)
            new_event = key in self._last_event and event_id != self._last_event[key]
            due = now - self._last_poll.get(key, float("-inf")) >= self.poll_interval_s
            self._last_event[key] = event_id
            if not (new_event or due):
                continue
            self._last_poll[key] = now
            verified += await verify_open_checklists(self.deps, tenant=tenant, connector=connector)
        return verified
