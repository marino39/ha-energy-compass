"""Standby energy the inverter takes from the battery in every interval.

A hybrid inverter keeps its DC side alive from the battery whenever the pack
is connected, so a parked battery loses energy even when the plan commands
neither charge nor discharge. The draw is not routed through the inverter's
discharge-current limit and it does not serve site load, so it is neither a
`discharge_kwh` nor a `load_kwh` term: it is a deterministic leak on the SOC
balance.

Modelling it as a constant per interval keeps the program linear, but a
constant would drive SOC below zero once the pack empties. The schedule below
is therefore capped by the "do nothing" trajectory — the SOC the battery would
hold if the plan never charged or discharged. That trajectory is always a
feasible solution, so the capped schedule can never make the program
infeasible, and once the pack is empty the modelled leak correctly stops.
"""

from datetime import UTC

from .models import Problem


def drain_schedule(problem: Problem) -> tuple[float, ...]:
    """Per-slot standby loss in kWh, capped by the do-nothing SOC trajectory."""
    battery = problem.battery
    if battery is None or battery.idle_drain_kw <= 0:
        return tuple(0.0 for _ in problem.slots)
    remaining = battery.initial_kwh
    schedule = []
    for slot in problem.slots:
        hours = (
            slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
        ).total_seconds() / 3600
        drain = min(battery.idle_drain_kw * hours, max(remaining, 0.0))
        remaining -= drain
        schedule.append(drain)
    return tuple(schedule)


def cumulative(schedule: tuple[float, ...]) -> tuple[float, ...]:
    """Running total of the schedule, aligned with the slot it ends on."""
    total = 0.0
    totals = []
    for drain in schedule:
        total += drain
        totals.append(total)
    return tuple(totals)
