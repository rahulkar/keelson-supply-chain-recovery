"""Valid example props/envelopes for every M1, M3 and M5 block kind (shared by several tests)."""

from __future__ import annotations

import copy
from typing import Any

HASH = "d1bbcbc4cb6b0706ba28795e350e70f90c2564efa920a0cdadbc1028667b83bc"

KPI = {
    "key": "units_at_risk",
    "label": "Units at risk",
    "value": 3200,
    "unit": "units",
    "delta": -1200,
    "prov": "simulated",
    "preliminary": False,
}

SCORECARD: dict[str, Any] = {
    "fill_rate_bp": 9650,
    "lost_units": 420,
    "lost_margin_cents": 840_000,
    "incremental_cost_cents": 183_000,
    "cost_breakdown": {"transfer": 63_000, "expedite_premium": 120_000, "nearshore_premium": 0},
    "net_benefit_cents": 1_517_000,
    "shortage_days": 12,
    "first_breach_shift_days": 7,
    "tts_gain_days": 5,
    "changes": 6,
}

ZERO_SCORECARD: dict[str, Any] = {
    "fill_rate_bp": 9100,
    "lost_units": 2100,
    "lost_margin_cents": 4_200_000,
    "incremental_cost_cents": 0,
    "cost_breakdown": {"transfer": 0, "expedite_premium": 0, "nearshore_premium": 0},
    "net_benefit_cents": 0,
    "shortage_days": 40,
    "first_breach_shift_days": 0,
    "tts_gain_days": 0,
    "changes": 0,
}

GOOD_PROPS: dict[str, dict[str, Any]] = {
    "kpi_strip@1": {
        "items": [
            KPI,
            {
                "key": "revenue_at_risk_cents",
                "label": "Revenue at risk",
                "value": 8_000_000,
                "unit": "eur_cents",
                "prov": "simulated",
                "preliminary": True,
            },
        ]
    },
    "approach_card@1": {
        "interrupt_id": "approach-case1-1",
        "event": {
            "family": "SUP",
            "type": "SUP.TRIM_DELAY",
            "description": "Meghna Trims ships zips 7 days late",
            "source": "chat",
        },
        "mode": "actual",
        "matched": {
            "count": 6,
            "sample": [{"type": "po", "code": "PO-26-0101", "label": "PO-26-0101 · Gazipur → UK1"}],
        },
        "assumptions": [
            {"key": "delay_days", "label": "Delay", "value_text": "7 days", "prov": "decision"},
            {"key": "ttr", "label": "Time to recover", "value_text": "3 weeks", "prov": "assumption"},
        ],
        "checks": ["Bangladesh POs to UK1/NL1 with ETA W44-W46"],
        "buttons": ["confirm", "edit", "what_if", "cancel"],
    },
    "recommended_option@1": {
        "plan_version": 1,
        "option": {
            "id": "opt-A",
            "title": "Rebalance between DCs",
            "summary_bullets": ["Move surplus from DE1 and PL1 to NL1 before peak"],
            "kpis": [KPI],
        },
        "alternatives": [{"id": "opt-0", "title": "Do nothing", "one_liner": "Accept the shortfall"}],
        "actions": [
            {"type": "apply", "label": "Apply in Merxa", "enabled": True},
            {"type": "guide", "label": "Show steps", "enabled": False, "disabled_reason": "Not needed"},
        ],
    },
    "plan_diff@1": {
        "plan_version": 1,
        "rows": [
            {
                "seq": 1,
                "op": "transfer_order.create",
                "target_label": "New transfer DE1 → NL1",
                "before_text": "—",
                "after_text": "520 units ship 19 Oct",
                "qty": 520,
                "system": "Merxa",
            }
        ],
        "totals": {"lines": 1, "units": 520, "value_cents": 1_300_000},
    },
    "approval_card@1": {
        "interrupt_id": "approval-cs_01-1",
        "change_set_id": "cs_01",
        "change_set_hash": HASH,
        "plan_version": 1,
        "summary": {"lines": 1, "units": 520, "value_cents": 1_300_000},
        "reason": "Over auto-approval limits",
        "required_roles": ["approver"],
        "connect": {"status": "missing", "connect_url": "http://keelson.localhost/api/connectors/merxa/connect"},
        "buttons": ["approve", "reject", "revise"],
    },
    "commit_tracker@1": {
        "change_set_id": "cs_01",
        "system": "Merxa",
        "status": "applied",
        "ext_code": "CS-0001",
        "ext_url": "http://merxa.localhost/change-batches/CS-0001",
        "applied": 2,
        "verified": 1,
    },
    "narrative@1": {"markdown": "Moving stock from **DE1** protects NL1 through peak."},
    "evidence_pack@1": {
        "items": [
            {
                "id": "ev_0192f0c0a1b27c3d8e9f0a1b2c3d4e5f",
                "source": "Merxa · list_purchase_orders",
                "label": "12 open purchase orders",
                "prov": "fact",
                "retrieved_at": "2026-10-05T09:14:00Z",
            }
        ]
    },
    # M3 canvas visuals
    "network_map@1": {
        "title": "Inbound network",
        "summary": {"nodes": 3, "at_risk_nodes": 1, "lanes": 2},
        "bbox": [-10_000_000, 20_000_000, 95_000_000, 60_000_000],
        "legend": [
            {"key": "at_risk", "label": "At risk", "kind": "node"},
            {"key": "sea_air", "label": "Sea-air", "kind": "link"},
            {"key": "Port closure", "label": "Port closure", "kind": "footprint"},
        ],
    },
    "impact_graph@1": {"summary": {"nodes": 4, "disrupted": 1, "impacted": 1}},
    "stockout_timeline@1": {
        "weeks": ["2026-W44", "2026-W45", "2026-W46"],
        "options": [{"id": "opt-0", "title": "Do nothing"}, {"id": "opt-A", "title": "Rebalance between DCs"}],
    },
    "scenario_compare@1": {
        "options": [
            {
                "id": "opt-0",
                "title": "Do nothing",
                "levers": [],
                "scorecard": ZERO_SCORECARD,
                "recommended": False,
                "guided_only": False,
                "guided_reason": None,
                "pareto": True,
            },
            {
                "id": "opt-B",
                "title": "Rebalance + targeted sea-air",
                "levers": ["reallocate_inventory", "expedite_mode_shift"],
                "scorecard": SCORECARD,
                "recommended": True,
                "guided_only": True,
                "guided_reason": "Merxa connector cannot apply capacity_booking.create",
                "pareto": True,
            },
        ],
        "axes": {"x": "incremental_cost_cents", "y": "lost_units"},
    },
}

GOOD_DATA: dict[str, dict[str, Any]] = {
    "network_map@1": {
        "nodes": [
            {"id": "port:BDCGP", "kind": "port", "label": "Chittagong", "lat_e6": 22_330_000,
             "lon_e6": 91_800_000, "status": "disrupted", "units_at_risk": None},
            {"id": "port:AEJEA", "kind": "port", "label": "Jebel Ali", "lat_e6": 25_011_000,
             "lon_e6": 55_061_000, "status": "ok"},
            {"id": "dc:UK1", "kind": "dc", "label": "UK1", "lat_e6": 52_486_000, "lon_e6": -1_890_000,
             "status": "at_risk", "units_at_risk": 3200},
        ],
        "links": [
            {"id": "BDCGP-UK1-CAPE", "kind": "inbound", "from": "port:BDCGP", "to": "dc:UK1",
             "path": [[91_800_000, 22_330_000], [18_470_000, -34_350_000], [-1_890_000, 52_486_000]],
             "status": "impacted", "units": 9000, "option_id": None},
            {"id": "opt-B:sea_air:1", "kind": "sea_air", "from": "port:AEJEA", "to": "dc:UK1",
             "path": [[55_061_000, 25_011_000], [-1_890_000, 52_486_000]], "status": "ok", "units": 1200,
             "option_id": "opt-B"},
        ],
        "footprint": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[91_000_000, 21_500_000], [92_600_000, 21_500_000],
                                         [92_600_000, 23_100_000], [91_000_000, 23_100_000],
                                         [91_000_000, 21_500_000]]],
                    },
                    "properties": {"label": "Port closure", "status": "disrupted"},
                }
            ],
        },
    },
    "impact_graph@1": {
        "nodes": [
            {"id": "supplier:TRIM-BD-01", "kind": "supplier", "label": "Meghna Trims", "status": "disrupted",
             "units_at_risk": 0, "first_breach_week": None},
            {"id": "factory:F-BD-01", "kind": "factory", "label": "Gazipur", "status": "impacted",
             "units_at_risk": 9000},
            {"id": "dc:UK1", "kind": "dc", "label": "UK1", "status": "at_risk", "units_at_risk": 3200,
             "first_breach_week": "2026-W45"},
            {"id": "cluster:UK1-COLD-A", "kind": "cluster", "label": "UK1 cold A", "status": "at_risk",
             "units_at_risk": 1400, "first_breach_week": "2026-W45"},
        ],
        "edges": [
            {"source": "supplier:TRIM-BD-01", "target": "factory:F-BD-01", "kind": "supplies"},
            {"source": "factory:F-BD-01", "target": "dc:UK1", "kind": "ships"},
            {"source": "dc:UK1", "target": "cluster:UK1-COLD-A", "kind": "serves"},
        ],
    },
    "stockout_timeline@1": {
        "rows": [
            {
                "id": "UK1|UK1-COLD-A|PUF-W-BLK",
                "dc_code": "UK1",
                "cluster_code": "UK1-COLD-A",
                "style_code": "PUF-W-BLK",
                "label": "UK1 cold A, Puffer black",
                "cells": [
                    {"week": "2026-W44", "status": "ok", "shortage_units": 0},
                    {"week": "2026-W45", "status": "tight", "shortage_units": 0},
                    {"week": "2026-W46", "status": "short", "shortage_units": 340},
                ],
                "first_breach_week": "2026-W46",
                "by_option": {"opt-0": "2026-W46", "opt-A": None},
            }
        ]
    },
}

# For each kind: (description, mutation) pairs that must be rejected.
BAD_PROPS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "kpi_strip@1": [
        ("float value", {"items": [{**KPI, "value": 3200.5}]}),
        ("unknown unit", {"items": [{**KPI, "unit": "eur"}]}),
        ("missing preliminary", {"items": [{k: v for k, v in KPI.items() if k != "preliminary"}]}),
        ("duplicate keys", {"items": [KPI, KPI]}),
        ("empty", {"items": []}),
        ("bad prov", {"items": [{**KPI, "prov": "guess"}]}),
    ],
    "approach_card@1": [
        ("bad mode", {"mode": "maybe"}),
        ("bad button", {"buttons": ["confirm", "approve"]}),
        ("sample > count", {"matched": {"count": 0, "sample": [{"type": "po", "code": "P", "label": "P"}]}}),
        ("bad family", {"event": {"family": "XYZ", "type": "t", "description": "d", "source": "s"}}),
        ("extra field", {"extra": 1}),
    ],
    "recommended_option@1": [
        ("plan_version 0", {"plan_version": 0}),
        ("disabled without reason", {"actions": [{"type": "apply", "label": "Apply", "enabled": False}]}),
        ("alternative reuses option id", {"alternatives": [{"id": "opt-A", "title": "x", "one_liner": "y"}]}),
    ],
    "plan_diff@1": [
        ("negative units", {"totals": {"lines": 1, "units": -1, "value_cents": 0}}),
        ("float value_cents", {"totals": {"lines": 1, "units": 1, "value_cents": 1.0}}),
        ("seq 0", {"rows": [{**GOOD_PROPS["plan_diff@1"]["rows"][0], "seq": 0}]}),
    ],
    "approval_card@1": [
        ("bad hash", {"change_set_hash": "ABC"}),
        ("bad connect status", {"connect": {"status": "ok", "connect_url": None}}),
        ("bad button", {"buttons": ["confirm"]}),
    ],
    "commit_tracker@1": [
        ("verified > applied", {"applied": 1, "verified": 2}),
        ("bad status", {"status": "done"}),
    ],
    "narrative@1": [("empty markdown", {"markdown": ""})],
    "evidence_pack@1": [
        ("bad evidence id", {"items": [{**GOOD_PROPS["evidence_pack@1"]["items"][0], "id": "e1"}]}),
        (
            "naive timestamp",
            {"items": [{**GOOD_PROPS["evidence_pack@1"]["items"][0], "retrieved_at": "2026-10-05T09:14:00"}]},
        ),
    ],
}


_SC_OPTS = GOOD_PROPS["scenario_compare@1"]["options"]

BAD_PROPS.update(
    {
        "network_map@1": [
            ("float bbox", {"bbox": [-10.5, 20_000_000, 95_000_000, 60_000_000]}),
            ("inverted bbox", {"bbox": [95_000_000, 20_000_000, -10_000_000, 60_000_000]}),
            ("latitude out of range", {"bbox": [-10_000_000, -91_000_000, 95_000_000, 60_000_000]}),
            ("bbox of 3", {"bbox": [-10_000_000, 20_000_000, 95_000_000]}),
            ("at_risk > nodes", {"summary": {"nodes": 1, "at_risk_nodes": 2, "lanes": 0}}),
            ("duplicate legend", {"legend": [{"key": "a", "label": "A", "kind": "node"}] * 2}),
        ],
        "impact_graph@1": [
            ("more flagged than nodes", {"summary": {"nodes": 1, "disrupted": 1, "impacted": 1}}),
            ("float count", {"summary": {"nodes": 1.0, "disrupted": 0, "impacted": 0}}),
        ],
        "stockout_timeline@1": [
            ("no weeks", {"weeks": []}),
            ("bad week", {"weeks": ["2026-W54"]}),
            ("unordered weeks", {"weeks": ["2026-W46", "2026-W45"]}),
            ("duplicate option ids", {"options": [{"id": "o", "title": "A"}, {"id": "o", "title": "B"}]}),
        ],
        "scenario_compare@1": [
            ("no options", {"options": []}),
            ("guided without reason", {"options": [{**_SC_OPTS[1], "guided_reason": None}]}),
            ("reason without guided", {"options": [{**_SC_OPTS[0], "guided_reason": "x"}]}),
            ("two recommended", {"options": [{**_SC_OPTS[0], "recommended": True}, _SC_OPTS[1]]}),
            ("breakdown does not add up",
             {"options": [{**_SC_OPTS[1], "scorecard": {**SCORECARD, "incremental_cost_cents": 1}}]}),
            ("float money", {"options": [{**_SC_OPTS[1], "scorecard": {**SCORECARD, "lost_margin_cents": 1.5}}]}),
            ("fill rate above 100%",
             {"options": [{**_SC_OPTS[1], "scorecard": {**SCORECARD, "fill_rate_bp": 10001}}]}),
            ("same axes", {"axes": {"x": "lost_units", "y": "lost_units"}}),
            ("unknown axis", {"axes": {"x": "vibes", "y": "lost_units"}}),
        ],
    }
)

CHECKLIST_STEP: dict[str, Any] = {
    "seq": 1,
    "title": "Create transfer order DE1 → NL1",
    "instructions_md": "Open **Transfer orders → New**, check the pre-filled lines and save.",
    "deep_link": "http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1&ship_date=2026-10-12&lines=SKU-1:120",
    "expected": {
        "entity_type": "transfer_order",
        "entity_ref": None,
        "match": {"from_dc": "DE1", "to_dc": "NL1", "lines": [{"sku": "SKU-1", "qty": 120}]},
    },
    "status": "verified",
    "verified_at": "2026-10-07T10:00:00Z",
    "evidence_id": "ev_0192f0c0a1b27c3d8e9f0a1b2c3d4e60",
}

GOOD_PROPS["remediation_checklist@1"] = {
    "checklist_id": "chk_case1_v1_opt2",
    "connector": "merxa",
    "steps": [
        CHECKLIST_STEP,
        {
            **CHECKLIST_STEP,
            "seq": 2,
            "title": "Reduce allocation for cluster NL1-mild-A",
            "deep_link": "/guide/allocations/ALLOC-7",
            "expected": {"entity_type": "allocation", "entity_ref": "ALLOC-7", "match": {"qty": 40}},
            "status": "pending",
            "verified_at": None,
            "evidence_id": None,
        },
        {**CHECKLIST_STEP, "seq": 3, "deep_link": None, "status": "skipped", "verified_at": None,
         "evidence_id": None},
    ],
    "progress": {"done": 2, "total": 3},
}

_STEPS = GOOD_PROPS["remediation_checklist@1"]["steps"]
BAD_PROPS["remediation_checklist@1"] = [
    ("no steps", {"steps": [], "progress": {"done": 0, "total": 0}}),
    ("seq gap", {"steps": [_STEPS[0], {**_STEPS[1], "seq": 3}, {**_STEPS[2], "seq": 4}]}),
    ("progress total off", {"progress": {"done": 2, "total": 4}}),
    ("progress done off", {"progress": {"done": 1, "total": 3}}),
    ("verified without verified_at", {"steps": [{**_STEPS[0], "verified_at": None}, *_STEPS[1:]]}),
    ("evidence on a pending step", {"steps": [_STEPS[0], {**_STEPS[1], "evidence_id": _STEPS[0]["evidence_id"]},
                                              _STEPS[2]]}),
    ("verified_at on a done step", {"steps": [{**_STEPS[0], "status": "done"}, *_STEPS[1:]]}),
    ("javascript deep link", {"steps": [{**_STEPS[0], "deep_link": "javascript:alert(1)"}, *_STEPS[1:]]}),
    ("protocol-relative link", {"steps": [{**_STEPS[0], "deep_link": "//evil.example/x"}, *_STEPS[1:]]}),
    ("userinfo in link", {"steps": [{**_STEPS[0], "deep_link": "http://user@merxa.localhost/x"}, *_STEPS[1:]]}),
    ("float in match", {"steps": [{**_STEPS[0], "expected": {**_STEPS[0]["expected"], "match": {"qty": 1.5}}},
                                  *_STEPS[1:]]}),
    ("unknown status", {"steps": [{**_STEPS[0], "status": "verifying"}, *_STEPS[1:]]}),
    ("bad evidence id", {"steps": [{**_STEPS[0], "evidence_id": "e1"}, *_STEPS[1:]]}),
    ("missing deep_link key", {"steps": [{k: v for k, v in _STEPS[0].items() if k != "deep_link"}, *_STEPS[1:]]}),
]

# M6 (100 §6): "Opened from alert" origin card.
GOOD_PROPS["alert_origin@1"] = {
    "alert_id": "0b8f3f5e-6a1c-5d2e-9f00-1a2b3c4d5e6f",
    "title": "Typhoon approaching Ho Chi Minh City",
    "severity": "high",
    "kind": "natural_hazard",
    "source_names": ["GDACS", "Demo Freight Daily"],
    "simulated": True,
    "opened_at": "2026-10-07T10:00:00Z",
}
BAD_PROPS["alert_origin@1"] = [
    ("alert id not a uuid", {"alert_id": "alert-1"}),
    ("unknown severity", {"severity": "severe"}),
    ("unknown kind", {"kind": "tsunami"}),
    ("duplicate sources", {"source_names": ["GDACS", "GDACS"]}),
    ("naive opened_at", {"opened_at": "2026-10-07T10:00:00"}),
    ("empty title", {"title": ""}),
    ("simulated as string", {"simulated": "yes"}),
]

DATA_REF_KEYS = frozenset(GOOD_DATA)
CANVAS_KINDS = frozenset({"evidence_pack", "network_map", "impact_graph", "stockout_timeline", "scenario_compare"})


def good_data(key: str) -> dict[str, Any]:
    return copy.deepcopy(GOOD_DATA[key])


def good_props(key: str) -> dict[str, Any]:
    return copy.deepcopy(GOOD_PROPS[key])


def bad_props(key: str, mutation: dict[str, Any]) -> dict[str, Any]:
    props = good_props(key)
    props.update(copy.deepcopy(mutation))
    return props


def provenance(kind: str = "simulated") -> dict[str, Any]:
    return {"kind": kind, "engine": "keelson-opt@0.1", "inputs_hash": HASH, "as_of": "2026-10-05"}


def envelope(key: str, *, placement: str | None = None) -> dict[str, Any]:
    kind, v = key.split("@")
    return {
        "id": f"blk_{kind}",
        "kind": kind,
        "v": int(v),
        "title": kind.replace("_", " ").title(),
        "placement": placement or ("canvas" if kind in CANVAS_KINDS else "inline"),
        "provenance": provenance(),
        "data_ref": f"/api/blocks/blk_{kind}/data" if key in DATA_REF_KEYS else None,
        "props": good_props(key),
    }
