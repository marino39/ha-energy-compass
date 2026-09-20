from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.autonomy import (
    AutonomyResult,
    autonomy_targets,
)


def _hourly(count: int, start: datetime = datetime(2026, 1, 1, tzinfo=UTC)):
    starts = tuple(start + timedelta(hours=i) for i in range(count))
    durations_h = tuple(1.0 for _ in range(count))
    return starts, durations_h


def _split_into_quarters(
    starts: tuple[datetime, ...],
    values: tuple[float, ...],
) -> tuple[tuple[datetime, ...], tuple[float, ...], tuple[float, ...]]:
    """Subdivide each hourly slot into four 15-minute slots of equal energy."""
    quarter_starts = []
    quarter_durations = []
    quarter_values = []
    for start, value in zip(starts, values, strict=True):
        for q in range(4):
            quarter_starts.append(start + timedelta(minutes=15 * q))
            quarter_durations.append(0.25)
            quarter_values.append(value / 4)
    return tuple(quarter_starts), tuple(quarter_durations), tuple(quarter_values)


def _window_boundaries(
    starts: tuple[datetime, ...],
    durations_h: tuple[float, ...],
    result: AutonomyResult,
) -> list[tuple[datetime, datetime]]:
    """(start, end) wall-clock boundary per window, from window ids."""
    boundaries: list[tuple[datetime, datetime]] = []
    window_start_idx = 0
    for i in range(1, len(result.window_ids) + 1):
        at_boundary = i == len(result.window_ids) or (
            result.window_ids[i] != result.window_ids[i - 1]
        )
        if at_boundary:
            end = starts[i - 1] + timedelta(hours=durations_h[i - 1])
            boundaries.append((starts[window_start_idx], end))
            window_start_idx = i
    return boundaries


def test_empty_input_returns_empty_result():
    result = autonomy_targets(
        (),
        (),
        (),
        (),
        reserve_kwh=1.0,
        usable_capacity_kwh=10.0,
        eta_discharge=0.9,
        timezone="UTC",
    )
    assert result == AutonomyResult((), ())


def test_targets_exclude_own_slot_and_include_reserve_offset():
    starts, durations_h = _hourly(3)
    load = (4.0, 4.0, 0.0)
    pv = (0.0, 0.0, 10.0)
    result = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=2.0,
        usable_capacity_kwh=100.0,
        eta_discharge=1.0,
        timezone="UTC",
    )
    assert result.window_ids == (0, 0, 0)
    assert result.targets_kwh == (6.0, 2.0, 2.0)


def test_targets_divide_by_discharge_efficiency():
    starts, durations_h = _hourly(3)
    load = (4.0, 4.0, 0.0)
    pv = (0.0, 0.0, 10.0)
    result = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=2.0,
        usable_capacity_kwh=100.0,
        eta_discharge=0.5,
        timezone="UTC",
    )
    # slot 0 sees 4 kWh of remaining deficit ahead of it (slot 1's 4 kWh,
    # slot 2 has none); stored kWh needed to deliver that is 4 / eta.
    assert result.targets_kwh[0] == pytest.approx(2.0 + 4.0 / 0.5)


def test_targets_are_capped_at_usable_capacity_without_moving_window_ids():
    starts, durations_h = _hourly(3)
    load = (4.0, 4.0, 0.0)
    pv = (0.0, 0.0, 10.0)
    uncapped = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=2.0,
        usable_capacity_kwh=100.0,
        eta_discharge=1.0,
        timezone="UTC",
    )
    capped = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=2.0,
        usable_capacity_kwh=5.0,
        eta_discharge=1.0,
        timezone="UTC",
    )
    assert capped.targets_kwh == (5.0, 2.0, 2.0)
    assert uncapped.targets_kwh == (6.0, 2.0, 2.0)
    assert capped.window_ids == uncapped.window_ids


def test_window_runs_to_horizon_end_when_surplus_never_recovers():
    starts, durations_h = _hourly(24)
    load = tuple(4.0 for _ in range(24))
    pv = tuple(0.0 for _ in range(24))
    result = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=1.0,
        usable_capacity_kwh=1000.0,
        eta_discharge=1.0,
        timezone="UTC",
    )
    assert result.window_ids == tuple(0 for _ in range(24))
    # last slot: reserve only (no slot strictly after it in the window)
    assert result.targets_kwh[-1] == 1.0
    # first slot: reserve plus every deficit strictly after it
    assert result.targets_kwh[0] == pytest.approx(1.0 + 4.0 * 23)


def test_window_spans_a_zero_deficit_run():
    # deficit, then a load==pv run (net 0, still inside the deficit window),
    # then a recovery slot big enough to close the window and hold through
    # the rest of this single-day fixture.
    starts, durations_h = _hourly(7)
    load = (4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0)
    pv = (0.0, 0.0, 4.0, 4.0, 4.0, 0.0, 16.0)
    result = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=1.0,
        usable_capacity_kwh=1000.0,
        eta_discharge=1.0,
        timezone="UTC",
    )
    # cumulative: -4, -8, -8, -8, -8, -12, 0 -> only recovers at slot 6
    assert result.window_ids == (0, 0, 0, 0, 0, 0, 0)
    # the zero-deficit run (slots 2-4) never lifts the running sum above
    # zero, so it stays part of the same still-open window.
    assert len(set(result.window_ids)) == 1


def test_window_does_not_close_on_a_candidate_that_later_dips_negative():
    starts, durations_h = _hourly(4)
    load = (4.0, 0.0, 4.0, 0.0)
    pv = (0.0, 4.0, 0.0, 4.0)
    result = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=1.0,
        usable_capacity_kwh=1000.0,
        eta_discharge=1.0,
        timezone="UTC",
    )
    # cumulative: -4, 0, -4, 0 -> slot 1 touches zero but dips again at
    # slot 2, so the window can only close at slot 3 (day/horizon end).
    assert result.window_ids == (0, 0, 0, 0)


def test_window_count_is_invariant_to_interval_splitting():
    # two identical days: heavy night/day deficit, then a surplus surge
    # concentrated in the last six hours that exactly zeroes the running
    # sum right at the last hour of the day (no trailing slots to
    # fragment into their own single-slot windows).
    day_load = [8.0] * 18 + [8.0] * 6
    day_pv = [0.0] * 18 + [32.0] * 6
    load = tuple(day_load * 2)
    pv = tuple(day_pv * 2)
    starts, durations_h = _hourly(48)

    hourly = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=1.5,
        usable_capacity_kwh=1000.0,
        eta_discharge=0.9,
        timezone="UTC",
    )

    q_starts, q_durations, q_load = _split_into_quarters(starts, load)
    _, _, q_pv = _split_into_quarters(starts, pv)
    quarterly = autonomy_targets(
        q_starts,
        q_durations,
        q_load,
        q_pv,
        reserve_kwh=1.5,
        usable_capacity_kwh=1000.0,
        eta_discharge=0.9,
        timezone="UTC",
    )

    hourly_windows = set(hourly.window_ids)
    quarterly_windows = set(quarterly.window_ids)
    assert len(hourly_windows) == 2
    assert len(quarterly_windows) == 2

    assert _window_boundaries(starts, durations_h, hourly) == _window_boundaries(
        q_starts, q_durations, quarterly
    )

    # sampling the quarter-hourly targets at the end of each hour reproduces
    # the hourly targets exactly (same real-time integral either way).
    for hour in range(48):
        assert quarterly.targets_kwh[4 * hour + 3] == pytest.approx(
            hourly.targets_kwh[hour]
        )


def test_capacity_clamping_never_merges_or_splits_windows():
    day_load = [8.0] * 18 + [8.0] * 6
    day_pv = [0.0] * 18 + [32.0] * 6
    load = tuple(day_load * 2)
    pv = tuple(day_pv * 2)
    starts, durations_h = _hourly(48)

    generous = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=1.5,
        usable_capacity_kwh=1000.0,
        eta_discharge=0.9,
        timezone="UTC",
    )
    tight = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=1.5,
        usable_capacity_kwh=3.0,
        eta_discharge=0.9,
        timezone="UTC",
    )
    assert tight.window_ids == generous.window_ids
    assert max(tight.targets_kwh) <= 3.0


def test_window_ids_are_contiguous_non_decreasing_and_start_at_zero():
    day_load = [8.0] * 18 + [8.0] * 6
    day_pv = [0.0] * 18 + [32.0] * 6
    load = tuple(day_load * 2)
    pv = tuple(day_pv * 2)
    starts, durations_h = _hourly(48)
    result = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=1.5,
        usable_capacity_kwh=1000.0,
        eta_discharge=0.9,
        timezone="UTC",
    )
    assert result.window_ids[0] == 0
    assert list(result.window_ids) == sorted(result.window_ids)
    seen = set()
    previous = None
    for window_id in result.window_ids:
        if window_id != previous:
            assert window_id not in seen
            seen.add(window_id)
            previous = window_id


def test_target_is_reserve_when_no_deficit_remains_in_the_window_tail():
    starts, durations_h = _hourly(2)
    load = (4.0, 0.0)
    pv = (0.0, 4.0)
    result = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=3.0,
        usable_capacity_kwh=1000.0,
        eta_discharge=1.0,
        timezone="UTC",
    )
    # cumulative -4, 0 -> one window covering both slots; slot 1 has no
    # deficit, so neither slot has any deficit left to pay for.
    assert result.window_ids == (0, 0)
    assert result.targets_kwh == (3.0, 3.0)


def test_new_window_can_close_on_its_own_first_slot():
    starts, durations_h = _hourly(3)
    load = (4.0, 0.0, 0.0)
    pv = (0.0, 4.0, 1.0)
    result = autonomy_targets(
        starts,
        durations_h,
        load,
        pv,
        reserve_kwh=1.0,
        usable_capacity_kwh=1000.0,
        eta_discharge=1.0,
        timezone="UTC",
    )
    # window 0 = slots 0-1 (cumulative -4, 0); window 1 = slot 2, whose own
    # net is already non-negative, so it closes immediately on itself.
    assert result.window_ids == (0, 0, 1)


def test_mismatched_lengths_raise():
    starts, durations_h = _hourly(3)
    with pytest.raises(ValueError):
        autonomy_targets(
            starts,
            durations_h,
            (1.0, 2.0),
            (0.0, 0.0, 0.0),
            reserve_kwh=1.0,
            usable_capacity_kwh=10.0,
            eta_discharge=0.9,
            timezone="UTC",
        )


def test_nonpositive_eta_discharge_raises():
    starts, durations_h = _hourly(2)
    with pytest.raises(ValueError):
        autonomy_targets(
            starts,
            durations_h,
            (1.0, 1.0),
            (0.0, 0.0),
            reserve_kwh=1.0,
            usable_capacity_kwh=10.0,
            eta_discharge=0.0,
            timezone="UTC",
        )
