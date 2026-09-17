from copy import deepcopy
from datetime import UTC, datetime

import pytest
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.settings import (
    default_configuration,
    validate_configuration,
)


async def test_options_preserve_invalid_edit(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Synthetic", version=2
    )
    entry.add_to_hass(hass)
    before = deepcopy(dict(entry.data))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "battery"}
    )
    assert result["step_id"] == "battery"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"operating_floor": 90, "soc_ceiling": 80}
    )
    assert result["errors"]
    assert entry.data == before


async def test_notification_form_offers_only_dispatchable_events(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["settings"]["notify_events"] = ["favorable", "invalid"]
    config["settings"]["notify_actions"] = [{"event": "legacy_test"}]
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Synthetic", version=2
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "notifications"}
    )
    fields = {
        str(key): (key, value) for key, value in result["data_schema"].schema.items()
    }
    assert "notify_actions" not in fields
    events_marker, events_selector = fields["notify_events"]
    assert isinstance(events_selector, selector.SelectSelector)
    assert events_selector.config["options"] == ["favorable", "limit"]
    assert events_marker.default() == ["favorable"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"notify_enabled": True, "notify_events": ["limit"]}
    )
    assert result["step_id"] == "menu"
    assert entry.data["settings"]["notify_actions"] == [{"event": "legacy_test"}]


def test_helper_unavailable_and_disabled_age():
    config = default_configuration("EUR", "UTC")
    config["helpers"] = {
        "grid_import_kw": {
            "fixed": None,
            "entity": {"entity_id": "input_number.limit"},
            "unit": "kW",
            "max_age_seconds": None,
        }
    }
    state = {
        "input_number.limit": {
            "state": "5",
            "attributes": {"unit_of_measurement": "kW"},
            "last_updated": datetime(2020, 1, 1, tzinfo=UTC),
        }
    }
    assert (
        validate_configuration(config, state, datetime.now(UTC))["grid_import_kw"] == 5
    )
    state["input_number.limit"]["state"] = "unavailable"
    with pytest.raises(InputError):
        validate_configuration(config, state, datetime.now(UTC))


@pytest.mark.parametrize(
    "unit,source_unit", [("PLN/kWh", "PLN/kWh"), ("EUR/kWh", "PLN/kWh")]
)
def test_economic_helpers_cannot_silently_convert_currency(unit, source_unit):
    config = default_configuration("EUR", "UTC")
    now = datetime.now(UTC)
    config["helpers"]["buy_rate"] = {
        "entity": {"entity_id": "sensor.rate"},
        "unit": unit,
        "source_unit": source_unit,
        "max_age_seconds": None,
    }
    states = {
        "sensor.rate": {
            "state": ".4",
            "attributes": {"unit_of_measurement": source_unit},
            "last_updated": now,
        }
    }
    with pytest.raises(InputError, match="currency"):
        validate_configuration(config, states, now)


async def test_options_commit_reloads_and_preserves_sources(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Options", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    before = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.options.async_configure(fid, {"buy_rate": 0.4})
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "preview"}
    )
    assert "import 0.4" in result["description_placeholders"]["preview"]
    result = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data is not before
    assert entry.runtime_data.configuration["sources"] == config["sources"]
    assert float(
        hass.states.get("sensor.options_consumption_cost").state
    ) == pytest.approx(0.4)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_commits_atomically(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant import config_entries

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Reconfigure", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    before = entry.runtime_data
    result = await hass.config_entries.flow.async_init(
        "energy_compass",
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.flow.async_configure(fid, {"buy_rate": 0.7})
    assert entry.data["settings"]["buy_rate"] == 0
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "preview"})
    result = await hass.config_entries.flow.async_configure(fid, {"confirm": True})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["settings"]["buy_rate"] == 0.7
    assert entry.runtime_data is not before
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_installation_name_updates_entry_title(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["name"] = "Original"
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Original", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "installation"}
    )
    await hass.config_entries.options.async_configure(
        fid,
        {
            "name": "Renamed installation",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "preview"})
    await hass.config_entries.options.async_configure(fid, {"confirm": True})
    await hass.async_block_till_done()
    assert entry.title == "Renamed installation"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_presentation_options_update_existing_entities(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant.helpers import entity_registry as er

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["name"] = "Visibility"
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Visibility", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    registry = er.async_get(hass)
    registry.async_update_entity(
        "sensor.visibility_expected_net_cost", disabled_by=er.RegistryEntryDisabler.USER
    )
    for enabled in (False, True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        fid = result["flow_id"]
        await hass.config_entries.options.async_configure(
            fid, {"next_step_id": "presentation"}
        )
        await hass.config_entries.options.async_configure(
            fid, {"expose_costs": enabled, "expose_windows": enabled}
        )
        await hass.config_entries.options.async_configure(
            fid, {"next_step_id": "preview"}
        )
        await hass.config_entries.options.async_configure(fid, {"confirm": True})
        await hass.async_block_till_done()
        for key in (
            "consumption_cost",
            "expected_wear_cost",
            "next_boost_start",
            "next_cheap_start",
            "next_limit_start",
            "next_change",
        ):
            assert registry.async_get(f"sensor.visibility_{key}").disabled_by == (
                None if enabled else er.RegistryEntryDisabler.INTEGRATION
            )
        assert (
            registry.async_get("sensor.visibility_expected_net_cost").disabled_by
            == er.RegistryEntryDisabler.USER
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_attribute_helper_uses_explicit_unit_not_parent_state_unit():
    config = default_configuration("EUR", "UTC")
    now = datetime.now(UTC)
    config["helpers"]["grid_import_kw"] = {
        "entity": {"entity_id": "sensor.controller", "attribute": "limit"},
        "unit": "kW",
        "source_unit": "kW",
        "max_age_seconds": None,
    }
    states = {
        "sensor.controller": {
            "state": "5",
            "attributes": {"unit_of_measurement": "kWh", "limit": 8},
            "last_updated": now,
        }
    }
    assert validate_configuration(config, states, now)["grid_import_kw"] == 8
    config["helpers"]["grid_import_kw"]["entity"]["attribute"] = None
    with pytest.raises(InputError, match="unit"):
        validate_configuration(config, states, now)
