"""Deterministic fixtures for the `cost_min` golden snapshot.

Coder's choice (plan Step 0): the `problem()` / `battery()` helper style from
`tests/test_optimize.py` is duplicated here, rather than imported, so this module has
no dependency on the test suite and can run standalone under `generate.py` inside the
read-only container. Never edit an existing fixture; add a new one instead.
"""

from datetime import UTC, datetime, timedelta
from math import sqrt

from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
)

_START = datetime(2026, 9, 17, tzinfo=UTC)

# Hourly buy price, 0..23h: night-cheap, evening-expensive, clear single peak.
_HOURLY_BUY = (
    0.32,
    0.30,
    0.28,
    0.27,
    0.29,
    0.35,
    0.45,
    0.55,
    0.60,
    0.58,
    0.55,
    0.52,
    0.50,
    0.48,
    0.50,
    0.55,
    0.65,
    0.80,
    0.95,
    0.98,
    0.90,
    0.75,
    0.55,
    0.40,
)
# Hourly PV generation (kWh), 0..23h: single midday bump, zero at night.
_HOURLY_PV = (
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.2,
    0.8,
    1.8,
    2.6,
    3.0,
    3.2,
    3.0,
    2.6,
    1.8,
    0.8,
    0.2,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
)
_HOURLY_LOAD = (1.0,) * 24


def _problem(rows, *, battery=None, site=None, start=None, timezone_name="UTC"):
    start = start or _START
    slots = tuple(
        Slot(slot_start, slot_end, buy, sell, pv, load)
        for (slot_start, slot_end), (buy, sell, pv, load) in rows
    )
    return Problem(
        slots,
        site or SiteLimits(8, 8, 8, True),
        battery,
        "preserve_initial",
        0.0,
        (),
        timezone_name,
    )


def _hourly_rows(start):
    return [
        (
            (start + timedelta(hours=hour), start + timedelta(hours=hour + 1)),
            (
                _HOURLY_BUY[hour],
                0.6 * _HOURLY_BUY[hour],
                _HOURLY_PV[hour],
                _HOURLY_LOAD[hour],
            ),
        )
        for hour in range(24)
    ]


def _quarter_hourly_rows(start):
    rows = []
    for hour in range(24):
        buy = _HOURLY_BUY[hour]
        sell = 0.6 * buy
        pv = _HOURLY_PV[hour] / 4
        load = _HOURLY_LOAD[hour] / 4
        for quarter in range(4):
            offset = timedelta(hours=hour, minutes=15 * quarter)
            rows.append(
                (
                    (start + offset, start + offset + timedelta(minutes=15)),
                    (buy, sell, pv, load),
                )
            )
    return rows


def _battery_24h() -> Problem:
    battery = Battery(13.5, 0.1, 0.9, 6.0, 3.3, 3.3, 0.94, 0.94, 0.05, True, True)
    return _problem(_hourly_rows(_START), battery=battery)


def _battery_96q() -> Problem:
    battery = Battery(13.5, 0.1, 0.9, 6.0, 3.3, 3.3, 0.94, 0.94, 0.05, True, True)
    return _problem(_quarter_hourly_rows(_START), battery=battery)


def _grid_only() -> Problem:
    # Mirrors tests/conftest.py:grid_only_problem exactly.
    rows = [
        (
            (_START + timedelta(hours=index), _START + timedelta(hours=index + 1)),
            (0.60, -0.10, 0.0, 1.0),
        )
        for index in range(2)
    ]
    battery = Battery(10.0, 0.2, 0.8, 5.0, 0.0, 0.0, 1.0, 1.0, 0.0, False, False)
    return _problem(
        rows,
        battery=battery,
        site=SiteLimits(0.0, 3.0, 0.0, False),
        timezone_name="Europe/Warsaw",
    )


def _negative_price() -> Problem:
    # Mirrors tests/conftest.py:negative_price_problem exactly.
    prices = ((-0.50, -0.20), (0.40, 0.30))
    rows = [
        (
            (_START + timedelta(hours=index), _START + timedelta(hours=index + 1)),
            (buy, sell, 0.0, 0.5),
        )
        for index, (buy, sell) in enumerate(prices)
    ]
    battery = Battery(
        25.0, 0.2, 1.0, 15.0, 8.0, 8.0, sqrt(0.88), sqrt(0.88), 0.14, True, True
    )
    return _problem(rows, battery=battery, timezone_name="Europe/Warsaw")


def golden_problems() -> dict[str, Problem]:
    """Deterministic fixtures, keyed by a stable name. Never edit an existing fixture."""
    return {
        "battery_24h": _battery_24h(),
        "battery_96q": _battery_96q(),
        "grid_only": _grid_only(),
        "negative_price": _negative_price(),
    }
