from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.consumption import (
    analyze_consumption,
    classify_cost,
    consumption_outlook,
    machine_snapshot,
    machine_state,
    merge_windows,
    next_transition,
    next_window,
    serialize_opportunity,
)
from custom_components.energy_compass.engine.models import (
    CompassSettings,
    Flow,
    InputError,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve

START = datetime(2026, 9, 17, tzinfo=UTC)


def grid_problem(prices, *, start=START, slot_minutes=60):
    slots = tuple(
        Slot(
            start + timedelta(minutes=index * slot_minutes),
            start + timedelta(minutes=(index + 1) * slot_minutes),
            price,
            -0.10,
            0.0,
            0.25,
        )
        for index, price in enumerate(prices)
    )
    return Problem(
        slots,
        SiteLimits(0.0, 10.0, 0.0, False),
        None,
        "preserve_initial",
        0.0,
        (),
        "Europe/Warsaw",
    )


def test_classification_boundaries_and_flat_distribution():
    assert classify_cost(0.05, q25=0.05, q75=0.90) == "BOOST"
    assert classify_cost(0.19, q25=0.20, q75=0.90) == "CHEAP"
    assert classify_cost(0.20, q25=0.20, q75=0.90) == "NORMAL"
    assert classify_cost(0.90, q25=0.20, q75=0.90) == "NORMAL"
    assert classify_cost(0.91, q25=0.20, q75=0.90) == "LIMIT"
    assert classify_cost(0.40, q25=0.20, q75=0.35) == "NORMAL"
    assert classify_cost(0.40, q25=0.40, q75=0.40) == "NORMAL"
    assert classify_cost(0.90, q25=0.90, q75=0.90) == "NORMAL"
    assert classify_cost(0.90, q25=1.20, q75=1.20) == "CHEAP"
    assert classify_cost(0.80, q25=None, q75=None) == "NORMAL"
    assert classify_cost(0.90, q25=None, q75=None) == "LIMIT"


def test_display_beyond_flat_reference_uses_strict_percentile_comparisons():
    source = grid_problem((1.20, 0.90))
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=2, reference_horizon_hours=1),
    )
    assert result.reference_complete
    assert result.classification_mode == "percentile"
    assert tuple(item.level for item in result.opportunities) == ("NORMAL", "CHEAP")


def test_negative_sell_does_not_make_grid_consumption_free(grid_only_problem):
    outlook = consumption_outlook(
        grid_only_problem,
        solve(grid_only_problem),
        settings=CompassSettings(display_horizon_hours=2, reference_horizon_hours=2),
    )
    assert abs(outlook[0].cost_per_kwh - 0.60) < 1e-6


def test_negative_import_price_produces_negative_marginal_cost():
    source = grid_problem((-0.20,))
    outlook = consumption_outlook(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=1, reference_horizon_hours=1),
    )
    assert outlook[0].cost_per_kwh == pytest.approx(-0.20)
    assert outlook[0].level == "BOOST"


def test_transition_windows_and_active_window():
    source = grid_problem((0.40, 0.00, 0.40, 1.00))
    outlook = consumption_outlook(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=4, reference_horizon_hours=4),
    )
    assert tuple(item.level for item in outlook) == (
        "NORMAL",
        "BOOST",
        "NORMAL",
        "LIMIT",
    )
    assert next_transition(outlook, START) == outlook[1]
    assert (
        next_transition(outlook, START + timedelta(hours=1, minutes=30)) == outlook[2]
    )
    windows = merge_windows(outlook, frozenset({"BOOST", "CHEAP"}))
    assert len(windows) == 1
    assert windows[0].start == START + timedelta(hours=1)
    assert next_window(windows, START + timedelta(hours=1, minutes=30)) == windows[0]
    assert next_window(windows, START + timedelta(hours=2)) is None


def test_no_future_window_or_transition_across_unknown_gap():
    source = grid_problem((0.40, 0.00))
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=4, reference_horizon_hours=4),
    )
    assert len(result.opportunities) == 4
    assert tuple(item.level for item in result.opportunities) == (
        "NORMAL",
        "BOOST",
        None,
        None,
    )
    assert result.classification_mode == "absolute_fallback"
    assert result.coverage_reason == "reference_horizon_uncovered"
    assert next_transition(result.opportunities, START + timedelta(hours=1)) is None
    assert (
        next_window(merge_windows(result.opportunities, frozenset({"LIMIT"})), START)
        is None
    )


def test_reference_extends_past_display_and_custom_percentiles():
    source = grid_problem((0.40, 0.60, 0.10, 1.00))
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(
            display_horizon_hours=2,
            reference_horizon_hours=4,
            cheap_percentile=50,
            limit_percentile=50,
            limit_floor=0.50,
        ),
    )
    assert len(result.opportunities) == 2
    assert result.classification_mode == "percentile"
    assert result.coverage_reason == "complete"
    assert tuple(item.level for item in result.opportunities) == ("CHEAP", "LIMIT")


def test_short_coverage_unavailable_keeps_known_cost_but_no_level():
    source = grid_problem((0.30, 0.90))
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(
            display_horizon_hours=2,
            reference_horizon_hours=3,
            short_coverage="unavailable",
        ),
    )
    assert result.classification_mode == "unavailable"
    assert tuple(item.level for item in result.opportunities) == (None, None)
    assert result.opportunities[0].cost_per_kwh == pytest.approx(0.30)


def test_failed_probe_stays_unknown_under_absolute_fallback(monkeypatch):
    source = grid_problem((0.30, 0.90))
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    original = consumption.solve

    def fail_second(problem, **kwargs):
        if problem.slots[1].load_kwh > source.slots[1].load_kwh:
            raise SolveError("timeout")
        return original(problem, **kwargs)

    monkeypatch.setattr(consumption, "solve", fail_second)
    result = analyze_consumption(
        source,
        baseline,
        settings=CompassSettings(display_horizon_hours=2, reference_horizon_hours=2),
    )
    assert result.classification_mode == "absolute_fallback"
    assert result.coverage_reason == "reference_probe_failed"
    assert result.opportunities[1].cost_per_kwh is None
    assert result.opportunities[1].level is None
    assert result.opportunities[0].level == "NORMAL"


@pytest.mark.parametrize(
    "budget_s,probe_limit_s,elapsed_s",
    [
        (1.0, 2.0, 2.0),
        (5.0, 0.5, 1.0),
    ],
)
def test_probe_completed_after_deadline_stays_unknown(
    monkeypatch, budget_s, probe_limit_s, elapsed_s
):
    source = grid_problem((0.40,))
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    clock = [0.0]
    calls = []

    def late_solve(problem, *, time_limit_s):
        calls.append(time_limit_s)
        clock[0] += elapsed_s
        return replace(baseline, objective=baseline.objective + 0.40)

    monkeypatch.setattr(consumption, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(consumption, "solve", late_solve)
    result = analyze_consumption(
        source,
        baseline,
        settings=CompassSettings(
            display_horizon_hours=1,
            reference_horizon_hours=1,
            probe_time_limit_s=probe_limit_s,
        ),
        budget_s=budget_s,
    )
    assert len(calls) == 1
    assert calls[0] <= min(budget_s, probe_limit_s)
    assert not result.reference_complete
    assert result.coverage_reason == "reference_probe_failed"
    assert result.opportunities[0].cost_per_kwh is None
    assert result.opportunities[0].level is None


def test_probe_spreads_load_by_utc_overlap_and_uses_actual_probe_size(monkeypatch):
    source = grid_problem(
        (0.60, 0.60), start=START + timedelta(minutes=30), slot_minutes=30
    )
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    original = consumption.solve
    observed = []

    def record(problem, **kwargs):
        assert problem.battery == source.battery
        assert problem.terminal_mode == source.terminal_mode
        assert problem.terminal_value_per_kwh == source.terminal_value_per_kwh
        assert problem.slots[0].start == source.slots[0].start
        observed.append(
            tuple(
                slot.load_kwh - prior.load_kwh
                for slot, prior in zip(problem.slots, source.slots, strict=True)
            )
        )
        return original(problem, **kwargs)

    monkeypatch.setattr(consumption, "solve", record)
    outlook = consumption_outlook(
        source,
        baseline,
        settings=CompassSettings(
            display_horizon_hours=1,
            display_interval_minutes=60,
            reference_horizon_hours=1,
            probe_kwh=0.4,
        ),
    )
    assert len(observed) == 1
    assert observed[0] == pytest.approx((0.2, 0.2))
    assert tuple(item.cost_per_kwh for item in outlook) == pytest.approx((0.6,))
    assert tuple((item.end - item.start).total_seconds() for item in outlook) == (3600,)


def test_repeated_local_hour_and_offset_serialization():
    start = datetime(2026, 10, 25, 0, tzinfo=UTC)
    source = grid_problem((0.20, 0.30, 0.40), start=start)
    outlook = consumption_outlook(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=3, reference_horizon_hours=3),
    )
    assert len(outlook) == 3
    assert serialize_opportunity(outlook[0])["start"].endswith("+02:00")
    assert serialize_opportunity(outlook[1])["start"].endswith("+01:00")
    assert outlook[0].start.hour == outlook[1].start.hour == 2
    assert all(
        (item.end.astimezone(UTC) - item.start.astimezone(UTC)).total_seconds() == 3600
        for item in outlook
    )


@pytest.mark.parametrize(
    "settings",
    [
        CompassSettings(display_horizon_hours=49),
        CompassSettings(display_interval_minutes=7),
        CompassSettings(probe_kwh=0),
        CompassSettings(cheap_percentile=80, limit_percentile=20),
        CompassSettings(reference_horizon_hours=48, display_interval_minutes=15),
    ],
)
def test_unsupported_settings_fail_before_probing(settings):
    source = grid_problem((0.20,))
    with pytest.raises(InputError):
        consumption_outlook(source, solve(source), settings=settings)


@pytest.mark.parametrize(
    "flow,pv,load,expected",
    [
        (Flow(0, 0, 0, 0, 1, 0), 2, 1, "CURTAIL"),
        (Flow(1, 0, 1, 0, 0, 0), 1, 1, "CHARGE_GRID"),
        (Flow(0, 0, 1, 0, 0, 0), 2, 1, "CHARGE_PV"),
        (Flow(0, 1, 0, 1, 0, 0), 1, 1, "DISCHARGE_GRID"),
        (Flow(0, 0, 0, 1, 0, 0), 1, 2, "SELF_CONSUME"),
        (Flow(1, 0, 0, 0, 0, 0), 1, 2, "HOLD"),
    ],
)
def test_machine_state_precedence(flow, pv, load, expected):
    slot = Slot(START, START + timedelta(hours=1), 0.20, 0.10, pv, load)
    assert machine_state(slot, flow) == expected
    snapshot = machine_snapshot(slot, flow)
    assert snapshot["state"] == expected
    assert snapshot["grid_import_kwh"] == flow.grid_import_kwh
    assert snapshot["discharge_kwh"] == flow.discharge_kwh


@pytest.mark.parametrize("hours,minutes", [(24, 60), (48, 30)])
def test_rolling_bins_preserve_complete_reference_at_microsecond_start(hours, minutes):
    start = START + timedelta(minutes=7, microseconds=7438)
    source = grid_problem(
        (0.2,) * (hours * 60 // minutes), start=start, slot_minutes=minutes
    )
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(
            display_horizon_hours=hours,
            reference_horizon_hours=hours,
            display_interval_minutes=minutes,
            probe_kwh=0.2,
        ),
    )
    assert len(result.opportunities) == hours * 60 // minutes
    assert result.reference_complete
    assert result.classification_mode == "percentile"
    assert result.opportunities[0].start == start
    assert result.opportunities[-1].end == start + timedelta(hours=hours)
    assert all(
        (item.end - item.start).total_seconds() == minutes * 60
        for item in result.opportunities
    )
