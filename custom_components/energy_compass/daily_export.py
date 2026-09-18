"""Observed counters for the current local day's export budget."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config_models import NumericSetting, resolve_numeric
from .engine.models import InputError
from .engine.normalize import finite
from .sources.bindings import parse_timestamp

DAILY_EXPORT_MEASUREMENTS = ("pv_energy_today", "grid_export_energy_today")


def daily_export_active(values: dict) -> bool:
    """Counters are required when the enabled policy can constrain export."""
    return values["limit_export_to_pv"] and values["grid_export_kw"] > 0


def daily_export_observations(config, states, values, now):
    """Read real daily totals; missing history must never become a fresh budget."""
    if not daily_export_active(values):
        return 0.0, 0.0, None
    zone = ZoneInfo(config["timezone"])
    today = now.astimezone(zone).date()
    deadline = datetime.combine(
        today + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(UTC)
    observed = []
    for name in DAILY_EXPORT_MEASUREMENTS:
        selected = config.get("measurements", {}).get(name)
        if (
            not selected
            or not selected.get("entity")
            or selected.get("_missing_registry")
        ):
            raise InputError(f"Sell only PV requires a daily counter: {name}")
        setting = NumericSetting.from_dict(selected)
        if setting.unit != "kWh":
            raise InputError(f"{name} must resolve to kWh")
        value = resolve_numeric(replace(setting, max_age_seconds=None), states, now)
        if value < 0:
            raise InputError(f"{name} must be nonnegative")
        state = states[setting.entity.entity_id]
        stamp = parse_timestamp(
            state["last_reported"]
            if "last_reported" in state
            else state.get("last_updated")
        )
        if stamp > now or stamp.astimezone(zone).date() != today:
            raise InputError(f"{name} must have a report from the current local day")
        if setting.max_age_seconds is not None:
            max_age = finite(setting.max_age_seconds, f"{name} age limit")
            if max_age <= 0:
                raise InputError(f"{name} age limit must be positive")
            expires = stamp + timedelta(seconds=max_age)
            if expires <= now:
                raise InputError(f"stale daily counter: {name}")
            deadline = min(deadline, expires)
        observed.append(value)
    return *observed, deadline
