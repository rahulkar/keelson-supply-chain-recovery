"""Framing a case from a Signal Hub event (M6, ``docs/contracts/100-m6-signals.md`` §6).

A ``signal`` run ``{event_id, alert_id}`` (``pre_route`` → ``frame_case`` with ``{signal: …}``)
frames the case **deterministically** — no intake, no LLM — and then follows the normal path
(``quick_exposure`` → ``planner`` → approach card → investigation incl. ``external_intel``):

* the event (``runtime.signal_hub.load_event_frame``, contract-validated) maps to a taxonomy key
  with :func:`keelson_contracts.signals.taxonomy_key_for` (tropical cyclone / storm → ``NAT.storm``,
  earthquake → ``NAT.earthquake``, flood → ``NAT.flood``, port closure → ``LOG.port_closure``,
  strike → ``LOG.strike``, supplier delay → ``SUP.delay``; anything else → ``OTHER``);
* the exposed nodes (``Exposure.node_type`` / ``node_code`` from the tenant network the engine
  pushed) become the matched entities, and the template's targets are taken from them;
* the window: the start comes from the event (not before the planning date); the duration comes
  from the event only for non-hazard events with an end (a port closure, a strike), otherwise the
  taxonomy prior (``assumption``) — the end of a storm is not the end of the disruption it causes;
* provenance: an event resting on an authoritative feed anchor (GDACS / USGS / a demo pack) is a
  ``fact``; a news-only event (``inference`` members only) makes every event-derived value an
  ``inference`` with ``unconfirmed=True`` citing the ``signal_hub.news`` evidence row
  (``untrusted_extraction``: the case is tainted, never auto-approved). News members of an
  anchored event add the unconfirmed assumption ``news_reports``;
* the description and assumption texts are built from enums, codes and the taxonomy labels only:
  no headline, summary or alert title (untrusted text) reaches a scenario, a prompt or a route;
* ``mode: actual``; the approach card waits for a person (``framing.is_signal_scenario``) and the
  ``alert_origin@1`` card ("Opened from alert") is shown at the top of the thread.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any, Final

import structlog
from google.adk.agents.context import Context
from keelson_contracts.blocks import AlertOriginProps
from keelson_contracts.common import EntityRef, Provenance, event_family_of
from keelson_contracts.optimizer import ProblemData
from keelson_contracts.scenario import Assumption, DisruptionScenario
from keelson_contracts.signals import SUBTYPE_TAXONOMY, alert_severity_for, taxonomy_key_for

from ..context import RunContext
from ..geo import get_geo
from ..mcp_client import get_gateway
from ..problem import ToolCaller, gather_problem_data
from ..quarantine import CONTENT_TRUST_EXTRACTION, looks_like_injection
from ..signal_hub import (
    SIGNAL_EVENT_TOOL,
    SIGNAL_HUB_CONNECTOR,
    SIGNAL_NEWS_TOOL,
    SIGNAL_STATE_KEY,
    EventFrame,
    SignalHubUnavailableError,
    clean_label,
    clean_location_ref,
    is_text_derived,
    load_event_frame,
)
from .common import ENGINE_FRAME, block_event, case_event, progress_event, say
from .framing import (
    SIGNAL_ASSUMPTION_KEY,
    Stated,
    _iso_date,
    _persist_problem,
    _RecordingCaller,
    _set_problem_state,
    _walk_receipts,
    bundle_ids,
    compile_template,
    data_source_assumption,
    frame_problem_bundle,
    overrides_from,
    place_index,
)
from .taxonomy import EventTemplate, Taxonomy, get_taxonomy

__all__ = [
    "EXPOSED_ASSUMPTION_KEY",
    "HAZARD_KINDS",
    "NEWS_ASSUMPTION_KEY",
    "build_event_scenario",
    "event_description",
    "event_stated",
    "exposed_refs",
    "fact_window",
    "frame_from_event",
    "frame_signal_case",
]

log = structlog.get_logger(__name__)

EXPOSED_ASSUMPTION_KEY: Final = "exposed_nodes"
NEWS_ASSUMPTION_KEY: Final = "news_reports"
HAZARD_KINDS: Final = frozenset({"natural_hazard", "weather"})
"""Event kinds whose feed window is the hazard's own lifetime (the duration is a taxonomy prior)."""
NODE_ORDER: Final = ("port", "factory", "supplier", "dc", "store", "lane")
NODE_NOUNS: Final[Mapping[str, str]] = {
    "port": "port", "factory": "factory", "supplier": "supplier", "dc": "DC", "store": "store", "lane": "lane",
}
MAX_DESCRIBED: Final = 6
_NODE_CODE_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.#:>/+-]{0,119}$")


# ───────────────────────────── pure framing ─────────────────────────────


def _utc_date(value: dt.datetime) -> dt.date:
    return value.astimezone(dt.UTC).date()


def _subtype_words(subtype: str) -> str:
    """The event subtype in words, only when it is a known deterministic subtype (news subtypes
    are written by the extractor: never echoed)."""
    return subtype.replace("_", " ") if subtype in SUBTYPE_TAXONOMY else "other event"


def _location_labels(problem: ProblemData | None) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    if problem is None:
        return out
    for loc in problem.locations:
        if loc.name:
            out[(str(loc.kind), loc.code)] = loc.name
    return out


def exposed_refs(frame: EventFrame, idx: Any, problem: ProblemData | None) -> list[EntityRef]:
    """The exposed nodes as matched entities (node order port → factory → supplier → DC → store →
    lane, then code), labelled from the tenant's own data. Codes that do not look like codes are
    dropped (the hub's exposure rows name the tenant network the engine pushed)."""
    labels = _location_labels(problem)
    seen: set[tuple[str, str]] = set()
    refs: list[EntityRef] = []
    for exposure in sorted(frame.exposures, key=lambda e: (NODE_ORDER.index(e.node_type), e.node_code)):
        code = exposure.node_code.strip()
        key = (exposure.node_type, code)
        if key in seen or not _NODE_CODE_RE.match(code) or looks_like_injection(code) is not None:
            continue
        seen.add(key)
        label = (labels.get(key) or (idx.ports.get(code) if exposure.node_type == "port" else None)
                 or (idx.located.get(exposure.node_type, {}).get(code, ("", ""))[0]) or code)
        refs.append(EntityRef(type=exposure.node_type, code=code, label=str(label)[:120]))
    return refs


def fact_window(frame: EventFrame) -> tuple[dt.datetime, dt.datetime | None]:
    """``(starts_at, ends_at)`` the frame states with the event's provenance. The hub's event window
    spans *every* member (``min`` start / ``max`` end), so a news member of a grounded event could
    move it; a grounded event with news members therefore takes the window of its deterministic
    members only (the anchor, connector facts). A news-only event keeps the event window (all of it
    is an unconfirmed inference anyway)."""
    window = frame.event.window
    if not frame.grounded or not frame.news_signals:
        return window.starts_at, window.ends_at
    facts = [s for s in frame.signals if not is_text_derived(s)]
    if not facts:  # the anchor was not part of the read (unexpected): the hub's event window
        return window.starts_at, window.ends_at
    ends = [s.ends_at for s in facts]
    end = None if any(e is None for e in ends) else max(e for e in ends if e is not None)
    start = min(s.starts_at for s in facts)
    return start, end if end is not None and end >= start else None


def event_stated(frame: EventFrame, template: EventTemplate, overrides: Mapping[str, Any], planning_date: dt.date,
                 prov: Provenance) -> Stated:
    """The event's own values as :class:`Stated` with provenance ``prov`` (approach-card edits in
    ``overrides`` win as decisions). The start is the event's start, not before the planning
    date; the duration is the event's window only for a non-hazard event with an end."""
    starts_at, ends_at = fact_window(frame)
    start = max(_utc_date(starts_at), planning_date)
    duration: int | None = None
    if (template.has_duration and ends_at is not None and frame.event.kind not in HAZARD_KINDS):
        end = _utc_date(ends_at)
        if end >= start:
            duration = (end - start).days + 1
    provs: dict[str, Provenance] = {"start_date": prov}
    if duration is not None:
        provs["duration_days"] = prov

    def num(key: str) -> int | None:
        value = overrides.get(key)
        return None if value is None or isinstance(value, list) else int(value)

    for key in ("duration_days", "start_date"):
        if overrides.get(key) is not None:
            provs.pop(key, None)  # the person's edit: a decision
    stated_duration = num("duration_days")
    receipts = overrides.get("affected_receipts")
    return Stated(
        delay_days=None if num("delay_days") is None else max(0, num("delay_days") or 0),
        duration_days=stated_duration if stated_duration is not None and stated_duration >= 1 else duration,
        demand_change_pct=num("demand_change_pct"),
        capacity_pct=num("capacity_pct"),
        start_date=_iso_date(overrides.get("start_date")) or start,
        receipts=tuple(str(r) for r in receipts) if isinstance(receipts, list) and receipts else None,
        provs=provs,
    )


def _severity(frame: EventFrame) -> str:
    return frame.alert.severity if frame.alert is not None else alert_severity_for(frame.event.severity_bp)


def _node_text(ref: EntityRef) -> str:
    return f"{NODE_NOUNS.get(ref.type, ref.type)} {ref.code}"


def event_description(frame: EventFrame, template: EventTemplate, matched: Sequence[EntityRef]) -> str:
    """"Opened from alert: Storm (tropical cyclone), high severity, exposing port VNSGN, factory
    F-VN-01 and supplier SUP-VN-01, plus lanes on the affected routes" — enums, taxonomy labels
    and entity codes only (no digits of its own, no untrusted text)."""
    head = "Opened from alert" if frame.alert is not None else "Opened from a Signal Hub event"
    text = f"{head}: {template.label} ({_subtype_words(frame.event.subtype)}), {_severity(frame)} severity"
    sites = [r for r in matched if r.type != "lane"]
    lanes = [r for r in matched if r.type == "lane"]
    if sites:
        named = [_node_text(r) for r in sites[:MAX_DESCRIBED]]
        listed = ", ".join(named[:-1]) + f" and {named[-1]}" if len(named) > 1 else named[0]
        text += f", exposing {listed}" + (" and other sites" if len(sites) > MAX_DESCRIBED else "")
    elif not lanes:
        text += ", no exposed site in your network yet"
    if lanes:
        text += ", plus lanes on the affected routes" if sites else ", exposing lanes on the affected routes"
    if frame.simulated:
        text += " (simulated demo data)"
    return text


def _sources_text(frame: EventFrame) -> str:
    names = frame.source_names(4)
    if not names:
        return "Signal Hub"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def _exposed_text(matched: Sequence[EntityRef]) -> str:
    sites = [r for r in matched if r.type != "lane"]
    lanes = [r for r in matched if r.type == "lane"]
    parts = [_node_text(r) + (f" ({r.label})" if r.label and r.label != r.code else "") for r in sites[:8]]
    if len(sites) > 8:
        parts.append(f"{len(sites) - 8} more sites")
    if lanes:
        parts.append(f"{len(lanes)} lane{'s' if len(lanes) != 1 else ''}")
    return ", ".join(parts) if parts else "No site or lane of your network is in the footprint"


def _news_text(frame: EventFrame) -> str:
    """What the news members report, as words from the deterministic vocabulary and codes only."""
    reports: list[str] = []
    for signal in sorted(frame.news_signals, key=lambda s: (s.observed_at, s.id)):
        refs = [r.split(":", 1)[1] for r in (clean_location_ref(x) for x in signal.location_refs) if r]
        where = f" at {', '.join(refs[:3])}" if refs else ""
        source = clean_label(signal.source.name)
        by = f" ({source})" if source else ""
        report = f"{_subtype_words(signal.subtype)}{where}{by}"
        if report not in reports:
            reports.append(report)
    return "Reported in the news, not confirmed: " + "; ".join(reports[:3]) if reports else ""


def frame_from_event(
    frame: EventFrame,
    *,
    tenant: str,
    scenario_id: str,
    planning_date: dt.date,
    vendor: str,
    config_version: str,
    default_delay_days: int,
    overrides: Mapping[str, Any] | None = None,
    problem: ProblemData | None = None,
    receipts: Sequence[str] = (),
    taxonomy: Taxonomy | None = None,
    idx: Any = None,
    evidence_id: str | None = None,
    news_evidence_id: str | None = None,
    data_source: Assumption | None = None,
) -> DisruptionScenario:
    """The deterministic scenario of a Signal Hub event (module docstring). Pure: ``receipts``
    are the open PO lines already walked from the exposed suppliers / factories (``resolve:
    receipts`` templates), ``idx`` the :func:`framing.place_index` of ``problem``."""
    tax = taxonomy or get_taxonomy()
    event_type = taxonomy_key_for(frame.event.subtype)
    template = tax.template(event_type)
    idx = idx if idx is not None else place_index(problem, get_geo())
    grounded = frame.grounded
    base = Provenance.FACT if grounded else Provenance.INFERENCE
    matched = exposed_refs(frame, idx, problem)
    stated = event_stated(frame, template, overrides or {}, planning_date, base)
    perturbations, assumptions, extra = compile_template(
        template=template, stated=stated, matched=matched, receipts=receipts, idx=idx, problem=problem,
        all_clusters=[c.code for c in problem.clusters] if problem is not None else [],
        planning_date=planning_date, prior_p50={"delay_days": default_delay_days}, vendor=vendor,
    )
    receipt_ids: list[str] = next((p.target.ids for p in perturbations if p.op == "lead_time_shift"
                                   and p.target.kinds == ["receipt"]), [])
    for code in sorted({r.split("#", 1)[0] for r in receipt_ids}):
        if not any(m.type == "po" and m.code == code for m in matched):
            matched.append(EntityRef(type="po", code=code, label=f"Purchase order {code}"))
    for ref in extra:
        if not any((m.type, m.code) == (ref.type, ref.code) for m in matched):
            matched.append(ref)
    assumptions = [
        a.model_copy(update={"value_text": "None of the exposed sites is a " + " or ".join(
            k.replace("_", " ") for k in next((t.target_kinds for t in template.perturbations
                                               if a.key == f"targets_{t.op}"), ("site",)))})
        if a.key.startswith("targets_") else a
        for a in assumptions
    ]
    severity = _severity(frame)
    signal_text = f"{severity.capitalize()} severity {template.label.lower()} alert from {_sources_text(frame)}"
    if frame.simulated:
        signal_text += " (DEMO: simulated scenario pack)"
    head = [
        Assumption(key=SIGNAL_ASSUMPTION_KEY, label="Opened from alert" if frame.alert is not None else "Signal Hub event",
                   value_text=signal_text, prov=base),
        Assumption(key=EXPOSED_ASSUMPTION_KEY, label="Exposed sites", value_text=_exposed_text(matched), prov=base),
    ]
    assumptions = head + assumptions
    if not template.perturbations:
        assumptions.append(Assumption(
            key="event_type", label="Event type",
            value_text="Not a disruption type I can simulate, so I will investigate it as a question",
            prov=Provenance.INFERENCE,
        ))
    taint_ids = [news_evidence_id or evidence_id] if (news_evidence_id or evidence_id) else []
    if not grounded:
        # a news-only event: every value taken from it is an inference to confirm
        assumptions = [
            a.model_copy(update={"prov": Provenance.INFERENCE, "unconfirmed": True,
                                 "source_evidence_ids": list(taint_ids)})
            if a.prov in (Provenance.FACT, Provenance.INFERENCE) else a
            for a in assumptions
        ]
        perturbations = [p.model_copy(update={"prov": Provenance.INFERENCE}) for p in perturbations]
    elif frame.news_signals and (news := _news_text(frame)):
        assumptions.append(Assumption(key=NEWS_ASSUMPTION_KEY, label="News reports", value_text=news,
                                      prov=Provenance.INFERENCE, unconfirmed=True,
                                      source_evidence_ids=list(taint_ids)))
    if data_source is not None:
        assumptions.append(data_source)
    return DisruptionScenario(
        id=scenario_id,
        tenant=tenant,
        mode="actual",
        event_family=event_family_of(event_type),
        event_type=event_type,
        description=event_description(frame, template, matched),
        perturbations=perturbations,
        matched=matched,
        assumptions=assumptions,
        config_version=config_version,
    )


async def build_event_scenario(
    run: RunContext,
    caller: ToolCaller,
    *,
    frame: EventFrame,
    overrides: Mapping[str, Any],
    po_lines: Sequence[Mapping[str, Any]],
    scenario_id: str,
    snapshot_data: ProblemData | None = None,
    problem: ProblemData | None = None,
    directory: Any = None,
    data_source: Assumption | None = None,
    evidence_id: str | None = None,
    news_evidence_id: str | None = None,
    taxonomy: Taxonomy | None = None,
) -> DisruptionScenario:
    """:func:`frame_from_event` plus the receipts walk of ``resolve: receipts`` templates (a
    supplier-delay event walks the exposed suppliers' BOM to their open PO lines)."""
    tax = taxonomy or get_taxonomy()
    template = tax.template(taxonomy_key_for(frame.event.subtype))
    data = problem if problem is not None else snapshot_data
    idx = place_index(data, get_geo(), directory)
    receipts: list[str] = []
    if any(t.resolve == "receipts" for t in template.perturbations) and not overrides.get("affected_receipts"):
        receipts = await _walk_receipts(caller, exposed_refs(frame, idx, data), po_lines, snapshot_data, data)
    return frame_from_event(
        frame, tenant=run.tenant, scenario_id=scenario_id, planning_date=_planning_date(run), vendor=run.spec.vendor_name,
        config_version=f"{run.spec.version}/{run.spec.policy.version}",
        default_delay_days=run.spec.planning.default_delay_days, overrides=overrides, problem=data,
        receipts=receipts, taxonomy=tax, idx=idx, evidence_id=evidence_id, news_evidence_id=news_evidence_id,
        data_source=data_source,
    )


def _planning_date(run: RunContext) -> dt.date:
    from .common import as_of  # noqa: PLC0415

    return as_of(run)


# ───────────────────────────── evidence ─────────────────────────────


def _source_url(signal: Any) -> str | None:
    """The source's attribution link (``SourceUrl``: plain absolute http(s), contract-validated)
    of a deterministic feed signal only. A news / text-derived item's link is chosen by the
    publisher (untrusted), so it never lands in an evidence row a ``{{ev:…}}`` placeholder could
    paste into a narrative."""
    url = getattr(signal.source, "url", None)
    return url if isinstance(url, str) and not is_text_derived(signal) else None


def _signal_record(signal: Any) -> dict[str, Any]:
    source = signal.source
    return {
        "id": signal.id, "origin": signal.origin, "kind": signal.kind,
        "subtype": signal.subtype if signal.subtype in SUBTYPE_TAXONOMY else "other",
        "provenance": signal.provenance, "severity_bp": signal.severity_bp, "confidence_bp": signal.confidence_bp,
        "location_refs": [r for r in (clean_location_ref(x) for x in signal.location_refs) if r],
        "starts_at": signal.starts_at.isoformat(), "ends_at": signal.ends_at.isoformat() if signal.ends_at else None,
        "observed_at": signal.observed_at.isoformat(), "version": signal.version,
        "source": {"connector_id": source.connector_id, "name": clean_label(source.name),
                   "license": clean_label(source.license), "url": _source_url(signal), "native_id": None},
    }


def event_record(frame: EventFrame) -> dict[str, Any]:
    """The structured part of an event read, as stored in the ``signal_hub.event`` evidence row:
    ids, enums, numbers, timestamps, codes and attribution — never titles, summaries or news text.

    For a grounded event the row is a ``fact``: its ``signals`` are the deterministic members only
    (the news members' fields live in the ``signal_hub.news`` row, marked ``untrusted_extraction``),
    so no claim can cite a news-derived value as a fact through this row."""
    event = frame.event
    members = [s for s in frame.signals if not (frame.grounded and is_text_derived(s))]
    out: dict[str, Any] = {
        "event": {
            "id": event.id, "kind": event.kind,
            "subtype": event.subtype if event.subtype in SUBTYPE_TAXONOMY else "other",
            "taxonomy_key": event.taxonomy_key, "status": event.status, "severity_bp": event.severity_bp,
            "confidence_bp": event.confidence_bp, "anchor_signal_id": event.anchor_signal_id,
            "member_count": len(event.member_signal_ids),
            "window": {"starts_at": event.window.starts_at.isoformat(),
                       "ends_at": event.window.ends_at.isoformat() if event.window.ends_at else None},
            "updated_at": event.updated_at.isoformat(),
        },
        "signals": [_signal_record(s) for s in members],
        "news_signal_count": len(frame.news_signals),
        "exposures": [e.model_dump(mode="json") for e in frame.exposures],
        "grounded": frame.grounded,
        "simulated": frame.simulated,
    }
    if frame.alert is not None:
        alert = frame.alert
        out["alert"] = {"id": alert.id, "status": alert.status, "severity": alert.severity,
                        "simulated": alert.simulated, "exposure_summary": alert.exposure_summary.model_dump(mode="json"),
                        "first_raised_at": alert.first_raised_at.isoformat()}
    return out


async def store_event_evidence(run: RunContext, frame: EventFrame) -> tuple[str, str | None]:
    """``(event evidence id, news evidence id | None)``. The event row is a ``fact`` for a
    grounded event (an ``inference`` row marked ``untrusted_extraction`` otherwise); the news row
    (``untrusted_extraction``) holds the structured fields of the news members."""
    grounded = frame.grounded
    args: dict[str, Any] = {"event_id": frame.event.id, "alert_id": frame.alert.id if frame.alert else None}
    if not grounded:
        args["content_trust"] = CONTENT_TRUST_EXTRACTION
    event_id = await run.deps.repo.save_evidence(
        tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector=SIGNAL_HUB_CONNECTOR,
        tool=SIGNAL_EVENT_TOOL, args=args, result=event_record(frame), prov="fact" if grounded else "inference",
    )
    news = frame.news_signals
    if not news:
        return event_id, None
    news_id = await run.deps.repo.save_evidence(
        tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector=SIGNAL_HUB_CONNECTOR,
        tool=SIGNAL_NEWS_TOOL,
        args={"event_id": frame.event.id, "signal_ids": [s.id for s in news], "content_trust": CONTENT_TRUST_EXTRACTION},
        result={"items": [_signal_record(s) for s in news]}, prov="inference",
    )
    return event_id, news_id


# ───────────────────────────── the node path ─────────────────────────────

UNAVAILABLE_MESSAGE: Final = (
    "I could not read this alert from Signal Hub, so I did not frame the case from it. Nothing was changed; "
    "open the alert again in a moment."
)


async def frame_signal_case(ctx: Context, run: RunContext, data: Mapping[str, Any]) -> AsyncGenerator[Any]:
    """``frame_case`` for ``{signal: {event_id, alert_id}, edits?, removed_checks?, edit_errors?}``:
    read the event, store its evidence, frame the scenario, show the "Opened from alert" card.
    Output ``{scenario_version, problem_evidence_id, mode, signal}`` (+ ``removed_checks``)."""
    ref = data["signal"]
    event_id = str(ref.get("event_id") or "")
    alert_id = str(ref["alert_id"]) if ref.get("alert_id") else None
    ctx.state[SIGNAL_STATE_KEY] = {"event_id": event_id, "alert_id": alert_id}
    for problem in data.get("edit_errors") or []:
        log.info("approach edit refused", problem=str(problem)[:200])
    if data.get("edit_errors"):
        yield progress_event("Your edit did not fit the card, so I kept the previous values", prov="decision")
    overrides = overrides_from(data.get("edits"))
    try:
        frame = await load_event_frame(run, event_id=event_id, alert_id=alert_id)
    except SignalHubUnavailableError:
        yield await say(run, "signal_unavailable", UNAVAILABLE_MESSAGE)
        raise
    evidence_id, news_id = await store_event_evidence(run, frame)
    template_label = get_taxonomy().template(taxonomy_key_for(frame.event.subtype)).label.lower()
    yield progress_event(
        f"Read the {_severity(frame)} severity {template_label} alert and its exposure from Signal Hub",
        prov="fact" if frame.grounded else "inference", evidence_refs=[e for e in (evidence_id, news_id) if e],
    )
    info, planning_date, bundle, directory = await frame_problem_bundle(run)
    connector = run.spec.connector
    async with get_gateway().session(tenant=run.tenant, connector=connector, principal="reader") as session:
        caller = _RecordingCaller(session, run, connector)
        if bundle is None:
            bundle = await gather_problem_data(
                run.tenant, session, lanes=info.lanes, as_of=planning_date,
                horizon_weeks=run.spec.planning.horizon_weeks, record=caller.record,
            )
        scenario = await build_event_scenario(
            run, caller, frame=frame, overrides=overrides, po_lines=bundle.po_lines,
            scenario_id=f"scn-{run.run_id}-{ctx.run_id}",
            snapshot_data=bundle.data if bundle.source == "scn" else None, problem=bundle.data,
            directory=directory, data_source=data_source_assumption(bundle, run.spec.vendor_name),
            evidence_id=evidence_id, news_evidence_id=news_id,
        )
    repo = run.deps.repo
    version = await repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario)
    problem_evidence_id = await _persist_problem(run, bundle, connector=connector, scenario_version=version,
                                                 planning_date=planning_date)
    if scenario.mode != run.case_mode:
        await repo.set_case_mode(tenant=run.tenant, case_id=run.case_id, mode=scenario.mode)
        run.case_mode = scenario.mode
        yield case_event(mode=scenario.mode, status="open", title=run.case_title)
    _set_problem_state(ctx, bundle, problem_evidence_id, [*caller.evidence_ids, evidence_id, *([news_id] if news_id else [])])
    if frame.alert is not None:
        props = AlertOriginProps(
            alert_id=frame.alert.id, title=frame.alert.title, severity=frame.alert.severity, kind=frame.event.kind,
            source_names=frame.source_names(), simulated=frame.simulated, opened_at=dt.datetime.now(dt.UTC),
        )
        yield await block_event(
            run, "alert_origin", props, title="Opened from alert",
            prov=Provenance.FACT if frame.grounded else Provenance.INFERENCE, engine=ENGINE_FRAME,
            key=("alert_origin", frame.alert.id),
        )
    counts = bundle.counts
    origin = f"the {run.spec.vendor_name} snapshot" if bundle.source == "scn" else run.spec.vendor_name
    verb = "Loaded" if bundle.source == "scn" else "Read"
    yield progress_event(
        f"{verb} {counts.get('purchase_orders', 0)} open purchase orders, {counts.get('inventory_positions', 0)} "
        f"inventory positions and {counts.get('forecast_rows', 0)} forecast rows from {origin}",
        evidence_refs=(caller.evidence_ids + bundle_ids(bundle))[:20],
    )
    out: dict[str, Any] = {"scenario_version": version, "problem_evidence_id": problem_evidence_id,
                           "mode": scenario.mode, "signal": {"event_id": event_id, "alert_id": alert_id}}
    if data.get("removed_checks"):
        out["removed_checks"] = [str(c) for c in data["removed_checks"]]
    yield out
