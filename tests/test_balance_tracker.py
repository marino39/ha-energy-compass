# tests/test_balance_tracker.py
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.balance_tracker import (
    BalanceSettings,
    balance_settings,
    empty_state,
    observe,
    planned_window,
    seed_from_history,
    sensor_state,
    soc_percent,
    status,
)

T0 = datetime(2026, 9, 24, 10, tzinfo=UTC)
S = BalanceSettings(interval_days=7, hold_minutes=60, threshold_percent=99)


def _feed(readings, state=None):
    state = state or empty_state()
    for minutes, soc in readings:
        state = observe(state, soc, T0 + timedelta(minutes=minutes), S)
    return state


def test_hold_completes_after_uninterrupted_minutes():
    state = _feed([(0, 99), (30, 100), (61, 100)])
    assert state["last_completed_at"] == (T0 + timedelta(minutes=60)).isoformat()
    assert status(state, T0 + timedelta(minutes=61), S)["phase"] == "ok"


def test_dip_below_threshold_restarts_hold():
    state = _feed([(0, 100), (30, 98), (31, 99), (80, 99)])
    assert state["last_completed_at"] is None
    assert state["hold_started_at"] == (T0 + timedelta(minutes=31)).isoformat()
    assert status(state, T0 + timedelta(minutes=80), S)["phase"] == "holding"


def test_unavailable_soc_freezes_hold():
    state = _feed([(0, 100), (20, None)])
    assert state["hold_started_at"] == T0.isoformat()


def test_completion_is_recognised_without_a_new_reading():
    state = _feed([(0, 100)])
    report = status(state, T0 + timedelta(minutes=75), S)
    assert report["phase"] == "ok"
    assert report["last_completed"] == (T0 + timedelta(minutes=60)).isoformat()


def test_drop_after_long_hold_keeps_the_completion():
    state = _feed([(0, 100), (90, 95)])
    assert state["last_completed_at"] == (T0 + timedelta(minutes=60)).isoformat()
    assert state["hold_started_at"] is None


@pytest.mark.parametrize(
    "days,phase,overdue",
    [(4.9, "ok", 0), (5.0, "eligible", 0), (7.0, "due", 0), (9.5, "due", 2)],
)
def test_phases(days, phase, overdue):
    state = {"last_completed_at": T0.isoformat(), "hold_started_at": None}
    report = status(state, T0 + timedelta(days=days), S)
    assert report["phase"] == phase
    assert report["days_overdue"] == overdue
    assert report["next_due"] == (T0 + timedelta(days=7)).isoformat()


def test_never_completed_is_due():
    report = status(empty_state(), T0, S)
    assert report["phase"] == "due"
    assert report["last_completed"] is None
    assert report["days_overdue"] == 0


def test_holding_reports_progress_and_remaining():
    report = status(_feed([(0, 100)]), T0 + timedelta(minutes=20), S)
    assert report["phase"] == "holding"
    assert report["hold_progress_minutes"] == 20
    assert report["hold_remaining_minutes"] == 40
    assert report["hold_required_minutes"] == 60


def test_short_interval_caps_the_eligible_lead():
    short = BalanceSettings(interval_days=1, hold_minutes=60, threshold_percent=99)
    state = {"last_completed_at": T0.isoformat(), "hold_started_at": None}
    assert status(state, T0 + timedelta(hours=11), short)["phase"] == "ok"
    assert status(state, T0 + timedelta(hours=12), short)["phase"] == "eligible"


def test_seed_from_history_finds_last_completion():
    samples = [
        (T0, 80.0),
        (T0 + timedelta(hours=1), 99.0),
        (T0 + timedelta(hours=2, minutes=5), 97.0),
        (T0 + timedelta(hours=3), None),
    ]
    state = seed_from_history(reversed(samples), S)
    assert state["last_completed_at"] == (T0 + timedelta(hours=2)).isoformat()
    assert state["hold_started_at"] is None


def test_seed_without_history_is_due():
    assert seed_from_history([], S) == empty_state()


@pytest.mark.parametrize(
    "raw,unit,expected", [(99, "%", 99), (0.5, "fraction", 50), (12.5, "kWh", 50)]
)
def test_soc_percent(raw, unit, expected):
    assert soc_percent(raw, unit, 25) == expected


def test_soc_percent_unknown_unit():
    assert soc_percent(1, "Wh", 25) is None


def test_settings_from_values_with_defaults():
    assert balance_settings({}) == S


def test_planned_window_is_first_contiguous_run():
    rows = [
        {"start": "a", "end": "b", "state": "HOLD"},
        {"start": "b", "end": "c", "state": "CHARGE_GRID", "balance_hold": True},
        {"start": "c", "end": "d", "state": "CHARGE_GRID", "balance_hold": True},
        {"start": "d", "end": "e", "state": "SELF_CONSUME"},
    ]
    assert planned_window(rows) == {"start": "b", "end": "d", "mode": "CHARGE_GRID"}
    assert planned_window(rows[:1]) is None


@pytest.mark.parametrize(
    "phase,scheduled,expected",
    [
        ("ok", False, "ok"),
        ("holding", True, "holding"),
        ("eligible", False, "eligible"),
        ("eligible", True, "scheduled"),
        ("due", True, "scheduled"),
        ("due", False, "overdue"),
    ],
)
def test_sensor_state(phase, scheduled, expected):
    assert sensor_state(phase, scheduled) == expected
