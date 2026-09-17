from collections.abc import Mapping
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from ..config_models import LoadSource, resolve_numeric
from ..engine.forecast import forecast_load
from ..engine.models import ForecastSettings, InputError
from ..engine.normalize import aware, finite, resample_energy
from .bindings import parse_intervals, parse_timestamp


def counter_statistics_to_hours(
    samples: list[tuple[Any, Any]] | tuple[tuple[Any, Any], ...],
    *,
    unit: str = "kWh",
    sign: float = 1.0,
    observed_before: datetime | None = None,
) -> tuple[tuple[datetime, float], ...]:
    """Convert complete cumulative-counter hours, skipping a reset crossing."""
    scale = 0.001 if unit == "Wh" else 1.0 if unit == "kWh" else None
    if scale is None:
        raise InputError("unsupported history energy unit")
    direction = finite(sign, "history sign")
    if direction == 0:
        raise InputError("history sign cannot be zero")
    ordered = sorted(
        (parse_timestamp(time), finite(value, "counter value"))
        for time, value in samples
    )
    result = []
    for (start, before), (end, after) in pairwise(ordered):
        if observed_before is not None and end > aware(
            observed_before, "observed before"
        ):
            continue
        if (
            end - start != timedelta(hours=1)
            or start.minute
            or start.second
            or start.microsecond
        ):
            continue
        delta = (after - before) * direction * scale
        if delta < 0:
            continue
        result.append((start, delta))
    return tuple(result)


def recorder_statistics_to_hours(
    statistics: tuple[Mapping[str, Any], ...],
    *,
    unit: str = "kWh",
    sign: float = 1.0,
    observed_before: datetime | None = None,
) -> tuple[tuple[datetime, float], ...]:
    """Use recorder's reset-adjusted hourly sum, never raw counter subtraction."""
    scale = 0.001 if unit == "Wh" else 1.0 if unit == "kWh" else None
    if scale is None:
        raise InputError("unsupported statistic energy unit")
    direction = finite(sign, "history sign")
    if direction == 0:
        raise InputError("history sign cannot be zero")
    ordered = sorted((parse_timestamp(row["start"]), row) for row in statistics)
    result = []
    for (start, first), (end, second) in pairwise(ordered):
        if end - start != timedelta(hours=1):
            continue
        if observed_before is not None and end + timedelta(hours=1) > aware(
            observed_before, "observed before"
        ):
            continue
        if first.get("sum") is None or second.get("sum") is None:
            continue
        delta = (
            (
                finite(second["sum"], "statistic sum")
                - finite(first["sum"], "statistic sum")
            )
            * direction
            * scale
        )
        if delta < 0:
            raise InputError("recorder sum decreased")
        # Each recorder row ends at the last five-minute sum within its hour.
        result.append((end, delta))
    return tuple(result)


def power_samples_to_hours(
    samples: tuple[tuple[Any, Any], ...],
    *,
    unit: str = "W",
    sign: float = 1.0,
    observed_before: datetime | None = None,
    max_gap_minutes: float = 60.0,
) -> tuple[tuple[datetime, float], ...]:
    """Integrate measured power only across complete, bounded hours."""
    scale = 0.001 if unit == "W" else 1.0 if unit == "kW" else None
    if scale is None:
        raise InputError("unsupported history power unit")
    direction = finite(sign, "history sign")
    maximum_gap = finite(max_gap_minutes, "power sample gap")
    if maximum_gap <= 0:
        raise InputError("power sample gap limit must be positive")
    ordered = sorted(
        (parse_timestamp(time), None if value is None else finite(value, "power"))
        for time, value in samples
    )
    totals: dict[datetime, float] = {}
    coverage: dict[datetime, float] = {}
    for (start, before), (end, _) in pairwise(ordered):
        if end <= start:
            raise InputError("power samples must increase")
        if before is None:
            continue
        if (end - start).total_seconds() > maximum_gap * 60:
            raise InputError("power sample gap exceeds configured limit")
        if observed_before is not None and end > aware(
            observed_before, "observed before"
        ):
            continue
        power = before * scale * direction
        if power < 0:
            raise InputError("household load cannot be negative")
        cursor = start
        while cursor < end:
            hour = cursor.replace(minute=0, second=0, microsecond=0)
            section_end = min(end, hour + timedelta(hours=1))
            seconds = (section_end - cursor).total_seconds()
            totals[hour] = totals.get(hour, 0.0) + power * seconds / 3600
            coverage[hour] = coverage.get(hour, 0.0) + seconds
            cursor = section_end
    return tuple(
        (hour, totals[hour])
        for hour in sorted(totals)
        if abs(coverage[hour] - 3600) < 1e-6
    )


def load_for_slots(
    source: LoadSource,
    states: Mapping[str, Any],
    now: datetime,
    slots: tuple[tuple[datetime, datetime], ...],
    timezone: str,
    settings: ForecastSettings,
    *,
    statistics: tuple[Mapping[str, Any], ...] = (),
    power_samples: tuple[tuple[Any, Any], ...] = (),
    fallback_daily_kwh: float | None = None,
) -> tuple[float, ...]:
    """Forecast from the explicitly selected load source and fallback policy."""
    aware(now, "now")
    if source.mode == "forecast":
        if source.forecast is None:
            raise InputError("forecast load source missing")
        series = parse_intervals(states, source.forecast, now)
        if any(row.value < 0 for row in series):
            raise InputError("household load cannot be negative")
        return resample_energy(series, slots)
    if source.mode == "daily_estimate":
        if source.daily_estimate is None:
            raise InputError("daily estimate missing")
        daily = resolve_numeric(source.daily_estimate, states, now)
        if daily < 0:
            raise InputError("daily load must be nonnegative")
        return tuple(
            daily * (end - start).total_seconds() / 86400 for start, end in slots
        )
    if source.mode == "recorder":
        if source.statistic_id:
            history = recorder_statistics_to_hours(
                statistics,
                unit=source.history_unit,
                sign=source.history_sign,
                observed_before=now,
            )
        elif source.power:
            history = power_samples_to_hours(
                power_samples,
                unit=source.history_unit,
                sign=source.history_sign,
                observed_before=now,
                max_gap_minutes=source.power_max_gap_minutes,
            )
        else:
            raise InputError("recorder load source missing")
        return forecast_load(history, slots, timezone, fallback_daily_kwh, settings)
    raise InputError("invalid load source mode")
