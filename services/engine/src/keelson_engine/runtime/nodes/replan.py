"""Signal-driven targeted re-plan (``docs/contracts/95-m5-remediation.md`` §4) — no LLM.

A ``signal`` run (``{signal_id}``, origin webhook) starts at ``pre_route`` → ``SIGNAL`` →
:func:`prepare_replan`, which is fully deterministic:

1. reads the stored signal (``SignalsPort.get_signal``); only typed events are handled
   (:data:`REPLAN_SIGNALS`: ``lane.capacity_changed {lane_code, factor_bp, weeks?, start_date?}``;
   structured fields are facts, any free text is ignored);
2. checks that the case's plan depends on the lane (``plan.kpis.dependencies`` / committed
   change-set lines); otherwise the run ends with a short note;
3. adds the perturbation ``capacity_scale`` on the lane to the scenario as ``fact`` (scenario
   v+1; an earlier signal on the same lane is replaced, never stacked);
4. waits (bounded, ``remediation.replan_resync_timeout_s``) for a fresh snapshot, else uses the
   current one — the problem data is the **current (post-apply) state**. When the resync
   finished, the snapshot already carries the new capacity, so the perturbation is not applied a
   second time in the optimizer request;
5. re-simulates only the affected options: the committed option when changes were applied
   (applied decisions stay fixed: they are part of the post-apply state), else the open plan's
   options that use the lane;
6. committed plans: the **delta change set** (:func:`~keelson_engine.runtime.remediation.delta_actions`)
   — the re-simulated lines not applied yet plus compensations of committed lines on the cut lane
   (``capacity_booking.cancel`` + smaller re-booking, ``po_line.reschedule`` of withdrawn sea-air
   splits) — becomes plan v+1's option and goes straight to ``compile_changeset`` (``COMPILE``;
   approval per policy, never auto: origin webhook);
7. emits the "New information: …" banner (``narrative@1.banner`` + ``plan_versions`` + "What
   changed") and the delta ``plan_diff``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from typing import Any, Final

import structlog
from google.adk.agents.context import Context
from keelson_contracts.blocks import NarrativeProps, PlanVersionLink, ReplanBanner
from keelson_contracts.common import Provenance
from keelson_contracts.optimizer import OptimizeRequest, PlanSpec, ProblemData
from keelson_contracts.scenario import CapacityScale, Selector, Window

from ..candidates import DO_NOTHING_ID, capability_table, lever_specs
from ..context import RunContext, current_run
from ..errors import McpToolError, McpTransportError
from ..mcp_client import get_gateway
from ..optimizer_client import get_optimizer
from ..remediation import applied_items, delta_actions, lane_label, plan_dependencies, remediation_mix
from .common import ENGINE_COMPOSE, as_of, block_event, input_dict, progress_event, routed, say
from .framing import ROUTE_DONE, SNAPSHOT_ERRORS, SNAPSHOT_STATE_KEY, load_case_bundle, load_problem, resimulate_payload
from .planning import (
    default_recommendation,
    option_from,
    plan_diff_props,
    plan_summary,
    solver_options,
    what_changed_line,
)

__all__ = ["REPLAN_SIGNALS", "ROUTE_COMPILE", "LaneSignal", "parse_lane_signal", "prepare_replan", "scale_lane"]

log = structlog.get_logger(__name__)

ROUTE_COMPILE: Final = "COMPILE"
REPLAN_SIGNALS: Final = frozenset({"lane.capacity_changed"})
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9#_.>-]{0,63}$")
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


class LaneSignal:
    """The typed fields of a ``lane.capacity_changed`` event."""

    def __init__(self, *, signal_id: str, lane_code: str, factor_bp: int, start: dt.date, end: dt.date | None,
                 overbooked: Mapping[str, int] | None = None, aliases: Sequence[str] = ()) -> None:
        self.signal_id = signal_id
        self.lane_code = lane_code
        """The lane as the plan knows it (perturbation target, dependency checks); starts as the
        event's lane code and may become one of :attr:`aliases` (:meth:`resolve`)."""
        self.event_lane_code = lane_code
        """The lane code the event carries (what people see in the connector)."""
        self.aliases: tuple[str, ...] = tuple(a for a in dict.fromkeys(aliases) if a != lane_code)
        """Other names of the same lane in plans: the router's ``plan_lane_code`` and the transfer
        pseudo-lane ``ORIGIN>DEST`` of the event's typed ``origin``/``dest``."""
        self.factor_bp = factor_bp
        self.start = start
        self.end = end
        self.overbooked: dict[str, int] = dict(overbooked or {})
        """ISO week → units the lane is over-booked by after the cut (all shippers' bookings,
        Merxa's ``weeks_detail[].overbooked_units``; a fact). Empty when the event has none."""

    @property
    def label(self) -> str:
        """The lane for people: the event's code, plus the plan's DC pair when the plan knows the
        lane by it (``DE1-NL1-RD (DE1 → NL1)``)."""
        if self.lane_code == self.event_lane_code:
            return self.lane_code
        if ">" in self.lane_code:
            a, _, b = self.lane_code.partition(">")
            return f"{self.event_lane_code} ({a} → {b})"
        return f"{self.event_lane_code} ({self.lane_code})"

    def resolve(self, depends: Callable[[str], bool]) -> bool:
        """Re-target the signal at the first of the event's lane code and its :attr:`aliases`
        the plan depends on (``False``, unchanged, when it depends on none)."""
        for candidate in (self.event_lane_code, *self.aliases):
            if depends(candidate):
                self.lane_code = candidate
                return True
        return False

    @property
    def percent_text(self) -> str:
        pct = self.factor_bp / 100
        return f"{int(pct)}%" if self.factor_bp % 100 == 0 else f"{pct:.1f}%"


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def parse_lane_signal(signal: Mapping[str, Any], *, today: dt.date) -> LaneSignal | None:
    """``None`` unless the event is a well-formed ``lane.capacity_changed`` (fields are facts)."""
    if str(signal.get("type") or "") not in REPLAN_SIGNALS:
        return None
    data = signal.get("data") if isinstance(signal.get("data"), Mapping) else {}
    lane = data.get("lane_code")
    factor = data.get("factor_bp")
    if not isinstance(lane, str) or not _CODE_RE.match(lane) or isinstance(factor, bool) or not isinstance(factor, int):
        return None
    if not 0 <= factor <= 100_000:
        return None
    start = _date(data.get("start_date") or data.get("valid_from")) or today
    weeks = data.get("weeks")
    end: dt.date | None = None
    first_week, last_week = data.get("from_week"), data.get("to_week")
    if isinstance(first_week, str) and _WEEK_RE.match(first_week) and not (
            data.get("start_date") or data.get("valid_from")):
        # Merxa's window: from_week (its sim week by default) .. to_week, both inclusive
        start = _week_start(first_week) or start
        if isinstance(last_week, str) and _WEEK_RE.match(last_week) and _week_start(last_week):
            end = _week_start(last_week) + dt.timedelta(days=6)  # type: ignore[operator]
        elif isinstance(weeks, int) and not isinstance(weeks, bool) and weeks > 0:
            end = start + dt.timedelta(days=7 * weeks - 1)
    elif isinstance(weeks, int) and not isinstance(weeks, bool) and weeks > 0:
        end = start + dt.timedelta(days=7 * weeks - 1)
    elif isinstance(weeks, list) and weeks:
        parsed = sorted(w for w in (str(x) for x in weeks) if _WEEK_RE.match(w))
        if parsed:
            first, last = parsed[0], parsed[-1]
            start = dt.date.fromisocalendar(int(first[:4]), int(first[6:8]), 1)
            end = dt.date.fromisocalendar(int(last[:4]), int(last[6:8]), 7)
    aliases: list[str] = []
    plan_lane = data.get("plan_lane_code")
    if isinstance(plan_lane, str) and _CODE_RE.match(plan_lane):
        aliases.append(plan_lane)
    origin, dest = data.get("origin"), data.get("dest")
    if (isinstance(origin, str) and isinstance(dest, str) and _CODE_RE.match(origin) and _CODE_RE.match(dest)
            and ">" not in origin + dest and origin != dest):
        aliases.append(f"{origin}>{dest}")
    return LaneSignal(signal_id=str(signal.get("id") or ""), lane_code=lane, factor_bp=int(factor), start=start, end=end,
                      overbooked=_overbooked(data.get("weeks_detail")), aliases=aliases)


def _week_start(week: str) -> dt.date | None:
    try:
        return dt.date.fromisocalendar(int(week[:4]), int(week[6:8]), 1)
    except ValueError:
        return None


def _overbooked(detail: Any) -> dict[str, int]:
    """``weeks_detail[]`` → ``{iso_week: overbooked_units}`` (well-formed rows only, > 0)."""
    out: dict[str, int] = {}
    if not isinstance(detail, list):
        return out
    for row in detail[:60]:
        if not isinstance(row, Mapping):
            continue
        week, units = row.get("iso_week"), row.get("overbooked_units")
        if (isinstance(week, str) and _WEEK_RE.match(week) and isinstance(units, int)
                and not isinstance(units, bool) and units > 0):
            out[week] = units
    return out


def scale_lane(scenario: Any, signal: LaneSignal) -> Any:
    """Scenario copy with ``capacity_scale`` on the lane as ``fact`` (earlier facts on the lane replaced)."""
    kept = [
        p for p in scenario.perturbations
        if not (p.op == "capacity_scale" and p.prov == Provenance.FACT and list(p.target.ids) == [signal.lane_code])
    ]
    perturbation = CapacityScale(
        op="capacity_scale", target=Selector(kinds=["lane"], ids=[signal.lane_code]), prov=Provenance.FACT,
        factor_bp=signal.factor_bp, window=Window(start=signal.start, end=signal.end),
    )
    return scenario.model_copy(update={"perturbations": [*kept, perturbation]})


def _lane_capacity(problem: ProblemData, lane: str, factor_bp: int, *, fresh: bool) -> tuple[int | None, str | None]:
    """``(units per week after the cut, mode)`` of the lane in the problem data."""
    inbound = next((il for il in problem.inbound_lanes if il.code == lane), None)
    if inbound is not None:
        base = int(inbound.weekly_capacity_units)
        return (base if fresh else base * factor_bp // 10_000), str(inbound.mode)
    dc_lane = next((ln for ln in problem.lanes if ln.code == lane), None)
    if dc_lane is not None:
        base = int(dc_lane.max_units_wk)
        return (base if fresh else base * factor_bp // 10_000), "road"
    return None, None


async def _resync(run: RunContext) -> bool:
    """Bounded wait for a fresh snapshot of the planning connector (``True`` = synced now)."""
    timeout = run.spec.remediation.replan_resync_timeout_s
    if run.deps.scn is None or timeout <= 0:
        return False
    try:
        await asyncio.wait_for(
            run.deps.scn.sync_connector(tenant=run.tenant, connector=run.spec.connector, reason="signal"), timeout
        )
        return True
    except (TimeoutError, *SNAPSHOT_ERRORS) as exc:
        log.info("re-plan uses the current snapshot", reason=type(exc).__name__)
        return False
    except Exception as exc:  # a sync failure never blocks the re-plan
        log.warning("re-plan resync failed", error=type(exc).__name__)
        return False


def _depends(plan: Mapping[str, Any], applied: Sequence[Mapping[str, Any]], lane: str,
             problem: ProblemData | None) -> bool:
    """A committed plan depends on the lane through its committed option / lines; an open plan
    through any of its options (the stored fingerprint, else computed from the options)."""
    stored = (plan.get("kpis") or {}).get("dependencies") or {}
    computed = plan_dependencies(plan.get("options") or [], problem, committed=applied)
    if applied:
        committed_ids = {str(i.get("option_id")) for i in applied if i.get("option_id")}
        lanes = set(computed["committed_lanes"]) | set(stored.get("committed_lanes") or [])
        for source in (stored.get("by_option") or {}, computed["by_option"]):
            for oid in committed_ids:
                lanes |= set((source.get(oid) or {}).get("lanes") or [])
        return lane in lanes
    return lane in set(stored.get("lanes") or []) | set(computed["lanes"])


async def _plan_versions(run: RunContext, current: int, previous_status: str) -> list[PlanVersionLink]:
    rows: list[dict[str, Any]] = []
    if run.deps.plans is not None:
        try:
            rows = await run.deps.plans.list_plan_versions(tenant=run.tenant, case_id=run.case_id)
        except Exception as exc:  # cosmetic: fall back to the two versions we know
            log.debug("plan versions unavailable", error=type(exc).__name__)
    known = {int(r["version"]): str(r.get("status") or "proposed") for r in rows}
    known[current - 1] = previous_status
    known[current] = "proposed"
    allowed = {"proposed", "committed", "superseded", "rolled_back"}
    return [
        PlanVersionLink(version=v, label=f"Plan v{v}", status=s if s in allowed else "superseded",  # type: ignore[arg-type]
                        current=v == current)
        for v, s in sorted(known.items()) if v >= 1
    ]


async def prepare_replan(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """``SIGNAL`` → ``COMPILE`` (delta change set of a committed plan) | ``DONE``."""
    run = current_run()
    data = input_dict(node_input) if node_input is not None else {}
    signal_id = str(data.get("signal_id") or run.payload.get("signal_id") or "")
    repo = run.deps.repo
    signal = None
    if run.deps.signals is not None and signal_id:
        signal = await run.deps.signals.get_signal(tenant=run.tenant, signal_id=signal_id)
    parsed = parse_lane_signal(signal, today=as_of(run)) if signal else None
    if parsed is None:
        log.info("signal run without a re-plannable signal", signal_id=signal_id,
                 signal_type=(signal or {}).get("type"))
        yield routed({"replanned": False, "reason": "unsupported_signal"}, ROUTE_DONE)
        return
    parsed.signal_id = parsed.signal_id or signal_id
    plan = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
    record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if plan is None or record is None:
        yield routed({"replanned": False, "reason": "no_plan"}, ROUTE_DONE)
        return
    handled = ((plan.get("kpis") or {}).get("replan") or {})
    if parsed.signal_id and str(handled.get("signal_id") or "") == parsed.signal_id:
        # the same signal again (duplicate delivery, crash-retried run): never re-plan twice
        version_now = int(plan["version"])
        delta_option = next((o for o in plan.get("options") or []
                             if (o.get("replan") or {}).get("signal_id") == parsed.signal_id
                             and (o.get("replan") or {}).get("delta") and o.get("actions")), None)
        compiled = any(int(c.get("plan_version") or 0) == version_now
                       for c in await repo.list_change_sets(tenant=run.tenant, case_id=run.case_id))
        log.info("signal already re-planned", signal_id=parsed.signal_id, plan_version=version_now,
                 compiled=compiled)
        if delta_option is not None and not compiled:
            yield routed({"option_id": str(delta_option["id"]), "plan_version": version_now, "mode": "apply",
                          "origin": "webhook", "signal_id": parsed.signal_id}, ROUTE_COMPILE)
            return
        yield routed({"replanned": False, "reason": "already_replanned", "plan_version": version_now}, ROUTE_DONE)
        return
    applied = applied_items(await _change_sets(run))
    if not parsed.resolve(lambda lane: _depends(plan, applied, lane, None)):
        yield await say(run, "replan_unaffected",
                        f"New information: capacity on lane {parsed.label} changed to {parsed.percent_text}. "
                        "This case's plan does not use that lane, so nothing changes.")
        yield routed({"replanned": False, "reason": "unaffected"}, ROUTE_DONE)
        return
    scenario = scale_lane(record.scenario, parsed)
    version = await repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario)
    yield progress_event(f"New information: capacity on {parsed.label} is now {parsed.percent_text}",
                         prov="fact")
    fresh = await _resync(run)
    bundle = await load_case_bundle(run)  # the current (post-apply) snapshot
    payload = await resimulate_payload(ctx, run, version, bundle, mode=scenario.mode)
    problem = await load_problem(run, str(payload["problem_evidence_id"]))
    perturbations = [
        p for p in scenario.perturbations
        if not (fresh and p.op == "capacity_scale" and p.prov == Provenance.FACT and list(p.target.ids) == [parsed.lane_code])
    ]
    capacity, mode = _lane_capacity(problem, parsed.lane_code, parsed.factor_bp, fresh=fresh)
    label = lane_label(mode)
    planning = run.spec.planning
    solver = solver_options(planning)
    options = [dict(o) for o in plan.get("options") or []]
    committed_ids = list(dict.fromkeys(str(i.get("option_id")) for i in applied if i.get("option_id")))
    previous_version = int(plan["version"])
    changes = [f"{label.capitalize()} capacity on {parsed.label}: now {parsed.percent_text} of normal (fact)"]
    delta = None
    target_id: str | None = None
    by_option = ((plan.get("kpis") or {}).get("dependencies") or {}).get("by_option") or {}
    if applied:
        target_id = next((i for i in committed_ids if any(str(o["id"]) == i for o in options)), committed_ids[0] if committed_ids else None)
        affected = [o for o in options if str(o["id"]) == target_id] or [options[0]]
    else:
        affected = [o for o in options if str(o["id"]) != DO_NOTHING_ID
                    and parsed.lane_code in (by_option.get(str(o["id"])) or {}).get("lanes", [])] or \
                   [o for o in options if str(o["id"]) != DO_NOTHING_ID and o.get("actions")]
    new_options: dict[str, dict[str, Any]] = {}
    for option in affected:
        levers = [str(x) for x in option.get("levers") or []]
        spec = PlanSpec.model_validate({"id": str(option["id"]), "title": str(option.get("title") or option["id"]),
                                        "levers": lever_specs(levers, planning) if levers else []})
        resp = await get_optimizer().optimize(
            OptimizeRequest(data=problem, perturbations=perturbations, plan_spec=spec, solver=solver)
        )
        fresh_option = option_from(resp, str(option.get("title") or option["id"]), levers=levers, problem=problem)
        new_options[str(option["id"])] = fresh_option
    if applied and target_id is not None and target_id in new_options:
        po_etas = {pl.ref: pl.eta.isoformat() for pl in problem.po_lines}
        delta = delta_actions(new_options[target_id].get("actions") or [], applied, dirty_lane=parsed.lane_code,
                              lane_capacity=capacity, po_etas=po_etas, overbooked=parsed.overbooked)
        await _fill_versions(run, delta.actions, applied)
        replanned = dict(new_options[target_id])
        replanned["actions"] = delta.actions
        replanned["replan"] = {"signal_id": parsed.signal_id, "lane_code": parsed.lane_code,
                               "factor_bp": parsed.factor_bp, "base_version": previous_version, "delta": True,
                               "summary": delta.summary(), "notes": delta.notes}
        new_options[target_id] = replanned
        if delta.summary():
            changes.append("Changes to make: " + ", ".join(delta.summary()))
        else:
            changes.append("the applied changes still fit; nothing needs to change")
        changes += delta.notes
    table = await capability_table(run.deps, tenant=run.tenant, connectors=run.spec.write_connectors(),
                                   vendor=run.spec.vendor_name)
    for fresh_option in new_options.values():
        if fresh_option.get("actions"):
            fresh_option["remediation"] = remediation_mix(fresh_option["actions"], table)
    merged = [new_options.get(str(o["id"]), o) for o in options]
    recommended = target_id or default_recommendation(merged)
    new_version = previous_version + 1
    kpis = dict(plan.get("kpis") or {})
    kpis.update({
        "scenario_version": version, "snapshot_id": ctx.state.get(SNAPSHOT_STATE_KEY),
        "replan": {"signal_id": parsed.signal_id, "lane_code": parsed.lane_code, "factor_bp": parsed.factor_bp,
                   "previous_version": previous_version, "fresh_snapshot": fresh,
                   **({"event_lane_code": parsed.event_lane_code}
                      if parsed.event_lane_code != parsed.lane_code else {})},
    })
    kpis["dependencies"] = plan_dependencies(merged, problem, run.spec.write_connectors(), applied)
    plan_id = await repo.save_plan(tenant=run.tenant, case_id=run.case_id, version=new_version, options=merged,
                                   recommended_option_id=recommended, kpis=kpis)
    snapshot_id = ctx.state.get(SNAPSHOT_STATE_KEY)
    if snapshot_id and run.deps.scn is not None and plan_id is not None:
        try:
            await run.deps.scn.pin_snapshot(tenant=run.tenant, snapshot_id=str(snapshot_id), plan_id=int(plan_id))
        except SNAPSHOT_ERRORS as exc:
            log.warning("could not pin the re-plan's snapshot", error=type(exc).__name__)
    if run.deps.plans is not None:
        try:
            await run.deps.plans.save_plan_dependencies(tenant=run.tenant, case_id=run.case_id, plan_version=new_version,
                                                        dependencies=kpis["dependencies"])
        except Exception as exc:  # best effort; the plan row keeps the fingerprint
            log.warning("plan dependency index not updated", error=type(exc).__name__)
    previous_status = str(plan.get("status") or "proposed")
    if previous_status not in ("committed", "rolled_back"):
        previous_status = "superseded"
        await repo.mark_plan_status(tenant=run.tenant, case_id=run.case_id, version=previous_version, status="superseded")
    await run.deps.interrupts.supersede_open(tenant=run.tenant, case_id=run.case_id, reason=f"re-plan v{new_version}")
    current = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id) or {"version": new_version,
                                                                                 "options": merged,
                                                                                 "recommended_option_id": recommended,
                                                                                 "kpis": kpis}
    banner_text = (f"New information: {label} capacity on {parsed.label} cut to {parsed.percent_text} — "
                   f"plan updated (v{new_version})")
    what = what_changed_line(plan_summary(plan), plan_summary(current), changes)
    rec = next((o for o in merged if str(o["id"]) == recommended), None)
    if delta is not None:
        body = (f"**{banner_text}.**\n\nI re-planned only what depends on {parsed.label}, starting from what is "
                f"already applied. " + (f"To stay on track: {', '.join(delta.summary())}." if delta.summary()
                                        else "The applied changes still fit; nothing needs to change."))
    else:
        title = str((rec or {}).get("title") or recommended)
        body = (f"**{banner_text}.**\n\nI re-ran the options that use {parsed.label} on the latest data; "
                f"the recommendation is now {title}.")
    if delta is not None and delta.summary():
        body += " The changes need approval before anything is applied."
    yield await block_event(
        run, "narrative",
        NarrativeProps(
            markdown=body, what_changed=what,
            banner=ReplanBanner(text=banner_text, plan_version=new_version, previous_plan_version=previous_version,
                                signal_id=parsed.signal_id or None),
            plan_versions=await _plan_versions(run, new_version, previous_status),
        ),
        title="Plan updated", prov=Provenance.FACT, engine=ENGINE_COMPOSE, key=("narrative", "replan", new_version),
    )
    if rec is not None and rec.get("actions"):
        yield await block_event(
            run, "plan_diff",
            plan_diff_props(plan_version=new_version, actions=rec["actions"], vendor=run.spec.vendor_name, problem=problem),
            title=f"Changes in {run.spec.vendor_name}" + (" (delta)" if delta is not None else ""),
            prov=Provenance.SIMULATED, engine=ENGINE_COMPOSE, key=("plan_diff", new_version, recommended),
            placement="canvas",
        )
    yield await say(run, "replan", body.replace("**", ""))
    if delta is not None and delta.actions and target_id is not None:
        yield routed({"option_id": target_id, "plan_version": new_version, "mode": "apply", "origin": "webhook",
                      "signal_id": parsed.signal_id}, ROUTE_COMPILE)
        return
    yield routed({"replanned": True, "plan_version": new_version}, ROUTE_DONE)


_PO_LINE_OPS: Final = frozenset({"po_line.reschedule", "po_line.change_qty", "po_line.cancel", "po_line.split"})


async def _fill_versions(run: RunContext, actions: list[dict[str, Any]], applied: Sequence[Mapping[str, Any]]) -> None:
    """Delta PO-line lines carry no ``expected_version`` (the applied split already moved it):
    read the line's current version back (reader principal, ``get_purchase_order``). If that read
    is not possible, count the applied changes on the line on top of the version they expected
    (the target system's dry-run still rejects a stale version, so nothing wrong is applied)."""
    need = [a for a in actions if a.get("op") in _PO_LINE_OPS and a.get("expected_version") is None
            and "#" in str(a.get("target_ref") or "")]
    if not need:
        return
    versions: dict[str, int] = {}
    pos = sorted({str(a["target_ref"]).rpartition("#")[0] for a in need})
    try:
        async with get_gateway().session(tenant=run.tenant, connector=run.spec.connector, principal="reader") as s:
            for po in pos:
                try:
                    data = await s.call("get_purchase_order", {"code": po})
                except McpToolError as exc:  # one unreadable PO never hides the others
                    log.info("re-plan could not read a PO", po_code=po, error=type(exc).__name__)
                    continue
                for line in (data or {}).get("lines") or []:
                    v = line.get("version")
                    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                        versions[f"{po}#{line.get('line_no')}"] = v
    except (McpToolError, McpTransportError, OSError) as exc:
        log.warning("re-plan could not read PO-line versions", error=type(exc).__name__)
    for action in need:
        ref = str(action["target_ref"])
        if ref in versions:
            action["expected_version"] = versions[ref]
            continue
        prior = [i for i in applied if str(i.get("target_ref") or "") == ref and i.get("expected_version")]
        action["expected_version"] = (max(int(i["expected_version"]) for i in prior) + len(prior)) if prior else 1


async def _change_sets(run: RunContext) -> list[dict[str, Any]]:
    """Every change set of the case with its items and result (the listing has no items)."""
    rows = await run.deps.repo.list_change_sets(tenant=run.tenant, case_id=run.case_id)
    out = []
    for row in rows:
        if str(row.get("status") or "") not in ("applied", "verified"):
            continue
        full = await run.deps.repo.get_change_set(tenant=run.tenant, change_set_id=str(row["id"]))
        if full is not None:
            out.append(full)
    return out
