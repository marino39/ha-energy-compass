from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.runtime import build_problem, compute
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import EntityBinding

NOW = datetime(2026, 9, 24, 10, tzinfo=UTC)


def battery_configuration():
    # Copied verbatim from tests/test_task5_runtime.py: the `tests.` import
    # path collides with a container-installed `tests` package that has
    # unrelated, uncollectable modules.
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.soc").to_dict()
    )
    config["settings"].update(
        capacity_kwh=20,
        operating_floor=15,
        hardware_floor=10,
        soc_ceiling=90,
        inverter_kw=5,
        grid_import_kw=7,
        grid_export_kw=2,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        eta_charge=0.9,
        eta_discharge=0.8,
        wear_per_kwh=0.04,
        allow_grid_charge=True,
        allow_battery_export=True,
        # These fixtures isolate SOC/hardware behavior, without daily counters.
        limit_export_to_pv=False,
    )
    return config


def _config(**settings):
    config = battery_configuration()
    defaults = {
        "soc_ceiling": 100,
        "horizon_hours": 4,
        "display_horizon_hours": 4,
        "reference_horizon_hours": 4,
        "lfp_balance": True,
        "balance_value": 50.0,
        "minimum_mode_minutes": 15,
    }
    defaults.update(settings)
    config["settings"].update(defaults)
    return config


def _states(soc="95"):
    return {"sensor.soc": {"state": soc, "attributes": {}, "last_updated": NOW}}


DUE = {
    "last_completed_at": (NOW - timedelta(days=8)).isoformat(),
    "hold_started_at": None,
}
OK = {
    "last_completed_at": (NOW - timedelta(days=1)).isoformat(),
    "hold_started_at": None,
}


def test_disabled_balance_leaves_problem_untouched():
    config = _config(lfp_balance=False)
    problem, _, quality = build_problem(config, _states(), NOW, balance_state=DUE)
    assert problem.balance_windows == () and problem.balance_threshold_kwh == 0
    assert quality["balance_phase"] is None


def test_ok_phase_sets_threshold_without_windows():
    problem, _, quality = build_problem(_config(), _states(), NOW, balance_state=OK)
    assert problem.balance_windows == ()
    assert problem.balance_threshold_kwh == pytest.approx(19.8)
    assert quality["balance_phase"] == "ok"


def test_due_phase_offers_windows_and_prices_the_miss():
    problem, _, quality = build_problem(_config(), _states(), NOW, balance_state=DUE)
    assert problem.balance_windows
    assert problem.balance_miss_cost == 50.0 * (1 + 1)
    assert quality["balance_phase"] == "due"


def test_short_horizon_warns_no_window():
    config = _config(balance_hold_minutes=360)
    _, _, quality = build_problem(config, _states(), NOW, balance_state=DUE)
    assert "balance_no_window_in_horizon" in quality["warnings"]


def test_chosen_window_rows_are_labelled():
    result = compute(_config(), _states(), NOW, balance_state=DUE)
    held = [row for row in result["intervals"] if row.get("balance_hold")]
    assert held
    assert {row["state"] for row in held} <= {"CHARGE_PV", "CHARGE_GRID"}
    assert all(row["discharge_kwh"] <= 1e-6 for row in held)
    others = [row for row in result["intervals"] if not row.get("balance_hold")]
    assert all("balance_hold" not in row for row in others)


def test_missed_due_balance_warns_overdue():
    # A free miss loses to any charging wear (fixture wear 0.04/kWh).
    config = _config(balance_value=0.0)
    result = compute(config, _states(), NOW, balance_state=DUE)
    assert "balance_overdue" in result["quality"]["warnings"]


def test_probes_keep_chosen_window():
    result = compute(_config(), _states(), NOW, balance_state=DUE)
    assert result["outlook"]
    costs = [
        row["cost_per_kwh"]
        for row in result["outlook"]
        if row["cost_per_kwh"] is not None
    ]
    assert costs
    # Pinned probes price load, not the balance: never cheaper than free and
    # never above the highest tariff in the fixture divided by efficiency.
    assert all(-1e-6 <= cost <= 10 for cost in costs)


def _holding(last_completed):
    return {
        "last_completed_at": last_completed,
        "hold_started_at": (NOW - timedelta(minutes=20)).isoformat(),
        "last_full_at": (NOW - timedelta(minutes=2)).isoformat(),
    }


def test_hold_after_recent_completion_is_not_scheduled():
    state = _holding((NOW - timedelta(days=1)).isoformat())
    problem, _, quality = build_problem(
        _config(), _states("100"), NOW, balance_state=state
    )
    assert problem.balance_windows == ()
    assert problem.balance_miss_cost == 0.0
    assert quality["balance_phase"] == "holding"


def test_hold_while_due_offers_the_current_window():
    state = _holding((NOW - timedelta(days=8)).isoformat())
    problem, _, _ = build_problem(_config(), _states("100"), NOW, balance_state=state)
    assert len(problem.balance_windows) == 1
    assert problem.balance_windows[0].slots[0] == 0
    assert problem.balance_miss_cost == 10 * 50.0
