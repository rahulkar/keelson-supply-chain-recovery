"""Robustness of a plan under sampled futures (method ``mc/1``,
``docs/contracts/126-m8-co2-robustness.md`` §5): keyed integer draws (:mod:`.draws`), sampled
futures and dated plan arrivals (:mod:`.sample`), the batch PAB simulator (:mod:`.batch`) and the
evaluation into scorecard bands (:mod:`.evaluate`). It re-simulates the request's own rounded plan
and never re-optimises."""

from keelson_opt.robust.evaluate import CELL_BUDGET, Robustness, evaluate
from keelson_opt.robust.sample import RobustnessError

__all__ = ["CELL_BUDGET", "Robustness", "RobustnessError", "evaluate"]
