"""Incremental consumption costs and advisory display queries."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil, floor, isfinite
from time import perf_counter
from zoneinfo import ZoneInfo

from .models import (
    CompassSettings,
    Flow,
    InputError,
    Level,
    MachineState,
    Opportunity,
    Plan,
    Problem,
    Slot,
    SolveError,
    Window,
)
from .normalize import aware, validate_problem
from .optimize import solve

_UTC = UTC
_TOL = 1e-6
_MAX_PROBES = 96


@dataclass(frozen=True)
class ConsumptionAnalysis:
    opportunities: tuple[Opportunity, ...]
    classification_mode: str
    coverage_reason: str
    reference_complete: bool


def _finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise InputError(f"{name} must be finite") from err
    if not isfinite(number):
        raise InputError(f"{name} must be finite")
    return number


def _settings(settings: CompassSettings, budget_s: float) -> float:
    if not isinstance(settings, CompassSettings):
        raise InputError("invalid compass settings")
    for name in ("display_horizon_hours", "reference_horizon_hours"):
        value = getattr(settings, name)
        if type(value) is not int or not 1 <= value <= 48:
            raise InputError(f"{name} must be an integer from 1 to 48")
    interval = settings.display_interval_minutes
    if type(interval) is not int or interval not in (15, 30, 60):
        raise InputError("display interval must be 15, 30, or 60 minutes")
    horizon = max(settings.display_horizon_hours, settings.reference_horizon_hours)
    if ceil(horizon * 60 / interval) > _MAX_PROBES:
        raise InputError("horizon and interval require too many probes")
    probe = _finite(settings.probe_kwh, "probe_kwh")
    if not 0 < probe <= 5:
        raise InputError("probe_kwh must be in (0, 5]")
    for name in ("boost_ceiling", "limit_floor"):
        _finite(getattr(settings, name), name)
    if settings.boost_ceiling >= settings.limit_floor:
        raise InputError("boost ceiling must be below limit floor")
    cheap = _finite(settings.cheap_percentile, "cheap_percentile")
    limit = _finite(settings.limit_percentile, "limit_percentile")
    if not 0 <= cheap <= limit <= 100:
        raise InputError("percentiles must be ordered within [0, 100]")
    if settings.short_coverage not in ("absolute_fallback", "unavailable"):
        raise InputError("invalid short-coverage policy")
    probe_time = _finite(settings.probe_time_limit_s, "probe_time_limit_s")
    if not 0 < probe_time <= 10:
        raise InputError("probe_time_limit_s must be in (0, 10]")
    budget = _finite(budget_s, "budget_s")
    if not 0 < budget <= 60:
        raise InputError("budget_s must be in (0, 60]")
    return budget


def classify_cost(
    cost: float,
    *,
    q25: float | None,
    q75: float | None,
    boost_ceiling: float = 0.05,
    limit_floor: float = 0.80,
) -> Level:
    """Classify a finite incremental cost using absolute and percentile limits."""
    value = _finite(cost, "cost")
    boost = _finite(boost_ceiling, "boost_ceiling")
    limit = _finite(limit_floor, "limit_floor")
    if boost >= limit:
        raise InputError("boost ceiling must be below limit floor")
    low = None if q25 is None else _finite(q25, "q25")
    high = None if q75 is None else _finite(q75, "q75")
    if (low is None) != (high is None):
        raise InputError("both percentile values must be supplied together")
    if low is not None and low > high:
        raise InputError("percentile values must be ordered")
    if value <= boost:
        return "BOOST"
    if value > limit and (high is None or value > high):
        return "LIMIT"
    if low is not None and value < low:
        return "CHEAP"
    return "NORMAL"


def _percentile(values: tuple[float, ...], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = floor(position)
    upper = ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _intervals(
    start: datetime, hours: int, minutes: int
) -> tuple[tuple[datetime, datetime], ...]:
    cursor = start.astimezone(_UTC)
    end = cursor + timedelta(hours=hours)
    width = timedelta(minutes=minutes)
    intervals = []
    while cursor < end:
        following = min(cursor + width, end)
        intervals.append((cursor, following))
        cursor = following
    return tuple(intervals)


def _probe(
    problem: Problem,
    baseline: Plan,
    start: datetime,
    end: datetime,
    amount: float,
    deadline: float,
) -> float | None:
    elapsed = (end - start).total_seconds()
    additions = []
    for slot in problem.slots:
        slot_start = slot.start.astimezone(_UTC)
        slot_end = slot.end.astimezone(_UTC)
        overlap = max(
            0.0, (min(end, slot_end) - max(start, slot_start)).total_seconds()
        )
        additions.append(amount * overlap / elapsed)
    actual = sum(additions)
    if abs(actual - amount) > max(_TOL, amount * 1e-9):
        return None
    slots = tuple(
        replace(slot, load_kwh=slot.load_kwh + extra)
        for slot, extra in zip(problem.slots, additions, strict=True)
    )
    remaining = deadline - perf_counter()
    if remaining <= 0:
        return None
    try:
        candidate = solve(replace(problem, slots=slots), time_limit_s=remaining)
    except SolveError:
        return None
    if perf_counter() >= deadline:
        return None
    cost = (candidate.objective - baseline.objective) / actual
    return cost if isfinite(cost) and perf_counter() < deadline else None


def analyze_consumption(
    problem: Problem, plan: Plan, *, settings: CompassSettings, budget_s: float = 50.0
) -> ConsumptionAnalysis:
    """Probe display and reference intervals under one serial time budget."""
    budget = _settings(settings, budget_s)
    validate_problem(problem)
    if len(plan.flows) != len(problem.slots) or not isfinite(plan.objective):
        raise InputError("baseline plan does not match problem")
    start = problem.slots[0].start
    display = _intervals(
        start, settings.display_horizon_hours, settings.display_interval_minutes
    )
    reference = _intervals(
        start, settings.reference_horizon_hours, settings.display_interval_minutes
    )
    requested = tuple(dict.fromkeys((*reference, *display)))
    if len(requested) > _MAX_PROBES:
        raise InputError("horizon and interval require too many probes")
    source_end = problem.slots[-1].end.astimezone(_UTC)
    zone = ZoneInfo(problem.timezone)
    costs: dict[tuple[datetime, datetime], float | None] = {}
    deadline = perf_counter() + budget
    for interval_start, interval_end in requested:
        if interval_end > source_end:
            costs[(interval_start, interval_end)] = None
            continue
        probe_start = perf_counter()
        remaining = deadline - probe_start
        if remaining <= 0:
            costs[(interval_start, interval_end)] = None
            continue
        probe_deadline = min(deadline, probe_start + settings.probe_time_limit_s)
        costs[(interval_start, interval_end)] = _probe(
            problem,
            plan,
            interval_start,
            interval_end,
            settings.probe_kwh,
            probe_deadline,
        )
    reference_costs = tuple(costs[item] for item in reference)
    complete = all(cost is not None for cost in reference_costs)
    if complete:
        mode = "percentile"
        reason = "complete"
        known = tuple(cost for cost in reference_costs if cost is not None)
        low = _percentile(known, settings.cheap_percentile)
        high = _percentile(known, settings.limit_percentile)
    else:
        mode = settings.short_coverage
        reason = (
            "reference_horizon_uncovered"
            if any(end > source_end for _, end in reference)
            else "reference_probe_failed"
        )
        low = high = None
    opportunities = tuple(
        Opportunity(
            interval_start.astimezone(zone),
            interval_end.astimezone(zone),
            costs[(interval_start, interval_end)],
            (
                None
                if costs[(interval_start, interval_end)] is None
                or mode == "unavailable"
                else classify_cost(
                    costs[(interval_start, interval_end)],
                    q25=low,
                    q75=high,
                    boost_ceiling=settings.boost_ceiling,
                    limit_floor=settings.limit_floor,
                )
            ),
        )
        for interval_start, interval_end in display
    )
    return ConsumptionAnalysis(opportunities, mode, reason, complete)


def consumption_outlook(
    problem: Problem, plan: Plan, *, settings: CompassSettings, budget_s: float = 50.0
) -> tuple[Opportunity, ...]:
    """Return incremental consumption opportunities at display resolution."""
    return analyze_consumption(
        problem, plan, settings=settings, budget_s=budget_s
    ).opportunities


def merge_windows(
    opportunities: tuple[Opportunity, ...], levels: frozenset[Level]
) -> tuple[Window, ...]:
    """Join adjacent selected levels, retaining each observed level in order."""
    windows: list[Window] = []
    for item in opportunities:
        if item.level not in levels:
            continue
        if windows and windows[-1].end.astimezone(_UTC) == item.start.astimezone(_UTC):
            previous = windows[-1]
            labels = (
                previous.levels
                if item.level in previous.levels
                else (*previous.levels, item.level)
            )
            windows[-1] = Window(previous.start, item.end, labels)
        else:
            windows.append(Window(item.start, item.end, (item.level,)))
    return tuple(windows)


def next_transition(
    opportunities: tuple[Opportunity, ...], now: datetime
) -> Opportunity | None:
    """Return the next different known level before the first unknown interval."""
    instant = aware(now, "now").astimezone(_UTC)
    current = next(
        (
            index
            for index, item in enumerate(opportunities)
            if item.start.astimezone(_UTC) <= instant < item.end.astimezone(_UTC)
        ),
        None,
    )
    if current is None:
        return None
    level = opportunities[current].level
    if level is None:
        return None
    previous_end = opportunities[current].end.astimezone(_UTC)
    for item in opportunities[current + 1 :]:
        if item.start.astimezone(_UTC) != previous_end or item.level is None:
            return None
        if item.level != level:
            return item
        previous_end = item.end.astimezone(_UTC)
    return None


def next_window(windows: tuple[Window, ...], now: datetime) -> Window | None:
    """Return an active or upcoming window, including its original start."""
    instant = aware(now, "now").astimezone(_UTC)
    return next(
        (window for window in windows if window.end.astimezone(_UTC) > instant), None
    )


def machine_state(slot: Slot, flow: Flow) -> MachineState:
    """Choose one display state by curtail, charge, export, discharge precedence."""
    if flow.curtail_kwh > _TOL:
        return "CURTAIL"
    if flow.charge_kwh > max(slot.pv_kwh - slot.load_kwh, 0) + _TOL:
        return "CHARGE_GRID"
    if flow.charge_kwh > _TOL:
        return "CHARGE_PV"
    if flow.discharge_kwh > _TOL and flow.grid_export_kwh > _TOL:
        return "DISCHARGE_GRID"
    if flow.discharge_kwh > _TOL:
        return "SELF_CONSUME"
    return "HOLD"


def machine_snapshot(slot: Slot, flow: Flow) -> dict[str, float | MachineState]:
    """Pair a display state with the aggregate flows it summarizes."""
    return {
        "state": machine_state(slot, flow),
        "pv_kwh": slot.pv_kwh,
        "load_kwh": slot.load_kwh,
        "grid_import_kwh": flow.grid_import_kwh,
        "grid_export_kwh": flow.grid_export_kwh,
        "charge_kwh": flow.charge_kwh,
        "discharge_kwh": flow.discharge_kwh,
        "curtail_kwh": flow.curtail_kwh,
        "end_soc_kwh": flow.end_soc_kwh,
    }


def serialize_opportunity(item: Opportunity) -> dict[str, str | float | None]:
    """Serialize an opportunity with its original timestamp offsets."""
    return {
        "start": item.start.isoformat(),
        "end": item.end.isoformat(),
        "cost_per_kwh": item.cost_per_kwh,
        "level": item.level,
    }
