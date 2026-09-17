"""Measure deterministic advisory solve workloads in the target runtime image."""

import argparse
import json
import resource
from datetime import UTC, datetime, timedelta
from math import pi, sin
from time import perf_counter

from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve


def make_problem(count: int) -> Problem:
    start = datetime(2026, 9, 17, tzinfo=UTC)
    slots = []
    for index in range(count):
        hour = (index % 96) / 4
        pv = 1.1 * max(0.0, sin(pi * (hour - 6) / 12))
        load = 0.38 + (0.18 if 6 <= hour < 9 or 17 <= hour < 22 else 0)
        buy = 0.09 if hour < 6 else 0.85 if 17 <= hour < 21 else 0.32
        sell = 0.07 if hour < 17 else 0.22
        slots.append(
            Slot(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                buy,
                sell,
                pv,
                load,
            )
        )
    battery = Battery(13.5, 0.1, 0.9, 6.75, 3.3, 3.3, 0.94, 0.94, 0.08, True, False)
    return Problem(
        tuple(slots),
        SiteLimits(5, 8, 8, True),
        battery,
        "preserve_initial",
        0,
        (),
        "Europe/Warsaw",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slots", type=int, choices=(192, 384), default=192)
    parser.add_argument("--time-limit", type=float, default=10.0)
    args = parser.parse_args()
    source = make_problem(args.slots)
    started = perf_counter()
    try:
        plan = solve(source, time_limit_s=args.time_limit)
    except SolveError as err:
        outcome = {"result": err.reason}
    else:
        outcome = {
            "result": "optimal",
            "objective": plan.objective,
            "flow_count": len(plan.flows),
        }
    outcome.update(
        {
            "slots": args.slots,
            "time_limit_s": args.time_limit,
            "elapsed_s": perf_counter() - started,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
    )
    print(json.dumps(outcome, sort_keys=True))


if __name__ == "__main__":
    main()
