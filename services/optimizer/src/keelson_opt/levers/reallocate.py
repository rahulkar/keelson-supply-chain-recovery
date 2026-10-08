"""``reallocate_inventory`` lever: DC->DC transfer LP solved with GLOP.

The model itself is built by :func:`keelson_opt.levers.composed.solve_composed` (with only
this lever it is exactly the M1 model below); this module keeps the M1 formulation, the
requirement split and the shared solver helpers.

Formulation (M1, ``docs/contracts/40-optimizer.md``), all over the perturbed scenario:

Variables
  x[lane, k, t] >= 0  units of SKU k shipped on ``lane`` in week t, arriving in week
                      ``t + ceil(transit_days / 7)``; only for ship weeks from
                      ``first_ship_week`` whose arrival is inside the horizon.
  s[c, k, t, p] >= 0  units of requirement part p (sales | buffer) served from the DC.
  l[c, k, t, p] >= 0  units of it not served.
  I[d, k, t]    >= 0  DC d end-of-week stock (the "DC stock balance >= 0").

Requirement (see ``_requirements``): what cluster c draws from its DC each week under the
simulator's request rule with ample supply (own on-hand consumed first, it cannot move
between clusters; the protected minimum is pre-positioned once), split into the *sales*
part and the protected-minimum *buffer* build-up.

Constraints
  demand     s + l = requirement part                          for every (c, k, t, p) > 0
  balance    I[d,k,t] = I[d,k,t-1] + R[d,k,t] + sum_in x(arriving t) - sum_c,p s[c,k,t,p]
                        - [t = 0] * sum_out x(all ship weeks)    with I[d,k,-1] = supply
             (outbound transfers are reserved at the source when the transfer order is
             created, exactly as Merxa does, so the simulator and the LP agree)
  atp        sum_out x[., k, .] <= ATP[d, k] = on_hand - allocated - reserved
             (Merxa validates ATP at the source DC when the transfer order is created)
  lane cap   sum_k x[lane, k, t] <= max_units_wk * capacity_scale(t)   per lane, ship week
  total      sum x <= max_units_total                                   (if given)

Objective (weights in basis points)
  min  sum margin_k * w_lost * (l_sales + BUFFER_LOST_FACTOR * l_buffer)
       + sum cost_lane * w_transfer * x  +  eps * t * x
  where ``eps`` (0.001 cent per unit per week of delay) only breaks ties in favour of
  shipping earlier; it never changes which transfers pay off.

Post-processing: transfer quantities are rounded *down* to integers (keeps every
``<=`` constraint satisfied), zero lines are dropped, and all KPIs are recomputed by the
simulator from the rounded plan — never from the objective value.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass

from keelson_contracts.optimizer import BindingConstraint, OptimizeStatus
from ortools.linear_solver.python import model_builder as mb
from ortools.linear_solver.python import model_builder_helper as mbh

from keelson_opt.scn import Scenario

SOLVER_NAME = "GLOP"
EPS_SHIP_DELAY_CENTS = 0.001
BUFFER_LOST_FACTOR = 0.25
"""Share of the lost margin charged for an unfilled protected-minimum buffer unit (see
:func:`_requirements`). An unfilled buffer is never a lost sale, but in the simulator it
competes pro-rata with sales for scarce DC stock, so it is not free either. 0.25 gave the
highest recomputed net benefit over a 150-problem randomized sweep (1.0, the former
behaviour, produced plans with negative net benefit)."""
_ROUND_TOL = 1e-6
_MAX_BINDING = 25


@dataclass(slots=True)
class _Cap:
    """A ``<=`` constraint whose dual we report when it binds (``ct`` = row index)."""

    name: str
    ct: int
    ub: float


class _LpBuilder:
    """Index-based model construction on ``Model.helper`` (``ModelBuilderHelper``).

    The expression API (``LinearExpr.weighted_sum`` + ``add_linear_constraint``) costs
    roughly 10x more per row; at ~60k demand rows that dominates the solve, so rows and
    variables are created directly by index. Variables are continuous and >= 0.
    """

    def __init__(self, model: mb.Model) -> None:
        self._h = model.helper
        self._obj_idx: list[int] = []
        self._obj_coef: list[float] = []

    def var(self, ub: float, cost: float = 0.0) -> int:
        i = self._h.add_var()
        self._h.set_var_lower_bound(i, 0.0)
        self._h.set_var_upper_bound(i, ub)
        if cost:
            self._obj_idx.append(i)
            self._obj_coef.append(cost)
        return i

    def row(self, idx: Sequence[int], coef: Sequence[float], lb: float, ub: float, name: str | None = None) -> int:
        c = self._h.add_linear_constraint()
        self._h.set_constraint_lower_bound(c, lb)
        self._h.set_constraint_upper_bound(c, ub)
        for i, a in zip(idx, coef, strict=True):
            self._h.add_term_to_constraint(c, i, a)
        if name:
            self._h.set_constraint_name(c, name)
        return c

    def set_objective(self) -> None:
        """Minimise sum(cost * var) over the costs registered in :meth:`var`."""
        self._h.clear_objective()
        self._h.set_maximize(False)
        self._h.set_objective_coefficients(self._obj_idx, self._obj_coef)


def _requirements(scn: Scenario) -> dict[tuple[str, str], tuple[list[int], list[int]]]:
    """Per cluster x SKU: what the cluster draws from its DC each week, split into a
    *sales* part and a protected-minimum *buffer* part.

    The total ``req = max(0, demand + protected_min - stock)`` is the simulator's request
    rule under ample supply (own on-hand consumed first, it cannot move between clusters;
    the protected minimum is pre-positioned once and then rotates). The sales part is the
    same recurrence without the protected minimum; the buffer part is the difference, i.e.
    the build-up of the buffer level (non-decreasing, at most ``protected_min``).

    Only unmet *sales* are lost sales in the simulator and the KPIs; an unfilled buffer is
    simply requested again the next week. Pricing the buffer at the full lost margin made
    the LP pay for transfers whose only effect is terminal buffer stock (recomputed net
    benefit < 0), so buffers carry :data:`BUFFER_LOST_FACTOR` of the margin instead."""
    cached: dict[tuple[str, str], tuple[list[int], list[int]]] | None = scn.memo.get("requirements")
    if cached is not None:
        return cached
    out: dict[tuple[str, str], tuple[list[int], list[int]]] = {}
    for key in sorted(set(scn.demand) | {k for k, v in scn.protected_min.items() if v}):
        stock = no_buffer = scn.cluster_on_hand.get(key, 0)
        pmin = scn.protected_min.get(key, 0)
        sales: list[int] = []
        buffer: list[int] = []
        level = 0
        for d in scn.demand.get(key, [0] * scn.horizon):
            req = max(0, d + pmin - stock)
            stock += req - d
            need = max(0, d - no_buffer)
            no_buffer += need - d
            sales.append(need)
            buffer.append(req - need)
            level += req - need
        if any(sales) or level:
            out[key] = (sales, buffer)
    scn.memo["requirements"] = out
    return out


def _primal_values[K: Hashable](
    solver: mb.Solver, helper: mbh.ModelBuilderHelper, x: Mapping[K, int]
) -> dict[K, float] | None:
    """Values of the given variables, or ``None`` when the solver holds no solution
    (``Solver.value`` returns ``pd.NA`` then; checked once on the objective)."""
    if not _is_finite_number(solver.objective_value):
        return None
    out: dict[K, float] = {}
    for key, v in x.items():
        value = solver.value(mb.Variable(helper, v))
        if not _is_finite_number(value):
            return None
        out[key] = float(value)
    return out


def _is_finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))  # type: ignore[arg-type]
    except TypeError, ValueError:  # pd.NA raises TypeError on float()
        return False


def _map_status(raw: mb.SolveStatus, solver: mb.Solver) -> tuple[OptimizeStatus, str | None]:
    if raw == mb.SolveStatus.OPTIMAL:
        return "OPTIMAL", None
    detail = raw.name + (f": {solver.status_string}" if solver.status_string else "")
    if raw == mb.SolveStatus.FEASIBLE:
        return "FEASIBLE", detail
    # The contract enumerates OPTIMAL|FEASIBLE|INFEASIBLE; anything without a usable
    # solution (time limit hit before feasibility, abnormal termination, ...) is
    # reported as INFEASIBLE with the raw solver status in ``status_detail``.
    return "INFEASIBLE", detail


def _binding(solver: mb.Solver, helper: mbh.ModelBuilderHelper, caps: list[_Cap]) -> list[BindingConstraint]:
    """Tight ``<=`` constraints with a non-zero shadow price.

    For a minimisation, GLOP's dual value of a tight ``<=`` row is the objective change
    per extra unit of right-hand side (<= 0); we report its negation, i.e. how many
    (weighted) cents one more unit of capacity would save.
    """
    out: list[BindingConstraint] = []
    for cap in caps:
        ct = mb.LinearConstraint(helper, index=cap.ct)
        price = -float(solver.dual_value(ct))
        if price < 0.5:
            continue
        if float(solver.activity(ct)) < cap.ub - 1e-6 * max(1.0, abs(cap.ub)):
            continue
        out.append(BindingConstraint(name=cap.name, shadow_price_cents=round(price)))
    out.sort(key=lambda b: (-b.shadow_price_cents, b.name))
    return out[:_MAX_BINDING]
