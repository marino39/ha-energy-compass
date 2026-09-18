from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import InputError, Problem


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    value: float


def finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise InputError(f"{name} must be numeric") from err
    if not isfinite(number):
        raise InputError(f"{name} must be finite")
    return number


def aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InputError(f"{name} must be timezone-aware")
    return value


def rce_to_kwh(value: float, unit: str) -> float:
    price = finite(value, "price")
    if unit in ("PLN/MWh", "EUR/MWh"):
        return price / 1000
    if unit in ("PLN/kWh", "EUR/kWh"):
        return price
    raise InputError(f"unsupported price unit: {unit}")


def _validated(intervals: tuple[Interval, ...]) -> tuple[Interval, ...]:
    ordered = sorted(intervals, key=lambda row: row.start)
    for index, row in enumerate(ordered):
        aware(row.start, "interval start")
        aware(row.end, "interval end")
        finite(row.value, "interval value")
        if row.end <= row.start:
            raise InputError("interval end must follow start")
        if index and row.start < ordered[index - 1].end:
            raise InputError("overlapping source intervals")
    return tuple(ordered)


def _resample(
    intervals: tuple[Interval, ...],
    slots: tuple[tuple[datetime, datetime], ...],
    kind: str,
) -> tuple[float, ...]:
    source = _validated(intervals)
    result = []
    for start, end in slots:
        aware(start, "slot start")
        aware(end, "slot end")
        if end <= start:
            raise InputError("slot end must follow start")
        covered = 0.0
        total = 0.0
        prices = set()
        for row in source:
            overlap = (min(end, row.end) - max(start, row.start)).total_seconds()
            if overlap <= 0:
                continue
            covered += overlap
            if kind == "energy":
                total += row.value * overlap / (row.end - row.start).total_seconds()
            elif kind == "power":
                total += row.value * overlap / 3600
            else:
                prices.add(row.value)
        if abs(covered - (end - start).total_seconds()) > 1e-6:
            raise InputError("source coverage gap")
        if kind == "price":
            if len(prices) != 1:
                raise InputError("slot crosses different settlement prices")
            total = prices.pop()
        result.append(total)
    return tuple(result)


def resample_energy(
    intervals: tuple[Interval, ...], slots: tuple[tuple[datetime, datetime], ...]
) -> tuple[float, ...]:
    """Allocate interval energy to covered slots by elapsed time."""
    return _resample(intervals, slots, "energy")


def resample_power(
    intervals: tuple[Interval, ...], slots: tuple[tuple[datetime, datetime], ...]
) -> tuple[float, ...]:
    """Integrate interval-average power over covered slots."""
    return _resample(intervals, slots, "power")


def resample_prices(
    intervals: tuple[Interval, ...], slots: tuple[tuple[datetime, datetime], ...]
) -> tuple[float, ...]:
    """Require each requested slot to have one fully covered settlement price."""
    return _resample(intervals, slots, "price")


def validate_problem(problem: Problem) -> None:
    """Reject invalid physical, chronological, or nonfinite optimization inputs."""
    if not problem.slots:
        raise InputError("at least one slot is required")
    try:
        ZoneInfo(problem.timezone)
    except (ZoneInfoNotFoundError, TypeError) as err:
        raise InputError("invalid timezone") from err
    for name in ("inverter_kw", "grid_import_kw", "grid_export_kw"):
        if finite(getattr(problem.site, name), name) < 0:
            raise InputError(f"{name} must be nonnegative")
    if not isinstance(problem.site.allow_curtailment, bool):
        raise InputError("curtailment setting must be boolean")
    previous_end_utc = None
    for slot in problem.slots:
        aware(slot.start, "slot start")
        aware(slot.end, "slot end")
        start_utc = slot.start.astimezone(UTC)
        end_utc = slot.end.astimezone(UTC)
        if end_utc <= start_utc or (
            previous_end_utc is not None and start_utc != previous_end_utc
        ):
            raise InputError("slots must be contiguous and increasing")
        previous_end_utc = end_utc
        for name in ("buy_per_kwh", "sell_per_kwh", "pv_kwh", "load_kwh"):
            value = finite(getattr(slot, name), name)
            if name in ("pv_kwh", "load_kwh") and value < 0:
                raise InputError(f"{name} must be nonnegative")
    if problem.terminal_mode not in ("preserve_initial", "value"):
        raise InputError("invalid terminal mode")
    finite(problem.terminal_value_per_kwh, "terminal value")
    if not 0 <= finite(problem.minimum_mode_minutes, "minimum_mode_minutes") <= 1440:
        raise InputError("minimum_mode_minutes must be in [0, 1440]")
    if problem.initial_battery_mode not in (None, "charge", "discharge"):
        raise InputError("invalid initial battery mode")
    if (problem.initial_battery_mode is None) != (
        problem.initial_battery_mode_since is None
    ):
        raise InputError("initial battery mode requires its start time")
    if problem.initial_battery_mode_since is not None:
        aware(problem.initial_battery_mode_since, "initial battery mode start")
        if problem.initial_battery_mode_since.astimezone(UTC) > problem.slots[
            0
        ].start.astimezone(UTC):
            raise InputError("initial battery mode starts in the future")
    if problem.battery is not None:
        battery = problem.battery
        if finite(battery.capacity_kwh, "capacity_kwh") <= 0:
            raise InputError("capacity_kwh must be positive")
        for name in ("charge_kw", "discharge_kw"):
            if finite(getattr(battery, name), name) < 0:
                raise InputError(f"{name} must be nonnegative")
        for name in ("maximum_soc_fraction", "eta_charge", "eta_discharge"):
            value = finite(getattr(battery, name), name)
            if not 0 < value <= 1:
                raise InputError(f"{name} must be in (0, 1]")
        floor = finite(battery.minimum_soc_fraction, "minimum_soc_fraction")
        if not 0 <= floor < 1:
            raise InputError("battery floor must be in [0, 1)")
        if battery.minimum_soc_fraction >= battery.maximum_soc_fraction:
            raise InputError("battery floor must be below ceiling")
        initial = finite(battery.initial_kwh, "initial_kwh")
        if (
            not battery.capacity_kwh * battery.minimum_soc_fraction
            <= initial
            <= battery.capacity_kwh * battery.maximum_soc_fraction
        ):
            raise InputError("initial SOC outside bounds")
        if finite(battery.wear_per_kwh, "wear_per_kwh") < 0:
            raise InputError("wear cost must be nonnegative")
        if not isinstance(battery.allow_grid_charge, bool) or not isinstance(
            battery.allow_battery_export, bool
        ):
            raise InputError("battery capabilities must be boolean")
        if type(battery.prevent_grid_energy_export) is not bool:
            raise InputError("grid energy export policy must be boolean")
    for day, value in problem.remaining_daily_throughput_kwh:
        try:
            canonical = date.fromisoformat(day).isoformat()
        except (TypeError, ValueError) as err:
            raise InputError("invalid throughput date") from err
        if day != canonical:
            raise InputError("invalid throughput date")
        if finite(value, "remaining throughput") < 0:
            raise InputError("remaining throughput must be nonnegative")
