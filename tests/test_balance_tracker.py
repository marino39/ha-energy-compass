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
S = BalanceSettings(
    interval_days=7, hold_minutes=60, threshold_percent=99, max_gap_seconds=600
)
# Recorder history stores changes only: a pinned SOC produces no samples.
SEED = BalanceSettings(
    interval_days=7, hold_minutes=60, threshold_percent=99, max_gap_seconds=None
)


def _feed(readings, state=None, settings=S):
    state = state or empty_state()
    for minutes, soc in readings:
        state = observe(state, soc, T0 + timedelta(minutes=minutes), settings)
    return state


def _full(first, last, step=5, soc=100):
    """Fresh readings every `step` minutes, as the health poll delivers them."""
    return [(minute, soc) for minute in range(first, last + 1, step)]


def test_hold_completes_after_uninterrupted_minutes():
    state = _feed([(0, 99), *_full(5, 60), (61, 100)])
    assert state["last_completed_at"] == (T0 + timedelta(minutes=60)).isoformat()
    assert status(state, T0 + timedelta(minutes=61), S)["phase"] == "ok"


def test_dip_below_threshold_restarts_hold():
    state = _feed([*_full(0, 25), (30, 98), *_full(31, 76, soc=99), (80, 99)])
    assert state["last_completed_at"] is None
    assert state["hold_started_at"] == (T0 + timedelta(minutes=31)).isoformat()
    assert status(state, T0 + timedelta(minutes=80), S)["phase"] == "holding"


def test_unavailable_soc_freezes_hold():
    state = _feed([(0, 100), (20, None)])
    assert state["hold_started_at"] == T0.isoformat()


def test_completion_is_recognised_without_a_new_reading():
    state = _feed(_full(0, 55))
    report = status(state, T0 + timedelta(minutes=62), S)
    assert report["phase"] == "ok"
    assert report["last_completed"] == (T0 + timedelta(minutes=60)).isoformat()


def test_drop_after_long_hold_keeps_the_completion():
    # Readings every 5 min cover the hold, but the lazy completion was never
    # observed; a later drop still credits the covered hour.
    state = _feed([(0, 100)])
    for minute in range(5, 61, 5):
        state = observe(state, 100, T0 + timedelta(minutes=minute), S)
    state = observe(state, 95, T0 + timedelta(minutes=62), S)
    assert state["last_completed_at"] == (T0 + timedelta(minutes=60)).isoformat()
    assert state["hold_started_at"] is None


def test_gap_longer_than_hold_then_drop_gives_no_credit():
    # 100 % at 12:00 and 12:10, no data until 13:30, then 90 %.
    state = _feed([(0, 100), (10, 100), (90, 90)])
    assert state["last_completed_at"] is None
    assert state["hold_started_at"] is None


def test_full_reading_after_gap_restarts_the_hold():
    state = _feed([(0, 100), (10, 100), (40, 100)])
    assert state["last_completed_at"] is None
    assert state["hold_started_at"] == (T0 + timedelta(minutes=40)).isoformat()
    assert state["last_full_at"] == (T0 + timedelta(minutes=40)).isoformat()


def test_stale_hold_is_neither_holding_nor_completed():
    state = _feed([(0, 100), (10, 100)])
    report = status(state, T0 + timedelta(minutes=75), S)
    assert report["phase"] == "due"
    assert report["last_completed"] is None
    assert status(state, T0 + timedelta(minutes=30), S)["phase"] == "due"
    assert status(state, T0 + timedelta(minutes=20), S)["phase"] == "holding"


def test_fraction_just_under_threshold_counts_as_full():
    # 24.6 kWh pack: 99 % arrives as 98.99999999999999.
    percent = 24.354 / 24.6 * 100
    assert percent < 99
    state = observe(empty_state(), percent, T0, S)
    assert state["hold_started_at"] == T0.isoformat()


def test_state_without_last_full_loads_and_gets_no_gap_credit():
    legacy = {"last_completed_at": None, "hold_started_at": T0.isoformat()}
    assert status(legacy, T0 + timedelta(minutes=75), S)["phase"] == "due"
    state = observe(legacy, 100, T0 + timedelta(minutes=75), S)
    assert state["last_completed_at"] is None
    assert state["hold_started_at"] == (T0 + timedelta(minutes=75)).isoformat()


def test_status_reports_due_phase_ignoring_the_hold():
    recent = {
        "last_completed_at": (T0 - timedelta(days=1)).isoformat(),
        "hold_started_at": T0.isoformat(),
        "last_full_at": T0.isoformat(),
    }
    report = status(recent, T0 + timedelta(minutes=5), S)
    assert report["phase"] == "holding"
    assert report["due_phase"] == "ok"
    assert status(_feed([(0, 100)]), T0, S)["due_phase"] == "due"


@pytest.mark.parametrize(
    "days,phase,overdue",
    [(4.9, "ok", 0), (5.0, "eligible", 0), (7.0, "due", 0), (9.5, "due", 2)],
)
def test_phases(days, phase, overdue):
    state = {"last_completed_at": T0.isoformat(), "hold_started_at": None}
    report = status(state, T0 + timedelta(days=days), S)
    assert report["phase"] == phase
    assert report["due_phase"] == phase
    assert report["days_overdue"] == overdue
    assert report["next_due"] == (T0 + timedelta(days=7)).isoformat()


def test_never_completed_is_due():
    report = status(empty_state(), T0, S)
    assert report["phase"] == "due"
    assert report["last_completed"] is None
    assert report["days_overdue"] == 0


def test_holding_reports_progress_and_remaining():
    report = status(_feed(_full(0, 15)), T0 + timedelta(minutes=20), S)
    assert report["phase"] == "holding"
    assert report["hold_progress_minutes"] == 20
    assert report["hold_remaining_minutes"] == 40
    assert report["hold_required_minutes"] == 60


def test_short_interval_caps_the_eligible_lead():
    short = BalanceSettings(
        interval_days=1, hold_minutes=60, threshold_percent=99, max_gap_seconds=600
    )
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
    state = seed_from_history(reversed(samples), SEED)
    assert state["last_completed_at"] == (T0 + timedelta(hours=2)).isoformat()
    assert state["hold_started_at"] is None


def test_seed_counts_a_pinned_full_soc_across_sparse_history():
    # Changes-only history: 100 % for 3 h leaves one sample, then the drop.
    samples = [(T0, 100.0), (T0 + timedelta(hours=3), 90.0)]
    state = seed_from_history(samples, SEED)
    assert state["last_completed_at"] is not None
    assert state["hold_started_at"] is None
    # The live (gap-checked) settings would give no credit for the same data.
    assert seed_from_history(samples, S)["last_completed_at"] is None


def test_seed_breaks_a_hold_at_an_unavailable_sample():
    # History stores changes only, so an explicit unavailable sample means SOC
    # was unknown from then on; the gap must not count as a hold.
    samples = [
        (T0, 100.0),
        (T0 + timedelta(minutes=10), None),
        (T0 + timedelta(minutes=90), 90.0),
    ]
    assert seed_from_history(samples, SEED)["last_completed_at"] is None


def test_seed_keeps_a_hold_completed_before_an_unavailable_sample():
    samples = [
        (T0, 100.0),
        (T0 + timedelta(minutes=70), None),
        (T0 + timedelta(minutes=90), 90.0),
    ]
    state = seed_from_history(samples, SEED)
    assert state["last_completed_at"] == (T0 + timedelta(hours=1)).isoformat()


def test_seed_without_history_is_due():
    assert seed_from_history([], SEED) == empty_state()


@pytest.mark.parametrize(
    "raw,unit,expected", [(99, "%", 99), (0.5, "fraction", 50), (12.5, "kWh", 50)]
)
def test_soc_percent(raw, unit, expected):
    assert soc_percent(raw, unit, 25) == expected


def test_soc_percent_unknown_unit():
    assert soc_percent(1, "Wh", 25) is None


def test_settings_from_values_with_defaults():
    assert balance_settings({}) == S
    assert balance_settings({"soc_max_age_seconds": 120}).max_gap_seconds == 120


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
