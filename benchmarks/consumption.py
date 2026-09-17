"""Measure serial incremental-load probes in the matched ARM64 runtime."""

import argparse
import json
import resource
from time import perf_counter

from benchmarks.optimizer import make_problem
from custom_components.energy_compass.engine.consumption import analyze_consumption
from custom_components.energy_compass.engine.models import CompassSettings, SolveError
from custom_components.energy_compass.engine.optimize import solve


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario", choices=("default", "nondefault"), default="default"
    )
    args = parser.parse_args()
    if args.scenario == "default":
        problem = make_problem(96)
        settings = CompassSettings()
    else:
        problem = make_problem(144)
        settings = CompassSettings(
            display_horizon_hours=12,
            display_interval_minutes=30,
            reference_horizon_hours=36,
            probe_kwh=0.5,
            cheap_percentile=20,
            limit_percentile=80,
        )
    started = perf_counter()
    try:
        plan = solve(problem, time_limit_s=10.0)
        base_elapsed = perf_counter() - started
        analysis = analyze_consumption(
            problem, plan, settings=settings, budget_s=max(0.001, 60 - base_elapsed)
        )
    except SolveError as err:
        result = {"result": err.reason}
    else:
        known = sum(item.cost_per_kwh is not None for item in analysis.opportunities)
        result = {
            "result": "complete"
            if analysis.reference_complete and known == len(analysis.opportunities)
            else "partial",
            "classification_mode": analysis.classification_mode,
            "coverage_reason": analysis.coverage_reason,
            "display_intervals": len(analysis.opportunities),
            "known_display_intervals": known,
            "base_solve_elapsed_s": base_elapsed,
        }
    result.update(
        {
            "scenario": args.scenario,
            "elapsed_s": perf_counter() - started,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
