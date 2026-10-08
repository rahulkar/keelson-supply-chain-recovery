"""``refine_scenario`` (``docs/contracts/90-m4-investigation.md`` §5) and the deterministic scenario
value edits shared by framing, follow-ups and ``confirm_assumptions``.

Specialists may propose ``Findings.scenario_updates`` (``delay_days``, ``duration_days``,
``demand_change_pct``, ``add_receipts``, ``remove_receipts``), each citing ``evidence_id`` +
``json_pointer``. ``refine_scenario``:

1. drops malformed updates (:meth:`ScenarioUpdate.shape_error`) and updates whose evidence row does
   not exist;
2. classifies each update — **fact-backed** only when the cited evidence is a *trusted* read
   (``prov == "fact"``, not produced by an ``untrusted_text`` tool or a quarantine wrapper) **and**
   the value at the pointer supports the proposed value (an equal integer in a field named for the
   key, :data:`FIELD_STEMS`; every receipt id present for ``add_receipts``; never for
   ``remove_receipts``), and the row belongs to this case;
   anything else (supplier messages, KB chunks, a value the evidence does not show) is
   **inference-backed**;
3. applies fact-backed updates (provenance ``fact``) and applies inference-backed ones as
   ``Assumption(prov="inference", unconfirmed=True, source_evidence_ids=[…])`` — the
   recommended option then shows "Confirm before applying" and Apply stays disabled until the user
   confirms (case action ``confirm_assumptions`` → :func:`confirm_scenario_keys`);
4. resolves conflicts deterministically: per key a fact-backed update beats an inference-backed
   one, otherwise the first in findings order wins; the others are dropped as ``conflict``.

The scenario value edits (:func:`set_scenario_value`) rewrite the perturbations *and* the matching
assumption, so the scenario, its fingerprint and the approach card always agree.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from google.adk.agents.context import Context
from keelson_contracts.common import Provenance, factor_bp_from_pct_change, is_evidence_id
from keelson_contracts.llm import (
    SCENARIO_UPDATE_ID_KEYS,
    Findings,
    ScenarioUpdate,
)
from keelson_contracts.scenario import Assumption, DisruptionScenario, Window

from ..agents import fill_nullable
from ..context import RunContext, current_run
from ..jsonptr import JsonPointerError, resolve_pointer
from ..quarantine import is_untrusted_evidence
from .common import input_dict, progress_event

__all__ = [
    "ASSUMPTION_LABELS",
    "FIELD_STEMS",
    "FINDINGS_STATE_KEY",
    "KEY_OPS",
    "QUARANTINE_TOOL_SUFFIX",
    "RefineResult",
    "SCENARIO_VALUE_KEYS",
    "UPDATE_ASSUMPTION_KEY",
    "UpdateOutcome",
    "capacity_text",
    "confirm_scenario_keys",
    "date_text",
    "days_text",
    "evidence_is_trusted",
    "pct_text",
    "receipts_text",
    "refine",
    "refine_scenario",
    "scenario_value",
    "set_scenario_value",
    "untrusted_tools",
    "update_supported",
]

log = structlog.get_logger(__name__)

FINDINGS_STATE_KEY: Final = "findings"
"""Same key as ``planning.FINDINGS_STATE_KEY`` (findings of the current investigation)."""
QUARANTINE_TOOL_SUFFIX: Final = "_structured"
"""Quarantine wrapper tools (``read_supplier_messages_structured``, ``search_playbooks_structured``)
return extractions of untrusted text: their evidence is never fact-backing."""

SCENARIO_VALUE_KEYS: Final = (
    "delay_days", "duration_days", "demand_change_pct", "capacity_pct", "start_date", "affected_receipts",
)
"""Scenario values that edits, refine updates and follow-ups may change (assumption keys)."""
KEY_OPS: Final[Mapping[str, frozenset[str]]] = {
    "delay_days": frozenset({"lead_time_shift"}),
    "demand_change_pct": frozenset({"demand_scale"}),
    "capacity_pct": frozenset({"capacity_scale"}),
    "affected_receipts": frozenset({"lead_time_shift"}),
    "duration_days": frozenset({"capacity_scale", "demand_scale", "lead_time_shift"}),
    "start_date": frozenset({"capacity_scale", "demand_scale", "lead_time_shift"}),
}
"""Ops whose perturbations carry each assumption key's value (``duration_days`` / ``start_date``:
only perturbations that have a window)."""
ASSUMPTION_LABELS: Final[Mapping[str, str]] = {
    "delay_days": "Delay",
    "duration_days": "Duration",
    "demand_change_pct": "Demand change",
    "capacity_pct": "Capacity during the event",
    "start_date": "Starts",
    "affected_receipts": "Affected purchase-order lines",
}
UPDATE_ASSUMPTION_KEY: Final[Mapping[str, str]] = {
    "delay_days": "delay_days",
    "duration_days": "duration_days",
    "demand_change_pct": "demand_change_pct",
    "add_receipts": "affected_receipts",
    "remove_receipts": "affected_receipts",
}
"""ScenarioUpdate key → the assumption key it changes."""


# ─────────────────────────────── formatting ───────────────────────────────


def days_text(days: int) -> str:
    return f"{days} day" + ("" if abs(days) == 1 else "s")


def pct_text(pct: int) -> str:
    return f"{pct:+d}%"


def capacity_text(pct: int) -> str:
    return "Closed (0% of normal capacity)" if pct == 0 else f"{pct}% of normal capacity"


def receipts_text(ids: Sequence[str]) -> str:
    pos = sorted({r.split("#", 1)[0] for r in ids})
    return f"{len(ids)} open line" + ("" if len(ids) == 1 else "s") + f" on {', '.join(pos)}"


def date_text(day: dt.date) -> str:
    return f"{day.day} {day:%b %Y}"


# ─────────────────────────── scenario value edits ──────────────────────────


def _windowed(p: Any) -> bool:
    return getattr(p, "window", None) is not None


def _carries(p: Any, key: str) -> bool:
    if p.op not in KEY_OPS.get(key, ()):
        return False
    if key in ("duration_days", "start_date"):
        return _windowed(p)
    if key == "affected_receipts":
        return set(p.target.kinds) <= {"receipt", "po_line"}
    return True


def scenario_value(scenario: DisruptionScenario, key: str) -> int | list[str] | dt.date | None:
    """The scenario's current value for an assumption key (``None`` = not applicable)."""
    for p in scenario.perturbations:
        if not _carries(p, key):
            continue
        if key == "delay_days":
            return int(p.days)
        if key == "demand_change_pct":
            return (int(p.factor_bp) - 10000) // 100
        if key == "capacity_pct":
            return int(p.factor_bp) // 100
        if key == "affected_receipts":
            return list(p.target.ids)
        if key == "duration_days" and p.window.end is not None:
            return (p.window.end - p.window.start).days + 1
        if key == "start_date":
            return p.window.start
    return None


def _assumption_text(key: str, value: Any) -> str:
    if key in ("delay_days", "duration_days"):
        return days_text(int(value))
    if key == "demand_change_pct":
        return pct_text(int(value))
    if key == "capacity_pct":
        return capacity_text(int(value))
    if key == "start_date":
        return date_text(value)
    if key == "affected_receipts":
        return receipts_text(list(value))
    return str(value)


def _drop_band(data: dict[str, Any]) -> None:
    data.pop("p10", None)
    data.pop("p90", None)


def _replace_assumption(assumptions: list[Assumption], new: Assumption) -> list[Assumption]:
    out = [a for a in assumptions if a.key != new.key]
    index = next((i for i, a in enumerate(assumptions) if a.key == new.key), len(out))
    out.insert(min(index, len(out)), new)
    return out


def set_scenario_value(
    scenario: DisruptionScenario,
    key: str,
    *,
    value_int: int | None = None,
    value_ids: Sequence[str] | None = None,
    value_date: dt.date | None = None,
    mode: str = "set",
    prov: Provenance,
    unconfirmed: bool = False,
    source_evidence_ids: Sequence[str] = (),
    note: str | None = None,
) -> DisruptionScenario | None:
    """A copy of ``scenario`` with assumption ``key`` set (perturbations + the assumption).

    ``mode`` is ``set`` / ``add`` / ``remove`` (``add``/``remove`` only for ``affected_receipts``).
    Changed perturbations take ``prov`` and lose their p10/p90 band (the value is no longer a prior).
    Returns ``None`` when the scenario has no perturbation carrying ``key`` or the result is invalid
    (e.g. removing every affected receipt). ``note`` is appended to the assumption text
    ("… (was 7 days)", "… from a supplier message").
    """
    if key not in KEY_OPS:
        raise ValueError(f"unknown scenario value key {key!r}")
    perturbations: list[Any] = []
    final: Any = None
    for p in scenario.perturbations:
        if not _carries(p, key):
            perturbations.append(p)
            continue
        data = p.model_dump(mode="json", exclude_none=True)
        data["prov"] = prov.value
        if key == "delay_days":
            data["days"] = int(value_int)  # type: ignore[arg-type]
            _drop_band(data)
            final = int(value_int)  # type: ignore[arg-type]
        elif key == "demand_change_pct":
            data["factor_bp"] = max(0, factor_bp_from_pct_change(int(value_int)))  # type: ignore[arg-type]
            _drop_band(data)
            final = int(value_int)  # type: ignore[arg-type]
        elif key == "capacity_pct":
            data["factor_bp"] = max(0, 100 * int(value_int))  # type: ignore[arg-type]
            _drop_band(data)
            final = int(value_int)  # type: ignore[arg-type]
        elif key == "duration_days":
            start = dt.date.fromisoformat(data["window"]["start"])
            data["window"] = {"start": start.isoformat(),
                              "end": (start + dt.timedelta(days=int(value_int) - 1)).isoformat()}  # type: ignore[arg-type]
            final = int(value_int)  # type: ignore[arg-type]
        elif key == "start_date":
            win = Window.model_validate(data["window"])
            length = None if win.end is None else (win.end - win.start).days
            start = value_date or win.start
            data["window"] = {"start": start.isoformat(),
                              "end": None if length is None else (start + dt.timedelta(days=length)).isoformat()}
            final = start
        else:  # affected_receipts
            ids = list(data["target"]["ids"])
            given = list(value_ids or [])
            if mode == "add":
                ids = sorted(set(ids) | set(given))
            elif mode == "remove":
                ids = sorted(set(ids) - set(given))
            else:
                ids = sorted(set(given))
            if not ids:
                return None
            data["target"] = {"kinds": ["receipt"], "ids": ids}
            final = ids
        perturbations.append(data)
    if final is None:
        return None
    text = _assumption_text(key, final) + (f" ({note})" if note else "")
    assumption = Assumption(
        key=key, label=ASSUMPTION_LABELS[key], value_text=text, prov=prov, unconfirmed=unconfirmed,
        source_evidence_ids=list(dict.fromkeys(source_evidence_ids)),
    )
    payload = scenario.model_dump(mode="json")
    payload["perturbations"] = [p if isinstance(p, dict) else p.model_dump(mode="json", exclude_none=True)
                                for p in perturbations]
    payload["assumptions"] = [a.model_dump(mode="json") for a in _replace_assumption(list(scenario.assumptions), assumption)]
    try:
        return DisruptionScenario.model_validate(payload)
    except ValueError:
        log.warning("scenario value edit produced an invalid scenario", key=key)
        return None


def confirm_scenario_keys(scenario: DisruptionScenario, keys: Iterable[str]) -> DisruptionScenario:
    """User confirmation of assumptions ``keys`` (``confirm_assumptions`` / approach **Looks right**):
    the assumptions become ``decision`` (``unconfirmed`` cleared, via
    :meth:`DisruptionScenario.confirm_assumptions`, which re-validates) and so do the
    ``inference``/``assumption`` perturbations that carry those keys. Unknown keys are ignored."""
    known = {a.key for a in scenario.assumptions}
    wanted = [k for k in dict.fromkeys(keys) if k in known]
    if not wanted:
        return scenario
    confirmed = scenario.confirm_assumptions(wanted)
    soft = (Provenance.INFERENCE, Provenance.ASSUMPTION)
    perturbations = []
    for p in confirmed.perturbations:
        if p.prov in soft and any(_carries(p, k) for k in wanted if k in KEY_OPS):
            p = type(p).model_validate({**p.model_dump(mode="json", exclude_none=True), "prov": Provenance.DECISION.value})
        perturbations.append(p)
    return confirmed.model_copy(update={"perturbations": perturbations})


# ─────────────────────────────── trust checks ───────────────────────────────


async def untrusted_tools(run: RunContext, connectors: Iterable[str]) -> set[tuple[str, str]]:
    """``(connector, tool)`` pairs classified ``untrusted_text`` in the approved tool snapshots."""
    out: set[tuple[str, str]] = set()
    for connector in sorted(set(connectors)):
        if connector in ("keelson", ""):
            continue
        try:
            tools = await run.deps.connectors.list_approved_tools(tenant=run.tenant, connector=connector)
        except Exception as exc:  # an unknown connector is simply not trusted below
            log.warning("tool snapshot unavailable for the trust check", connector=connector, error=type(exc).__name__)
            out.add((connector, "*"))
            continue
        out |= {(connector, t.name) for t in tools if getattr(t, "content_trust", "trusted_data") != "trusted_data"}
    return out


def evidence_is_trusted(row: Mapping[str, Any], untrusted: set[tuple[str, str]]) -> bool:
    """A trusted read: ``prov == "fact"``, not from an ``untrusted_text`` tool (snapshot classification
    or a ``content_trust`` marker on the row / its args) and not a quarantine wrapper's output."""
    if str(row.get("prov") or "") != Provenance.FACT.value or is_untrusted_evidence(row):
        return False
    raw_args = row.get("args")
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}
    if "untrusted_text" in (row.get("content_trust"), args.get("content_trust")):
        return False
    connector, tool = str(row.get("connector") or ""), str(row.get("tool") or "")
    if tool.endswith(QUARANTINE_TOOL_SUFFIX):
        return False
    return (connector, tool) not in untrusted and (connector, "*") not in untrusted


def _ids_in(value: Any) -> set[str]:
    out: set[str] = set()

    def one(item: Any) -> None:
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, Mapping):
            if isinstance(item.get("ref"), str):
                out.add(item["ref"])
            elif item.get("po_code") is not None and item.get("line_no") is not None:
                out.add(f"{item['po_code']}#{item['line_no']}")

    if isinstance(value, list):
        for item in value:
            one(item)
    else:
        one(value)
    return out


FIELD_STEMS: Final[Mapping[str, tuple[str, ...]]] = {
    "delay_days": ("delay", "late", "slip"),
    "duration_days": ("duration", "closure", "closed", "outage"),
    "demand_change_pct": ("demand", "uplift", "pct", "percent"),
}
"""An integer update is supported only by a field whose name says what it measures: the last
non-index token of ``json_pointer`` must contain one of these stems. Otherwise any integer that
happens to be equal (a ``line_no``, a ``qty``) would turn a value proposed after reading
untrusted text into a ``fact``."""


def _field_name(pointer: str) -> str:
    """The last non-index reference token of an RFC 6901 pointer, unescaped and lower-cased."""
    for token in reversed(pointer.split("/")[1:]):
        if token and not token.isdigit():
            return token.replace("~1", "/").replace("~0", "~").lower()
    return ""


def update_supported(update: ScenarioUpdate, result: Any) -> bool:
    """The evidence value at ``json_pointer`` supports the update:

    * ``value_int`` keys: an equal integer in a field named for the key (:data:`FIELD_STEMS`);
    * ``add_receipts``: every ``value_ids`` entry among the receipt ids found there;
    * ``remove_receipts``: never — a read that *lists* a line does not show it is unaffected, so
      shrinking the exposure always needs the user's confirmation."""
    if update.key == "remove_receipts":
        return False
    try:
        value = resolve_pointer(result, update.json_pointer)
    except JsonPointerError:
        return False
    if update.key in SCENARIO_UPDATE_ID_KEYS:
        return bool(update.value_ids) and set(update.value_ids or []) <= _ids_in(value)
    name = _field_name(update.json_pointer)
    if not any(stem in name for stem in FIELD_STEMS.get(update.key, ())):
        return False
    return isinstance(value, int) and not isinstance(value, bool) and value == update.value_int


# ─────────────────────────────── refine core ───────────────────────────────


@dataclass(frozen=True)
class UpdateOutcome:
    task_id: str
    key: str
    evidence_id: str
    outcome: str
    """``fact`` (applied) · ``inference`` (applied, unconfirmed) · ``dropped``."""
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"task_id": self.task_id, "key": self.key, "evidence_id": self.evidence_id,
                "outcome": self.outcome, "reason": self.reason}


@dataclass
class RefineResult:
    scenario: DisruptionScenario
    outcomes: list[UpdateOutcome] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(o.outcome in ("fact", "inference") for o in self.outcomes)

    def of(self, outcome: str) -> list[UpdateOutcome]:
        return [o for o in self.outcomes if o.outcome == outcome]


def _findings(items: Iterable[Any]) -> list[Findings]:
    out: list[Findings] = []
    for item in items:
        try:
            out.append(Findings.model_validate(fill_nullable(Findings, item) if isinstance(item, Mapping) else item))
        except ValueError:
            log.warning("skipping malformed findings in refine")
    return out


def refine(
    scenario: DisruptionScenario,
    findings: Iterable[Any],
    evidence: Mapping[str, Mapping[str, Any]],
    untrusted: set[tuple[str, str]],
) -> RefineResult:
    """Pure core of ``refine_scenario`` (see the module docstring)."""
    candidates: list[tuple[str, ScenarioUpdate, str]] = []  # (task, update, "fact"|"inference")
    outcomes: list[UpdateOutcome] = []
    for f in _findings(findings):
        if f.status != "ok":
            continue
        for u in f.scenario_updates:
            if (problem := u.shape_error()) is not None:
                outcomes.append(UpdateOutcome(f.task_id, u.key, u.evidence_id, "dropped", f"malformed: {problem}"))
                continue
            row = evidence.get(u.evidence_id)
            if row is None or not is_evidence_id(u.evidence_id):
                outcomes.append(UpdateOutcome(f.task_id, u.key, u.evidence_id, "dropped", "unknown evidence"))
                continue
            backed = "fact" if evidence_is_trusted(row, untrusted) and update_supported(u, row.get("result")) else "inference"
            candidates.append((f.task_id, u, backed))
    # per scenario key: a fact-backed update beats inference; otherwise first wins
    winners: dict[str, tuple[str, ScenarioUpdate, str]] = {}
    for cand in candidates:
        key = cand[1].key
        held = winners.get(key)
        if held is None or (held[2] == "inference" and cand[2] == "fact"):
            if held is not None:
                outcomes.append(UpdateOutcome(held[0], key, held[1].evidence_id, "dropped", "conflict"))
            winners[key] = cand
        else:
            outcomes.append(UpdateOutcome(cand[0], key, cand[1].evidence_id, "dropped", "conflict"))
    current = scenario
    for key in sorted(winners, key=lambda k: [c[1].key for c in candidates].index(k)):
        task_id, u, backed = winners[key]
        target = UPDATE_ASSUMPTION_KEY[u.key]
        before = scenario_value(current, target)
        inferred = backed == "inference"
        note = None
        if inferred:
            was = _assumption_text(target, before) if before is not None and target != "affected_receipts" else None
            note = "inferred from the investigation" + (f"; was {was}" if was else "")
        updated = set_scenario_value(
            current, target,
            value_int=u.value_int, value_ids=u.value_ids,
            mode={"add_receipts": "add", "remove_receipts": "remove"}.get(u.key, "set"),
            prov=Provenance.INFERENCE if inferred else Provenance.FACT,
            unconfirmed=inferred, source_evidence_ids=[u.evidence_id], note=note,
        )
        if updated is None:
            outcomes.append(UpdateOutcome(task_id, u.key, u.evidence_id, "dropped", "not applicable to this scenario"))
            continue
        unchanged = before == scenario_value(updated, target)
        if unchanged and not inferred and _same_assumption(current, updated, target):
            outcomes.append(UpdateOutcome(task_id, u.key, u.evidence_id, "dropped", "no change"))
            continue
        if unchanged and inferred and _assumption_prov(current, target) in (Provenance.FACT, Provenance.DECISION):
            # an inference that only repeats a fact / the user's own statement changes nothing
            outcomes.append(UpdateOutcome(task_id, u.key, u.evidence_id, "dropped", "no change"))
            continue
        current = updated
        outcomes.append(UpdateOutcome(task_id, u.key, u.evidence_id, backed))
    return RefineResult(scenario=current, outcomes=outcomes)


def _assumption_prov(s: DisruptionScenario, key: str) -> Provenance | None:
    hit = next((x for x in s.assumptions if x.key == key), None)
    return None if hit is None else hit.prov


def _same_assumption(a: DisruptionScenario, b: DisruptionScenario, key: str) -> bool:
    def get(s: DisruptionScenario) -> Any:
        hit = next((x for x in s.assumptions if x.key == key), None)
        return None if hit is None else (hit.prov, hit.value_text.split(" (", 1)[0])

    return get(a) == get(b)


# ─────────────────────────────── the node ───────────────────────────────


async def refine_scenario(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """critic → **refine_scenario** → impact. Input: the critic's output (a dict; ``findings`` =
    Findings dumps, else ``ctx.state["findings"]``). Output: the input passed through plus
    ``scenario_version``, ``mode`` and ``refined {applied, pending, dropped}``."""
    run = current_run()
    data = input_dict(node_input) if node_input is not None else {}
    findings = list(data.get("findings") or ctx.state.get(FINDINGS_STATE_KEY) or [])
    repo = run.deps.repo
    record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        raise LookupError("no scenario saved for the case")
    cited = sorted({u.evidence_id for f in _findings(findings) for u in f.scenario_updates if is_evidence_id(u.evidence_id)})
    rows = await repo.get_evidence(tenant=run.tenant, ids=cited) if cited else []
    # only this case's evidence can back its scenario (another case's row is "unknown evidence")
    evidence = {str(r["id"]): r for r in rows if r.get("case_id") in (None, run.case_id)}
    untrusted = await untrusted_tools(run, (str(r.get("connector") or "") for r in rows))
    result = refine(record.scenario, findings, evidence, untrusted)
    version = record.version
    if result.changed and result.scenario != record.scenario:
        version = await repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=result.scenario)
    applied, pending, dropped = result.of("fact"), result.of("inference"), result.of("dropped")
    for o in dropped:
        log.info("scenario update dropped", task_id=o.task_id, key=o.key, reason=o.reason)
    if applied or pending:
        parts = []
        if applied:
            parts.append("updated " + ", ".join(sorted({ASSUMPTION_LABELS[UPDATE_ASSUMPTION_KEY[o.key]].lower() for o in applied}))
                         + " from the systems")
        if pending:
            parts.append("noted " + ", ".join(sorted({ASSUMPTION_LABELS[UPDATE_ASSUMPTION_KEY[o.key]].lower() for o in pending}))
                         + " from the investigation; please confirm before applying")
        yield progress_event("Refined the scenario: " + "; ".join(parts),
                             prov="inference" if pending else "fact",
                             evidence_refs=[o.evidence_id for o in applied + pending][:20])
    yield {
        **data,
        "findings": findings,
        "scenario_version": version,
        "mode": result.scenario.mode,
        "refined": {
            "applied": [o.as_dict() for o in applied],
            "pending": [o.as_dict() for o in pending],
            "dropped": [o.as_dict() for o in dropped],
        },
    }
