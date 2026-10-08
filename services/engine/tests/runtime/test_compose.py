"""Numbers rule: placeholder filling, unit formatting, numeric linter, repair turn, stripping."""

from __future__ import annotations

import datetime as dt

import pytest
from fakes.harness import Harness
from fakes.scripts import (
    intake_disruption,
    investigator_turns,
    m4_review_turns,
    recommendation,
    structurer_turn,
)

from keelson_engine.runtime.compose import (
    UNVERIFIED_NOTE,
    ComposeContext,
    KpiValue,
    allowed_tokens_from,
    check_text,
    fill_placeholders,
    format_value,
    numeric_lint,
    strip_unverified,
)
from keelson_engine.runtime.llm import FakeTurn


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (1240000, "eur_cents", "€12,400"),
        (-5050, "eur_cents", "-€51"),
        (3200, "units", "3,200 units"),
        (1, "units", "1 unit"),
        (7, "days", "7 days"),
        (1, "days", "1 day"),
        (1250, "pct_bp", "12.5%"),
        (1800, "pct_bp", "18%"),
        (42, "count", "42"),
        ("2026-11-02", "date", "2 Nov 2026"),
        ("2026-W45", "week", "W45 (w/c 2 Nov)"),
        (dt.date(2026, 10, 29), "date", "29 Oct 2026"),
    ],
)
def test_format_value(value: object, unit: str, expected: str) -> None:
    assert format_value(value, unit) == expected  # type: ignore[arg-type]


def test_format_rejects_floats() -> None:
    with pytest.raises(ValueError):
        format_value(1.5, "units")


def _ctx() -> ComposeContext:
    return ComposeContext(
        kpis={"opt-A": {"units_recovered": KpiValue(280, "units"), "net_benefit_cents": KpiValue(437500, "eur_cents")}},
        default_option="opt-A",
        evidence={"ev_abc": {"items": [{"atp": 270, "eta_dc": "2026-10-29", "code": "PO-BD-0001"}]}},
        calcs={"ev_calc1": 1800},
    )


def test_fill_placeholders_resolves_all_kinds() -> None:
    text, violations = fill_placeholders(
        "Recovers {{kpi:opt-A.units_recovered}} ({{kpi:net_benefit_cents}} net); UK1 has {{ev:ev_abc#/items/0/atp}} "
        "until {{ev:ev_abc#/items/0/eta_dc}}; total {{calc:ev_calc1}}.",
        _ctx(),
    )
    assert violations == []
    assert text == "Recovers 280 units (€4,375 net); UK1 has 270 until 29 Oct 2026; total 1,800."


def test_repeated_unit_after_placeholder_is_not_duplicated() -> None:
    # seen live: the model wrote "{{kpi:opt-A.units_recovered}} units"
    text, _ = fill_placeholders("Recovers {{kpi:opt-A.units_recovered}} units within {{kpi:opt-A.d}} days.", ComposeContext(
        kpis={"opt-A": {"units_recovered": KpiValue(280, "units"), "d": KpiValue(1, "days")}}))
    assert text == "Recovers 280 units within 1 day."


def test_unknown_placeholders_are_reported_and_kept() -> None:
    text, violations = fill_placeholders("X {{kpi:opt-B.units_recovered}} Y {{ev:ev_abc#/nope}} Z {{calc:ev_x}}", _ctx())
    assert len(violations) == 3
    assert "{{kpi:opt-B.units_recovered}}" in text


def test_numeric_lint_allows_codes_from_evidence() -> None:
    allowed = allowed_tokens_from([{"po": "PO-BD-0001#2", "dc": "UK1", "week": "2026-W45", "sku": "PUF-W-BLK-M"}])
    assert numeric_lint("PO-BD-0001 lands in UK1 in 2026-W45 (W45) for PUF-W-BLK-M.", allowed) == []
    assert numeric_lint("Ship 300 units by 2026-W46 for €12,400.", allowed) == [
        "free number '300'", "free number '2026-W46'", "free number '€12,400'"
    ]
    # numbers inside placeholders are fine; PO#line references allow the PO code
    assert numeric_lint("{{kpi:opt-A.units_recovered}} via PO-BD-0001#2", allowed) == []


def test_check_and_strip_unverified() -> None:
    ctx = _ctx()
    allowed = allowed_tokens_from([ctx.evidence])
    filled, violations = check_text("Moves {{kpi:opt-A.units_recovered}} today.", ctx, allowed)
    assert violations == [] and filled == "Moves 280 units today."
    text, dropped = strip_unverified(
        "Moves {{kpi:opt-A.units_recovered}} today. It costs 500 euros. PO-BD-0001 is late.", ctx, allowed
    )
    assert dropped == 1 and text == "Moves 280 units today. PO-BD-0001 is late."


async def _chat_with(h: Harness, rec: FakeTurn, repair: FakeTurn | None) -> str:
    h.script.add("intake", intake_disruption(delay_days=7))
    m4_review_turns(h.script)
    h.script.add("inventory_demand_investigator", *investigator_turns())
    h.script.add("inventory_demand_structurer", structurer_turn(h.core.repo))
    h.script.add("recommend", rec)
    if repair is not None:
        h.script.add("recommender_repair", repair)
    row = h.row("chat", {"text": "Meghna trims 7 days late"})
    assert await h.run(row) == "succeeded"
    [text] = [e["delta"] for e in h.events(row.id) if e["type"] == "TEXT_MESSAGE_CONTENT"]
    return text


async def test_free_number_triggers_one_repair_turn(harness: Harness) -> None:
    text = await _chat_with(harness, recommendation(free_number=True), recommendation())
    assert len(harness.script.requests["recommender_repair"]) == 1
    repair_input = harness.script.requests["recommender_repair"][0].contents[-1].parts[0].text
    assert "free number '300'" in repair_input
    assert "300 units" in text  # the KPI placeholder, filled deterministically
    assert UNVERIFIED_NOTE not in text and "{{" not in text


async def test_failed_repair_strips_and_marks_unverified(harness: Harness) -> None:
    text = await _chat_with(harness, recommendation(free_number=True), recommendation(free_number=True))
    assert "Moves 300 units from DE1" not in text
    assert UNVERIFIED_NOTE in text
    rec = harness.core.repo.blocks_of("recommended_option")[-1]["props"]
    assert all("300" not in b for b in rec["option"]["summary_bullets"])


async def test_clean_recommendation_needs_no_repair(harness: Harness) -> None:
    text = await _chat_with(harness, recommendation(), None)
    assert "recommender_repair" not in harness.script.requests
    assert "Rebalance stock from DE1 to NL1" in text and "€" in text
