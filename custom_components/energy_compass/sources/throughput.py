"""Validation for AC-side charge-plus-discharge energy used by a cycle limit."""

from datetime import datetime
from zoneinfo import ZoneInfo

from ..config_models import NumericSetting, resolve_numeric
from ..engine.models import InputError
from .bindings import parse_timestamp


def resolve_daily_throughput(
    config: dict, values: dict, states: dict, now: datetime
) -> float:
    """Return today's AC-side charge-plus-discharge total in kWh."""
    selected = config.get("measurements", {}).get("throughput_today")
    if (
        not selected
        or not selected.get("entity")
        or selected.get("statistic_id")
        or selected.get("_missing_registry")
    ):
        raise InputError(
            "daily throughput requires an available measurement entity in Sources"
        )
    try:
        setting = NumericSetting.from_dict(selected)
        source_unit = setting.source_unit or setting.unit
        if setting.unit != "kWh" or source_unit not in ("kWh", "Wh"):
            raise InputError("source unit must be kWh or Wh")
        factor = 0.001 if source_unit == "Wh" else 1
        if setting.multiplier != factor:
            raise InputError("source unit must be converted to kWh exactly once")
        observed = resolve_numeric(setting, states, now)
        if observed < 0:
            raise InputError("measurement must be nonnegative")
        updated = parse_timestamp(states[setting.entity.entity_id]["last_updated"])
        zone = ZoneInfo(config["timezone"])
        if updated.astimezone(zone).date() != now.astimezone(zone).date():
            raise InputError("measurement must be from the current local date")
        return observed
    except (KeyError, ValueError, TypeError, InputError) as err:
        raise InputError(f"daily throughput {err}") from err
