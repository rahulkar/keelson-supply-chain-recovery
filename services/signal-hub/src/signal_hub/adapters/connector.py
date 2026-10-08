"""Connector events (``100`` §3): Merxa webhooks forwarded by the engine (tenant-scoped).

``POST /v1/ingest/connector-event`` body = the webhook envelope ``{type, id, timestamp, data}``
(+ optional ``connector_id``, default ``merxa``). Structured fields become ``fact`` signals
(``origin connected_system``); free text (``supplier_message.received`` subject/body,
``lane.capacity_changed`` ``carrier_note``) goes to the quarantine and can only ever become an
``inference`` signal. The free text never influences the structured signal.

| type | signal |
|---|---|
| ``sailing.updated`` | ``logistics/sailing_delay`` when ``eta`` moved later; refs ``LANE:<lane_code>`` |
| ``lane.capacity_changed`` | ``logistics/lane_capacity_cut`` (severity ``10000 − factor_bp``); refs lane + origin port |
| ``po_line.updated`` | ``supplier/supplier_delay`` when ``changed.eta_dc`` moved later |
| ``supplier_message.received`` | text only → quarantine (trusted ref ``SUPPLIER:<supplier_code>``) |

Other types are accepted and ignored.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from keelson_contracts.signals import CONNECTOR_ID_PATTERN
from signal_hub import scoring
from signal_hub.models import NewsItemDraft, SignalDraft, SourceContext, clip
from signal_hub.timeutil import not_after, parse_ts

__all__ = ["SUPPORTED_TYPES", "ConnectorEvent", "parse_connector_event"]

SUPPORTED_TYPES: Final = ("sailing.updated", "lane.capacity_changed", "po_line.updated", "supplier_message.received")
_REF_CODE_RE: Final = re.compile(r"^[A-Za-z0-9._>-]{1,64}$")
_UNLOCODE_RE: Final = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")
LICENSE: Final = "Tenant data (connected system)"
MAX_TEXT_CHARS: Final = 3500


class ConnectorEvent(BaseModel):
    """A forwarded webhook delivery (Merxa envelope, ``22-merxa-m2.md`` §5)."""

    model_config = ConfigDict(extra="ignore")

    connector_id: Annotated[str, Field(pattern=CONNECTOR_ID_PATTERN)] = "merxa"
    type: Annotated[str, Field(min_length=1, max_length=100)]
    id: Annotated[str, Field(min_length=1, max_length=200)]
    timestamp: AwareDatetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)


def _ref(scheme: str, code: Any) -> str | None:
    if not isinstance(code, str) or not _REF_CODE_RE.match(code):
        return None
    if scheme == "UNLOCODE" and not _UNLOCODE_RE.match(code):
        return None
    return f"{scheme}:{code}"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _days(before: Any, after: Any) -> int | None:
    b, a = parse_ts(before), parse_ts(after)
    if b is None or a is None:
        return None
    return int((a - b).total_seconds() // 86400)


def _base(
    ev: ConnectorEvent, ctx: SourceContext, *, native: str, kind: str, subtype: str, title: str,
    refs: list[str], severity: int, native_sev: dict[str, Any], observed: dt.datetime, expires: dt.datetime,
) -> SignalDraft:
    return SignalDraft(
        tenant=ctx.tenant,
        connector_id=ctx.connector(ev.connector_id),
        native_key=ctx.key(native),
        origin=ctx.origin("connected_system"),
        kind=kind,
        subtype=subtype,
        title=clip(title, 300) or subtype,
        summary=None,
        raw_text=None,
        geometry=None,
        location_refs=[r for i, r in enumerate(refs) if r and r not in refs[:i]],
        starts_at=observed,
        ends_at=None,
        observed_at=observed,
        expires_at=expires,
        severity_bp=scoring.clamp_bp(severity),
        severity_native=native_sev,
        provenance=ctx.provenance("fact"),
        extracted_by=None,
        source_name=ctx.source_name(f"{ev.connector_id.capitalize()} webhook"),
        source_url=None,
        license=LICENSE,
        authority_bp=scoring.AUTHORITY["connector"],
        retrieved_at=ctx.retrieved_at,
        native_id=f"{ev.type}:{ev.id}"[:500],
        authoritative=False,
        pack_id=ctx.pack_id,
    )


def parse_connector_event(
    ev: ConnectorEvent, ctx: SourceContext
) -> tuple[list[SignalDraft], list[NewsItemDraft]]:
    if ctx.tenant is None:
        raise ValueError("connector events are tenant-scoped")
    data = ev.data or {}
    observed = min(ev.timestamp.astimezone(dt.UTC), ctx.retrieved_at) if ev.timestamp else ctx.retrieved_at
    drafts: list[SignalDraft] = []
    texts: list[NewsItemDraft] = []

    if ev.type == "sailing.updated":
        delay = _days(data.get("eta_before"), data.get("eta"))
        lane = data.get("lane_code")
        if delay is not None and delay > 0:
            eta_at = parse_ts(data.get("eta")) or observed
            drafts.append(_base(
                ev, ctx, native=f"sailing/{data.get('code') or ev.id}", kind="logistics", subtype="sailing_delay",
                title=f"Sailing {data.get('code') or ''} delayed {delay} day(s)" + (f" on lane {lane}" if isinstance(lane, str) else ""),
                refs=[r for r in [_ref("LANE", lane)] if r],
                severity=scoring.delay_severity_bp(delay),
                native_sev={"scheme": "connector", "event_type": ev.type, "delay_days": delay},
                observed=observed, expires=max(eta_at, observed) + dt.timedelta(days=7),
            ))
    elif ev.type == "lane.capacity_changed":
        factor = data.get("factor_bp")
        lane = data.get("lane_code")
        if isinstance(factor, int) and not isinstance(factor, bool) and 0 <= factor < 10_000:
            refs = [r for r in (_ref("LANE", lane), _ref("UNLOCODE", data.get("origin"))) if r]
            window = f" ({data.get('from_week')}–{data.get('to_week')})" if data.get("from_week") else ""
            drafts.append(_base(
                ev, ctx, native=f"lane_capacity/{lane}/{data.get('from_week') or ev.id}", kind="logistics",
                subtype="lane_capacity_cut",
                title=f"Capacity on lane {lane} cut to {factor / 100:.0f}%{window}",
                refs=refs, severity=10_000 - factor,
                native_sev={"scheme": "connector", "event_type": ev.type, "factor_bp": factor,
                            "weeks": data.get("weeks") if isinstance(data.get("weeks"), int) else None},
                observed=observed, expires=observed + dt.timedelta(days=21),
            ))
            note = data.get("carrier_note")
            if isinstance(note, str) and note.strip():
                texts.append(_text_item(ev, ctx, native=f"lane_capacity_note/{ev.id}", title=f"Carrier note on lane {lane}",
                                        body=note, refs=refs, observed=observed))
    elif ev.type == "po_line.updated":
        changed = _dict(data.get("changed"))
        eta_change = _dict(changed.get("eta_dc"))
        delay = _days(eta_change.get("before"), eta_change.get("after")) if eta_change else None
        if delay is not None and delay > 0:
            ref = str(data.get("ref") or f"{data.get('po_code')}#{data.get('line_no')}")
            drafts.append(_base(
                ev, ctx, native=f"po_line/{ref}/{ev.id}", kind="supplier", subtype="supplier_delay",
                title=f"PO line {ref} ETA moved {delay} day(s) later",
                refs=[], severity=scoring.delay_severity_bp(delay),
                native_sev={"scheme": "connector", "event_type": ev.type, "delay_days": delay},
                observed=observed, expires=observed + dt.timedelta(days=14),
            ))
    elif ev.type == "supplier_message.received":
        supplier = data.get("supplier_code")
        subject = _str(data.get("subject"))
        body = _str(data.get("body"))
        if subject.strip() or body.strip():
            texts.append(_text_item(
                ev, ctx, native=f"supplier_message/{data.get('id') or ev.id}",
                title=subject or "Supplier message", body=body,
                refs=[r for r in [_ref("SUPPLIER", supplier)] if r], observed=not_after(parse_ts(data.get("received_at")), ctx.retrieved_at) or observed,
            ))
    return drafts, texts


def _text_item(
    ev: ConnectorEvent, ctx: SourceContext, *, native: str, title: str, body: str, refs: list[str], observed: dt.datetime
) -> NewsItemDraft:
    return NewsItemDraft(
        tenant=ctx.tenant,
        connector_id=ctx.connector(ev.connector_id),
        native_key=ctx.key(native),
        origin=ctx.origin("connected_system"),
        source_name=ctx.source_name(f"{ev.connector_id.capitalize()} webhook"),
        license=LICENSE,
        authority_bp=scoring.AUTHORITY["free_text"],
        url=None,
        title=clip(title, 300) or "Message",
        snippet=clip(body, MAX_TEXT_CHARS),
        published_at=observed,
        retrieved_at=ctx.retrieved_at,
        context={"refs": refs, "native_id": f"{ev.type}:{ev.id}"[:500], "kind_hint": "supplier"},
        pack_id=ctx.pack_id,
    )
