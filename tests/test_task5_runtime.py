from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.energy_compass.config_models import PriceSource
from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.runtime import (
    build_problem,
    compute,
    freshness_deadline,
)
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)


def battery_configuration():
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
    )
    return config


def test_all_physical_values_reach_engine():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    config = battery_configuration()
    states = {"sensor.soc": {"state": "50", "attributes": {}, "last_updated": now}}
    problem, _, quality = build_problem(config, states, now)
    battery = problem.battery
    assert battery.capacity_kwh == 20
    assert battery.initial_kwh == 10
    assert battery.minimum_soc_fraction == 0.15
    assert battery.maximum_soc_fraction == 0.9
    assert battery.eta_charge == 0.9
    assert battery.eta_discharge == 0.8
    assert battery.wear_per_kwh == 0.04
    assert problem.site.inverter_kw == 5
    assert problem.site.grid_import_kw == 7
    assert problem.site.grid_export_kw == 2
    assert "unvalidated_capacity" in quality["warnings"]


def test_load_quality_distinguishes_daily_estimate_from_missing_recorder_history():
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=2, display_horizon_hours=2, reference_horizon_hours=2
    )
    _, _, daily_quality = build_problem(config, {}, now)
    assert daily_quality["load"]["source_mode"] == "daily_estimate"
    assert daily_quality["load"]["method"] == "daily_estimate"
    assert daily_quality["load"]["insufficient_history_hours"] is None
    assert "load_history_fallback" not in daily_quality["warnings"]
    config["sources"]["load"] = {
        "mode": "recorder",
        "statistic_id": "sensor.household",
        "history_unit": "kWh",
    }
    config["settings"].update(allow_fallback=True, fallback_daily_kwh=0)
    _, _, fallback_quality = build_problem(config, {}, now)
    load = fallback_quality["load"]
    assert (load["source_mode"], load["method"]) == ("recorder", "fallback")
    assert (
        load["coverage_hours"],
        load["fallback_coverage_hours"],
        load["fallback_fraction"],
    ) == (2, 2, 1)
    assert load["insufficient_history_hours"] == 2
    assert all(
        row["samples_available"] == 0 and row["samples_required"] == 2
        for row in load["coverage"]
    )
    assert "load_history_fallback" in fallback_quality["warnings"]
    assert "load_history_fallback" in compute(config, {}, now)["quality"]["warnings"]


def test_short_next_day_coverage_does_not_invent_prices():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    binding = IntervalBinding(
        EntityBinding("sensor.market", attribute="rows"),
        start_path="start",
        end_path="end",
        value_path="price",
        unit="EUR/kWh",
        value_kind="price",
    )
    config["sources"]["buy"] = PriceSource("forecast", (binding,)).to_dict()
    states = {
        "sensor.market": {
            "state": "ok",
            "attributes": {
                "rows": [{"start": now, "end": now + timedelta(hours=2), "price": 0.2}]
            },
            "last_updated": now,
        }
    }
    result = compute(config, states, now)
    assert result["valid"]
    assert result["classification_mode"] == "absolute_fallback"
    assert result["quality"]["coverage_complete"] is False
    assert len(result["intervals"]) == 2
    config["settings"]["short_coverage"] = "unavailable"
    assert compute(config, states, now)["guidance_valid"] is False


def test_total_budget_subtracts_base_elapsed():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    config["settings"].update(total_time_limit_s=30)
    from custom_components.energy_compass.runtime import analyze_consumption

    with (
        patch(
            "custom_components.energy_compass.runtime.perf_counter",
            side_effect=[0, 7, 8],
        ),
        patch(
            "custom_components.energy_compass.runtime.analyze_consumption",
            wraps=analyze_consumption,
        ) as analyze,
    ):
        compute(config, {}, now)
    assert analyze.call_args.kwargs["budget_s"] == 23


def test_unobserved_daily_throughput_is_rejected():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    config = battery_configuration()
    config["settings"]["daily_cycles"] = 1
    with pytest.raises(InputError, match="throughput"):
        build_problem(
            config,
            {"sensor.soc": {"state": "50", "attributes": {}, "last_updated": now}},
            now,
        )


def test_daily_throughput_wh_is_normalized_into_public_remaining_budget():
    now = datetime(2026, 9, 18, 10, tzinfo=UTC)
    config = battery_configuration()
    config["settings"]["daily_cycles"] = 1
    config["measurements"]["throughput_today"] = {
        "entity": EntityBinding("sensor.battery_throughput").to_dict(),
        "unit": "kWh",
        "source_unit": "Wh",
        "multiplier": 0.001,
        "max_age_seconds": 3600,
    }
    states = {
        "sensor.soc": {"state": "50", "attributes": {}, "last_updated": now},
        "sensor.battery_throughput": {
            "state": "4000",
            "attributes": {"unit_of_measurement": "Wh"},
            "last_updated": now,
        },
    }

    problem, _, _ = build_problem(config, states, now)

    assert dict(problem.remaining_daily_throughput_kwh)["2026-09-18"] == 36


@pytest.mark.parametrize(
    "source_unit, multiplier, state",
    [("kW", 1, "4"), ("Wh", -0.001, "4000"), ("Wh", 1, "4000"), ("kWh", 1, "-4")],
)
def test_daily_throughput_rejects_non_energy_or_negative_observation(
    source_unit, multiplier, state
):
    now = datetime(2026, 9, 18, 10, tzinfo=UTC)
    config = battery_configuration()
    config["settings"]["daily_cycles"] = 1
    config["measurements"]["throughput_today"] = {
        "entity": EntityBinding("sensor.battery_throughput").to_dict(),
        "unit": "kWh",
        "source_unit": source_unit,
        "multiplier": multiplier,
        "max_age_seconds": 3600,
    }
    states = {
        "sensor.soc": {"state": "50", "attributes": {}, "last_updated": now},
        "sensor.battery_throughput": {
            "state": state,
            "attributes": {"unit_of_measurement": source_unit},
            "last_updated": now,
        },
    }

    with pytest.raises(InputError, match="throughput"):
        build_problem(config, states, now)


def test_zero_native_coarsening_guard():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    binding = IntervalBinding(
        EntityBinding("sensor.market", attribute="rows"),
        start_path="start",
        interval_minutes=1,
        value_path="price",
        unit="EUR/kWh",
        value_kind="price",
    )
    config["sources"]["buy"] = PriceSource("forecast", (binding,)).to_dict()
    rows = [{"start": now + timedelta(minutes=i), "price": i % 2} for i in range(400)]
    states = {
        "sensor.market": {
            "state": "ok",
            "attributes": {"rows": rows},
            "last_updated": now,
        }
    }
    with pytest.raises(InputError, match="384"):
        build_problem(config, states, now)


def test_source_age_deadline_limits_advice_validity():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    binding = IntervalBinding(
        EntityBinding("sensor.market", attribute="rows"),
        start_path="start",
        interval_minutes=60,
        value_path="price",
        unit="EUR/kWh",
        value_kind="price",
        max_age_seconds=90,
    )
    config["sources"]["buy"] = PriceSource("forecast", (binding,)).to_dict()
    states = {
        "sensor.market": {
            "state": "ok",
            "attributes": {"rows": [{"start": now, "price": 0.2}]},
            "last_updated": now - timedelta(seconds=89),
        }
    }
    result = compute(config, states, now)
    assert result["valid_until"] == (now + timedelta(seconds=1)).isoformat()


@pytest.mark.parametrize("target", ["soc", "bms_soc"])
def test_soc_rejects_conflicting_state_unit_metadata(target):
    config = battery_configuration()
    config["sources"]["bms_soc"] = EntityBinding("sensor.bms_soc").to_dict()
    now = datetime(2026, 9, 17, tzinfo=UTC)
    states = {
        name: {
            "state": "50",
            "attributes": {"unit_of_measurement": "%"},
            "last_updated": now,
        }
        for name in ("sensor.soc", "sensor.bms_soc")
    }
    states[f"sensor.{target}"]["attributes"]["unit_of_measurement"] = "kWh"
    with pytest.raises(InputError, match="unit"):
        build_problem(config, states, now)


@pytest.mark.parametrize("target", ["soc", "bms_soc"])
def test_soc_attribute_unit_is_independent_of_entity_state(target):
    config = battery_configuration()
    config["sources"]["bms_soc"] = EntityBinding("sensor.bms_soc").to_dict()
    config["sources"][target] = EntityBinding(
        f"sensor.{target}", attribute="soc"
    ).to_dict()
    now = datetime(2026, 9, 17, tzinfo=UTC)
    states = {
        name: {
            "state": "50",
            "attributes": {"unit_of_measurement": "%"},
            "last_updated": now,
        }
        for name in ("sensor.soc", "sensor.bms_soc")
    }
    states[f"sensor.{target}"].update(
        state="10", attributes={"unit_of_measurement": "kWh", "soc": 50}
    )
    problem, _, _ = build_problem(config, states, now)
    assert problem.battery.initial_kwh == 10


@pytest.mark.parametrize("target", ["soc", "bms_soc"])
@pytest.mark.parametrize("path", ["last_reported", "last_updated"])
def test_native_report_timestamp_controls_soc_validation_and_expiry(target, path):
    config = battery_configuration()
    config["sources"]["bms_soc"] = EntityBinding("sensor.bms_soc").to_dict()
    config["soc_options"].update(timestamp_path=path, bms_timestamp_path=path)
    config["soc_options"].pop("timestamp_policy", None)
    config["soc_options"].pop("bms_timestamp_policy", None)
    now = datetime(2026, 9, 17, 10, 11, tzinfo=UTC)
    states = {
        name: {
            "state": "50",
            "attributes": {},
            "last_updated": now - timedelta(minutes=11),
            "last_reported": now - timedelta(minutes=1),
        }
        for name in ("sensor.soc", "sensor.bms_soc")
    }
    problem, values, _ = build_problem(config, states, now)
    assert problem.battery.initial_kwh == 10
    assert freshness_deadline(config, states, values, now) == now + timedelta(minutes=9)
    states[f"sensor.{target}"]["last_reported"] = now - timedelta(minutes=11)
    with pytest.raises(InputError, match="stale"):
        build_problem(config, states, now)


@pytest.mark.parametrize("target", ["soc", "bms_soc"])
@pytest.mark.parametrize("path", ["last_reported", "last_updated"])
@pytest.mark.parametrize("report", ["future", "malformed"])
def test_present_invalid_native_report_never_falls_back_to_receipt(
    target, path, report
):
    config = battery_configuration()
    config["sources"]["bms_soc"] = EntityBinding("sensor.bms_soc").to_dict()
    config["soc_options"].update(timestamp_path=path, bms_timestamp_path=path)
    config["soc_options"].pop("timestamp_policy", None)
    config["soc_options"].pop("bms_timestamp_policy", None)
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    states = {
        name: {
            "state": "50",
            "attributes": {},
            "last_updated": now,
            "last_reported": now,
        }
        for name in ("sensor.soc", "sensor.bms_soc")
    }
    states[f"sensor.{target}"]["last_reported"] = (
        now + timedelta(seconds=1) if report == "future" else "bad timestamp"
    )
    with pytest.raises(InputError, match="stale or future|invalid timestamp"):
        build_problem(config, states, now)


@pytest.mark.parametrize("stamp", ["stale", "future", "invalid"])
def test_custom_soc_measurement_timestamp_never_uses_recent_receipt(stamp):
    config = battery_configuration()
    config["soc_options"]["timestamp_path"] = "attributes.reported_at"
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    observed = {
        "stale": now - timedelta(minutes=11),
        "future": now + timedelta(minutes=11),
        "invalid": "bad timestamp",
    }[stamp]
    states = {
        "sensor.soc": {
            "state": "50",
            "attributes": {"reported_at": observed},
            "last_updated": now,
            "last_reported": now,
        }
    }
    with pytest.raises(InputError):
        build_problem(config, states, now)


def test_exact_native_last_updated_keeps_change_time_semantics():
    config = battery_configuration()
    config["soc_options"]["timestamp_policy"] = "exact_path"
    config["soc_options"]["timestamp_path"] = "last_updated"
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    states = {
        "sensor.soc": {
            "state": "50",
            "attributes": {},
            "last_updated": now - timedelta(minutes=11),
            "last_reported": now,
        }
    }
    with pytest.raises(InputError, match="stale"):
        build_problem(config, states, now)


@pytest.mark.parametrize("role", ["buy", "pv"])
@pytest.mark.parametrize("missing", ["unknown", "unavailable", "absent", "empty"])
def test_unavailable_continuation_keeps_current_coverage(role, missing):
    from custom_components.energy_compass.config_models import PvSource

    now = datetime(2026, 9, 17, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    config["settings"]["inverter_kw"] = 5
    bindings = tuple(
        IntervalBinding(
            EntityBinding(name, attribute="rows"),
            start_path="start",
            end_path="end",
            value_path="value",
            unit="EUR/kWh" if role == "buy" else "kWh",
            value_kind="price" if role == "buy" else "energy",
        )
        for name in ("sensor.today", "sensor.tomorrow")
    )
    if role == "buy":
        config["sources"]["buy"] = PriceSource("forecast", bindings).to_dict()
    else:
        config["sources"]["pv"] = PvSource(True, (bindings,)).to_dict()
    states = {
        "sensor.today": {
            "state": "ok",
            "attributes": {
                "rows": [{"start": now, "end": now + timedelta(hours=2), "value": 0.2}]
            },
            "last_updated": now,
        }
    }
    if missing != "absent":
        states["sensor.tomorrow"] = {
            "state": "ok" if missing == "empty" else missing,
            "attributes": {"rows": []},
            "last_updated": now,
        }
    result = compute(config, states, now)
    assert result["valid"]
    assert result["quality"]["coverage_end"] == (now + timedelta(hours=2)).isoformat()
    assert result["quality"]["coverage_complete"] is False
    assert result["quality"]["missing_sources"] == ["sensor.tomorrow"]


def test_missing_independent_pv_array_or_current_coverage_is_invalid():
    from custom_components.energy_compass.config_models import PvSource

    now = datetime(2026, 9, 17, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    bindings = tuple(
        IntervalBinding(
            EntityBinding(name, attribute="rows"),
            start_path="start",
            end_path="end",
            value_path="value",
            unit="kWh",
        )
        for name in ("sensor.first", "sensor.second")
    )
    config["sources"]["pv"] = PvSource(
        True, tuple((binding,) for binding in bindings)
    ).to_dict()
    states = {
        "sensor.first": {
            "state": "ok",
            "attributes": {
                "rows": [{"start": now, "end": now + timedelta(hours=2), "value": 0.2}]
            },
            "last_updated": now,
        }
    }
    with pytest.raises(InputError):
        build_problem(config, states, now)
    config["sources"]["pv"] = PvSource(True, (bindings,)).to_dict()
    states["sensor.first"]["attributes"]["rows"][0]["start"] = now + timedelta(hours=1)
    with pytest.raises(InputError):
        build_problem(config, states, now)
