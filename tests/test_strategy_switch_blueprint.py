"""Statically exercise the strategy_switch blueprint's decision templates.

The select entity this blueprint drives does not exist yet (it ships in a
later step), so this module never instantiates a real automation. Instead it
validates the blueprint's schema, asserts its inputs/selectors match the
design, and renders the individual decision/state-machine templates found in
the blueprint's first action (the `variables:` step) directly via
`homeassistant.helpers.template.Template`, against fake entity states.
"""

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.helpers import template as template_helper
from homeassistant.util import yaml as yaml_util

BLUEPRINT = (
    Path(__file__).resolve().parents[1]
    / "blueprints/automation/energy_compass/strategy_switch.yaml"
)
PATH = "energy_compass/strategy_switch.yaml"
WARSAW = ZoneInfo("Europe/Warsaw")

DEFAULT_VARIABLES = {
    "strategy_select_entity": "select.test_strategy",
    "plan_entity": "sensor.test_plan",
    "forecast_valid_entity": "binary_sensor.test_forecast_valid",
    "pv_forecast_tomorrow_entity": "sensor.test_pv_forecast_tomorrow",
    "daily_load_kwh": 20,
    "rce_next_day_entity": "sensor.test_rce_next_day",
    "rce_prices_attribute": "prices",
    "rce_price_key": "rce_pln",
    "rce_time_key": "dtime",
    "rce_unit": "PLN/MWh",
    "alert_entity": "binary_sensor.test_alert",
    "alert_on_states": ["on"],
    "night_start": "22:00:00",
    "night_end": "06:00:00",
    "evening_start": "17:00:00",
    "evening_end": "21:00:00",
    "round_trip_efficiency": 0.9,
    "spread_margin": 0.15,
    "swap_margin": 0.05,
    "run_time": "14:05:00",
    "retry_until": "20:00:00",
    "last_run_entity": "input_datetime.test_last_run",
    "manual_marker_entity": "input_text.test_manual_marker",
    "pending_marker_entity": "input_text.test_pending_marker",
    "enabled_rules": ["alert", "pv_swap", "self_sufficiency"],
}

EXPECTED_INPUTS = {
    "strategy_select",
    "plan",
    "forecast_valid",
    "pv_forecast_tomorrow",
    "daily_load_kwh",
    "rce_next_day",
    "rce_prices_attribute",
    "rce_price_key",
    "rce_time_key",
    "rce_unit",
    "alert_entity",
    "alert_on_states",
    "night_start",
    "night_end",
    "evening_start",
    "evening_end",
    "round_trip_efficiency",
    "spread_margin",
    "swap_margin",
    "run_time",
    "retry_until",
    "last_run",
    "manual_marker",
    "pending_marker",
    "enabled_rules",
}


def _blueprint_data():
    raw = yaml_util.load_yaml(BLUEPRINT)
    return models.Blueprint(
        raw,
        expected_domain="automation",
        path=PATH,
        schema=automation.config.AUTOMATION_BLUEPRINT_SCHEMA,
    )


def _actions():
    return _blueprint_data().data["actions"]


def _variable_template(name):
    variables = _actions()[0]["variables"]
    assert name in variables, f"variable {name!r} not found in the blueprint"
    return str(variables[name])


@pytest.fixture(autouse=True)
async def _warsaw_time_zone(hass):
    """The blueprint's now()/today_at() read hass.config.time_zone.

    The test hass fixture defaults to US/Pacific; every test comment below
    ("13:00 Warsaw", etc.) is only true once this is set.
    """
    await hass.config.async_set_time_zone("Europe/Warsaw")


def render(hass, name, variables):
    tpl = template_helper.Template(_variable_template(name), hass)
    return tpl.async_render(variables=variables)


async def decision(hass, **overrides):
    """Render the whole rule chain, in dependency order, and return every value."""
    variables = {**DEFAULT_VARIABLES, **overrides}
    for key in ("night_buy", "day_max_buy", "evening_sell", "next_day_available"):
        variables[key] = render(hass, key, variables)
    for key in ("alert_match", "pv_swap_match", "self_sufficiency_match"):
        variables[key] = render(hass, key, variables)
    variables["chosen_strategy"] = render(hass, "chosen_strategy", variables)
    variables["select_change_needed"] = render(hass, "select_change_needed", variables)
    return variables


def plan_state(hass, intervals):
    hass.states.async_set(
        DEFAULT_VARIABLES["plan_entity"], "ready", {"intervals": intervals}
    )


def rce_state(hass, records, prices_attribute="prices"):
    hass.states.async_set(
        DEFAULT_VARIABLES["rce_next_day_entity"], "ready", {prices_attribute: records}
    )


def interval_row(start, buy, sell=None):
    end = start + timedelta(hours=1)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "buy_per_kwh": buy,
        "sell_per_kwh": sell if sell is not None else round(buy * 0.6, 4),
    }


def hourly_intervals(day, buy_by_hour, default=0.5):
    return [
        interval_row(
            datetime.combine(day, time(hour), tzinfo=WARSAW),
            buy_by_hour.get(hour, default),
        )
        for hour in range(24)
    ]


def rce_row(end, price):
    return {"dtime": end.isoformat(), "rce_pln": price}


def full_day_rce_records(day, price=100.0, count=96):
    midnight = datetime.combine(day, time(0, 0), tzinfo=WARSAW)
    return [
        rce_row(midnight + timedelta(minutes=15 * (i + 1)), price) for i in range(count)
    ]


def with_price_window(records, day, start_str, end_str, price):
    """Overwrite rce_pln for every record whose start falls in [start, end) on day."""
    window_start = datetime.combine(day, time.fromisoformat(start_str), tzinfo=WARSAW)
    window_end = datetime.combine(day, time.fromisoformat(end_str), tzinfo=WARSAW)
    updated = []
    for record in records:
        end = datetime.fromisoformat(record["dtime"])
        row_start = end - timedelta(minutes=15)
        if window_start <= row_start < window_end:
            updated.append({**record, "rce_pln": price})
        else:
            updated.append(record)
    return updated


# ---------------------------------------------------------------------------
# Structural / schema tests
# ---------------------------------------------------------------------------


def test_blueprint_loads_and_validates():
    blueprint = _blueprint_data()
    assert blueprint.domain == "automation"
    assert blueprint.validate() is None
    assert set(blueprint.inputs) == EXPECTED_INPUTS
    assert blueprint.inputs["strategy_select"]["selector"]["entity"]["filter"][0][
        "domain"
    ] == ["select"]
    assert blueprint.inputs["enabled_rules"]["default"] == [
        "alert",
        "pv_swap",
        "self_sufficiency",
    ]
    assert blueprint.inputs["run_time"]["default"] == "14:05:00"
    assert blueprint.inputs["retry_until"]["default"] == "20:00:00"
    assert blueprint.inputs["night_start"]["default"] == "22:00:00"
    assert blueprint.inputs["night_end"]["default"] == "06:00:00"
    assert blueprint.inputs["evening_start"]["default"] == "17:00:00"
    assert blueprint.inputs["evening_end"]["default"] == "21:00:00"
    assert blueprint.inputs["round_trip_efficiency"]["default"] == 0.9
    assert blueprint.inputs["rce_unit"]["default"] == "PLN/MWh"
    assert blueprint.inputs["alert_entity"]["default"] == ""


def test_alert_rule_is_gated_on_enabled_rules():
    template = _variable_template("alert_match")
    assert "'alert' in enabled_rules" in template


def test_missing_next_day_prices_does_not_set_last_run():
    actions = _actions()
    guard = actions[3]
    assert guard["if"][0]["value_template"].strip() == "{{ chosen_strategy == 'none' }}"
    assert guard["then"] == [{"stop": "No decision, next-day prices unavailable."}]


def test_alert_does_not_set_last_run():
    completion = _actions()[-1]
    assert completion["if"][0]["value_template"].strip() == "{{ alert_match }}"
    then_actions = [step.get("action") for step in completion["then"]]
    else_actions = [step.get("action") for step in completion["else"]]
    assert then_actions == ["input_text.set_value"]
    assert "input_datetime.set_datetime" not in then_actions
    assert "input_datetime.set_datetime" in else_actions


def test_scheduled_run_clears_manual_marker(hass):
    completion = _actions()[-1]
    write = completion["else"][0]
    assert write["action"] == "input_text.set_value"
    value_template = write["data"]["value"]
    tpl = template_helper.Template(value_template, hass)
    assert tpl.async_render(variables={"chosen_strategy": "pv_swap"}) == "pv_swap"


def test_pending_marker_written_before_select_option():
    write_block = _actions()[4]
    assert (
        write_block["if"][0]["value_template"].strip() == "{{ select_change_needed }}"
    )
    sequence = write_block["then"]
    assert sequence[0]["action"] == "input_text.set_value"
    assert sequence[0]["target"]["entity_id"] == "{{ pending_marker_entity }}"
    assert sequence[1]["action"] == "select.select_option"
    assert "wait_template" in sequence[2]


def test_manual_change_write_uses_manual_prefix(hass):
    manual_block = _actions()[1]["choose"][0]
    write = manual_block["sequence"][0]["then"][0]
    assert write["action"] == "input_text.set_value"
    tpl = template_helper.Template(write["data"]["value"], hass)
    result = tpl.async_render(variables={"trigger": {"to_state": {"state": "pv_swap"}}})
    assert result == "manual:pv_swap"


# ---------------------------------------------------------------------------
# Rule tests
# ---------------------------------------------------------------------------


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_alert_selects_backup_ready(hass):
    hass.states.async_set(DEFAULT_VARIABLES["alert_entity"], "on")
    result = await decision(hass)
    assert result["alert_match"] is True
    assert result["chosen_strategy"] == "backup_ready"


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_alert_beats_economic_rule(hass):
    day = date(2026, 9, 20)
    hass.states.async_set(DEFAULT_VARIABLES["alert_entity"], "on")
    plan_state(
        hass,
        hourly_intervals(
            day,
            {h: 0.30 for h in list(range(22, 24)) + list(range(6))},
            default=0.35,
        ),
    )
    rce_state(hass, full_day_rce_records(date(2026, 9, 21)))
    result = await decision(hass)
    assert result["self_sufficiency_match"] is True
    assert result["chosen_strategy"] == "backup_ready"


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_alert_state_change_off_alert_entity_does_not_force_a_run(hass):
    """An 'alert' trigger id alone must not bypass the retry/time gate.

    The alert entity flipping to a state NOT in alert_on_states (the alert
    clearing) is a routine event that can happen at any time of day; it must
    not consume today's scheduled run or write the select outside the
    run_time/retry window, only the alert rule itself (alert_match) should.
    """
    hass.states.async_set(DEFAULT_VARIABLES["alert_entity"], "off")
    variables = {
        **DEFAULT_VARIABLES,
        "trigger": {"id": "alert", "to_state": {"state": "off"}},
    }
    variables["alert_match"] = render(hass, "alert_match", variables)
    assert variables["alert_match"] is False
    assert render(hass, "run_allowed", variables) is False


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_low_pv_forecast_and_swap_margin_selects_pv_swap(hass):
    today = date(2026, 9, 20)
    tomorrow = date(2026, 9, 21)
    night_hours = list(range(22, 24)) + list(range(6))
    plan_state(
        hass, hourly_intervals(today, dict.fromkeys(night_hours, 0.15), default=0.60)
    )
    records = full_day_rce_records(tomorrow, price=100.0)
    records = with_price_window(records, tomorrow, "17:00:00", "21:00:00", 300.0)
    rce_state(hass, records)
    hass.states.async_set(DEFAULT_VARIABLES["pv_forecast_tomorrow_entity"], "5.0")
    result = await decision(hass)
    assert result["next_day_available"] is True
    assert result["night_buy"] == pytest.approx(0.15)
    assert result["evening_sell"] == pytest.approx(0.30)
    assert result["pv_swap_match"] is True
    assert result["chosen_strategy"] == "pv_swap"


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_narrow_spread_selects_self_sufficiency(hass):
    today = date(2026, 9, 20)
    tomorrow = date(2026, 9, 21)
    night_hours = list(range(22, 24)) + list(range(6))
    plan_state(
        hass, hourly_intervals(today, dict.fromkeys(night_hours, 0.30), default=0.35)
    )
    rce_state(hass, full_day_rce_records(tomorrow, price=100.0))
    hass.states.async_set(DEFAULT_VARIABLES["pv_forecast_tomorrow_entity"], "25.0")
    result = await decision(hass)
    assert result["next_day_available"] is True
    assert result["pv_swap_match"] is False
    assert result["self_sufficiency_match"] is True
    assert result["chosen_strategy"] == "self_sufficiency"


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_default_selects_cost_min(hass):
    today = date(2026, 9, 20)
    tomorrow = date(2026, 9, 21)
    night_hours = list(range(22, 24)) + list(range(6))
    plan_state(
        hass, hourly_intervals(today, dict.fromkeys(night_hours, 0.10), default=0.90)
    )
    rce_state(hass, full_day_rce_records(tomorrow, price=100.0))
    hass.states.async_set(DEFAULT_VARIABLES["pv_forecast_tomorrow_entity"], "25.0")
    result = await decision(hass)
    assert result["alert_match"] is False
    assert result["pv_swap_match"] is False
    assert result["self_sufficiency_match"] is False
    assert result["chosen_strategy"] == "cost_min"


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_disabled_rule_is_skipped(hass):
    today = date(2026, 9, 20)
    tomorrow = date(2026, 9, 21)
    night_hours = list(range(22, 24)) + list(range(6))
    plan_state(
        hass, hourly_intervals(today, dict.fromkeys(night_hours, 0.15), default=0.90)
    )
    records = full_day_rce_records(tomorrow, price=100.0)
    records = with_price_window(records, tomorrow, "17:00:00", "21:00:00", 300.0)
    rce_state(hass, records)
    hass.states.async_set(DEFAULT_VARIABLES["pv_forecast_tomorrow_entity"], "5.0")
    result = await decision(hass, enabled_rules=["alert", "self_sufficiency"])
    assert result["pv_swap_match"] is False
    assert result["chosen_strategy"] == "cost_min"


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_missing_next_day_prices_keeps_current_strategy(hass):
    hass.states.async_set(DEFAULT_VARIABLES["strategy_select_entity"], "cost_min")
    result = await decision(hass)
    assert result["next_day_available"] is False
    assert result["chosen_strategy"] == "none"
    assert result["select_change_needed"] is False


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
@pytest.mark.parametrize("count,expected", [(89, False), (90, True)])
async def test_partial_next_day_array_below_threshold_counts_as_missing(
    hass, count, expected
):
    tomorrow = date(2026, 9, 21)
    rce_state(hass, full_day_rce_records(tomorrow, price=100.0, count=count))
    result = render(hass, "next_day_available", DEFAULT_VARIABLES)
    assert result is expected


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
@pytest.mark.parametrize(
    "unit,price,expected", [("PLN/MWh", 300.0, 0.30), ("PLN/kWh", 0.30, 0.30)]
)
async def test_rce_prices_convert_from_pln_per_mwh(hass, unit, price, expected):
    tomorrow = date(2026, 9, 21)
    records = full_day_rce_records(tomorrow, price=0.0)
    records = with_price_window(records, tomorrow, "17:00:00", "21:00:00", price)
    rce_state(hass, records)
    result = render(hass, "evening_sell", {**DEFAULT_VARIABLES, "rce_unit": unit})
    assert result == pytest.approx(expected)


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_rce_dtime_is_treated_as_interval_end(hass):
    tomorrow = date(2026, 9, 21)
    window_start = datetime.combine(tomorrow, time(17, 0), tzinfo=WARSAW)
    records = [
        # end == 17:00 -> start == 16:45, just outside [17:00, 21:00)
        rce_row(window_start, 900.0),
        # end == 17:15 -> start == 17:00, the first included row
        rce_row(window_start + timedelta(minutes=15), 300.0),
    ]
    rce_state(hass, records)
    result = render(hass, "evening_sell", DEFAULT_VARIABLES)
    assert result == pytest.approx(0.30)


@pytest.mark.freeze_time("2026-09-20T10:00:00+00:00")
async def test_night_and_evening_windows_use_configured_bounds(hass):
    today = date(2026, 9, 20)
    tomorrow = date(2026, 9, 21)
    plan_state(
        hass,
        hourly_intervals(today, {23: 0.10}, default=0.50)
        + hourly_intervals(tomorrow, {0: 0.12, 4: 0.20, 5: 0.90}, default=0.50),
    )
    records = full_day_rce_records(tomorrow, price=100.0)
    records = with_price_window(records, tomorrow, "18:00:00", "20:00:00", 500.0)
    rce_state(hass, records)
    default_night = render(hass, "night_buy", DEFAULT_VARIABLES)
    custom_night = render(
        hass,
        "night_buy",
        {**DEFAULT_VARIABLES, "night_start": "23:00:00", "night_end": "05:00:00"},
    )
    assert default_night == pytest.approx(0.10)
    assert custom_night == pytest.approx(0.10)
    narrowed_night = render(
        hass,
        "night_buy",
        {**DEFAULT_VARIABLES, "night_start": "23:30:00", "night_end": "04:30:00"},
    )
    assert narrowed_night == pytest.approx(0.12)
    default_evening = render(hass, "evening_sell", DEFAULT_VARIABLES)
    custom_evening = render(
        hass,
        "evening_sell",
        {**DEFAULT_VARIABLES, "evening_start": "18:00:00", "evening_end": "20:00:00"},
    )
    assert default_evening == pytest.approx(0.5)
    assert custom_evening == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# State-machine tests
# ---------------------------------------------------------------------------


@pytest.mark.freeze_time("2026-09-20T12:00:00+00:00")
async def test_retry_runs_only_between_run_time_and_retry_until(hass, freezer):
    variables = DEFAULT_VARIABLES
    freezer.move_to("2026-09-20T11:00:00+00:00")  # 13:00 Warsaw, before run_time
    assert render(hass, "retry_time_window", variables) is False
    freezer.move_to("2026-09-20T13:10:00+00:00")  # 15:10 Warsaw, inside window
    assert render(hass, "retry_time_window", variables) is True
    freezer.move_to("2026-09-20T18:10:00+00:00")  # 20:10 Warsaw, past retry_until
    assert render(hass, "retry_time_window", variables) is False


@pytest.mark.freeze_time("2026-09-20T13:10:00+00:00")
async def test_retry_skipped_when_last_run_is_today(hass):
    hass.states.async_set(
        DEFAULT_VARIABLES["last_run_entity"],
        "2026-09-20 14:05:00",
        {"timestamp": datetime(2026, 9, 20, 14, 5, tzinfo=WARSAW).timestamp()},
    )
    hass.states.async_set(DEFAULT_VARIABLES["manual_marker_entity"], "cost_min")
    assert render(hass, "last_run_before_today", DEFAULT_VARIABLES) is False
    prereqs = {
        **DEFAULT_VARIABLES,
        "retry_time_window": render(hass, "retry_time_window", DEFAULT_VARIABLES),
        "last_run_before_today": render(
            hass, "last_run_before_today", DEFAULT_VARIABLES
        ),
        "manual_override_active": render(
            hass, "manual_override_active", DEFAULT_VARIABLES
        ),
    }
    assert render(hass, "retry_eligible", prereqs) is False

    hass.states.async_set(
        DEFAULT_VARIABLES["last_run_entity"],
        "2026-09-19 14:05:00",
        {"timestamp": datetime(2026, 9, 19, 14, 5, tzinfo=WARSAW).timestamp()},
    )
    assert render(hass, "last_run_before_today", DEFAULT_VARIABLES) is True
    prereqs = {
        **DEFAULT_VARIABLES,
        "retry_time_window": render(hass, "retry_time_window", DEFAULT_VARIABLES),
        "last_run_before_today": render(
            hass, "last_run_before_today", DEFAULT_VARIABLES
        ),
        "manual_override_active": render(
            hass, "manual_override_active", DEFAULT_VARIABLES
        ),
    }
    assert render(hass, "retry_eligible", prereqs) is True


@pytest.mark.freeze_time("2026-09-20T13:10:00+00:00")
async def test_retry_blocked_by_manual_marker(hass):
    hass.states.async_set(
        DEFAULT_VARIABLES["last_run_entity"],
        "2026-09-19 14:05:00",
        {"timestamp": datetime(2026, 9, 19, 14, 5, tzinfo=WARSAW).timestamp()},
    )
    hass.states.async_set(DEFAULT_VARIABLES["manual_marker_entity"], "manual:pv_swap")
    assert render(hass, "manual_override_active", DEFAULT_VARIABLES) is True
    prereqs = {
        **DEFAULT_VARIABLES,
        "retry_time_window": render(hass, "retry_time_window", DEFAULT_VARIABLES),
        "last_run_before_today": render(
            hass, "last_run_before_today", DEFAULT_VARIABLES
        ),
        "manual_override_active": render(
            hass, "manual_override_active", DEFAULT_VARIABLES
        ),
    }
    assert render(hass, "retry_eligible", prereqs) is False


@pytest.mark.freeze_time("2026-09-20T12:00:00+00:00")
async def test_manual_change_sets_manual_marker(hass):
    hass.states.async_set(DEFAULT_VARIABLES["manual_marker_entity"], "cost_min")
    variables = {
        **DEFAULT_VARIABLES,
        "trigger": {"to_state": {"state": "pv_swap"}},
    }
    assert render(hass, "manual_change_detected", variables) is True


@pytest.mark.freeze_time("2026-09-20T12:00:00+00:00")
async def test_blueprint_write_is_not_recorded_as_manual(hass):
    hass.states.async_set(DEFAULT_VARIABLES["manual_marker_entity"], "pv_swap")
    variables = {
        **DEFAULT_VARIABLES,
        "trigger": {"to_state": {"state": "pv_swap"}},
    }
    assert render(hass, "manual_change_detected", variables) is False


@pytest.mark.freeze_time("2026-09-20T13:10:00+00:00")
async def test_startup_reevaluates_only_when_today_has_no_successful_run(hass):
    hass.states.async_set(DEFAULT_VARIABLES["manual_marker_entity"], "cost_min")

    hass.states.async_set(
        DEFAULT_VARIABLES["last_run_entity"],
        "2026-09-20 14:05:00",
        {"timestamp": datetime(2026, 9, 20, 14, 5, tzinfo=WARSAW).timestamp()},
    )
    variables = {**DEFAULT_VARIABLES, "trigger": {"id": "startup"}}
    variables["alert_match"] = render(hass, "alert_match", variables)
    variables["retry_time_window"] = render(hass, "retry_time_window", variables)
    variables["last_run_before_today"] = render(
        hass, "last_run_before_today", variables
    )
    variables["manual_override_active"] = render(
        hass, "manual_override_active", variables
    )
    variables["retry_eligible"] = render(hass, "retry_eligible", variables)
    assert render(hass, "run_allowed", variables) is False

    hass.states.async_set(
        DEFAULT_VARIABLES["last_run_entity"],
        "2026-09-19 14:05:00",
        {"timestamp": datetime(2026, 9, 19, 14, 5, tzinfo=WARSAW).timestamp()},
    )
    variables["last_run_before_today"] = render(
        hass, "last_run_before_today", variables
    )
    variables["retry_eligible"] = render(hass, "retry_eligible", variables)
    assert render(hass, "run_allowed", variables) is True


@pytest.mark.freeze_time("2026-09-20T12:00:00+00:00")
async def test_select_is_not_called_when_target_equals_current(hass):
    hass.states.async_set(DEFAULT_VARIABLES["strategy_select_entity"], "pv_swap")
    variables = {**DEFAULT_VARIABLES, "chosen_strategy": "pv_swap"}
    assert render(hass, "select_change_needed", variables) is False

    hass.states.async_set(DEFAULT_VARIABLES["strategy_select_entity"], "cost_min")
    assert render(hass, "select_change_needed", variables) is True


@pytest.mark.freeze_time("2026-09-20T12:06:00+00:00")
async def test_reconcile_reapplies_stale_pending_target_after_restart(hass):
    hass.states.async_set(
        DEFAULT_VARIABLES["last_run_entity"],
        "2026-09-19 14:05:00",
        {"timestamp": datetime(2026, 9, 19, 14, 5, tzinfo=WARSAW).timestamp()},
    )
    hass.states.async_set(DEFAULT_VARIABLES["strategy_select_entity"], "cost_min")
    hass.states.async_set(DEFAULT_VARIABLES["pending_marker_entity"], "pv_swap")
    assert render(hass, "reconcile_needed", DEFAULT_VARIABLES) is True


@pytest.mark.freeze_time("2026-09-20T12:06:00+00:00")
async def test_reconcile_skipped_when_pending_matches_current(hass):
    hass.states.async_set(
        DEFAULT_VARIABLES["last_run_entity"],
        "2026-09-19 14:05:00",
        {"timestamp": datetime(2026, 9, 19, 14, 5, tzinfo=WARSAW).timestamp()},
    )
    hass.states.async_set(DEFAULT_VARIABLES["strategy_select_entity"], "pv_swap")
    hass.states.async_set(DEFAULT_VARIABLES["pending_marker_entity"], "pv_swap")
    assert render(hass, "reconcile_needed", DEFAULT_VARIABLES) is False
