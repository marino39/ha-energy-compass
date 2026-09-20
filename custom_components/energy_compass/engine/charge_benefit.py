"""Decision reserve for additional physical grid-fed battery-charge periods."""

import numpy as np

from .models import Problem, SolveError

_TOL = 1e-6


def enabled(problem: Problem) -> bool:
    """A battery and a positive hurdle enable grid-charge-period accounting.

    The indicator is the named `CHARGE_GRID` mode itself, so the policy only
    applies when operating-mode tracking is on (`minimum_mode_minutes > 0`).
    Deliberate: `CHARGE_GRID` is defined on total charge against forecast PV
    surplus (`dispatch_policy`: `charge >= surplus + activity`), while the
    `grid_battery` allocation is free to route grid import to the battery
    inside a `CHARGE_PV` interval. Keying the hurdle to anything but the mode
    variable would let a valid solution disagree with the indicator and fail
    the independent validator.
    """
    return (
        problem.battery is not None
        and problem.minimum_grid_charge_episode_benefit > 0
        and problem.minimum_mode_minutes > 0
    )


def constrain_charge_benefit(model, problem: Problem, vectors) -> None:
    """Charge only rising edges of the named grid-charge mode."""
    if not enabled(problem):
        return
    if any("mode_CHARGE_GRID" not in v for v in vectors):
        return
    model.exact_mip_gap = True
    for i, v in enumerate(vectors):
        e = v["charge_active"] = v["mode_CHARGE_GRID"]
        s = v["charge_start"] = model.variable(
            binary=True, cost=problem.minimum_grid_charge_episode_benefit
        )
        previous = vectors[i - 1]["charge_active"] if i else None
        initial = int(problem.initial_grid_charge_active) if i == 0 else 0
        lower = {s: 1, e: -1}
        upper = {s: 1}
        if previous is not None:
            lower[previous] = 1
            upper[previous] = 1
        model.constrain(lower, -initial, np.inf)
        model.constrain({s: 1, e: -1}, -np.inf, 0)
        model.constrain(upper, -np.inf, 1 - initial)


def validate_charge_benefit(problem: Problem, vectors, values) -> tuple[int, float]:
    """Certify exact starts and reserve independently of the solver's rows.

    The physical truth of each `CHARGE_GRID` interval is already certified by
    `validate_modes`; this only proves the rising-edge identity and that the
    episode reserve matches the starts actually taken.
    """
    if not enabled(problem) or any("charge_start" not in v for v in vectors):
        return 0, 0.0
    previous = problem.initial_grid_charge_active
    starts = 0
    for v in vectors:
        for key in ("charge_active", "charge_start"):
            value = values[v[key]]
            if not np.isfinite(value) or min(abs(value), abs(value - 1)) > _TOL:
                raise SolveError(
                    "solver_failure", f"invalid solver result: fractional {key}"
                )
        active = values[v["charge_active"]] > 0.5
        start = values[v["charge_start"]] > 0.5
        if start != (active and not previous):
            raise SolveError(
                "solver_failure", "invalid solver result: grid-charge start identity"
            )
        starts += int(start)
        previous = active
    return starts, starts * problem.minimum_grid_charge_episode_benefit
