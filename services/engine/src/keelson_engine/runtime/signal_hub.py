"""Signal Hub reads for the runtime (M6, ``docs/contracts/100-m6-signals.md`` §5, §6).

Two consumers:

* **signal runs** ``{event_id, alert_id}`` (``nodes/signal_frame.py``): :func:`load_event_frame`
  reads the alert / event through the platform port ``RuntimeDeps.hub`` (``deps.SignalHubPort``)
  and validates it against the contract models (:class:`EventFrame`); nothing unvalidated is used;
* the **external_intel** specialist (``runtime.quarantine`` Signal Hub wrappers):
  :func:`project_hub_result` splits an MCP result into *structured records* (ids, enums, integers,
  timestamps and codes, each validated; anything else dropped) and the *untrusted texts* (titles,
  summaries, news text) that only the quarantine (guard + extractor) reads.

Untrusted text rule (``100`` §5, plan §3): news headlines, summaries and alert titles never reach a
tool-holding LLM, never become a scenario description and never decide a route or an approval.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from keelson_contracts.common import EVENT_TYPE_KEYS
from keelson_contracts.signals import (
    ALERT_SEVERITIES,
    ALERT_STATUSES,
    CONNECTOR_ID_PATTERN,
    EVENT_STATUSES,
    EXPOSURE_METHODS,
    NODE_TYPES,
    SIGNAL_KINDS,
    SIGNAL_ORIGINS,
    SIGNAL_PROVENANCES,
    SUBTYPE_TAXONOMY,
    Alert,
    DisruptionEvent,
    DisruptionSignal,
    Exposure,
    is_uuid,
)
from pydantic import ValidationError

from .context import RunContext
from .spec import SIGNAL_HUB_CONNECTOR

__all__ = [
    "EVENT_SOURCE_ALERT",
    "SIGNAL_EVENT_TOOL",
    "SIGNAL_HUB_CONNECTOR",
    "SIGNAL_NEWS_TOOL",
    "SIGNAL_STATE_KEY",
    "EventFrame",
    "HubProjection",
    "SignalHubUnavailableError",
    "case_event_id",
    "clean_code",
    "clean_label",
    "clean_location_ref",
    "kinds_arg",
    "load_event_frame",
    "node_ref",
    "parse_event_frame",
    "is_text_derived",
    "parse_node_ref",
    "project_hub_result",
]

log = structlog.get_logger(__name__)

SIGNAL_STATE_KEY: Final = "signal_frame"
"""``ctx.state``: ``{event_id, alert_id}`` of the Signal Hub event a signal run framed the case from
(read by ``approach_router`` to re-frame on **Edit**)."""
SIGNAL_EVENT_TOOL: Final = "signal_hub.event"
"""Evidence ``tool`` of the event a case was framed from (structured fields only, prov ``fact``);
``args.event_id`` / ``args.alert_id`` name it for later runs."""
SIGNAL_NEWS_TOOL: Final = "signal_hub.news"
"""Evidence ``tool`` of the news-derived structured fields of that event (prov ``inference``,
``args.content_trust = untrusted_extraction``): assumptions citing it taint the case."""
EVENT_SOURCE_ALERT: Final = "alert"
"""``approach_card.event.source`` of a case opened from an alert (``chat`` otherwise)."""

MAX_RECORDS: Final = 20
"""Structured records kept per list in a projected hub result (the rest are counted)."""
MAX_DEPTH: Final = 6


class SignalHubUnavailableError(LookupError):
    """The Signal Hub event (or alert) of a signal run cannot be read or is invalid."""


# ───────────────────────────── signal-run reads ─────────────────────────────


def is_text_derived(signal: DisruptionSignal) -> bool:
    """A signal whose fields were extracted from untrusted text: provenance ``inference``, origin
    ``news``, or any ``extracted_by`` (a demo pack's news item is ``simulated`` with a pre-baked
    extraction, ``extracted_by: prebaked`` — still news, so still unconfirmed)."""
    return signal.provenance == "inference" or signal.origin == "news" or signal.extracted_by is not None


@dataclass(frozen=True)
class EventFrame:
    """A Signal Hub event as a signal run uses it: everything validated by the contract models."""

    event: DisruptionEvent
    alert: Alert | None
    signals: tuple[DisruptionSignal, ...] = ()
    exposures: tuple[Exposure, ...] = ()

    @property
    def anchor(self) -> DisruptionSignal | None:
        return next((s for s in self.signals if s.id == self.event.anchor_signal_id), None)

    @property
    def news_signals(self) -> tuple[DisruptionSignal, ...]:
        """Member signals read from free text (:func:`is_text_derived`: news, supplier messages)."""
        return tuple(s for s in self.signals if is_text_derived(s))

    @property
    def grounded(self) -> bool:
        """The event rests on deterministic data: an authoritative anchor (GDACS / USGS / a demo
        pack's feed step) that is not text-derived, or — without an anchor — only deterministic
        members. A news-only event is not grounded: everything derived from it is an inference."""
        if self.event.anchor_signal_id is not None:
            anchor = self.anchor
            return anchor is None or not is_text_derived(anchor)
        return bool(self.signals) and not any(is_text_derived(s) for s in self.signals)

    @property
    def simulated(self) -> bool:
        return (self.alert is not None and self.alert.simulated) or any(s.provenance == "simulated"
                                                                      for s in self.signals)

    def source_names(self, limit: int = 20) -> list[str]:
        """Attributed source names (``SourceRef.name``), anchor first, cleaned, unique."""
        ordered = sorted(self.signals, key=lambda s: (s.id != self.event.anchor_signal_id, s.observed_at, s.id))
        out: list[str] = []
        for s in ordered:
            name = clean_label(s.source.name)
            if name and name not in out:
                out.append(name)
        return out[:limit]


def _items(raw: Any, key: str) -> list[Any]:
    value = raw.get(key) if isinstance(raw, Mapping) else None
    return list(value) if isinstance(value, list) else []


def parse_event_frame(raw: Mapping[str, Any], *, event_id: str, alert_id: str | None,
                      tenant: str | None = None) -> EventFrame:
    """Validate a hub read: an ``AlertDetail`` (``{alert, event, signals, exposures}``) or an event
    read (``{event, signals?, exposures?}`` or the bare ``DisruptionEvent``). Raises
    :class:`SignalHubUnavailableError` when it does not validate or names another event / alert.
    Signals and exposures that belong to another event are dropped (never trusted).

    ``tenant`` (the run's tenant; defence in depth behind the hub's RLS and the service JWT's
    tenant claim): an alert or a tenant event of another tenant is refused, and member signals /
    exposures of another tenant are dropped — another tenant's network never frames this case."""
    try:
        event_raw = raw.get("event") if isinstance(raw.get("event"), Mapping) else raw
        event = DisruptionEvent.model_validate(event_raw)
        alert = Alert.model_validate(raw["alert"]) if isinstance(raw.get("alert"), Mapping) else None
        members = set(event.member_signal_ids)
        signals = tuple(s for s in (DisruptionSignal.model_validate(x) for x in _items(raw, "signals"))
                        if s.id in members and (tenant is None or s.tenant in (None, tenant)))
        exposures = tuple(e for e in (Exposure.model_validate(x) for x in _items(raw, "exposures"))
                          if e.event_id == event.id and (tenant is None or e.tenant == tenant))
    except (ValidationError, ValueError, TypeError) as exc:
        raise SignalHubUnavailableError(f"the Signal Hub read does not validate: {type(exc).__name__}") from exc
    if event.id != event_id:
        raise SignalHubUnavailableError("the Signal Hub returned another event")
    if tenant is not None and (event.tenant not in (None, tenant)
                               or (alert is not None and alert.tenant != tenant)):
        raise SignalHubUnavailableError("the Signal Hub returned another tenant's event or alert")
    if alert_id is not None and (alert is None or alert.id != alert_id or alert.event_id != event.id):
        raise SignalHubUnavailableError("the alert does not belong to the event")
    return EventFrame(event=event, alert=alert, signals=signals, exposures=exposures)


async def load_event_frame(run: RunContext, *, event_id: str, alert_id: str | None) -> EventFrame:
    """The event of a signal run through ``run.deps.hub`` (the alert detail when an alert is
    named, else the event read). Raises :class:`SignalHubUnavailableError`."""
    if not is_uuid(event_id) or (alert_id is not None and not is_uuid(alert_id)):
        raise SignalHubUnavailableError("signal run ids must be canonical UUIDs")
    hub = getattr(run.deps, "hub", None)
    if hub is None:
        raise SignalHubUnavailableError("no Signal Hub port is configured")
    try:
        raw = (await hub.get_alert(tenant=run.tenant, alert_id=alert_id) if alert_id is not None
               else await hub.get_event(tenant=run.tenant, event_id=event_id))
    except SignalHubUnavailableError:
        raise
    except Exception as exc:  # transport / auth / HTTP: the run reports a generic failure
        log.warning("signal hub read failed", event_id=event_id, alert_id=alert_id, error=type(exc).__name__)
        raise SignalHubUnavailableError(f"Signal Hub read failed: {type(exc).__name__}") from exc
    if not isinstance(raw, Mapping):
        raise SignalHubUnavailableError("the Signal Hub has no such event or alert")
    if alert_id is not None and isinstance(raw.get("event"), Mapping) and not _items(raw, "exposures"):
        # an alert detail without exposures: the event read may carry them
        try:
            extra = await hub.get_event(tenant=run.tenant, event_id=event_id)
        except Exception as exc:
            log.info("event exposures unavailable", error=type(exc).__name__)
            extra = None
        if isinstance(extra, Mapping) and _items(extra, "exposures"):
            raw = {**raw, "exposures": _items(extra, "exposures")}
    return parse_event_frame(raw, event_id=event_id, alert_id=alert_id, tenant=run.tenant)


async def case_event_id(run: RunContext, state: Mapping[str, Any] | None = None) -> str | None:
    """The Signal Hub event the case was framed from: this session's state, else the case's latest
    :data:`SIGNAL_EVENT_TOOL` evidence row (later runs start with an empty state)."""
    frame = (state or {}).get(SIGNAL_STATE_KEY) if state is not None else None
    if isinstance(frame, Mapping) and is_uuid(frame.get("event_id")):
        return str(frame["event_id"])
    latest = getattr(run.deps.repo, "latest_evidence", None)
    if latest is None:
        return None
    try:
        row = await latest(tenant=run.tenant, case_id=run.case_id, tool=SIGNAL_EVENT_TOOL)
    except Exception as exc:  # a default argument only: never fail the tool over it
        log.info("case event lookup failed", error=type(exc).__name__)
        return None
    args = (row or {}).get("args") or {}
    value = args.get("event_id") if isinstance(args, Mapping) else None
    return str(value) if is_uuid(value) else None


# ───────────────────────────── text hygiene ─────────────────────────────

_LABEL_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,&'()/+-]{0,79}$")
_CODE_RE: Final = re.compile(r"^[A-Z0-9][A-Z0-9_.#:>/-]{0,40}$")
_REF_RE: Final = re.compile(r"^(UNLOCODE|ISO3166|CHOKEPOINT|REGION|SUPPLIER|FACTORY|DC|STORE|LANE):([A-Z0-9][A-Z0-9._>-]{0,40})$")
_ISO_TIME_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}(T[0-9:.+\-Z]{2,40})?$")
_CONNECTOR_RE: Final = re.compile(CONNECTOR_ID_PATTERN)


def _normalise(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch)[0] != "C")


def clean_label(value: Any) -> str | None:
    """A short configured label (a source name such as ``GDACS`` or ``Demo Freight Daily``, a
    licence) that may be shown to an agent: plain words only, never an instruction-like string."""
    if not isinstance(value, str):
        return None
    from .quarantine import looks_like_injection  # noqa: PLC0415 - quarantine imports this module

    text = " ".join(_normalise(value).split())
    if not _LABEL_RE.match(text) or looks_like_injection(text) is not None:
        return None
    return text


def clean_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _CODE_RE.match(text) else None


def clean_location_ref(value: Any) -> str | None:
    """``SCHEME:CODE`` with an upper-case code (``UNLOCODE:VNSGN``), else ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _REF_RE.match(text) else None


# ───────────────────────────── MCP projection ─────────────────────────────

_ID_KEYS: Final = frozenset({"id", "event_id", "alert_id", "signal_id", "anchor_signal_id", "supersedes", "case_ref"})
_ID_LIST_KEYS: Final = frozenset({"member_signal_ids", "signal_ids", "event_ids"})
_ENUMS: Final[Mapping[str, frozenset[str]]] = {
    "kind": frozenset(SIGNAL_KINDS),
    "origin": frozenset(SIGNAL_ORIGINS),
    "provenance": frozenset(SIGNAL_PROVENANCES),
    "status": frozenset(EVENT_STATUSES) | frozenset(ALERT_STATUSES),
    "method": frozenset(EXPOSURE_METHODS),
    "node_type": frozenset(NODE_TYPES),
    "type": frozenset(NODE_TYPES),
    "severity": frozenset(ALERT_SEVERITIES),
    "taxonomy_key": frozenset(EVENT_TYPE_KEYS),
}
_INT_KEYS: Final = frozenset({
    "severity_bp", "confidence_bp", "authority_bp", "distance_km", "weight_bp", "version", "radius_km",
    "open_po_lines", "units_inbound", "supplier", "factory", "port", "dc", "store", "lane", "total", "count",
    "sites", "exposed_nodes",
})
_TIME_KEYS: Final = frozenset({
    "starts_at", "ends_at", "observed_at", "expires_at", "updated_at", "retrieved_at", "first_raised_at",
    "since",
})
_CODE_KEYS: Final = frozenset({"node_code", "code"})
_LABEL_KEYS: Final = frozenset({"name", "license"})
"""Configured labels (a source's name / licence): kept only inside a ``source`` object."""
_TEXT_KEYS: Final = frozenset({
    "title", "summary", "raw_text", "headline", "snippet", "description", "why", "text", "body", "note",
})
"""Free text (news headlines, summaries, LLM-written event titles): quarantine only."""
_NESTED: Final = frozenset({
    "event", "alert", "source", "window", "exposure_summary", "nodes_by_type", "summary", "node", "anchor",
})
_LISTS: Final = frozenset({
    "signals", "items", "events", "hazards", "exposures", "results", "members", "nodes", "alerts",
})
_DROPPED: Final = frozenset({"geometry", "url", "raw", "severity_native", "extracted_by", "tenant"})


@dataclass
class HubProjection:
    """``records``: the validated structured part; ``texts``: ``[{item_id, fields: {name: text}}]``
    (untrusted, for the quarantine only)."""

    records: dict[str, Any] = field(default_factory=dict)
    texts: list[dict[str, Any]] = field(default_factory=list)


def _subtype(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value in SUBTYPE_TAXONOMY else "other"


def _scalar(key: str, value: Any, *, in_source: bool) -> tuple[bool, Any]:
    """``(keep, cleaned)`` for a scalar under ``key``."""
    if key in _ID_KEYS:
        return (True, value) if is_uuid(value) else (False, None)
    if key in _ENUMS:
        return (True, value) if isinstance(value, str) and value in _ENUMS[key] else (False, None)
    if key == "subtype":
        cleaned = _subtype(value)
        return cleaned is not None, cleaned
    if key in _INT_KEYS:
        ok = isinstance(value, int) and not isinstance(value, bool) and -10**12 < value < 10**12
        return (True, value) if ok else (False, None)
    if key in _TIME_KEYS:
        return (True, value) if isinstance(value, str) and _ISO_TIME_RE.match(value) else (False, None)
    if key in _CODE_KEYS:
        cleaned = clean_code(value)
        return cleaned is not None, cleaned
    if key == "connector_id":
        return (True, value) if isinstance(value, str) and _CONNECTOR_RE.match(value) else (False, None)
    if key in ("simulated", "tsunami") and isinstance(value, bool):
        return True, value
    if in_source and key in _LABEL_KEYS:
        cleaned = clean_label(value)
        return cleaned is not None, cleaned
    return False, None


def project_hub_result(result: Any, *, max_texts: int = 8) -> HubProjection:
    """Split a Signal Hub MCP result into validated structured records and untrusted texts.

    Only known keys survive, each validated (ids are UUIDs, enums are contract values, a subtype
    outside the deterministic taxonomy map becomes ``other``, codes are upper-case codes, location
    refs ``SCHEME:CODE``); lists keep their first :data:`MAX_RECORDS` entries (``<list>_total``
    counts all). Text fields of a record become one text item (``item_id`` = the record's id)."""
    out = HubProjection()
    seen: set[tuple[str, str]] = set()

    def walk(node: Mapping[str, Any], depth: int, *, in_source: bool = False) -> dict[str, Any]:
        record: dict[str, Any] = {}
        texts: dict[str, str] = {}
        for raw_key, value in node.items():
            key = str(raw_key)
            if key in _DROPPED or depth > MAX_DEPTH:
                continue
            if key in _TEXT_KEYS and isinstance(value, str):
                if value.strip():
                    texts[key] = value
                continue
            if isinstance(value, Mapping):
                if key in _NESTED:
                    inner = walk(value, depth + 1, in_source=key == "source")
                    if inner:
                        record[key] = inner
                continue
            if isinstance(value, list):
                if key == "location_refs":
                    refs = [r for r in (clean_location_ref(v) for v in value) if r]
                    record[key] = list(dict.fromkeys(refs))[:MAX_RECORDS]
                elif key in _ID_LIST_KEYS:
                    ids = [v for v in value if is_uuid(v)]
                    record[key] = ids[:MAX_RECORDS]
                    record[f"{key}_total"] = len(ids)
                elif key in _LISTS:
                    rows = [walk(v, depth + 1) for v in value if isinstance(v, Mapping)]
                    record[key] = [r for r in rows if r][:MAX_RECORDS]
                    record[f"{key}_total"] = len(rows)
                elif key == "kinds":
                    record[key] = [v for v in value if isinstance(v, str) and v in _ENUMS["kind"]][:MAX_RECORDS]
                continue
            keep, cleaned = _scalar(key, value, in_source=in_source)
            if keep:
                record[key] = cleaned
        if texts and len(out.texts) < max_texts:
            item_id = record.get("id") if isinstance(record.get("id"), str) else f"item-{len(out.texts) + 1}"
            fingerprint = "\n".join(f"{k}:{v}" for k, v in sorted(texts.items()))
            if (str(item_id), fingerprint) not in seen:
                seen.add((str(item_id), fingerprint))
                out.texts.append({"item_id": str(item_id), "fields": dict(sorted(texts.items()))})
        return record

    if isinstance(result, Mapping):
        out.records = walk(result, 0)
    return out


def node_ref(node_type: str, node_code: str) -> str:
    """``port:VNSGN`` (the ``near_node`` form)."""
    return f"{node_type}:{node_code}"


def parse_node_ref(value: Any) -> tuple[str, str] | None:
    """``"port:VNSGN"`` → ``("port", "VNSGN")`` (valid node type + code only)."""
    if not isinstance(value, str) or ":" not in value:
        return None
    node_type, code = value.split(":", 1)
    node_type = node_type.strip().lower()
    cleaned = clean_code(code.strip())
    if node_type not in NODE_TYPES or cleaned is None:
        return None
    return node_type, cleaned


def kinds_arg(value: Any) -> list[str]:
    """``"natural_hazard, labor"`` / a list → valid signal kinds."""
    parts: Sequence[Any] = value if isinstance(value, list) else str(value or "").split(",")
    return [p.strip() for p in parts if isinstance(p, str) and p.strip() in SIGNAL_KINDS][:len(SIGNAL_KINDS)]
