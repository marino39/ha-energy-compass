from datetime import UTC, datetime

import pytest
from homeassistant import config_entries
from homeassistant.helpers import selector

from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.flow_schema import settings_schema
from custom_components.energy_compass.settings import (
    default_configuration,
    validate_configuration,
)


@pytest.mark.parametrize(
    "group",
    [
        "battery",
        "hardware",
        "tariffs",
        "forecast",
        "planning",
        "compass",
        "performance",
        "presentation",
        "notifications",
    ],
)
def test_settings_use_native_selectors(group):
    schema = settings_schema(group, default_configuration("EUR", "UTC")["settings"])
    assert schema.schema
    assert all(isinstance(value, selector.Selector) for value in schema.schema.values())


def test_defaults_are_generic():
    config = default_configuration("EUR", "UTC")
    assert config["settings"]["calibration"] == "unvalidated"
    assert config["settings"]["operating_floor"] == 0
    assert config["settings"]["boost_ceiling"] != 0.05


@pytest.mark.parametrize(
    "changes",
    [
        {"hardware_floor": 30, "operating_floor": 20},
        {"operating_floor": 80, "soc_ceiling": 80},
        {"display_horizon_hours": 48, "horizon_hours": 24},
        {"solve_time_limit_s": 100},
        {"probe_kwh": float("nan")},
    ],
)
def test_invalid_preferences_rejected(changes):
    config = default_configuration("EUR", "UTC")
    config["settings"].update(changes)
    with pytest.raises(InputError):
        validate_configuration(config, {}, datetime.now(UTC))


async def test_native_initial_flow(recorder_mock, hass, enable_custom_integrations):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Synthetic",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    assert result["type"] == "menu"
    assert "sources" in result["menu_options"]


async def test_generic_observed_mapping_preview(
    recorder_mock, hass, enable_custom_integrations
):
    hass.states.async_set(
        "sensor.synthetic_prices",
        "ok",
        {
            "rows": [
                {
                    "from": "2026-09-17T00:00:00+00:00",
                    "to": "2026-09-17T01:00:00+00:00",
                    "tariff": {"amount": 0.3},
                }
            ]
        },
    )
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Synthetic",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "sources"}
    )
    result = await hass.config_entries.flow.async_configure(
        fid, {"target": "buy", "mode": "forecast", "operation": "replace", "group": 1}
    )
    assert result["step_id"] == "source_entity"
    result = await hass.config_entries.flow.async_configure(
        fid, {"entity_id": "sensor.synthetic_prices"}
    )
    assert isinstance(
        next(iter(result["data_schema"].schema.values())), selector.AttributeSelector
    )
    result = await hass.config_entries.flow.async_configure(fid, {"attribute": "rows"})
    assert result["step_id"] == "source_mapping"
    fields = {str(key): val for key, val in result["data_schema"].schema.items()}
    assert isinstance(fields["value_path"], selector.SelectSelector)
    assert "tariff.amount" in fields["value_path"].config["options"]


async def test_renamed_own_output_rejected(
    recorder_mock, hass, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.flow_schema import entity_binding

    entry = MockConfigEntry(domain="energy_compass")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    own = registry.async_get_or_create(
        "sensor",
        "energy_compass",
        "owned",
        config_entry=entry,
        suggested_object_id="unrelated_name",
    )
    with pytest.raises(InputError, match="feedback"):
        entity_binding(hass, own.entity_id)


@pytest.mark.parametrize(
    "key,value",
    [
        ("horizon_hours", 48),
        ("lookback_days", 28),
        ("minimum_samples", 3),
        ("probe_kwh", 0.5),
        ("wear_per_kwh", 0.08),
        ("operating_floor", 25),
        ("debounce_seconds", 3),
        ("soc_trigger_percent", 4),
        ("total_time_limit_s", 30),
        ("power_max_gap_minutes", 15),
        ("boost_ceiling", -0.1),
        ("limit_floor", 0.4),
        ("monthly_charge", 12),
        ("notify_daily_max", 5),
    ],
)
def test_nondefault_settings_survive_validation(key, value):
    config = default_configuration("EUR", "UTC")
    config["settings"][key] = value
    assert validate_configuration(config, {}, datetime.now(UTC))[key] == value


async def test_soc_source_freshness_is_saved(
    recorder_mock, hass, enable_custom_integrations
):
    hass.states.async_set("sensor.synthetic_soc", "50")
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Synthetic",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": True,
        },
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "sources"})
    await hass.config_entries.flow.async_configure(
        fid,
        {"target": "soc", "mode": "measurement", "operation": "replace", "group": 1},
    )
    await hass.config_entries.flow.async_configure(
        fid, {"entity_id": "sensor.synthetic_soc"}
    )
    await hass.config_entries.flow.async_configure(fid, {})
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "unit": "%",
            "sign": 1,
            "timestamp_path": "last_updated",
            "max_age_seconds": 10,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "battery"}
    )
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults["soc_max_age_seconds"] == 10


@pytest.mark.parametrize("currency,expected", [("PLN", (0.05, 0.8)), ("EUR", (0, 1))])
async def test_preset_thresholds_are_currency_specific(
    recorder_mock, hass, enable_custom_integrations, currency, expected
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Synthetic",
            "currency": currency,
            "timezone": "UTC",
            "preset": "pse_solcast",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "compass"}
    )
    values = {str(key): key.default() for key in result["data_schema"].schema}
    assert (values["boost_ceiling"], values["limit_floor"]) == expected


def test_named_provider_presets_are_explicit():
    from custom_components.energy_compass.presets import PRESETS

    assert {"pse", "solcast", "pstryk_bankilo", "deye_solarman"} <= PRESETS.keys()
    pstryk = PRESETS["pstryk_bankilo"]
    assert (
        pstryk.price_attribute,
        pstryk.price_start_field,
        pstryk.price_value_field,
        pstryk.price_unit,
        pstryk.price_interval_minutes,
    ) == ("prices", "time", "price", "PLN/kWh", 60)
