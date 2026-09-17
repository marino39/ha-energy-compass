from datetime import UTC, datetime

import pytest

from custom_components.energy_compass.engine.forecast import forecast_load
from custom_components.energy_compass.engine.models import ForecastSettings, InputError


def stamp(day, hour, month=9, fold=0):
    from zoneinfo import ZoneInfo

    return datetime(2026, month, day, hour, tzinfo=ZoneInfo("Europe/Warsaw"), fold=fold)


def test_local_weekday_hour_mean_and_partial_interval():
    history = ((stamp(15, 10), 1.0), (stamp(16, 10), 3.0))
    slots = (
        (
            stamp(17, 10),
            stamp(
                17,
                10,
            ).replace(minute=30),
        ),
    )
    assert forecast_load(
        history, slots, "Europe/Warsaw", None, ForecastSettings(minimum_samples=2)
    ) == (1.0,)


def test_fallback_requires_value_and_policy():
    slots = ((stamp(17, 10), stamp(17, 11)),)
    assert forecast_load((), slots, "Europe/Warsaw", 24, ForecastSettings()) == (1.0,)
    with pytest.raises(InputError, match="insufficient"):
        forecast_load(
            (), slots, "Europe/Warsaw", 24, ForecastSettings(allow_fallback=False)
        )


def test_lookback_excludes_old_and_future_history():
    history = ((stamp(1, 10), 10.0), (stamp(17, 10), 20.0))
    slots = ((stamp(17, 10), stamp(17, 11)),)
    assert forecast_load(
        history, slots, "Europe/Warsaw", 24, ForecastSettings(lookback_days=14)
    ) == (1.0,)


def test_dst_spring_23_and_autumn_25_distinct_hours():
    spring_slots = (
        (datetime(2026, 3, 28, 23, tzinfo=UTC), datetime(2026, 3, 29, 22, tzinfo=UTC)),
    )
    autumn_slots = (
        (
            datetime(2026, 10, 24, 22, tzinfo=UTC),
            datetime(2026, 10, 25, 23, tzinfo=UTC),
        ),
    )
    assert forecast_load((), spring_slots, "Europe/Warsaw", 24, ForecastSettings()) == (
        23.0,
    )
    assert forecast_load((), autumn_slots, "Europe/Warsaw", 24, ForecastSettings()) == (
        25.0,
    )
    assert stamp(25, 2, 10, 0).astimezone(UTC) != stamp(25, 2, 10, 1).astimezone(UTC)
