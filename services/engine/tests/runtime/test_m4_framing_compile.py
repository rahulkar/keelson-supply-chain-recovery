"""Generic template compiler (``90`` §1, §8): every taxonomy event type compiles to the right
perturbations, mentions resolve deterministically (chokepoint aliases, UN/LOCODE / port names,
regions), provenance follows the rules (user-stated → decision, priors → assumption, resolved →
fact) and the Act 2 Suez what-if compiles on a full-shaped snapshot."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from keelson_contracts.common import Provenance
from keelson_contracts.llm import DisruptionExtract, EntityMention, IntakeResult
from keelson_contracts.optimizer import ImpactRequest
from keelson_contracts.runs import ApproachEdit
from keelson_contracts.scenario import CapacityScale, DemandScale, LeadTimeShift
from test_m4_framing_support import AS_OF, FakeCaller, full_problem, po_line_dicts, run_ctx

from keelson_engine.runtime.geo import get_geo
from keelson_engine.runtime.nodes.framing import (
    Stated,
    build_scenario,
    coerce_legacy_intake,
    editable_values,
    lane_effects,
    overrides_from,
    parse_edits,
    place_index,
    resolve_static,
    retarget_scenario,
)
from keelson_engine.runtime.nodes.taxonomy import get_taxonomy

PROBLEM = full_problem()


def extract(event_type: str, mentions: list[tuple[str, str]], **kw: Any) -> DisruptionExtract:
    family = event_type.split(".", 1)[0]
    data: dict[str, Any] = {
        "event_family": family, "event_type": event_type, "description": f"{event_type} test",
        "entity_mentions": [{"type": t, "text": x} for t, x in mentions],
        "delay_days": None, "demand_change_pct": None, "start_date": None, "duration_days": None,
        "is_hypothetical": False,
    }
    data.update(kw)
    return DisruptionExtract.model_validate(data)


async def compile_(ext: DisruptionExtract, *, overrides: dict[str, Any] | None = None, snapshot: bool = True,
                   case_mode: str = "actual", caller: FakeCaller | None = None) -> Any:
    run = run_ctx(case_mode=case_mode)
    return await build_scenario(
        run, caller or FakeCaller(), extract=ext, overrides=overrides or {}, po_lines=po_line_dicts(PROBLEM),
        all_clusters=[c.code for c in PROBLEM.clusters], scenario_id="scn-t",
        snapshot_data=PROBLEM if snapshot else None, problem=PROBLEM,
    )


def assumption(scenario: Any, key: str) -> Any:
    return next(a for a in scenario.assumptions if a.key == key)


# ─────────────────────────── every event type ───────────────────────────


async def test_sup_delay_walks_the_bom_to_receipts_with_the_prior_band() -> None:
    s = await compile_(extract("SUP.delay", [("supplier", "Saigon Knit")]))
    [p] = s.perturbations
    assert isinstance(p, LeadTimeShift)
    assert p.target.kinds == ["receipt"] and set(p.target.ids) == {pl.ref for pl in PROBLEM.po_lines if pl.po_code.startswith("PO-VN")}
    assert (p.days, p.p10, p.p90, p.prov) == (7, 5, 14, Provenance.ASSUMPTION) and p.window is None
    assert assumption(s, "delay_days").prov == Provenance.ASSUMPTION and assumption(s, "delay_days").value_text == "7 days"
    assert assumption(s, "affected_receipts").prov == Provenance.FACT
    assert {m.code for m in s.matched} >= {"SUP-VN-01", "PO-VN-0101", "PO-VN-0102"}
    assert (s.event_family, s.event_type, s.mode) == ("SUP", "SUP.delay", "actual")
    assert {a.key for a in s.assumptions} == {"delay_days", "affected_receipts"}  # grounded except the prior


async def test_sup_delay_stated_value_is_a_decision_without_band() -> None:
    s = await compile_(extract("SUP.delay", [("supplier", "TRIM-BD-01")], delay_days=10))
    [p] = s.perturbations
    assert (p.days, p.p10, p.p90, p.prov) == (10, None, None, Provenance.DECISION)
    assert assumption(s, "delay_days").prov == Provenance.DECISION
    assert {r.split("#")[0] for r in p.target.ids} == {"PO-BD-0001", "PO-BD-0002"}  # puffer BOM (any factory)
    assert not s.unconfirmed_assumptions


async def test_sup_delay_without_snapshot_uses_the_connector_bom() -> None:
    caller = FakeCaller(bom_lines={"TRIM-BD-01": ["PO-BD-0001#1", "PO-BD-0001#2", "PO-XX-9#1"]})
    s = await compile_(extract("SUP.delay", [("supplier", "Meghna")], delay_days=7), snapshot=False, caller=caller)
    [p] = s.perturbations
    assert p.target.ids == ["PO-BD-0001#1", "PO-BD-0001#2"]  # only open lines
    assert ("get_bom_where_used", {"supplier_code": "TRIM-BD-01"}) in caller.calls


async def test_sup_delay_with_nothing_matched_is_an_inference_and_no_perturbation() -> None:
    s = await compile_(extract("SUP.delay", [("supplier", "Nobody Ltd")]))
    assert s.perturbations == []
    assert assumption(s, "affected_receipts").prov == Provenance.INFERENCE
    assert assumption(s, "unmatched_mentions").value_text == "Nobody Ltd"


async def test_sup_capacity_loss_scales_the_factory_for_the_prior_window() -> None:
    s = await compile_(extract("SUP.capacity_loss", [("factory", "F-VN-01")]))
    [p] = s.perturbations
    assert isinstance(p, CapacityScale)
    assert (p.target.kinds, p.target.ids) == (["factory"], ["F-VN-01"])
    assert (p.factor_bp, p.p10, p.p90, p.prov) == (5000, 2500, 7500, Provenance.ASSUMPTION)
    assert (p.window.start, p.window.end) == (AS_OF, AS_OF + dt.timedelta(days=13))
    assert assumption(s, "capacity_pct").value_text == "50% of normal capacity"
    assert assumption(s, "duration_days").prov == Provenance.ASSUMPTION


async def test_chokepoint_closure_compiles_to_zero_capacity_with_lane_facts() -> None:
    s = await compile_(extract("LOG.chokepoint_closure", [("chokepoint", "Suez Canal")], duration_days=21,
                               start_date="2026-10-12"))
    [p] = s.perturbations
    assert isinstance(p, CapacityScale)
    assert (p.target.kinds, p.target.ids, p.factor_bp, p.p10, p.p90) == (["chokepoint"], ["SUEZ"], 0, None, None)
    assert (p.window.start, p.window.end) == (dt.date(2026, 10, 12), dt.date(2026, 11, 1))
    assert p.prov == Provenance.DECISION  # closure is the event itself; duration + start were stated
    lanes = assumption(s, "affected_lanes")
    assert lanes.prov == Provenance.FACT
    assert "SGN-HAM-SEA+HAM-DE1-RD → SGN-HAM-CAPE+HAM-DE1-RD (+12 days)" in lanes.value_text
    assert "SGN-GDN-SEA+GDN-PL1-RD → SGN-GDN-CAPE+GDN-PL1-RD (+12 days)" in lanes.value_text
    assert "24 open PO lines" in lanes.value_text
    assert "capacity_pct" not in {a.key for a in s.assumptions}  # fixed value, not an assumption
    assert any(m.type == "chokepoint" and m.code == "SUEZ" and m.label == "Suez Canal" for m in s.matched)


@pytest.mark.parametrize(
    ("event_type", "mention", "kinds", "ids", "factor", "band", "days"),
    [
        ("LOG.port_closure", ("port", "Port of Hamburg"), ["port"], ["DEHAM"], 0, (None, None), 7),
        ("LOG.strike", ("port", "NLRTM"), ["port"], ["NLRTM"], 3000, (1000, 6000), 7),
        ("NAT.flood", ("port", "Chittagong"), ["port"], ["BDCGP"], 0, (0, 5000), 7),
    ],
)
async def test_port_events(event_type: str, mention: tuple[str, str], kinds: list[str], ids: list[str], factor: int,
                           band: tuple[int | None, int | None], days: int) -> None:
    s = await compile_(extract(event_type, [mention]))
    [p] = s.perturbations
    assert isinstance(p, CapacityScale)
    assert (p.target.kinds, p.target.ids, p.factor_bp, (p.p10, p.p90)) == (kinds, ids, factor, band)
    assert (p.window.end - p.window.start).days + 1 == days


async def test_storm_region_expands_to_the_countrys_ports_plus_named_factories() -> None:
    s = await compile_(extract("NAT.storm", [("region", "VN"), ("factory", "F-VN-01")]))
    [p] = s.perturbations
    assert set(p.target.kinds) == {"port", "factory"}
    assert set(p.target.ids) == {"VNSGN", "F-VN-01"}
    assert any(m.type == "port" and m.code == "VNSGN" for m in s.matched)
    assert any(m.type == "region" and m.code == "VN" for m in s.matched)


async def test_demand_spike_defaults_to_every_cluster() -> None:
    s = await compile_(extract("DEM.spike", []))
    [p] = s.perturbations
    assert isinstance(p, DemandScale)
    assert p.target.kinds == ["cluster"] and len(p.target.ids) == len(PROBLEM.clusters)
    assert (p.factor_bp, p.p10, p.p90, p.prov) == (11800, 11000, 13000, Provenance.ASSUMPTION)
    assert p.window is None  # no duration prior, none stated
    assert assumption(s, "demand_change_pct").value_text == "+18%"


async def test_demand_drop_on_a_style_with_a_stated_change_and_duration() -> None:
    s = await compile_(extract("DEM.drop", [("style", "KNT-03")], demand_change_pct=-25, duration_days=14))
    [p] = s.perturbations
    assert (p.target.kinds, p.target.ids) == (["style"], ["KNT-03"])
    assert (p.factor_bp, p.p10, p.p90) == (7500, None, None)
    assert p.window is not None and (p.window.end - p.window.start).days == 13
    assert p.prov == Provenance.ASSUMPTION  # the start date is a default (today)
    assert assumption(s, "start_date").prov == Provenance.ASSUMPTION


async def test_other_is_investigation_only() -> None:
    s = await compile_(extract("OTHER", [("supplier", "TRIM-BD-01")]))
    assert s.perturbations == []
    assert assumption(s, "event_type").prov == Provenance.INFERENCE
    assert s.event_family == "OTHER"


async def test_every_taxonomy_type_compiles_and_the_optimizer_accepts_the_perturbations() -> None:
    mentions = {"SUP": [("supplier", "Saigon Knit"), ("factory", "F-VN-01")], "LOG": [("chokepoint", "Suez"), ("port", "DEHAM")],
                "NAT": [("port", "BDCGP")], "DEM": [], "OTHER": []}
    for key, tpl in get_taxonomy().ordered():
        s = await compile_(extract(key, mentions[key.split(".")[0]]))
        assert len(s.perturbations) == len(tpl.perturbations), key
        assert [p.op for p in s.perturbations] == [t.op for t in tpl.perturbations], key
        ImpactRequest(data=PROBLEM, perturbations=list(s.perturbations))  # the optimizer contract shape


# ─────────────────────────── mode and provenance ───────────────────────────


async def test_hypothetical_or_what_if_case_compiles_a_what_if() -> None:
    s = await compile_(extract("SUP.delay", [("supplier", "TRIM-BD-01")], is_hypothetical=True))
    assert s.mode == "what_if"
    s2 = await compile_(extract("SUP.delay", [("supplier", "TRIM-BD-01")]), case_mode="what_if")
    assert s2.mode == "what_if"


# ─────────────────────────── mention resolution ───────────────────────────


@pytest.mark.parametrize(
    ("text", "code"),
    [("Suez", "SUEZ"), ("the Suez Canal", "SUEZ"), ("SUEZ", "SUEZ"), ("suez canal closure", "SUEZ"),
     ("Red Sea", "BAB_EL_MANDEB"), ("Bab-el-Mandeb", "BAB_EL_MANDEB"), ("Strait of Hormuz", "HORMUZ"),
     ("Bosphorus", "BOSPORUS"), ("Panama Canal", "PANAMA"), ("Cape of Good Hope", "CAPE"),
     ("strait of malacca", "MALACCA"), ("English Channel", "DOVER"), ("Gibraltar", "GIBRALTAR"),
     ("Atlantis", None), ("", None)],
)
def test_chokepoint_aliases(text: str, code: str | None) -> None:
    cp = get_geo().resolve_chokepoint(text)
    assert (cp.code if cp else None) == code


@pytest.mark.parametrize(
    ("mtype", "text", "expected"),
    [
        ("chokepoint", "Suez", ("chokepoint", "SUEZ")),
        ("region", "the Suez canal", ("chokepoint", "SUEZ")),
        ("port", "Suez", ("chokepoint", "SUEZ")),
        ("port", "DEHAM", ("port", "DEHAM")),
        ("port", "deham", ("port", "DEHAM")),
        ("port", "Port of Hamburg", ("port", "DEHAM")),
        ("port", "Gdansk", ("port", "PLGDN")),
        ("port", "Ho Chi Minh City", ("port", "VNSGN")),
        ("port", "Tanger", ("port", "MATNG")),
        ("other", "AEJEA", ("port", "AEJEA")),
        ("other", "Jebel Ali", None),  # a name alone is only a port when typed as one
        ("region", "VN", ("region", "VN")),
        ("region", "Narnia", None),
        ("lane", "SGN-HAM-SEA+HAM-DE1-RD", ("lane", "SGN-HAM-SEA+HAM-DE1-RD")),
        ("component", "cmp-zip-01", ("component", "CMP-ZIP-01")),
        ("chokepoint", "Atlantis", None),
    ],
)
def test_static_resolution(mtype: str, text: str, expected: tuple[str, str] | None) -> None:
    idx = place_index(PROBLEM, get_geo())
    ref = resolve_static(EntityMention(type=mtype, text=text), idx, get_geo())  # type: ignore[arg-type]
    assert ((ref.type, ref.code) if ref else None) == expected


async def test_static_mentions_never_call_the_connector() -> None:
    caller = FakeCaller()
    await compile_(extract("LOG.chokepoint_closure", [("chokepoint", "Suez"), ("region", "Red Sea"), ("port", "Hamburg")]),
                   caller=caller)
    assert caller.calls == []


# ─────────────────────────── edits and legacy intakes ───────────────────────────


def test_typed_and_free_text_edits() -> None:
    assert parse_edits("It is 10 days, not a week") == {"delay_days": 10}
    assert parse_edits("2 weeks and +20%") == {"delay_days": 14, "demand_change_pct": 20}
    assert overrides_from("make it 3 weeks") == {"delay_days": 21}
    edits = [ApproachEdit(key="duration_days", value_int=28), {"key": "affected_receipts", "value_ids": ["PO-VN-0101#1"]}]
    assert overrides_from(edits) == {"duration_days": 28, "affected_receipts": ["PO-VN-0101#1"]}
    assert overrides_from(None) == {} and overrides_from("") == {}


async def test_a_free_text_weeks_edit_on_a_closure_is_the_duration() -> None:
    tpl = get_taxonomy().template("LOG.chokepoint_closure")
    stated = Stated.of(extract("LOG.chokepoint_closure", [("chokepoint", "Suez")]), overrides_from("4 weeks"), tpl)
    assert (stated.duration_days, stated.delay_days) == (28, None)
    s = await compile_(extract("LOG.chokepoint_closure", [("chokepoint", "Suez")]), overrides=overrides_from("4 weeks"))
    assert assumption(s, "duration_days").value_text == "28 days" and assumption(s, "duration_days").prov == Provenance.DECISION


async def test_typed_receipt_scope_edit_is_a_decision() -> None:
    s = await compile_(extract("SUP.delay", [("supplier", "Saigon Knit")], delay_days=5),
                       overrides={"affected_receipts": ["PO-VN-0101#1", "PO-VN-0101#2"]})
    [p] = s.perturbations
    assert p.target.ids == ["PO-VN-0101#1", "PO-VN-0101#2"]
    assert assumption(s, "affected_receipts").prov == Provenance.DECISION
    values = {e.key: e for e in editable_values(s)}
    assert values["delay_days"].value_int == 5 and values["affected_receipts"].value_ids == ["PO-VN-0101#1", "PO-VN-0101#2"]


@pytest.mark.parametrize(
    ("family", "event_type", "delay", "pct", "mapped"),
    [("SUP", "trim_delay", None, None, "SUP.delay"), ("LOG", "port congestion", 4, None, "SUP.delay"),
     ("DEM", "heatwave", None, -12, "DEM.drop"), ("DEM", "cold snap", None, 15, "DEM.spike"),
     ("NAT", "earthquake", None, None, "OTHER"), ("SUP", "SUP.capacity_loss", None, None, "SUP.capacity_loss")],
)
def test_legacy_intake_event_types_map_onto_the_taxonomy(family: str, event_type: str, delay: int | None,
                                                         pct: int | None, mapped: str) -> None:
    raw = {"kind": "NEW_DISRUPTION", "summary": "s", "disruption": {
        "event_family": family, "event_type": event_type, "description": "d", "entity_mentions": [],
        "delay_days": delay, "demand_change_pct": pct, "start_date": None, "duration_days": None,
        "is_hypothetical": False}}
    fixed = coerce_legacy_intake(raw)
    intake = IntakeResult.model_validate(fixed)
    assert intake.disruption is not None and intake.disruption.event_type == mapped
    assert intake.disruption.event_family == mapped.split(".")[0]


# ─────────────────────────── Act 2: Suez what-if ───────────────────────────


async def test_suez_what_if_on_a_full_shaped_snapshot() -> None:
    ext = extract("LOG.chokepoint_closure", [("chokepoint", "Suez Canal")], duration_days=21, is_hypothetical=True,
                  description="What if the Suez Canal closes for 3 weeks?")
    s = await compile_(ext)
    assert (s.mode, s.event_family, s.event_type) == ("what_if", "LOG", "LOG.chokepoint_closure")
    [p] = s.perturbations
    assert (p.op, p.factor_bp, p.target.ids) == ("capacity_scale", 0, ["SUEZ"])
    assert (p.window.start, p.window.end) == (AS_OF, AS_OF + dt.timedelta(days=20))
    assert assumption(s, "duration_days").prov == Provenance.DECISION
    assert assumption(s, "start_date").prov == Provenance.ASSUMPTION  # "from today" until the user says otherwise
    effects = lane_effects(PROBLEM, {"SUEZ"})
    assert [(e.lane, e.alt_lane, e.extra_days) for e in effects] == [
        ("SGN-GDN-SEA+GDN-PL1-RD", "SGN-GDN-CAPE+GDN-PL1-RD", 12),
        ("SGN-HAM-SEA+HAM-DE1-RD", "SGN-HAM-CAPE+HAM-DE1-RD", 12),
    ]
    assert {e.key for e in editable_values(s)} == {"duration_days"}
    # deterministic: the same input compiles to the same fingerprint
    again = await compile_(ext)
    assert again.fingerprint() == s.fingerprint()
    # the receipts riding the closed lanes are the VN knit lines into DE1 / PL1
    riding = {pl.ref for pl in PROBLEM.po_lines if pl.lane_code in {e.lane for e in effects}}
    assert {r.split("#")[0] for r in riding} == {"PO-VN-0101", "PO-VN-0102"}
    ImpactRequest(data=PROBLEM, perturbations=list(s.perturbations))


async def test_retarget_on_a_newer_snapshot_keeps_user_scope() -> None:
    s = await compile_(extract("SUP.delay", [("supplier", "Saigon Knit")], delay_days=7))
    newer = PROBLEM.model_copy(update={"po_lines": [pl for pl in PROBLEM.po_lines if pl.po_code != "PO-VN-0102"]})
    moved = retarget_scenario(s, newer, "Merxa")
    assert {r.split("#")[0] for r in moved.perturbations[0].target.ids} == {"PO-VN-0101"}
    assert assumption(moved, "affected_receipts").value_text.startswith("12 open lines on PO-VN-0101")
    user = await compile_(extract("SUP.delay", [("supplier", "Saigon Knit")], delay_days=7),
                          overrides={"affected_receipts": ["PO-VN-0102#1"]})
    assert retarget_scenario(user, newer, "Merxa").perturbations[0].target.ids == ["PO-VN-0102#1"]


async def test_a_typed_receipt_scope_keeps_only_open_lines() -> None:
    s = await compile_(extract("SUP.delay", [("supplier", "Saigon Knit")], delay_days=5),
                       overrides={"affected_receipts": ["PO-VN-0101#1", "PO-NOPE#1"]})
    assert s.perturbations[0].target.ids == ["PO-VN-0101#1"]
    s = await compile_(extract("SUP.delay", [("supplier", "Saigon Knit")], delay_days=5),
                       overrides={"affected_receipts": ["PO-NOPE#1"]})
    assert len(s.perturbations[0].target.ids) == 24  # nothing usable chosen: the walk decides
    assert assumption(s, "affected_receipts").prov == Provenance.FACT


# ─────────────── review regressions: chokepoints never swallow nearby places ───────────────


@pytest.mark.parametrize(("text", "code"), [("Cape Town", None), ("cape town port strike", None), ("CAPE", "CAPE"),
                                            ("Cape", "CAPE"), ("cape route", "CAPE")])
def test_a_chokepoint_code_only_matches_exactly(text: str, code: str | None) -> None:
    cp = get_geo().resolve_chokepoint(text)
    assert (cp.code if cp else None) == code


@pytest.mark.parametrize(
    ("mtype", "text", "not_expected"),
    [("port", "Port of Dover", ("chokepoint", "DOVER")), ("port", "Panama City port", ("chokepoint", "PANAMA")),
     ("port", "Gibraltar port", ("chokepoint", "GIBRALTAR")), ("region", "Cape Town", ("chokepoint", "CAPE"))],
)
def test_port_mentions_are_not_read_as_the_neighbouring_chokepoint(mtype: str, text: str,
                                                                   not_expected: tuple[str, str]) -> None:
    ref = resolve_static(EntityMention(type=mtype, text=text), place_index(PROBLEM, get_geo()), get_geo())  # type: ignore[arg-type]
    assert ref is None or (ref.type, ref.code) != not_expected
    # a port mention that *is* the chokepoint's name still resolves to it
    exact = resolve_static(EntityMention(type="port", text="Panama Canal"), place_index(PROBLEM, get_geo()), get_geo())
    assert exact is not None and (exact.type, exact.code) == ("chokepoint", "PANAMA")
