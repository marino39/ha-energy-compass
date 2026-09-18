from datetime import UTC, datetime

import pytest
from homeassistant import config_entries
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.config_models import NumericSetting
from custom_components.energy_compass.flow_schema import snapshot
from custom_components.energy_compass.runtime import build_problem
from custom_components.energy_compass.settings import (
    default_configuration,
    merged_configuration,
)
from custom_components.energy_compass.sources.bindings import EntityBinding


async def _edit_buy_entity(hass, config):
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "tariffs"})
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "tariff_buy"}
    )
    result = await hass.config_entries.options.async_configure(
        fid, result["data_schema"]({})
    )
    assert result["step_id"] == "source_entity"
    return entry, fid, result


@pytest.mark.parametrize("change_entity", [False, True])
async def test_tariff_attribute_defaults_follow_only_the_same_entity(
    recorder_mock, hass, enable_custom_integrations, freezer, change_entity
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    config["helpers"]["buy_rate"] = NumericSetting(
        entity=EntityBinding("sensor.tariff", attribute="rate"),
        unit="EUR/kWh",
        source_unit="EUR/kWh",
        minimum=-1000,
        maximum=1000,
        max_age_seconds=None,
    ).to_dict()
    hass.states.async_set("sensor.tariff", "9", {"rate": 0.3})
    hass.states.async_set(
        "sensor.replacement", "0.4", {"unit_of_measurement": "EUR/kWh"}
    )
    entry, fid, result = await _edit_buy_entity(hass, config)
    await hass.config_entries.options.async_configure(
        fid,
        {"entity_id": "sensor.replacement"}
        if change_entity
        else result["data_schema"]({}),
    )
    result = await hass.config_entries.options.async_configure(fid, {})
    result = await hass.config_entries.options.async_configure(
        fid, result["data_schema"]({})
    )
    assert result["step_id"] == "tariffs"
    flow = hass.config_entries.options._progress[fid]
    saved_binding = flow._draft["helpers"]["buy_rate"]["entity"]
    assert saved_binding["attribute"] == (None if change_entity else "rate")
    problem, _, _ = build_problem(
        flow._draft, snapshot(hass, flow._draft), datetime(2026, 9, 18, 10, tzinfo=UTC)
    )
    assert problem.slots[0].buy_per_kwh == pytest.approx(0.4 if change_entity else 0.3)
    assert dict(entry.data) == config


async def test_rejected_tariff_unit_change_keeps_retry_default_rate_unchanged(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    config["helpers"]["buy_rate"] = NumericSetting(
        entity=EntityBinding("sensor.tariff"),
        unit="EUR/kWh",
        source_unit="EUR/kWh",
        minimum=-1000,
        maximum=1000,
        max_age_seconds=None,
    ).to_dict()
    hass.states.async_set("sensor.tariff", "0.3", {"unit_of_measurement": "EUR/kWh"})
    entry, fid, result = await _edit_buy_entity(hass, config)
    await hass.config_entries.options.async_configure(fid, result["data_schema"]({}))
    result = await hass.config_entries.options.async_configure(fid, {})
    submitted = result["data_schema"]({})
    submitted.update(source_unit="EUR/MWh", source_scale=1)
    rejected = await hass.config_entries.options.async_configure(fid, submitted)
    assert rejected["errors"] == {"base": "invalid_source"}
    flow = hass.config_entries.options._progress[fid]
    assert flow._draft == config
    result = await hass.config_entries.options.async_configure(
        fid, rejected["data_schema"]({})
    )
    assert result["step_id"] == "tariffs"
    problem, _, _ = build_problem(
        flow._draft, snapshot(hass, flow._draft), datetime(2026, 9, 18, 10, tzinfo=UTC)
    )
    assert problem.slots[0].buy_per_kwh == pytest.approx(0.3)
    assert flow._draft["helpers"]["buy_rate"] == config["helpers"]["buy_rate"]
    assert dict(entry.data) == config


async def test_tariffs_menu_offers_independent_buy_and_sell_sources(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tariffs"}
    )

    assert result["type"] == "menu"
    assert result["menu_options"][:3] == ["tariff_values", "tariff_buy", "tariff_sell"]


async def test_tariff_scalar_mwh_sources_save_and_resolve_with_transformations_once(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("PLN", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        buy_multiplier=2,
        buy_addition=0.1,
        buy_apply_vat=True,
        vat_percent=20,
    )
    hass.states.async_set("sensor.buy_rate", "750", {"unit_of_measurement": "PLN/MWh"})
    hass.states.async_set("sensor.sell_rate", "-50", {"unit_of_measurement": "PLN/MWh"})
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "tariffs"}
    )
    assert result["type"] == "menu"
    for side in ("buy", "sell"):
        result = await hass.config_entries.options.async_configure(
            fid, {"next_step_id": f"tariff_{side}"}
        )
        assert result["step_id"] == f"tariff_{side}"
        result = await hass.config_entries.options.async_configure(
            fid, {"mode": "entity"}
        )
        assert result["step_id"] == "source_entity"
        await hass.config_entries.options.async_configure(
            fid, {"entity_id": f"sensor.{side}_rate"}
        )
        result = await hass.config_entries.options.async_configure(fid, {})
        assert result["step_id"] == "source_measurement"
        result = await hass.config_entries.options.async_configure(
            fid,
            {
                "source_unit": "PLN/MWh",
                "source_scale": 1,
                "check_age": True,
                "max_age_hours": 24,
            },
        )
        assert result["type"] == "menu"
        assert result["step_id"] == "tariffs"
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "menu"}
    )
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "preview"}
    )
    assert not result["errors"]
    assert "sensor.buy_rate" in result["description_placeholders"]["preview"]
    assert "sensor.sell_rate" in result["description_placeholders"]["preview"]
    result = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert result["type"] == "create_entry"
    saved = merged_configuration(entry)
    problem, _, _ = build_problem(
        saved, snapshot(hass, saved), datetime(2026, 9, 18, 10, tzinfo=UTC)
    )
    assert problem.slots[0].buy_per_kwh == pytest.approx(1.9)
    assert problem.slots[0].sell_per_kwh == pytest.approx(-0.05)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_tariff_state_unit_mismatch_preserves_prior_selection(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("PLN", "UTC")
    hass.states.async_set(
        "sensor.rates",
        "0.4",
        {"unit_of_measurement": "PLN/kWh"},
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "tariff_buy"}
    )
    await hass.config_entries.options.async_configure(fid, {"mode": "entity"})
    await hass.config_entries.options.async_configure(
        fid, {"entity_id": "sensor.rates"}
    )
    await hass.config_entries.options.async_configure(fid, {})
    result = await hass.config_entries.options.async_configure(
        fid,
        {
            "source_unit": "PLN/kWh",
            "source_scale": 1,
            "check_age": True,
            "max_age_hours": 24,
        },
    )
    assert result["type"] == "menu"
    flow = hass.config_entries.options._progress[fid]
    prior = flow._draft["helpers"]["buy_rate"].copy()
    hass.states.async_set(
        "sensor.rates",
        "0.4",
        {"unit_of_measurement": "PLN/MWh"},
    )
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "tariff_buy"}
    )
    await hass.config_entries.options.async_configure(fid, {"mode": "entity"})
    await hass.config_entries.options.async_configure(
        fid, {"entity_id": "sensor.rates"}
    )
    await hass.config_entries.options.async_configure(fid, {})
    rejected = await hass.config_entries.options.async_configure(
        fid,
        {
            "source_unit": "PLN/kWh",
            "source_scale": 1,
            "check_age": True,
            "max_age_hours": 24,
        },
    )
    assert rejected["errors"] == {"base": "invalid_source"}
    assert flow._draft["helpers"]["buy_rate"] == prior
    assert "buy_rate" not in entry.data["helpers"]


async def test_buy_source_switches_entity_forecast_fixed_without_touching_sell_helper(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["helpers"]["sell_rate"] = {
        "entity": {"entity_id": "sensor.sell"},
        "unit": "EUR/kWh",
        "source_unit": "EUR/kWh",
        "multiplier": 1,
        "max_age_seconds": None,
    }
    hass.states.async_set("sensor.buy", "0.4", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.sell", "0.1", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(
        "sensor.buy_forecast",
        "ready",
        {
            "rows": [
                {
                    "start": "2026-09-18T10:00:00+00:00",
                    "end": "2026-09-18T11:00:00+00:00",
                    "price": 0.6,
                }
            ]
        },
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "tariff_buy"}
    )
    await hass.config_entries.options.async_configure(fid, {"mode": "entity"})
    await hass.config_entries.options.async_configure(fid, {"entity_id": "sensor.buy"})
    await hass.config_entries.options.async_configure(fid, {})
    await hass.config_entries.options.async_configure(
        fid,
        {
            "source_unit": "EUR/kWh",
            "source_scale": 1,
            "check_age": False,
            "max_age_hours": 24,
        },
    )
    flow = hass.config_entries.options._progress[fid]
    assert flow._draft["sources"]["buy"]["mode"] == "fixed"
    assert flow._draft["helpers"]["buy_rate"]["entity"]["entity_id"] == "sensor.buy"
    assert flow._draft["helpers"]["sell_rate"] == config["helpers"]["sell_rate"]

    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "tariff_buy"}
    )
    await hass.config_entries.options.async_configure(fid, {"mode": "forecast"})
    await hass.config_entries.options.async_configure(
        fid, {"entity_id": "sensor.buy_forecast"}
    )
    await hass.config_entries.options.async_configure(fid, {"attribute": "rows"})
    result = await hass.config_entries.options.async_configure(
        fid,
        {
            "value_path": "price",
            "start_path": "start",
            "end_path": "end",
            "duration_path": "",
            "unit_path": "",
            "published_path": "",
            "interval_minutes": 60,
            "unit": "EUR/kWh",
            "value_kind": "price",
            "value_sign": 1,
            "source_timezone": "UTC",
            "check_age": False,
            "max_age_hours": 24,
        },
    )
    assert result["type"] == "menu"
    assert flow._draft["sources"]["buy"]["mode"] == "forecast"
    assert "buy_rate" not in flow._draft["helpers"]
    assert flow._draft["helpers"]["sell_rate"] == config["helpers"]["sell_rate"]

    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "tariff_buy"}
    )
    result = await hass.config_entries.options.async_configure(fid, {"mode": "fixed"})
    assert result["type"] == "menu"
    assert flow._draft["sources"]["buy"]["mode"] == "fixed"
    assert flow._draft["sources"]["buy"]["forecast"] == []
    assert "buy_rate" not in flow._draft["helpers"]
    assert flow._draft["helpers"]["sell_rate"] == config["helpers"]["sell_rate"]
    assert entry.data["sources"]["buy"] == config["sources"]["buy"]


async def test_fresh_setup_saves_numeric_attribute_tariff_and_resolves_its_rate(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    hass.states.async_set("sensor.market", "ready", {"import_price": 750})
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Attribute tariff",
            "currency": "PLN",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariff_buy"})
    await hass.config_entries.flow.async_configure(fid, {"mode": "entity"})
    await hass.config_entries.flow.async_configure(fid, {"entity_id": "sensor.market"})
    await hass.config_entries.flow.async_configure(fid, {"attribute": "import_price"})
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "source_unit": "PLN/MWh",
            "source_scale": 1,
            "check_age": True,
            "max_age_hours": 24,
        },
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "menu"})
    preview = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "preview"}
    )
    assert not preview["errors"]
    result = await hass.config_entries.flow.async_configure(
        fid, {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True}
    )
    assert result["type"] == "create_entry"
    saved = result["data"]
    assert saved["helpers"]["buy_rate"]["entity"]["attribute"] == "import_price"
    problem, _, _ = build_problem(
        saved, snapshot(hass, saved), datetime(2026, 9, 18, 10, tzinfo=UTC)
    )
    assert problem.slots[0].buy_per_kwh == pytest.approx(0.75)
