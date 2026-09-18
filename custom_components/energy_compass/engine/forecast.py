from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    ForecastSettings,
    InputError,
    LoadCoverageSegment,
    LoadForecastResult,
)
from .normalize import aware, finite


def forecast_load(
    history: tuple[tuple[datetime, float], ...],
    slots: tuple[tuple[datetime, datetime], ...],
    timezone: str,
    fallback_daily_kwh: float | None,
    settings: ForecastSettings,
) -> tuple[float, ...]:
    """Predict interval energy from complete local-hour weekday/weekend means."""
    return forecast_load_with_quality(
        history, slots, timezone, fallback_daily_kwh, settings
    ).values


def forecast_load_with_quality(
    history: tuple[tuple[datetime, float], ...],
    slots: tuple[tuple[datetime, datetime], ...],
    timezone: str,
    fallback_daily_kwh: float | None,
    settings: ForecastSettings,
) -> LoadForecastResult:
    """Keep each history decision with the predicted interval energy."""
    try:
        local = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError) as err:
        raise InputError("invalid forecast timezone") from err
    if settings.lookback_days <= 0 or settings.minimum_samples <= 0:
        raise InputError("invalid forecast sample settings")
    if not slots:
        return LoadForecastResult((), ())
    first = aware(slots[0][0], "slot start").astimezone(UTC)
    cutoff = first - timedelta(days=settings.lookback_days)
    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    seen = set()
    for start, energy in history:
        instant = aware(start, "history timestamp").astimezone(UTC)
        amount = finite(energy, "history energy")
        if amount < 0:
            raise InputError("history energy must be nonnegative")
        if instant in seen:
            raise InputError("duplicate history hour")
        seen.add(instant)
        if cutoff <= instant and instant + timedelta(hours=1) <= first:
            wall = instant.astimezone(local)
            group = (
                int(wall.weekday() >= 5) if settings.group_weekends else wall.weekday()
            )
            buckets[(group, wall.hour)].append(amount)
    if fallback_daily_kwh is not None:
        fallback = finite(fallback_daily_kwh, "fallback daily load")
        if fallback < 0:
            raise InputError("fallback daily load must be nonnegative")
    else:
        fallback = None
    result = []
    coverage = []
    for start, end in slots:
        cursor = aware(start, "slot start").astimezone(UTC)
        stop = aware(end, "slot end").astimezone(UTC)
        if stop <= cursor:
            raise InputError("slot end must follow start")
        energy = 0.0
        while cursor < stop:
            next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
            section_end = min(next_hour, stop)
            wall = cursor.astimezone(local)
            group = (
                int(wall.weekday() >= 5) if settings.group_weekends else wall.weekday()
            )
            samples = buckets[(group, wall.hour)]
            if len(samples) >= settings.minimum_samples:
                hourly = sum(samples) / len(samples)
                method = "history"
            elif settings.allow_fallback and fallback is not None:
                hourly = fallback / 24
                method = "fallback"
            else:
                raise InputError(
                    f"insufficient load history at {cursor.isoformat()}: "
                    f"{len(samples)}/{settings.minimum_samples} samples available/required"
                )
            energy += hourly * (section_end - cursor).total_seconds() / 3600
            coverage.append(
                LoadCoverageSegment(
                    cursor, section_end, method, len(samples), settings.minimum_samples
                )
            )
            cursor = section_end
        result.append(energy)
    return LoadForecastResult(tuple(result), tuple(coverage))
