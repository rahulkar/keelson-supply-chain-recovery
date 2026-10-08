"""User-facing activity labels for connector tool calls (thinking timeline).

Every read tool gets three sentences (``70-backoffice-m2.md`` §6 carry-over):

* running — ``TOOL_CALL_START`` label: "Reading open purchase orders from Merxa";
* done — ``TOOL_CALL_RESULT`` content/label: "Read 12 open purchase orders from Merxa";
* error — when the result is a tool error: "Couldn't read purchase orders from Merxa".

Known tools (all Merxa ``/mcp`` read tools of ``20-merxa.md`` + ``22-merxa-m2.md`` and the
``/mcp-lite`` CRUD reads) have curated phrases; any other tool falls back to
"Reading <entity> from <Vendor>" with the entity taken from the tool name (``list_``/``get_``…
prefix dropped) or, for generic CRUD tools (``list_items``/``get_item``), from the ``type``
argument. Labels never contain raw payloads, only counts and entity codes from the arguments.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["ActivityLabels", "activity_labels", "humanise_tool", "is_error_result"]

_QUALIFIERS = frozenset(
    {
        "open", "planned", "released", "shipped", "received", "cancelled", "scheduled", "departed",
        "arrived", "active", "held", "confirmed", "running", "done", "draft", "validated", "applied", "new",
    }
)
"""Statuses that read naturally as an adjective ("open purchase orders")."""

_READ_PREFIXES = ("list_", "get_", "search_", "read_", "fetch_", "find_", "query_", "lookup_")
_MAX_ENTITY_CHARS = 40
"""Entity words taken from a tool name or a model-chosen ``type`` argument are capped (labels are
persisted on every TOOL_CALL_* event and shown in the timeline)."""
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.#:/-]{0,40}$")


@dataclass(frozen=True)
class ActivityLabels:
    running: str
    done: str
    error: str


@dataclass(frozen=True)
class _Noun:
    singular: str
    plural: str

    def count(self, n: int | None, qualifier: str = "") -> str:
        q = f"{qualifier} " if qualifier else ""
        if n is None:
            return f"{q}{self.plural}"
        return f"{n} {q}{self.singular if n == 1 else self.plural}"


def _n(singular: str, plural: str | None = None) -> _Noun:
    return _Noun(singular, plural or f"{singular}s")


# Record types of generic CRUD tools (Merxa /mcp-lite ``list_items(type)``) and get_entity types.
TYPE_NOUNS: dict[str, _Noun] = {
    "sku": _n("SKU"),
    "style": _n("style"),
    "store": _n("store"),
    "dc": _n("DC"),
    "purchase_order": _n("purchase order"),
    "po": _n("purchase order"),
    "po_line": _n("purchase order line"),
    "inventory": _n("inventory position"),
    "forecast": _n("forecast row"),
    "allocation": _n("allocation"),
    "transfer_order": _n("transfer order"),
    "supplier": _n("supplier"),
    "factory": _n("factory", "factories"),
    "component": _n("component"),
    "port": _n("port"),
    "lane": _n("lane"),
    "sailing": _n("sailing"),
    "shipment": _n("shipment"),
    "promotion": _n("promotion"),
}


@dataclass(frozen=True)
class _ListPhrase:
    noun: _Noun
    count_key: str = "items"
    qualifier_arg: str | None = "status"


@dataclass(frozen=True)
class _GetPhrase:
    noun: _Noun
    key_arg: str = "code"


# Plural reads: "Reading <qualifier> <plural> from <V>" / "Read <n> <qualifier> <plural> from <V>".
LIST_TOOLS: dict[str, _ListPhrase] = {
    "list_purchase_orders": _ListPhrase(_n("purchase order")),
    "get_inventory": _ListPhrase(_n("inventory position"), qualifier_arg=None),
    "get_demand_forecast": _ListPhrase(_n("demand forecast row"), qualifier_arg=None),
    "list_allocations": _ListPhrase(_n("store allocation")),
    "list_stores": _ListPhrase(_n("store"), qualifier_arg=None),
    "list_transfer_orders": _ListPhrase(_n("transfer order")),
    "list_supplier_messages": _ListPhrase(_n("supplier message"), qualifier_arg=None),
    "get_audit_trail": _ListPhrase(_n("audit trail entry", "audit trail entries"), qualifier_arg=None),
    "get_operator_guide": _ListPhrase(_n("operator guide step"), count_key="steps", qualifier_arg=None),
    "list_sailings": _ListPhrase(_n("sailing")),
    "list_shipments": _ListPhrase(_n("shipment")),
    "get_lane_options": _ListPhrase(_n("lane option"), count_key="options", qualifier_arg=None),
    "list_online_orders_at_risk": _ListPhrase(_n("at-risk online order"), qualifier_arg=None),
    "list_promotions": _ListPhrase(_n("promotion"), qualifier_arg=None),
    "list_events": _ListPhrase(_n("recent event"), qualifier_arg=None),
}

# Single-record reads: "Reading purchase order PO-1 from <V>".
GET_TOOLS: dict[str, _GetPhrase] = {
    "get_purchase_order": _GetPhrase(_n("purchase order")),
    "get_change_set": _GetPhrase(_n("change batch", "change batches"), key_arg="change_set_id"),
    "get_factory_schedule": _GetPhrase(_n("production schedule"), key_arg="factory_code"),
}


def is_error_result(result: Mapping[str, Any] | None) -> bool:
    return result is not None and isinstance(result.get("error"), str)


def _count(result: Mapping[str, Any] | None, key: str) -> int | None:
    if not result:
        return None
    value = result.get(key)
    return len(value) if isinstance(value, list) else None


def _code(value: Any) -> str:
    text = str(value or "").strip()
    return text if _CODE_RE.match(text) else ""


def _qualifier(args: Mapping[str, Any], arg: str | None) -> str:
    if not arg:
        return ""
    value = args.get(arg)
    if isinstance(value, str) and value.lower() in _QUALIFIERS:
        return value.lower()
    return ""


def _from(vendor: str) -> str:
    return f"from {vendor}"


def _list_labels(phrase: _ListPhrase, args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str,
                 *, qualifier: str | None = None) -> ActivityLabels:
    q = _qualifier(args, phrase.qualifier_arg) if qualifier is None else qualifier
    noun = phrase.noun
    return ActivityLabels(
        running=f"Reading {noun.count(None, q)} {_from(vendor)}",
        done=f"Read {noun.count(_count(result, phrase.count_key), q)} {_from(vendor)}",
        error=f"Couldn't read {noun.plural} {_from(vendor)}",
    )


def _get_labels(noun: _Noun, key: str, vendor: str, *, detail: str = "") -> ActivityLabels:
    target = f"{noun.singular} {key}" if key else f"the {noun.singular}"
    return ActivityLabels(
        running=f"Reading {target} {_from(vendor)}",
        done=f"Read {target}{detail} {_from(vendor)}",
        error=f"Couldn't read {target} {_from(vendor)}",
    )


def _type_noun(value: Any) -> _Noun:
    key = str(value or "").strip().lower()
    if key in TYPE_NOUNS:
        return TYPE_NOUNS[key]
    words = re.sub(r"[^a-z0-9]+", " ", key[:_MAX_ENTITY_CHARS]).strip() or "record"
    return _n(words)


def _filter_status(args: Mapping[str, Any]) -> str:
    raw = args.get("filter_json") or args.get("filter")
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return ""
    if isinstance(data, Mapping):
        return _qualifier(data, "status")
    return ""


def _entity_from_name(tool: str) -> str:
    lowered = tool.lower()
    for prefix in _READ_PREFIXES:
        if lowered.startswith(prefix) and len(lowered) > len(prefix):
            lowered = lowered[len(prefix) :]
            break
    words = re.sub(r"[^a-z0-9]+", " ", lowered[:_MAX_ENTITY_CHARS]).strip()
    return words or "data"


def _special(tool: str) -> Callable[[Mapping[str, Any], Mapping[str, Any] | None, str], ActivityLabels] | None:
    return _SPECIAL.get(tool)


def _locations(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    dcs, clusters = _count(result, "dcs"), _count(result, "clusters")
    done = (
        f"Read {_n('DC').count(dcs)} and {_n('store cluster').count(clusters)} {_from(vendor)}"
        if dcs is not None and clusters is not None
        else f"Read DCs and store clusters {_from(vendor)}"
    )
    return ActivityLabels(
        running=f"Reading DCs and store clusters {_from(vendor)}",
        done=done,
        error=f"Couldn't read DCs and store clusters {_from(vendor)}",
    )


def _where_used(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    lines = _count(result, "open_po_lines")
    detail = f" ({_n('open PO line').count(lines)})" if lines is not None else ""
    return ActivityLabels(
        running=f"Checking where the components are used in {vendor}",
        done=f"Checked where the components are used{detail} in {vendor}",
        error=f"Couldn't check where the components are used in {vendor}",
    )


def _search(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    query = str(args.get("query") or "").strip()[:40]
    about = f" for “{query}”" if query else ""
    n = _count(result, "items")
    return ActivityLabels(
        running=f"Searching {vendor}{about}",
        done=f"Found {_n('match', 'matches').count(n)} in {vendor}{about}" if n is not None else f"Searched {vendor}{about}",
        error=f"Couldn't search {vendor}{about}",
    )


def _entity(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    return _get_labels(_type_noun(args.get("type")), _code(args.get("code")), vendor)


def _get_item(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    return _get_labels(_type_noun(args.get("type")), _code(args.get("key") or args.get("code")), vendor)


def _list_items(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    phrase = _ListPhrase(_type_noun(args.get("type")), qualifier_arg=None)
    return _list_labels(phrase, args, result, vendor, qualifier=_filter_status(args))


def _calc(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    op = {"count": "a count", "sum": "a sum", "min": "a minimum", "max": "a maximum",
          "date_diff": "a date difference"}.get(str(args.get("op") or ""), "a value")
    return ActivityLabels(
        running=f"Calculating {op} over the evidence",
        done=f"Calculated {op} over the evidence",
        error=f"Couldn't calculate {op} over the evidence",
    )


def _factory_schedule(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    lines = _count(result, "lines")
    detail = f" ({_n('production line').count(lines)})" if lines is not None else ""
    return _get_labels(GET_TOOLS["get_factory_schedule"].noun, _code(args.get("factory_code")), vendor, detail=detail)


def _change_set(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    code = _code((result or {}).get("code")) if result else ""
    labels = _get_labels(GET_TOOLS["get_change_set"].noun, "", vendor)
    if code:
        return ActivityLabels(labels.running, f"Read change batch {code} {_from(vendor)}", labels.error)
    return labels


def _snapshot_open(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    entities = (result or {}).get("entities")
    n = len(entities) if isinstance(entities, Mapping) else None
    detail = f" ({_n('entity', 'entities').count(n)})" if n is not None else ""
    return ActivityLabels(
        running=f"Opening a data snapshot in {vendor}",
        done=f"Opened a data snapshot{detail} in {vendor}",
        error=f"Couldn't open a data snapshot in {vendor}",
    )


def _snapshot_export(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    entity = _type_noun(args.get("entity")).plural
    n = _count(result, "parts")
    detail = f" ({_n('file').count(n)})" if n is not None else ""
    return ActivityLabels(
        running=f"Exporting {entity} from {vendor}",
        done=f"Exported {entity}{detail} from {vendor}",
        error=f"Couldn't export {entity} from {vendor}",
    )


def _changes_since(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    n = _count(result, "changes")
    return ActivityLabels(
        running=f"Reading recent changes from {vendor}",
        done=f"Read {_n('recent change').count(n)} from {vendor}",  # n None → "recent changes"
        error=f"Couldn't read recent changes from {vendor}",
    )


def _screened(what: _Noun, where: str | None = None) -> Callable[[Mapping[str, Any], Mapping[str, Any] | None, str], ActivityLabels]:
    """Quarantine wrappers (M4): untrusted texts screened and read as structured items."""

    def labels(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
        source = where or _from(vendor)
        screened = result.get("screened") if result else None
        excluded = result.get("excluded") if result else None
        detail = ""
        if isinstance(excluded, int) and excluded:
            detail = f"; set aside {excluded} that looked like instructions"
        count = what.count(screened if isinstance(screened, int) else None)
        return ActivityLabels(
            running=f"Screening {what.plural} {source}",
            done=f"Screened {count} {source}{detail}",
            error=f"Couldn't read {what.plural} {source}",
        )

    return labels


def _playbooks(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    query = str(args.get("query") or "").strip()[:40]
    about = f" for “{query}”" if query else ""
    base = _screened(_n("playbook passage"), "in the knowledge base")(args, result, vendor)
    return ActivityLabels(
        running=f"Searching the knowledge base{about}",
        done=base.done.replace("in the knowledge base", f"in the knowledge base{about}"),
        error=f"Couldn't search the knowledge base{about}",
    )


def _hub_count(result: Mapping[str, Any] | None) -> int | None:
    """Records a Signal Hub wrapper returned (``<list>_total`` of its projected result)."""
    records = (result or {}).get("records")
    if not isinstance(records, Mapping):
        return None
    for key in ("signals_total", "items_total", "events_total", "hazards_total", "results_total", "alerts_total"):
        value = records.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _hub_search(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    """M6 ``search_signals_structured`` (``100`` §5): "Reading hazard alerts from Signal Hub"."""
    near = _code(str(args.get("near_node") or "").split(":", 1)[-1])
    about = f" near {near}" if near else ""
    alerts = _n("hazard alert")
    return ActivityLabels(
        running=f"Reading hazard alerts{about} {_from(vendor)}",
        done=f"Read {alerts.count(_hub_count(result))}{about} {_from(vendor)}",
        error=f"Couldn't read hazard alerts {_from(vendor)}",
    )


def _hub_event(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    return ActivityLabels(
        running=f"Reading the hazard alert's event {_from(vendor)}",
        done=f"Read the hazard alert's event {_from(vendor)}",
        error=f"Couldn't read the hazard alert's event {_from(vendor)}",
    )


def _hub_near(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    code = _code(args.get("node_code"))
    about = f" near {code}" if code else ""
    hazards = _n("hazard")
    return ActivityLabels(
        running=f"Reading hazard alerts{about} {_from(vendor)}",
        done=f"Read {hazards.count(_hub_count(result))}{about} {_from(vendor)}",
        error=f"Couldn't read hazard alerts{about} {_from(vendor)}",
    )


def _hub_exposure(args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> ActivityLabels:
    records = (result or {}).get("records")
    exposures = records.get("exposures_total") if isinstance(records, Mapping) else None
    counted = isinstance(exposures, int) and not isinstance(exposures, bool)
    detail = f" ({_n('exposed site or lane', 'exposed sites and lanes').count(exposures)})" if counted else ""
    return ActivityLabels(
        running=f"Checking which sites the hazard exposes in {vendor}",
        done=f"Checked which sites the hazard exposes{detail} in {vendor}",
        error=f"Couldn't check which sites the hazard exposes in {vendor}",
    )


_SPECIAL: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any] | None, str], ActivityLabels]] = {
    "search_signals_structured": _hub_search,
    "get_event_structured": _hub_event,
    "hazards_near_node_structured": _hub_near,
    "assess_exposure_structured": _hub_exposure,
    "read_supplier_messages_structured": _screened(_n("supplier message")),
    "read_events_structured": _screened(_n("recent event")),
    "search_playbooks_structured": _playbooks,
    "scn.snapshot_open": _snapshot_open,
    "scn.snapshot_export": _snapshot_export,
    "scn.changes_since": _changes_since,
    "list_locations": _locations,
    "get_bom_where_used": _where_used,
    "search_entities": _search,
    "get_entity": _entity,
    "get_item": _get_item,
    "list_items": _list_items,
    "calc": _calc,
    "get_factory_schedule": _factory_schedule,
    "get_change_set": _change_set,
}


def activity_labels(
    tool: str, args: Mapping[str, Any] | None, result: Mapping[str, Any] | None, vendor: str
) -> ActivityLabels:
    """Running / done / error sentences for one tool call (``result`` may be ``None`` while running)."""
    args = args or {}
    special = _special(tool)
    if special is not None:
        return special(args, result, vendor)
    if tool in LIST_TOOLS:
        return _list_labels(LIST_TOOLS[tool], args, result, vendor)
    if tool in GET_TOOLS:
        phrase = GET_TOOLS[tool]
        return _get_labels(phrase.noun, _code(args.get(phrase.key_arg)), vendor)
    entity = _entity_from_name(tool)
    n = _count(result, "items")
    return ActivityLabels(
        running=f"Reading {entity} {_from(vendor)}",
        done=f"Read {n} {entity} {_from(vendor)}" if n is not None else f"Read {entity} {_from(vendor)}",
        error=f"Couldn't read {entity} {_from(vendor)}",
    )


def humanise_tool(tool: str, args: Mapping[str, Any], result: Mapping[str, Any] | None, vendor: str) -> str:
    """The finished-call sentence (``TOOL_CALL_RESULT``): done, or the error sentence."""
    labels = activity_labels(tool, args, result, vendor)
    return labels.error if is_error_result(result) else labels.done
