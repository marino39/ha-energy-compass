# custom_components/energy_compass/balance_tracker.py
"""Periodic LFP balance bookkeeping: pure state, no Home Assistant imports.

A balance counts once SOC stays at or above the threshold for the whole hold.
Completion is evaluated lazily, so an unchanged full SOC completes the hold
without needing another state event.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from .settings import NUMBERS

SENSOR_STATES = ("ok", "eligible", "scheduled", "holding", "overdue")
_LEAD = timedelta(days=2)


@dataclass(frozen=True)
class BalanceSettings:
    interval_days: int
    hold_minutes: int
    threshold_percent: float

    @property
    def interval(self) -> timedelta:
        return timedelta(days=self.interval_days)

    @property
    def hold(self) -> timedelta:
        return timedelta(minutes=self.hold_minutes)

    @property
    def lead(self) -> timedelta:
        return min(_LEAD, self.interval / 2)


def balance_settings(values: Mapping) -> BalanceSettings:
    def get(key):
        return values.get(key, NUMBERS[key][1])

    return BalanceSettings(
        int(get("balance_interval_days")),
        int(get("balance_hold_minutes")),
        float(get("balance_soc_threshold")),
    )


def empty_state() -> dict:
    return {"last_completed_at": None, "hold_started_at": None}


def _time(raw: str | None) -> datetime | None:
    return None if raw is None else datetime.fromisoformat(raw)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def observe(
    state: dict, soc_percent: float | None, at: datetime, settings: BalanceSettings
) -> dict:
    """Advance the hold with one SOC reading; None (unavailable) freezes it."""
    if soc_percent is None:
        return dict(state)
    last = _time(state.get("last_completed_at"))
    started = _time(state.get("hold_started_at"))
    if started is not None and at - started >= settings.hold:
        last = max(filter(None, (last, started + settings.hold)))
        # Still full: the next hold continues from this completion.
        started = last if soc_percent >= settings.threshold_percent else None
        return {"last_completed_at": _iso(last), "hold_started_at": _iso(started)}
    if soc_percent >= settings.threshold_percent:
        return {"last_completed_at": _iso(last), "hold_started_at": _iso(started or at)}
    return {"last_completed_at": _iso(last), "hold_started_at": None}


def _completed(state: dict, now: datetime, settings: BalanceSettings):
    last = _time(state.get("last_completed_at"))
    started = _time(state.get("hold_started_at"))
    if started is not None and now - started >= settings.hold:
        candidate = started + settings.hold
        return candidate if last is None or candidate > last else last
    return last


def _holding(state: dict, now: datetime, settings: BalanceSettings) -> bool:
    last = _time(state.get("last_completed_at"))
    started = _time(state.get("hold_started_at"))
    return (
        started is not None
        and (last is None or started > last)
        and now - started < settings.hold
    )


def status(state: dict, now: datetime, settings: BalanceSettings) -> dict:
    last = _completed(state, now, settings)
    holding = _holding(state, now, settings)
    if holding:
        phase = "holding"
    elif last is None or now - last >= settings.interval:
        phase = "due"
    elif now - last >= settings.interval - settings.lead:
        phase = "eligible"
    else:
        phase = "ok"
    overdue = 0
    if last is not None and now - last >= settings.interval:
        overdue = (now - last - settings.interval).days
    progress = 0.0
    if holding:
        started = _time(state["hold_started_at"])
        progress = round((now - started).total_seconds() / 60, 1)
    return {
        "phase": phase,
        "last_completed": _iso(last),
        "next_due": _iso(last + settings.interval) if last else None,
        "days_overdue": overdue,
        "hold_progress_minutes": progress,
        "hold_remaining_minutes": round(settings.hold_minutes - progress, 1)
        if holding
        else 0.0,
        "hold_required_minutes": settings.hold_minutes,
        "threshold_percent": settings.threshold_percent,
    }


def seed_from_history(
    samples: Iterable[tuple[datetime, float | None]], settings: BalanceSettings
) -> dict:
    """Replay recorder SOC history; no qualifying hold means due now."""
    state = empty_state()
    for at, value in sorted(samples, key=lambda item: item[0]):
        state = observe(state, value, at, settings)
    return state


def soc_percent(raw: float, unit: str, capacity_kwh: float) -> float | None:
    if unit == "%":
        return float(raw)
    if unit == "fraction":
        return float(raw) * 100
    if unit == "kWh":
        return float(raw) / capacity_kwh * 100
    return None


def planned_window(rows: list[dict]) -> dict | None:
    first = last = None
    for row in rows:
        if row.get("balance_hold") is True:
            first = first or row
            last = row
        elif first is not None:
            break
    if first is None:
        return None
    return {"start": first["start"], "end": last["end"], "mode": first["state"]}


def sensor_state(phase: str, scheduled: bool) -> str:
    if phase in ("ok", "holding"):
        return phase
    if scheduled:
        return "scheduled"
    return "overdue" if phase == "due" else "eligible"
