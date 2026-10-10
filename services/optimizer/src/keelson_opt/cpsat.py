"""Shared CP-SAT runner and per-solve records for ``integer_stage="cpsat"`` (M8,
``docs/contracts/125-m8-optimizer-cpsat.md``).

Determinism rules (the response must be a pure function of the request):

* ``num_workers`` = env ``CPSAT_NUM_WORKERS`` (default and compose value 1); more than one
  worker makes CP-SAT non-deterministic, which is recorded (``cpsat_deterministic=false``,
  plus a warning);
* ``random_seed`` = ``SolverOptions.seed``;
* every solve stops on ``max_deterministic_time`` (a code constant per stage, drawn from a
  per-request :class:`Budget`), never on wall time. ``max_time_in_seconds`` is only a safety
  cap (env ``OPTIMIZER_CPSAT_WALL_GUARD_MS``, default 3000, never above the request's
  ``time_limit_s``): a solve it stops is machine-dependent, so its result is discarded and the
  stage falls back with reason ``wall_clock_guard``;
* models are built in sorted order by the callers; objectives are checked against int64
  overflow before solving (:func:`objective_fits`), so CP-SAT never rejects a model as invalid
  because of large weights.

:class:`StageLog` collects one :class:`keelson_contracts.optimizer.SolverStage` per solve
(``SolverInfo.stages``); ``model_hash`` is :func:`stage_hash` of the request ``inputs_hash``,
the stage name and the stage's own inputs - never a hash of the solver's model.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from keelson_contracts.optimizer import IntegerFallbackReason, SolverStage, SolverStageStatus
from ortools.sat.python import cp_model

from keelson_opt.canonical import sha256_hex

log = logging.getLogger(__name__)

WALL_GUARD_ENV = "OPTIMIZER_CPSAT_WALL_GUARD_MS"
DEFAULT_WALL_GUARD_MS = 3000
REQUEST_DET_BUDGET = 0.6
"""Deterministic seconds every CP-SAT stage of one request may use together (integer stage
plus resequencing); a stage that finds less than :data:`MIN_STAGE_DET` left falls back with
``budget_exhausted``."""
MIN_STAGE_DET = 0.005
OBJECTIVE_LIMIT = 2**61
"""Objective magnitude bound: ``sum |coef| * max |value| + |offset|`` must stay below it (CP-SAT
needs the objective's range inside int64)."""

_STATUS: dict[int, SolverStageStatus] = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.UNKNOWN: "UNKNOWN",
}


def cpsat_workers() -> int:
    """``CPSAT_NUM_WORKERS`` (compose sets 1; default 1). More than one worker makes CP-SAT
    non-deterministic, so only 1 keeps the optimizer's determinism guarantee."""
    try:
        return max(1, int(os.environ.get("CPSAT_NUM_WORKERS", "1")))
    except ValueError:
        return 1


def wall_guard_ms(time_limit_s: int) -> int:
    """Safety cap on one CP-SAT solve's wall time: env ``OPTIMIZER_CPSAT_WALL_GUARD_MS``
    (default 3000; invalid or negative values use the default), never above the request's
    ``time_limit_s``."""
    try:
        guard = int(os.environ.get(WALL_GUARD_ENV, str(DEFAULT_WALL_GUARD_MS)))
    except ValueError:
        guard = DEFAULT_WALL_GUARD_MS
    if guard < 0:
        guard = DEFAULT_WALL_GUARD_MS
    return min(guard, time_limit_s * 1000)


def set_hints(model: cp_model.CpModel, values: Mapping[int, int]) -> None:
    """Replace the model's solution hint by ``values`` (variable index -> value), in bulk (one
    ``add_hint`` call per variable costs ~6 us, which adds up on the full-profile models)."""
    model.clear_hints()
    hint = model.proto.solution_hint
    hint.vars.extend(values.keys())
    hint.values.extend(values.values())


def objective_fits(terms: Iterable[tuple[int, int, int]], offset: int = 0) -> bool:
    """``terms`` = ``(coef, lb, ub)`` of every objective term: ``True`` when the objective's
    magnitude stays below :data:`OBJECTIVE_LIMIT`."""
    total = abs(offset)
    for coef, lb, ub in terms:
        total += abs(coef) * max(abs(lb), abs(ub))
        if total >= OBJECTIVE_LIMIT:
            return False
    return True


def stage_hash(inputs_hash: str, stage: str, payload: Any) -> str:
    """Per-solve input hash: sha256 of the canonical JSON of the request's ``inputs_hash``, the
    stage name and the stage's own inputs (``payload``: ints, strings and lists only)."""
    return sha256_hex({"inputs_hash": inputs_hash, "stage": stage, "payload": payload})


@dataclass(slots=True)
class Budget:
    """Deterministic time left for this request's CP-SAT stages (seconds), optionally capped
    per kind of stage (e.g. all resequenced lines together)."""

    left: float = REQUEST_DET_BUDGET
    used: dict[str, float] = field(default_factory=dict)

    def take(self, want: float, *, kind: str = "", cap: float | None = None) -> float | None:
        """Up to ``want`` seconds (``None`` when less than :data:`MIN_STAGE_DET` is left)."""
        give = min(want, self.left)
        if cap is not None:
            give = min(give, cap - self.used.get(kind, 0.0))
        if give < MIN_STAGE_DET:
            return None
        return give

    def spend(self, det_time_s: float, *, kind: str = "") -> None:
        self.left = max(0.0, self.left - det_time_s)
        self.used[kind] = self.used.get(kind, 0.0) + det_time_s


@dataclass(slots=True)
class CpRun:
    """One CP-SAT solve. ``objective`` is recomputed exactly from the solution (integer);
    ``bound`` is CP-SAT's best bound rounded to an integer."""

    status: SolverStageStatus
    objective: int | None
    bound: int | None
    det_limit_ms: int
    det_time_ms: int
    num_workers: int
    num_variables: int
    num_constraints: int
    guard_hit: bool
    solver: cp_model.CpSolver
    det_time_s: float = 0.0

    @property
    def has_solution(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE") and not self.guard_hit

    @property
    def fallback(self) -> IntegerFallbackReason | None:
        """Why the run's result cannot be used (``None`` = usable)."""
        if self.guard_hit:
            return "wall_clock_guard"
        if self.status == "INFEASIBLE":
            return "infeasible"
        if self.status == "MODEL_INVALID":
            return "model_invalid"
        if self.status == "UNKNOWN":
            return "time_limit_no_solution"
        return None

    @property
    def gap_bp(self) -> int:
        """Relative gap to the best bound in basis points of the objective (0 when optimal)."""
        if self.status == "OPTIMAL" or self.objective is None or self.bound is None:
            return 0
        return min(10_000, max(0, (self.objective - self.bound) * 10_000 // max(1, abs(self.objective))))

    def value(self, var: cp_model.IntVar) -> int:
        return int(self.solver.value(var))


LINEARIZATION_LEVEL = 2
"""Both M8 models are assignment / flow models with a strong LP relaxation: with the full
linearization one CP-SAT worker proves the optimum of the full-profile instances in a fraction
of the deterministic budget (level 1 stalled at a 40 % gap in the same budget)."""
PROBING_LEVEL = 0
"""Presolve probing over the order x day encodings cost half of a 0.2 s budget on the
full-profile lines without tightening anything the LP does not already find."""
CUT_LEVEL = 0
"""No generic cuts: on the full-profile models the LP relaxation is already tight, and without
them one worker reached equal or better solutions in the same budget (the bd+vn integer model:
proven optimal in 0.09 s instead of a 2 % gap at 0.15 s)."""


def stopped_by_wall_clock(status: SolverStageStatus, det_time_s: float, det_limit: float) -> bool:
    """Whether a solve was stopped by the wall-clock cap: an incomplete answer (``FEASIBLE`` /
    ``UNKNOWN``) before the deterministic limit has no other cause (one worker, no conflict or
    solution limits). Decided on the deterministic time only: CP-SAT may stop on the cap with a
    reported ``wall_time`` just short of it (seen: 177 ms on a 200 ms cap), which the earlier
    wall-time test missed - recording a machine-dependent stop as ``time_limit_no_solution``, or
    using a ``FEASIBLE`` result it should have discarded (review fix)."""
    return status in ("FEASIBLE", "UNKNOWN") and det_time_s < det_limit * 0.999


def run_cpsat(
    model: cp_model.CpModel, *, seed: int, det_limit: float, time_limit_s: int, presolve: bool = True
) -> CpRun:
    """Solve ``model`` (one worker by default, ``random_seed=seed``, stop on ``det_limit``
    deterministic seconds; the wall-clock guard is a safety cap only, see the module doc).
    ``presolve=False`` for models whose presolve costs more than it saves (the time-indexed
    resequencing model: the presolved model found no solution in the line's budget)."""
    workers = cpsat_workers()
    guard_ms = wall_guard_ms(time_limit_s)
    solver = cp_model.CpSolver()
    params = solver.parameters
    params.num_workers = workers
    params.random_seed = seed
    params.max_deterministic_time = det_limit
    params.max_time_in_seconds = guard_ms / 1000.0
    params.linearization_level = LINEARIZATION_LEVEL
    params.cp_model_probing_level = PROBING_LEVEL
    params.cut_level = CUT_LEVEL
    params.cp_model_presolve = presolve
    params.log_search_progress = False
    proto = model.proto
    raw = solver.solve(model)
    status = _STATUS.get(raw, "UNKNOWN")
    response = solver.response_proto
    det = float(response.deterministic_time)
    wall_ms = float(response.wall_time) * 1000.0
    # A stop forced by the wall-clock cap (before the deterministic limit) depends on machine
    # speed and load: such a result must not be used.
    guard_hit = stopped_by_wall_clock(status, det, det_limit)
    objective: int | None = None
    bound: int | None = None
    if status in ("OPTIMAL", "FEASIBLE") and proto.has_objective():
        objective = exact_objective(proto, response.solution)
        bound_f = float(response.best_objective_bound)
        bound = round(bound_f) if math.isfinite(bound_f) else None
    run = CpRun(
        status=status,
        objective=objective,
        bound=bound,
        det_limit_ms=round(det_limit * 1000),
        det_time_ms=math.floor(det * 1000),
        num_workers=workers,
        num_variables=len(proto.variables),
        num_constraints=len(proto.constraints),
        guard_hit=guard_hit,
        solver=solver,
        det_time_s=det,
    )
    log.info(
        "cp-sat solved",
        extra={
            "status": status,
            "det_time_ms": run.det_time_ms,
            "det_limit_ms": run.det_limit_ms,
            "wall_ms": round(wall_ms),
            "guard_hit": guard_hit,
            "num_variables": run.num_variables,
            "num_constraints": run.num_constraints,
        },
    )
    return run


def exact_objective(proto: Any, solution: Any) -> int:
    """The objective value of ``solution`` in exact integer arithmetic (CP-SAT reports a
    double, which cannot hold every int64)."""
    obj = proto.objective
    total = 0
    for ref, coef in zip(obj.vars, obj.coeffs, strict=True):
        value = solution[ref] if ref >= 0 else -solution[-ref - 1]
        total += int(coef) * int(value)
    return total + round(obj.offset)


# ───────────────────────────── per-solve records ─────────────────────────────


@dataclass(slots=True)
class StageLog:
    """``SolverInfo.stages`` of a ``cpsat`` request (inactive in ``repair`` mode: nothing is
    recorded and no stage hash is computed)."""

    enabled: bool
    seed: int
    inputs_hash: str = ""
    budget: Budget = field(default_factory=lambda: Budget(REQUEST_DET_BUDGET))
    stages: list[SolverStage] = field(default_factory=list)
    cpsat_runs: int = 0

    def hash(self, name: str, payload: Any) -> str | None:
        return stage_hash(self.inputs_hash, name, payload) if self.enabled else None

    def lp(self, name: str, status: str, params: Mapping[str, Any], payload: Any, *, used: bool = True) -> None:
        """A GLOP solve (``params`` = the LP's ``num_variables`` / ``num_constraints``)."""
        if not self.enabled:
            return
        self.stages.append(
            SolverStage(
                name=name,
                solver="GLOP",
                status=status,  # type: ignore[arg-type]
                used=used,
                seed=self.seed,
                model_hash=self.hash(name, payload),
                num_variables=int(params.get("num_variables", 0)),
                num_constraints=int(params.get("num_constraints", 0)),
            )
        )

    def cp(
        self,
        name: str,
        run: CpRun | None,
        payload: Any,
        *,
        used: bool,
        fallback: IntegerFallbackReason | None,
        det_limit: float | None = None,
        size: tuple[int, int] = (0, 0),
    ) -> None:
        """A CP-SAT stage: ``run=None`` = not solved (``SKIPPED``, with ``fallback``)."""
        if not self.enabled:
            return
        if run is None:
            stage = SolverStage(
                name=name,
                solver="CP-SAT",
                status="SKIPPED",
                used=False,
                seed=self.seed,
                model_hash=self.hash(name, payload),
                det_time_limit_ms=round(det_limit * 1000) if det_limit is not None else None,
                num_variables=size[0],
                num_constraints=size[1],
                fallback_reason=fallback,
            )
        else:
            stage = SolverStage(
                name=name,
                solver="CP-SAT",
                status=run.status,
                used=used,
                seed=self.seed,
                model_hash=self.hash(name, payload),
                objective=run.objective,
                bound=run.bound,
                det_time_limit_ms=run.det_limit_ms,
                det_time_ms=run.det_time_ms,
                num_workers=run.num_workers,
                num_variables=run.num_variables,
                num_constraints=run.num_constraints,
                fallback_reason=fallback,
            )
        self.stages.append(stage)

    def mark(self, name: str, *, used: bool) -> None:
        """Set ``used`` on the latest stage called ``name`` (e.g. the losing phase-1 LP)."""
        for i in range(len(self.stages) - 1, -1, -1):
            if self.stages[i].name == name:
                self.stages[i] = self.stages[i].model_copy(update={"used": used})
                return

    def settle(self, name: str, *, used: bool, fallback: IntegerFallbackReason | None) -> None:
        """The outcome of the latest stage called ``name``, once later solves decided it (a stage is
        recorded when it is solved, so the records stay in solve order)."""
        for i in range(len(self.stages) - 1, -1, -1):
            if self.stages[i].name == name:
                self.stages[i] = self.stages[i].model_copy(update={"used": used, "fallback_reason": fallback})
                return

    def unuse(self, names: Callable[[str], bool]) -> None:
        """``used=False`` on every stage whose name matches (results the returned plan does not keep)."""
        for i, stage in enumerate(self.stages):
            if stage.used and names(stage.name):
                self.stages[i] = stage.model_copy(update={"used": False})


__all__ = [
    "DEFAULT_WALL_GUARD_MS",
    "OBJECTIVE_LIMIT",
    "REQUEST_DET_BUDGET",
    "WALL_GUARD_ENV",
    "Budget",
    "CpRun",
    "StageLog",
    "cpsat_workers",
    "exact_objective",
    "objective_fits",
    "run_cpsat",
    "set_hints",
    "stage_hash",
    "stopped_by_wall_clock",
    "wall_guard_ms",
]
