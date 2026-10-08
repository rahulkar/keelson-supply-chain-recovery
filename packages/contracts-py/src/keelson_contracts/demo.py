"""Full demo reset (``POST /api/demo/reset``, ``95-m5-remediation.md`` §6).

The endpoint (platform-admin or demo-presenter, ``DEMO_MODE`` only) runs the reset steps in
order and answers with a :class:`DemoResetResult` step log; ``scripts/demo-reset.ps1``/``.sh``,
Playwright and the evals use it as a fixture. :data:`DEMO_RESET_STEPS` names the standard
steps in their order; other step names are allowed (a deployment may add steps).
``keep_consents: true`` (rehearsal mode) skips :data:`CONSENT_STEPS`.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from .common import ContractModel, NonNegInt

__all__ = [
    "CONSENT_STEPS",
    "DEMO_RESET_STEPS",
    "DEMO_RESET_TARGET_MS",
    "DemoResetRequest",
    "DemoResetResult",
    "DemoResetStep",
    "DemoStepStatus",
]

DEMO_RESET_STEPS: Final = (
    "merxa_scenario_reset",
    "keelson_rows",
    "adk_sessions",
    "valkey_keys",
    "merxa_consents",
    "token_broker",
    "agent_spec",
    "sim_clock",
    "scn_resync",
)
"""Standard steps in order: Merxa ``scenario:reset`` (golden) · truncate the demo tenant's rows
(cases, runs, events, interrupts, plans, change sets, checklists, evidence, blocks, older scn
snapshots) · ADK sessions · Valkey demo keys · revoke the demo users' Merxa offline sessions /
consents (Keycloak admin API, merxa realm) · delete their Token Broker rows + identity links ·
reset the agent spec to the pack default · reset SimClock · re-sync the scn snapshot."""

CONSENT_STEPS: Final = frozenset({"merxa_consents", "token_broker"})
"""Steps skipped in rehearsal mode (``keep_consents: true``)."""

DEMO_RESET_TARGET_MS: Final = 30_000

DemoStepStatus = Literal["ok", "skipped", "failed"]
NAME_PATTERN: Final = r"^[a-z][a-z0-9_.-]*$"
StepName = Annotated[str, Field(min_length=1, max_length=64, pattern=NAME_PATTERN)]
_NAME_RE: Final = re.compile(NAME_PATTERN)


class DemoResetRequest(ContractModel):
    """Body of ``POST /api/demo/reset`` (may be empty)."""

    keep_consents: StrictBool = False


class DemoResetStep(ContractModel):
    """One step of the reset log. ``counts`` = what the step touched (e.g. rows per table);
    ``detail`` = a short human-readable note (the error summary for a failed step)."""

    step: StepName
    status: DemoStepStatus
    duration_ms: NonNegInt
    detail: Annotated[str, Field(max_length=500)] | None = None
    counts: dict[str, NonNegInt] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _failed_says_why(self) -> DemoResetStep:
        if self.status == "failed" and not self.detail:
            raise ValueError("a failed step carries a detail")
        if bad := sorted(k for k in self.counts if len(k) > 64 or not _NAME_RE.match(k)):
            raise ValueError(f"counts keys must match {NAME_PATTERN} (<= 64 chars): {bad[:5]}")
        return self


class DemoResetResult(ContractModel):
    """The reset's step log. ``ok`` is true exactly when no step failed."""

    ok: StrictBool
    keep_consents: StrictBool
    started_at: AwareDatetime
    duration_ms: NonNegInt
    steps: Annotated[list[DemoResetStep], Field(min_length=1)]

    @model_validator(mode="after")
    def _check(self) -> DemoResetResult:
        names = [s.step for s in self.steps]
        if len(set(names)) != len(names):
            raise ValueError("step names must be unique")
        if self.ok != all(s.status != "failed" for s in self.steps):
            raise ValueError("ok must be true exactly when no step failed")
        if self.keep_consents:
            if ran := sorted(s.step for s in self.steps if s.step in CONSENT_STEPS and s.status != "skipped"):
                raise ValueError(f"keep_consents skips the consent steps, but these ran: {ran}")
        return self

    @property
    def within_target(self) -> bool:
        """The reset finished within the 30 s target."""
        return self.duration_ms < DEMO_RESET_TARGET_MS

    @property
    def failed_steps(self) -> list[str]:
        return [s.step for s in self.steps if s.status == "failed"]
