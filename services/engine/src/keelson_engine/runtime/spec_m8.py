"""M8 planning settings of the agent spec (``docs/contracts/126-m8-co2-robustness.md`` §6):
``AgentSpec.planning.emissions`` (CO₂e KPI), ``planning.robustness`` (p10 / p50 / p90 bands,
``mc/1``) and ``planning.scoring`` (weight profiles, :mod:`keelson_contracts.scoring`).

Pure settings models (no runtime imports), so :mod:`.spec` can embed them. Tenants change them
through pack ``spec_overrides`` or the back-office (``PUT /api/admin/agents/spec`` merge patch, the
Policies page's *Option scoring* section); none of them is a guarded change. Sample counts and seeds
live here (spec settings), never in deployment env (``120`` §3.5).
"""

from __future__ import annotations

from typing import Annotated, Final

from keelson_contracts.optimizer import Band, LeadTimeNoise
from keelson_contracts.scoring import DEFAULT_SCORING, ScoringConfig
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

__all__ = [
    "DEFAULT_DEMAND_NOISE",
    "DEFAULT_LEAD_TIME_NOISE",
    "EmissionsDefaults",
    "RobustnessDefaults",
    "ScoringConfig",
    "UnitWeightRule",
    "DEFAULT_SCORING",
]


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UnitWeightRule(_Settings):
    """Shipped weight of one unit for the styles whose code starts with ``style_prefix`` (a plain
    prefix, never a pattern: the longest matching prefix wins)."""

    style_prefix: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]
    grams: Annotated[int, Field(ge=1, le=100_000)]


class EmissionsDefaults(_Settings):
    """CO₂e of the plan options (transport, well-to-wheel per the factor set)."""

    enabled: bool = True
    factors_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{0,63}$")] = "glec-v3-indicative"
    """A set of ``data/emissions/factors.yaml`` (platform data, not editable by tenants)."""
    unit_weights: Annotated[tuple[UnitWeightRule, ...], Field(max_length=200)] = ()
    default_unit_weight_g: Annotated[int, Field(ge=1, le=100_000)] | None = None
    """Weight of units no rule matches; ``None`` = ``planning.grams_per_unit`` (the weight the
    per-kg rate cards already assume, so cost and CO₂e share one assumption)."""

    @field_validator("unit_weights")
    @classmethod
    def _unique_prefixes(cls, v: tuple[UnitWeightRule, ...]) -> tuple[UnitWeightRule, ...]:
        prefixes = [r.style_prefix for r in v]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("unit_weights: style prefixes must be unique")
        return v


DEFAULT_LEAD_TIME_NOISE: Final[tuple[LeadTimeNoise, ...]] = (
    LeadTimeNoise(mode="sea", days=Band(p10=-2, p50=0, p90=5)),
    LeadTimeNoise(mode="sea_air", days=Band(p10=-1, p50=0, p90=2)),
    LeadTimeNoise(mode="air", days=Band(p10=0, p50=0, p90=2)),
    LeadTimeNoise(mode="road", days=Band(p10=-1, p50=0, p90=2)),
    LeadTimeNoise(mode="default", days=Band(p10=-1, p50=0, p90=3)),
)
"""Transit-time noise by lane mode (days): ocean schedules slip more often late than early."""
DEFAULT_DEMAND_NOISE: Final = Band(p10=9000, p50=10000, p90=11000)
"""Forecast bias per (cluster, style): ±10 % at p10 / p90."""


class RobustnessDefaults(_Settings):
    """p10 / p50 / p90 bands of every option (``mc/1``: the option's own plan, re-simulated in
    sampled futures; never re-optimised)."""

    enabled: bool = True
    samples: Annotated[int, Field(ge=16, le=256)] = 64
    seed: Annotated[int, Field(ge=0)] | None = None
    """``None`` = ``planning.solver_seed``. One seed for every option of a plan (the options are
    compared in the same futures)."""
    value_bands: bool = True
    """Sample the perturbations' own p10 / p90 (taxonomy priors of assumed values)."""
    duration_bands: bool = True
    """Sample the taxonomy's duration band of a windowed event whose duration is an assumption."""
    lead_time_noise: Annotated[tuple[LeadTimeNoise, ...], Field(max_length=5)] = DEFAULT_LEAD_TIME_NOISE
    demand_noise_bp: Band | None = DEFAULT_DEMAND_NOISE

    @field_validator("lead_time_noise")
    @classmethod
    def _unique_modes(cls, v: tuple[LeadTimeNoise, ...]) -> tuple[LeadTimeNoise, ...]:
        modes = [n.mode for n in v]
        if len(set(modes)) != len(modes):
            raise ValueError("lead_time_noise: modes must be unique")
        return v

    @field_validator("demand_noise_bp")
    @classmethod
    def _demand_range(cls, v: Band | None) -> Band | None:
        if v is not None and (v.p10 < 0 or v.p90 > 100_000):
            raise ValueError("demand_noise_bp must stay within 0..100000")
        return v
