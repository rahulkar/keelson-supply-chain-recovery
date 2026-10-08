"""Option scoring: weight profiles over the plan scorecard (``docs/contracts/126-m8-co2-robustness.md`` §4).

Plan §9 "Scoring": the PlanSpec carries a weight profile; results always show the Pareto view
plus a scorecard. A :class:`WeightProfile` turns a :class:`~keelson_contracts.optimizer.Scorecard`
into one integer score in cents (higher is better)::

    recovered = net_benefit + incremental_cost                (margin the plan protects)
    robust    = bands.net_benefit_cents.p10 + incremental_cost  (pessimistic case; = recovered
                                                                without bands)
    blended   = ((10000 - risk_aversion_bp) * recovered + risk_aversion_bp * robust) / 10000
    score     = blended * lost_margin_bp / 10000
              - incremental_cost * cost_bp / 10000
              - incremental_co2e_kg * carbon_price_cents_per_t / 1000

Every division rounds half up (integer arithmetic only); a scorecard without CO₂e counts as 0 kg
(:func:`co2_missing` says so). Under the default ``net-benefit`` profile (10000 / 10000 / 0 / 0)
the score *is* the net benefit, so the deterministic default recommendation is unchanged.

The engine keeps the tenant's profiles in ``AgentSpec.planning.scoring``; the default profile's
``lost_margin_bp`` / ``cost_bp`` also become ``PlanSpec.weights`` (the LP objective), the other
profiles only rank the options (``scenario_compare@1`` ``scores``). Scoring is not a guarded change
(``110`` §1): it never changes who approves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from .optimizer import Scorecard

__all__ = [
    "DEFAULT_PROFILE_ID",
    "DEFAULT_PROFILES",
    "DEFAULT_SCORING",
    "MAX_PROFILES",
    "ProfileId",
    "ScoringConfig",
    "WeightProfile",
    "co2_missing",
    "div_half_up",
    "profile_score",
    "rank_options",
]

MAX_PROFILES: Final = 8
DEFAULT_PROFILE_ID: Final = "net-benefit"

ProfileId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
_Weight = Annotated[StrictInt, Field(ge=0, le=50_000)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WeightProfile(_Model):
    """How much each scorecard component counts (basis points; 10000 = x1.0)."""

    id: ProfileId
    label: Annotated[str, StringConstraints(min_length=1, max_length=40, strip_whitespace=True)]
    lost_margin_bp: _Weight = 10_000
    """Weight of the margin the plan protects."""
    cost_bp: _Weight = 10_000
    """Weight of the plan's incremental cost."""
    carbon_price_cents_per_t: Annotated[StrictInt, Field(ge=0, le=1_000_000)] = 0
    """Internal carbon price applied to the plan's incremental CO₂e (cents per tonne)."""
    risk_aversion_bp: Annotated[StrictInt, Field(ge=0, le=10_000)] = 0
    """Weight of the pessimistic (p10) outcome versus the nominal one (needs robustness bands)."""


DEFAULT_PROFILES: Final[tuple[WeightProfile, ...]] = (
    WeightProfile(id=DEFAULT_PROFILE_ID, label="Net benefit"),
    WeightProfile(id="cost-first", label="Cost first", cost_bp=15_000),
    WeightProfile(id="service-first", label="Service first", lost_margin_bp=15_000, cost_bp=7_500,
                  risk_aversion_bp=5_000),
    WeightProfile(id="low-carbon", label="Low carbon", carbon_price_cents_per_t=20_000, risk_aversion_bp=2_500),
)
"""Built-in profiles; ``low-carbon`` prices CO₂e at €200 per tonne."""


class ScoringConfig(_Model):
    """``AgentSpec.planning.scoring``: the tenant's profiles and which one is the default."""

    default_profile: ProfileId = DEFAULT_PROFILE_ID
    profiles: Annotated[tuple[WeightProfile, ...], Field(min_length=1, max_length=MAX_PROFILES)] = DEFAULT_PROFILES

    @model_validator(mode="after")
    def _consistent(self) -> ScoringConfig:
        ids = [p.id for p in self.profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("profile ids must be unique")
        if self.default_profile not in ids:
            raise ValueError(f"default_profile {self.default_profile!r} is not one of the profiles")
        return self

    @property
    def default(self) -> WeightProfile:
        return next(p for p in self.profiles if p.id == self.default_profile)


DEFAULT_SCORING: Final = ScoringConfig()


def div_half_up(num: int, den: int) -> int:
    """``num / den`` rounded half up (towards +inf on .5), integers only; ``den > 0``."""
    return (2 * num + den) // (2 * den)


def co2_missing(card: Scorecard) -> bool:
    """The scorecard has no CO₂e (it then counts as 0 kg in every profile)."""
    return card.incremental_co2e_kg is None


def profile_score(card: Scorecard, profile: WeightProfile) -> int:
    """The option's score under ``profile`` in cents (see the module docstring)."""
    cost = card.incremental_cost_cents
    recovered = card.net_benefit_cents + cost
    r = profile.risk_aversion_bp
    if r and card.bands is not None:
        robust = card.bands.net_benefit_cents.p10 + cost
        blended = div_half_up((10_000 - r) * recovered + r * robust, 10_000)
    else:
        blended = recovered
    co2 = card.incremental_co2e_kg or 0
    return (
        div_half_up(blended * profile.lost_margin_bp, 10_000)
        - div_half_up(cost * profile.cost_bp, 10_000)
        - div_half_up(co2 * profile.carbon_price_cents_per_t, 1_000)
    )


def rank_options(cards: Mapping[str, Scorecard] | Sequence[tuple[str, Scorecard]],
                 profile: WeightProfile) -> dict[str, tuple[int, int]]:
    """``option id → (score, rank)`` under ``profile``: rank 1 is the best score; ties keep the
    input (candidate) order."""
    items = list(cards.items()) if isinstance(cards, Mapping) else list(cards)
    scored = [(profile_score(card, profile), i, oid) for i, (oid, card) in enumerate(items)]
    ranked = sorted(scored, key=lambda s: (-s[0], s[1]))
    return {oid: (score, n) for n, (score, _i, oid) in enumerate(ranked, start=1)}
