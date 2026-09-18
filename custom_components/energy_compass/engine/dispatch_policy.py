"""Elapsed-time direction commitments for the advisory battery schedule."""

from datetime import UTC, timedelta

import numpy as np

from .models import Problem, SolveError


def constrain_directions(model, problem: Problem, vectors) -> None:
    """Keep each direction for its dwell time, allowing zero power within it."""
    if not problem.battery or not problem.minimum_mode_minutes:
        return
    dwell = timedelta(minutes=problem.minimum_mode_minutes)
    starts = [slot.start.astimezone(UTC) for slot in problem.slots]
    previous = problem.initial_battery_mode
    if previous is not None:
        held_until = problem.initial_battery_mode_since.astimezone(UTC) + dwell
        for start, vector in zip(starts, vectors, strict=True):
            if start < held_until:
                mode = int(previous == "charge")
                model.constrain({vector["battery_mode"]: 1}, mode, mode)
    for index, vector in enumerate(vectors):
        current = vector["battery_mode"]
        for later in range(index + 1, len(vectors)):
            if starts[later] >= starts[index] + dwell:
                break
            following = vectors[later]["battery_mode"]
            if index:
                preceding = vectors[index - 1]["battery_mode"]
                model.constrain({current: 1, preceding: -1, following: -1}, -np.inf, 0)
                model.constrain({current: -1, preceding: 1, following: 1}, -np.inf, 1)
            elif previous is None:
                model.constrain({following: 1, current: -1}, 0, 0)
            else:
                mode = int(previous == "charge")
                model.constrain({current: 1, following: -1}, -np.inf, mode)
                model.constrain({current: -1, following: 1}, -np.inf, 1 - mode)


def validate_directions(problem: Problem, modes: list[str]) -> None:
    """Independently reject early switches, including a carried commitment."""
    if not problem.battery or not problem.minimum_mode_minutes:
        return
    dwell = timedelta(minutes=problem.minimum_mode_minutes)
    previous = problem.initial_battery_mode
    since = problem.initial_battery_mode_since
    for slot, mode in zip(problem.slots, modes, strict=True):
        start = slot.start.astimezone(UTC)
        if previous is None:
            since = start
        elif mode != previous:
            if start < since.astimezone(UTC) + dwell:
                raise SolveError(
                    "solver_failure", "invalid solver result: minimum mode duration"
                )
            since = start
        previous = mode
