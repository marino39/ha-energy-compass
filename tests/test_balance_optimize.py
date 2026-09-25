from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.balance import (
    candidate_windows,
    lift_slots,
    pin_balance,
)
from custom_components.energy_compass.engine.models import (
    BalanceWindow,
    Battery,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve

T0 = datetime(2026, 9, 24, 0, tzinfo=UTC)
CAP = 25.0


def _slots(rows):
    return tuple(
        Slot(T0 + timedelta(hours=i), T0 + timedelta(hours=i + 1), buy, 0.1, pv, load)
        for i, (buy, pv, load) in enumerate(rows)
    )


def _problem(rows, *, phase="eligible", miss=5.0, initial=20.0, ceiling=1.0, hold=60):
    slots = _slots(rows)
    battery = Battery(CAP, 0.1, ceiling, initial, 8, 8, 1.0, 1.0, 0.0, True, False)
    base = Problem(
        slots,
        SiteLimits(8, 25, 8, False),
        battery,
        "value",
        0.0,
        (),
        "UTC",
        minimum_mode_minutes=15,
    )
    windows = candidate_windows(
        slots,
        phase=phase,
        now=T0,
        hold_minutes=hold,
        remaining_minutes=hold,
        grid_charge_ok=(True,) * len(slots),
    )
    return replace(
        base,
        balance_windows=windows,
        balance_threshold_kwh=0.99 * CAP,
        balance_miss_cost=miss,
        balance_lift_slots=lift_slots(windows, battery, len(slots)),
    )


def _held(problem, plan):
    window = next(
        w for w in problem.balance_windows if w.slots[0] == plan.balance_start
    )
    return window.slots


def test_chooses_cheapest_grid_hours_when_due():
    rows = [(0.9, 0, 0.5), (0.2, 0, 0.5), (0.2, 0, 0.5), (0.9, 0, 0.5)]
    problem = _problem(rows, phase="due", miss=50)
    plan = solve(problem)
    assert plan.balance_start is not None and not plan.balance_missed
    for t in _held(problem, plan):
        assert plan.flows[t].end_soc_kwh >= 0.99 * CAP - 1e-6
        assert plan.flows[t].discharge_kwh <= 1e-6
    assert plan.balance_start in (1, 2)


def test_skips_expensive_balance_when_only_eligible():
    rows = [(2.0, 0, 0.5)] * 4
    plan = solve(_problem(rows, miss=0.5))
    assert plan.balance_start is None and plan.balance_missed


def test_escalated_value_buys_the_balance():
    rows = [(2.0, 0, 0.5)] * 4
    plan = solve(_problem(rows, phase="due", miss=200))
    assert plan.balance_start is not None


def test_ok_phase_adds_no_variables():
    rows = [(0.5, 0, 0.5)] * 3
    counts = []
    original = optimize._Model.solve

    def spy(self, limit):
        counts.append((len(self.cost), len(self.rows)))
        return original(self, limit)

    plain = replace(
        _problem(rows),
        balance_windows=(),
        balance_threshold_kwh=0.0,
        balance_miss_cost=0.0,
        balance_lift_slots=(),
    )
    optimize._Model.solve = spy
    try:
        solve(plain)
        solve(replace(plain, balance_threshold_kwh=0.99 * CAP))
    finally:
        optimize._Model.solve = original
    assert counts[0] == counts[1]


def test_full_battery_holds_through_the_window_with_pv():
    rows = [(0.5, 3, 1)] * 3
    problem = _problem(rows, initial=CAP, miss=50)
    plan = solve(problem)
    assert plan.balance_start is not None
    for t in _held(problem, plan):
        assert plan.flows[t].discharge_kwh <= 1e-6
        assert plan.flows[t].end_soc_kwh >= 0.99 * CAP - 1e-6


def test_ceiling_below_threshold_is_lifted_inside_the_window():
    rows = [(0.2, 0, 0.5), (0.2, 0, 0.5), (0.9, 0, 0.5)]
    problem = _problem(rows, phase="due", miss=50, ceiling=0.9, initial=20)
    plan = solve(problem)
    # Charge at 0.2 in slot 0, hold slot 1, spend the surplus in slot 2.
    assert plan.balance_start == 1
    assert plan.flows[0].end_soc_kwh >= 0.99 * CAP - 1e-6
    assert problem.balance_lift_slots == (0, 1, 2)


def test_holding_start_below_threshold_is_missed_not_infeasible():
    rows = [(0.5, 0, 0.5)] * 3
    problem = _problem(rows, phase="holding", initial=20, miss=50)
    plan = solve(problem)
    assert plan.balance_missed


def test_pin_balance_fixes_the_chosen_window():
    rows = [(0.9, 0, 0.5), (0.2, 0, 0.5), (0.2, 0, 0.5), (0.9, 0, 0.5)]
    problem = _problem(rows, phase="due", miss=50)
    plan = solve(problem)
    pinned = pin_balance(problem, plan)
    assert pinned.balance_fixed and len(pinned.balance_windows) == 1
    assert pinned.balance_windows[0].slots[0] == plan.balance_start
    assert pinned.balance_lift_slots == problem.balance_lift_slots
    again = solve(pinned)
    assert again.balance_start == plan.balance_start
    assert again.objective == pytest.approx(plan.objective, abs=1e-6)


def test_pin_balance_drops_windows_when_missed():
    rows = [(2.0, 0, 0.5)] * 4
    problem = _problem(rows, miss=0.5)
    plan = solve(problem)
    pinned = pin_balance(problem, plan)
    assert pinned.balance_windows == () and pinned.balance_miss_cost == 0


def test_validator_rejects_discharge_inside_a_chosen_window():
    problem = replace(
        _problem([(0.5, 0, 0.5)] * 2, initial=CAP),
        balance_windows=(BalanceWindow((0,), "CHARGE_GRID"),),
    )
    vectors = [{"energy": 0, "bd": 1}, {"energy": 2, "bd": 3}]
    values = np.array([CAP, 0.2, CAP, 0.0, 1.0, 0.0])  # choice at 4, miss at 5
    with pytest.raises(SolveError, match="balance discharge"):
        optimize._validate_balance(problem, vectors, values, [4], 5)


def test_validator_rejects_a_fractional_choice():
    problem = replace(
        _problem([(0.5, 0, 0.5)] * 2, initial=CAP),
        balance_windows=(BalanceWindow((0,), "CHARGE_GRID"),),
    )
    vectors = [{"energy": 0, "bd": 1}, {"energy": 2, "bd": 3}]
    values = np.array([CAP, 0.0, CAP, 0.0, 0.5, 0.5])
    with pytest.raises(SolveError, match="fractional balance choice"):
        optimize._validate_balance(problem, vectors, values, [4], 5)


def test_fixed_window_rejects_invalid_start():
    rows = [(0.5, 0, 0.5)] * 2
    problem = replace(
        _problem(rows, initial=10),
        balance_windows=(BalanceWindow((0,), "CHARGE_GRID"),),
        balance_fixed=True,
    )
    with pytest.raises(SolveError):
        solve(problem)
