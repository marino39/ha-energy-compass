# tests/test_balance_engine.py
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.balance import (
    candidate_windows,
    lift_slots,
    miss_cost,
)
from custom_components.energy_compass.engine.models import (
    BalanceWindow,
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.normalize import validate_problem

T0 = datetime(2026, 9, 24, 0, tzinfo=UTC)


def _slots(rows, minutes=60, start=T0):
    return tuple(
        Slot(
            start + timedelta(minutes=minutes * i),
            start + timedelta(minutes=minutes * (i + 1)),
            1.0,
            0.5,
            pv,
            load,
        )
        for i, (pv, load) in enumerate(rows)
    )


def _windows(slots, phase="eligible", ok=None, now=T0, hold=60, remaining=0):
    return candidate_windows(
        slots,
        phase=phase,
        now=now,
        hold_minutes=hold,
        remaining_minutes=remaining,
        grid_charge_ok=ok or (True,) * len(slots),
    )


def test_ok_phase_has_no_windows():
    assert _windows(_slots([(0, 1)] * 4), phase="ok") == ()


def test_pv_window_when_pv_covers_load_everywhere():
    windows = _windows(_slots([(2, 1), (0, 1)]))
    assert windows[0] == BalanceWindow((0,), "CHARGE_PV")
    assert windows[1] == BalanceWindow((1,), "CHARGE_GRID")


def test_mixed_window_is_grid_labelled():
    windows = _windows(_slots([(2, 1), (0, 1)]), hold=120)
    assert windows == (BalanceWindow((0, 1), "CHARGE_GRID"),)


def test_mixed_window_dropped_without_grid_charge():
    assert _windows(_slots([(2, 1), (0, 1)]), hold=120, ok=(True, False)) == ()


def test_only_hour_aligned_starts_with_quarter_slots():
    slots = _slots([(0, 1)] * 8, minutes=15)
    windows = _windows(slots)
    assert [w.slots for w in windows] == [(0, 1, 2, 3), (4, 5, 6, 7)]


def test_window_needs_full_hold_inside_horizon():
    assert _windows(_slots([(0, 1)] * 2), hold=180) == ()


def test_due_limits_starts_to_24_hours():
    slots = _slots([(0, 1)] * 48)
    starts = [w.slots[0] for w in _windows(slots, phase="due")]
    assert starts == list(range(24))


def test_holding_is_one_window_at_slot_zero_for_remaining_minutes():
    slots = _slots([(0, 1)] * 8, minutes=15, start=T0 + timedelta(minutes=5))
    windows = _windows(slots, phase="holding", remaining=20)
    assert windows == (BalanceWindow((0, 1), "CHARGE_GRID"),)


@pytest.mark.parametrize(
    "phase,days,expected",
    [
        ("ok", 0, 0),
        ("eligible", 0, 0.5),
        ("due", 0, 5),
        ("due", 2, 15),
        ("holding", 0, 50),
    ],
)
def test_miss_cost(phase, days, expected):
    assert miss_cost(phase, 5.0, days) == expected


def _battery(**changes):
    return replace(
        Battery(25.0, 0.1, 0.9, 20.0, 8, 8, 1.0, 1.0, 0.0, True, True), **changes
    )


def test_lift_runs_from_the_slot_before_the_first_window_to_the_end():
    windows = (BalanceWindow((3, 4), "CHARGE_GRID"), BalanceWindow((5,), "CHARGE_PV"))
    assert lift_slots(windows, _battery(), 7) == (2, 3, 4, 5, 6)


def test_lift_covers_everything_when_initial_above_ceiling():
    assert lift_slots((), _battery(initial_kwh=24.0), 3) == (0, 1, 2)


def test_no_lift_without_windows():
    assert lift_slots((), _battery(), 3) == ()


def _problem(**changes):
    slots = _slots([(0, 1)] * 4)
    return replace(
        Problem(
            slots, SiteLimits(8, 25, 8, False), _battery(), "value", 0.6, (), "UTC"
        ),
        **changes,
    )


def test_initial_above_ceiling_accepted_while_balance_active():
    problem = _problem(
        battery=_battery(initial_kwh=24.9),
        balance_threshold_kwh=24.75,
        balance_lift_slots=(0,),
    )
    validate_problem(problem)
    with pytest.raises(InputError, match="initial SOC outside bounds"):
        validate_problem(
            replace(problem, balance_threshold_kwh=0.0, balance_lift_slots=())
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"balance_windows": (BalanceWindow((0, 2), "CHARGE_PV"),)},
        {"balance_windows": (BalanceWindow((0,), "HOLD"),)},
        {"balance_windows": (BalanceWindow((3, 4), "CHARGE_PV"),)},
        {"balance_windows": (BalanceWindow((), "CHARGE_PV"),)},
        {"balance_threshold_kwh": 26.0},
        {"balance_miss_cost": -1.0},
        {"balance_fixed": True},
        {"balance_lift_slots": (9,)},
    ],
)
def test_invalid_balance_fields(changes):
    base = {"balance_threshold_kwh": 24.75}
    with pytest.raises(InputError):
        validate_problem(_problem(**{**base, **changes}))


def test_balance_without_battery_rejected():
    with pytest.raises(InputError):
        validate_problem(_problem(battery=None, balance_threshold_kwh=1.0))
