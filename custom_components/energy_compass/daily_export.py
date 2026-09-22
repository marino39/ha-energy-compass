"""Observed counters for the current local day's export budget."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config_models import NumericSetting, resolve_numeric
from .engine.models import InputError
from .engine.normalize import finite
from .sources.bindings import parse_timestamp

DAILY_EXPORT_MEASUREMENTS = ("pv_energy_today", "grid_export_energy_today")
# An inverter resets its daily totals on its own clock and an integration reports
# them on its own poll, so the first minutes of a local day can still carry
# yesterday's totals, with yesterday's or even today's timestamp.
ROLLOVER_GRACE = timedelta(minutes=30)
# Counter resolution and poll jitter on top of the physical accumulation bound.
ROLLOVER_SLACK_KWH = 0.2


def daily_export_active(values: dict) -> bool:
    """Counters are required when the enabled policy can constrain export."""
    return values["limit_export_to_pv"] and values["grid_export_kw"] > 0


def daily_export_observations(config, states, values, now):
    """Read real daily totals; missing history must never become a fresh budget."""
    if not daily_export_active(values):
        return 0.0, 0.0, None
    zone = ZoneInfo(config["timezone"])
    today = now.astimezone(zone).date()
    midnight = datetime.combine(today, time.min, tzinfo=zone).astimezone(UTC)
    elapsed_hours = max(0.0, (now - midnight).total_seconds() / 3600)
    deadline = datetime.combine(
        today + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(UTC)
    # Largest energy each counter can physically hold since local midnight. PV
    # may exceed the AC rating, so its bound is deliberately generous.
    export_kw = values["grid_export_kw"]
    possible = {
        "pv_energy_today": 2 * max(values["inverter_kw"], export_kw) * elapsed_hours,
        "grid_export_energy_today": export_kw * elapsed_hours,
    }
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
        if stamp > now:
            raise InputError(f"{name} must have a report from the current local day")
        reset = (
            stamp.astimezone(zone).date() == today
            and value <= possible[name] + ROLLOVER_SLACK_KWH
        )
        if not reset:
            if now - midnight >= ROLLOVER_GRACE:
                if stamp.astimezone(zone).date() != today:
                    raise InputError(
                        f"{name} must have a report from the current local day"
                    )
                raise InputError(f"{name} did not reset for the current local day")
            # Until the counter rolls over, assume the conservative end of what
            # today can hold: no PV yet, and as much export as was possible.
            # Both shrink the export budget; neither invents one.
            observed.append(0.0 if name == "pv_energy_today" else possible[name])
            deadline = min(deadline, midnight + ROLLOVER_GRACE)
            continue
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
