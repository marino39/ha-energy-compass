"""Strategy type, Problem/Plan fields, validation, and strategy bundle weights.

Covers plan Step 1 (`engine/models.py`, `engine/normalize.py`) and Step 2
(`engine/strategy.py`) in one file, per the task's file scope.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.models import (
    STRATEGIES,
    Battery,
    InputError,
    Plan,
    Problem,
    SiteLimits,
    Slot,
    plan_monetary_cost,
)
from custom_components.energy_compass.engine.normalize import validate_problem
from custom_components.energy_compass.engine.strategy import (
    STRATEGY_OVERRIDES,
    STRATEGY_OWNED_KEYS,
    resolve_flags,
    strategy_weights,
)

_START = datetime(2026, 9, 17, tzinfo=UTC)


def problem(rows, **overrides):
    slots = tuple(
        Slot(
            _START + timedelta(hours=index),
            _START + timedelta(hours=index + 1),
            buy,
            sell,
            pv,
            load,
        )
        for index, (buy, sell, pv, load) in enumerate(rows)
    )
    base = Problem(
        slots,
        SiteLimits(8, 8, 8, True),
        Battery(10, 0.2, 0.8, 5, 4, 4, 1, 1, 0.1, True, True),
        "preserve_initial",
        0.0,
        (),
        "UTC",
    )
    return replace(base, **overrides)


def _three_slot_problem(**overrides):
    rows = [(0.5, 0.3, 0.0, 1.0)] * 3
    return problem(rows, **overrides)


# --- Step 1: Problem / Plan fields --------------------------------------


def test_problem_defaults_are_cost_min():
    prob = problem([(0.5, 0.3, 0.0, 1.0)])
    assert prob.strategy == "cost_min"
    assert prob.import_weight == 1.0
    assert prob.export_weight == 1.0
    assert prob.import_kwh_weight == 0.0
    assert prob.battery_export_penalty_per_kwh == 0.0
    assert prob.pv_export_margin == 0.0
    assert prob.soc_target_kwh == ()
    assert prob.soc_target_window == ()
    assert prob.soc_target_weight == 0.0
    assert prob.peak_import_weight == 0.0
    assert prob.soft_import_cap_kw is None
    assert prob.soft_export_cap_kw is None
    assert prob.cap_violation_weight == 0.0
    assert prob.strategy_changed is False


def test_plan_report_fields_default_to_zero():
    plan = Plan(
        flows=(), objective=0.0, grid_cost=0.0, wear_cost=0.0, terminal_credit=0.0
    )
    assert plan.autonomy_shortfall_kwh == 0.0
    assert plan.cap_violation_kwh == 0.0
    assert plan.peak_import_kw == 0.0


def test_plan_monetary_cost_excludes_weights():
    plan = Plan(
        flows=(),
        objective=999.0,
        grid_cost=10.0,
        wear_cost=2.0,
        terminal_credit=3.0,
        autonomy_shortfall_kwh=5.0,
        cap_violation_kwh=1.0,
        peak_import_kw=4.0,
    )
    assert plan_monetary_cost(plan) == 9.0


def test_validate_problem_rejects_unknown_strategy():
    prob = problem([(0.5, 0.3, 0.0, 1.0)], strategy="not_a_strategy")
    with pytest.raises(InputError):
        validate_problem(prob)


@pytest.mark.parametrize(
    "name",
    [
        "import_weight",
        "export_weight",
        "import_kwh_weight",
        "battery_export_penalty_per_kwh",
        "soc_target_weight",
        "peak_import_weight",
        "cap_violation_weight",
    ],
)
def test_validate_problem_rejects_negative_weight(name):
    prob = problem([(0.5, 0.3, 0.0, 1.0)], **{name: -1.0})
    with pytest.raises(InputError):
        validate_problem(prob)


def test_validate_problem_rejects_soc_target_length_mismatch():
    prob = _three_slot_problem(soc_target_kwh=(1.0, 2.0), soc_target_window=(0, 0))
    with pytest.raises(InputError):
        validate_problem(prob)


def test_validate_problem_rejects_window_ids_without_targets():
    prob = _three_slot_problem(soc_target_window=(0, 0, 0))
    with pytest.raises(InputError):
        validate_problem(prob)


def test_validate_problem_rejects_noncontiguous_window_ids():
    prob = _three_slot_problem(
        soc_target_kwh=(1.0, 1.0, 1.0), soc_target_window=(0, 1, 0)
    )
    with pytest.raises(InputError):
        validate_problem(prob)


def test_validate_problem_rejects_decreasing_window_ids():
    prob = _three_slot_problem(
        soc_target_kwh=(1.0, 1.0, 1.0), soc_target_window=(1, 0, 0)
    )
    with pytest.raises(InputError):
        validate_problem(prob)


def test_validate_problem_accepts_single_window():
    prob = _three_slot_problem(
        soc_target_kwh=(1.0, 1.0, 1.0),
        soc_target_window=(0, 0, 0),
        soc_target_weight=1.0,
    )
    validate_problem(prob)


# --- Step 2: strategy bundles and weights --------------------------------


def test_strategy_overrides_cover_every_strategy():
    assert set(STRATEGY_OVERRIDES) == set(STRATEGIES)


def test_overrides_only_touch_owned_keys():
    for overrides in STRATEGY_OVERRIDES.values():
        for key in overrides:
            assert key in STRATEGY_OWNED_KEYS


def test_resolve_flags_skips_explicit_fields():
    flags = resolve_flags("pv_swap", {}, explicit={"limit_export_to_pv"})
    assert "limit_export_to_pv" not in flags
    assert flags["limit_grid_charge_price"] is False
    assert flags["autonomy_reserve"] is True


def test_resolve_flags_returns_empty_for_cost_min():
    assert resolve_flags("cost_min", {}, explicit=()) == {}


_PLANNING_VALUES = {
    "self_sufficiency_import_price_per_kwh": 5.0,
    "self_sufficiency_export_penalty_per_kwh": 0.20,
    "pv_swap_margin_per_kwh": 0.05,
    "peak_import_price_per_kw": 0.50,
    "cap_violation_price_per_kwh": 2.0,
    "grid_friendly_import_cap_kw": 0,
    "grid_friendly_export_cap_kw": 0,
}


def test_strategy_weights_cost_min_returns_defaults():
    weights = strategy_weights(
        "cost_min",
        _PLANNING_VALUES,
        max_abs_buy_per_kwh=1.0,
        site_import_kw=8.0,
        site_export_kw=8.0,
    )
    assert weights == {
        "import_weight": 1.0,
        "export_weight": 1.0,
        "import_kwh_weight": 0.0,
        "battery_export_penalty_per_kwh": 0.0,
        "pv_export_margin": 0.0,
        "peak_import_weight": 0.0,
        "cap_violation_weight": 0.0,
        "soft_import_cap_kw": None,
        "soft_export_cap_kw": None,
    }


def test_strategy_weights_self_sufficiency_dominates_price():
    values = dict(_PLANNING_VALUES, self_sufficiency_import_price_per_kwh=0.0)
    weights = strategy_weights(
        "self_sufficiency",
        values,
        max_abs_buy_per_kwh=0.80,
        site_import_kw=8.0,
        site_export_kw=8.0,
    )
    assert weights["import_kwh_weight"] > 0.80


def test_strategy_weights_grid_friendly_uses_site_limit_sentinel():
    weights = strategy_weights(
        "grid_friendly",
        _PLANNING_VALUES,
        max_abs_buy_per_kwh=1.0,
        site_import_kw=8.0,
        site_export_kw=6.0,
    )
    assert weights["soft_import_cap_kw"] == 8.0
    assert weights["soft_export_cap_kw"] == 6.0


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_strategy_weights_returns_the_nine_keys(strategy):
    weights = strategy_weights(
        strategy,
        _PLANNING_VALUES,
        max_abs_buy_per_kwh=1.0,
        site_import_kw=8.0,
        site_export_kw=8.0,
    )
    assert set(weights) == {
        "import_weight",
        "export_weight",
        "import_kwh_weight",
        "battery_export_penalty_per_kwh",
        "pv_export_margin",
        "peak_import_weight",
        "cap_violation_weight",
        "soft_import_cap_kw",
        "soft_export_cap_kw",
    }
