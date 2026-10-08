"""M8 inputs for the full-profile fixture (``126``): unit weights, lane legs and DC-lane distances
as the engine enriches them (``runtime/emissions.py``), an indicative factor set and the robustness
spec of the golden (seed 7, 64 samples, the trims-delay value band 5/7/14, the VN closure window
band 7/10/14 and the default noise).

Distances are fixed integers (km) close to the engine's catalogue routes: the sea legs around the
Cape, the sea-air sea leg into Jebel Ali plus the air leg (+95 km), the road legs from the
destination port. Weights follow the value-fashion pack's style prefixes."""

from __future__ import annotations

import copy
from typing import Any

from .full_problem import BD_TRIMS_DELAY, VN_PORT_CLOSURE, full_data

UNIT_WEIGHTS_G = {"PUF": 1100, "PRK": 1500, "GIL": 600, "FLC": 500, "KNT": 450, "CRD": 550, "HDY": 600}
LEGS_KM: dict[str, list[tuple[str, int]]] = {
    "CGP-FXT-SEA": [("sea", 21040), ("road", 96)],
    "CGP-RTM-SEA": [("sea", 21130), ("road", 162)],
    "CGP-UK1-SA": [("sea", 5310), ("air", 5585)],
    "CGP-NL1-SA": [("sea", 5310), ("air", 5251)],
    "SGN-HAM-SEA": [("sea", 21860), ("road", 336)],
    "SGN-GDN-SEA": [("sea", 22480), ("road", 302)],
    "SGN-DE1-SA": [("sea", 7020), ("air", 4724)],
    "SGN-PL1-SA": [("sea", 7020), ("air", 4492)],
    "IST-DE1-RD": [("road", 2257)],
    "IST-NL1-RD": [("road", 2624)],
    "TNG-NL1-RD": [("road", 2711)],
    "TNG-UK1-RD": [("road", 2350)],
}
DC_LANE_KM = {"DE1>NL1": 545, "DE1>UK1": 1088, "PL1>NL1": 941, "PL1>DE1": 449, "NL1>UK1": 547}
FACTORS = [
    {"mode": "sea", "g_co2e_per_tkm": 16},
    {"mode": "air", "g_co2e_per_tkm": 850},
    {"mode": "road", "g_co2e_per_tkm": 100},
    {"mode": "rail", "g_co2e_per_tkm": 25},
]
EMISSIONS = {"factors": FACTORS, "basis": "WTW", "source_id": "glec-v3-indicative"}
DEFAULT_NOISE = [
    {"mode": "sea", "days": {"p10": -2, "p50": 0, "p90": 5}},
    {"mode": "sea_air", "days": {"p10": -1, "p50": 0, "p90": 2}},
    {"mode": "air", "days": {"p10": 0, "p50": 0, "p90": 2}},
    {"mode": "road", "days": {"p10": -1, "p50": 0, "p90": 2}},
    {"mode": "default", "days": {"p10": -1, "p50": 0, "p90": 3}},
]
DEMAND_NOISE = {"p10": 9000, "p50": 10000, "p90": 11000}

BD_DELAY_BANDED = {**BD_TRIMS_DELAY, "p10": 5, "p90": 14, "prov": "assumption"}
"""The trims delay with the SUP.delay prior band (5 / 7 / 14 days)."""
VN_CLOSURE_10D = {**VN_PORT_CLOSURE, "prov": "assumption"}
"""The 10-day VNSGN closure; its duration band (7 / 10 / 14) travels in ``window_bands``."""


def m8_data() -> dict[str, Any]:
    data = full_data()
    for sku in data["skus"]:
        sku["unit_weight_g"] = UNIT_WEIGHTS_G[sku["style_code"][:3]]
    for lane in data["lanes"]:
        lane["distance_km"] = DC_LANE_KM[f"{lane['from_dc']}>{lane['to_dc']}"]
    for il in data["inbound_lanes"]:
        il["legs"] = [{"mode": m, "distance_km": km} for m, km in LEGS_KM[il["code"]]]
    return data


def robustness(
    *, samples: int = 64, seed: int = 7, window: int | None = 1, noise: bool = True, demand: bool = True,
    value_bands: bool = True,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"samples": samples, "seed": seed, "value_bands": value_bands}
    if window is not None:
        spec["window_bands"] = [{"perturbation": window, "duration_days": {"p10": 7, "p50": 10, "p90": 14}}]
    if noise:
        spec["lead_time_noise"] = copy.deepcopy(DEFAULT_NOISE)
    if demand:
        spec["demand_noise_bp"] = dict(DEMAND_NOISE)
    return spec


def m8_request(
    levers: list[dict[str, Any]],
    *,
    plan_id: str = "opt",
    perturbations: list[dict[str, Any]] | None = None,
    emissions: bool = True,
    robust: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A full-profile request with the M8 inputs (default: banded BD delay + 10-day VN closure)."""
    req: dict[str, Any] = {
        "data": m8_data(),
        "perturbations": copy.deepcopy([BD_DELAY_BANDED, VN_CLOSURE_10D] if perturbations is None else perturbations),
        "plan_spec": {"id": plan_id, "title": plan_id, "levers": copy.deepcopy(levers)},
        "solver": {"time_limit_s": 5, "seed": 7},
    }
    if emissions:
        req["emissions"] = copy.deepcopy(EMISSIONS)
    if robust is not None:
        req["robustness"] = robust
    return req
