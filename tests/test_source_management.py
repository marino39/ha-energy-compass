from copy import deepcopy

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)


def _forecast(entity_id, attribute):
    return IntervalBinding(
        EntityBinding(entity_id, attribute=attribute),
        value_path="price",
        start_path="start",
        end_path="end",
        unit="EUR/kWh",
        value_kind="price",
        source_timezone="UTC",
    ).to_dict()


def _source_value(result, entity_id):
    choices = next(
        field.config["options"]
        for key, field in result["data_schema"].schema.items()
        if str(key) == "source"
    )
    matches = [choice["value"] for choice in choices if entity_id in choice["label"]]
    assert matches, f"No source inventory entry for {entity_id}"
    return matches[0]


async def test_sources_inventory_exposes_each_price_continuation_for_precise_edit(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    today = _forecast("sensor.market_today", "prices")
    tomorrow = _forecast("sensor.market_tomorrow", "prices")
    config["sources"]["buy"].update(
        mode="forecast", fixed=None, forecast=[today, tomorrow]
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    before = deepcopy(dict(entry.data))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sources"}
    )

    assert result["type"] == "menu"
    assert "source_inventory" in result["menu_options"]
    assert "source_add" in result["menu_options"]
    inventory = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_inventory"}
    )
    choices = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    labels = [choice["label"] for choice in choices]
    assert any("sensor.market_today" in label and "prices" in label for label in labels)
    assert any(
        "sensor.market_tomorrow" in label and "prices" in label for label in labels
    )
    assert entry.data == before


async def test_pv_inventory_keeps_groups_and_continuations_distinct(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["sources"]["pv"].update(
        enabled=True,
        arrays=[
            [
                {
                    **_forecast("sensor.east_today", "watts"),
                    "unit": "kWh",
                    "value_kind": "energy",
                },
                {
                    **_forecast("sensor.east_tomorrow", "watts"),
                    "unit": "kWh",
                    "value_kind": "energy",
                },
            ],
            [
                {
                    **_forecast("sensor.west_today", "watts"),
                    "unit": "kWh",
                    "value_kind": "energy",
                },
                {
                    **_forecast("sensor.west_tomorrow", "watts"),
                    "unit": "kWh",
                    "value_kind": "energy",
                },
            ],
        ],
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sources"}
    )
    assert result["type"] == "menu"
    assert "source_inventory" in result["menu_options"]
    inventory = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_inventory"}
    )
    choices = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    labels = [choice["label"] for choice in choices]
    for name in ("east_today", "east_tomorrow", "west_today", "west_tomorrow"):
        assert sum(name in label for label in labels) == 1
    assert any("group 1" in label.lower() and "east_today" in label for label in labels)
    assert any("group 2" in label.lower() and "west_today" in label for label in labels)


async def test_remove_one_price_continuation_preserves_sibling_and_cancel_preserves_draft(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    today = _forecast("sensor.market_today", "prices")
    tomorrow = _forecast("sensor.market_tomorrow", "prices")
    config["sources"]["buy"].update(
        mode="forecast", fixed=None, forecast=[today, tomorrow]
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    selected = _source_value(inventory, "market_tomorrow")
    await hass.config_entries.options.async_configure(fid, {"source": selected})
    removal = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_remove"}
    )
    assert "sensor.market_tomorrow" in removal["description_placeholders"]["target"]
    cancelled = await hass.config_entries.options.async_configure(
        fid, {"confirm": False}
    )
    assert cancelled["type"] == "menu"
    flow = hass.config_entries.options._progress[fid]
    assert flow._draft["sources"]["buy"]["forecast"] == [today, tomorrow]
    assert entry.data["sources"]["buy"]["forecast"] == [today, tomorrow]

    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"source": _source_value(inventory, "market_tomorrow")}
    )
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_remove"}
    )
    await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert flow._draft["sources"]["buy"]["forecast"] == [today]
    assert entry.data["sources"]["buy"]["forecast"] == [today, tomorrow]


async def test_remove_pv_continuation_then_edit_reindexed_group_preserves_other_group(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    first = {
        **_forecast("sensor.east_today", "rows"),
        "unit": "kWh",
        "value_kind": "energy",
    }
    second = {
        **_forecast("sensor.east_tomorrow", "rows"),
        "unit": "kWh",
        "value_kind": "energy",
    }
    west = {
        **_forecast("sensor.west_today", "rows"),
        "unit": "kWh",
        "value_kind": "energy",
    }
    config["sources"]["pv"].update(enabled=True, arrays=[[first, second], [west]])
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"source": _source_value(inventory, "east_tomorrow")}
    )
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_remove"}
    )
    await hass.config_entries.options.async_configure(fid, {"confirm": True})
    flow = hass.config_entries.options._progress[fid]
    assert flow._draft["sources"]["pv"]["arrays"] == [[first], [west]]

    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"source": _source_value(inventory, "west_today")}
    )
    edited = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_edit"}
    )
    assert edited["step_id"] == "source_entity"
    entity_marker = next(
        key for key in edited["data_schema"].schema if str(key) == "entity_id"
    )
    assert entity_marker.default() == "sensor.west_today"
    assert flow._draft["sources"]["pv"]["arrays"] == [[first], [west]]


@pytest.mark.parametrize(
    "target, mode", [("pv", "measurement"), ("throughput_today", "statistic")]
)
async def test_forged_incompatible_source_modes_leave_saved_entry_intact(
    recorder_mock, hass, enable_custom_integrations, target, mode
):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    before = deepcopy(dict(entry.data))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_add"}
    )
    result = await hass.config_entries.options.async_configure(fid, {"target": target})
    assert result["step_id"] == "source_mode"
    flow = hass.config_entries.options._progress[fid]
    rejected = await flow.async_step_source_mode({"mode": mode})
    assert rejected["errors"] == {"base": "invalid_input"}
    assert flow._draft == config
    assert entry.data == before


async def test_unchanged_soc_edit_preserves_existing_age_helper(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True,
        soc=EntityBinding("sensor.soc").to_dict(),
    )
    config["helpers"]["soc_max_age_seconds"] = {
        "entity": EntityBinding("input_number.soc_age").to_dict(),
        "unit": "s",
        "source_unit": "s",
        "multiplier": 1,
        "max_age_seconds": None,
    }
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("input_number.soc_age", "600", {"unit_of_measurement": "s"})
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"source": _source_value(inventory, "sensor.soc")}
    )
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_edit"}
    )
    await hass.config_entries.options.async_configure(fid, {"entity_id": "sensor.soc"})
    await hass.config_entries.options.async_configure(fid, {})
    result = await hass.config_entries.options.async_configure(
        fid,
        {
            "unit": "%",
            "sign": 1,
            "timestamp_path": "last_reported",
            "timestamp_policy": "auto",
            "max_age_seconds": 600,
        },
    )
    assert result["type"] == "menu"
    flow = hass.config_entries.options._progress[fid]
    assert (
        flow._draft["helpers"]["soc_max_age_seconds"]
        == config["helpers"]["soc_max_age_seconds"]
    )
    assert (
        entry.data["helpers"]["soc_max_age_seconds"]
        == config["helpers"]["soc_max_age_seconds"]
    )


async def test_legacy_explicit_endpoint_mapping_edits_without_inventing_interval_length(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    selected = _forecast("sensor.legacy_prices", "rows")
    selected["interval_minutes"] = None
    selected.pop("published_path")
    selected["max_age_seconds"] = None
    config["sources"]["buy"].update(mode="forecast", fixed=None, forecast=[selected])
    hass.states.async_set(
        "sensor.legacy_prices",
        "ready",
        {
            "rows": [
                {
                    "start": "2026-09-18T10:00:00+00:00",
                    "end": "2026-09-18T11:00:00+00:00",
                    "price": 0.3,
                }
            ]
        },
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"source": _source_value(inventory, "legacy_prices")}
    )
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_edit"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"entity_id": "sensor.legacy_prices"}
    )
    result = await hass.config_entries.options.async_configure(
        fid, {"attribute": "rows"}
    )
    assert result["step_id"] == "source_mapping"
    fields = {str(key): key for key in result["data_schema"].schema}
    assert isinstance(fields["interval_minutes"], vol.Optional)
    assert fields["value_path"].default() == "price"
    assert fields["start_path"].default() == "start"
    assert fields["end_path"].default() == "end"
    assert fields["check_age"].default() is False
    result = await hass.config_entries.options.async_configure(
        fid,
        {
            "value_path": "price",
            "start_path": "start",
            "end_path": "end",
            "duration_path": "",
            "unit_path": "",
            "published_path": "",
            "unit": "EUR/kWh",
            "value_kind": "price",
            "value_sign": 1,
            "source_timezone": "UTC",
            "check_age": False,
            "max_age_hours": 24,
        },
    )
    assert result["type"] == "menu"
    flow = hass.config_entries.options._progress[fid]
    edited = flow._draft["sources"]["buy"]["forecast"][0]
    assert edited["interval_minutes"] is None
    assert edited["value_path"] == "price"
    assert edited["end_path"] == "end"
    assert edited["max_age_seconds"] is None


async def test_cancelled_source_removal_can_return_to_main_preview(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    config["measurements"]["pv_power"] = {
        "entity": EntityBinding("sensor.pv_power").to_dict(),
        "unit": "kW",
        "source_unit": "kW",
        "multiplier": 1,
        "max_age_seconds": None,
    }
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "sources"}
    )
    assert "menu" in result["menu_options"]
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    result = await hass.config_entries.options.async_configure(
        fid, {"source": _source_value(inventory, "pv_power")}
    )
    assert "sources" in result["menu_options"]
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_remove"}
    )
    result = await hass.config_entries.options.async_configure(fid, {"confirm": False})
    assert result["step_id"] == "sources"
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "menu"}
    )
    assert "preview" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "preview"}
    )
    assert result["step_id"] == "preview"


async def test_registry_renamed_source_is_shown_and_prefilled_under_current_entity_id(
    recorder_mock, hass, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    selected = registry.async_get_or_create(
        "sensor", "test", "market_price", suggested_object_id="market_old"
    )
    config = default_configuration("EUR", "UTC")
    forecast = _forecast(selected.entity_id, "rows")
    forecast["entity"]["registry_id"] = selected.id
    config["sources"]["buy"].update(mode="forecast", fixed=None, forecast=[forecast])
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    registry.async_update_entity(selected.entity_id, new_entity_id="sensor.market_new")
    hass.states.async_set("sensor.market_new", "ready", {"rows": []})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    selected_value = _source_value(inventory, "sensor.market_new")
    await hass.config_entries.options.async_configure(fid, {"source": selected_value})
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_edit"}
    )
    entity_marker = next(
        key for key in result["data_schema"].schema if str(key) == "entity_id"
    )
    assert entity_marker.default() == "sensor.market_new"
    assert (
        entry.data["sources"]["buy"]["forecast"][0]["entity"]["entity_id"]
        == selected.entity_id
    )


async def test_unavailable_unrelated_helper_does_not_block_optional_source_removal(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["helpers"]["grid_import_kw"] = {
        "entity": EntityBinding("input_number.unavailable_limit").to_dict(),
        "unit": "kW",
        "max_age_seconds": None,
    }
    config["measurements"]["pv_power"] = {
        "entity": EntityBinding("sensor.old_pv_power").to_dict(),
        "unit": "kW",
        "source_unit": "kW",
        "multiplier": 1,
        "max_age_seconds": None,
    }
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"source": _source_value(inventory, "old_pv_power")}
    )
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_remove"}
    )
    result = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert result["type"] == "menu"
    flow = hass.config_entries.options._progress[fid]
    assert "pv_power" not in flow._draft["measurements"]
    assert "pv_power" in entry.data["measurements"]


async def test_remove_first_pv_group_reindexes_remaining_binding_without_deleting_it(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    first = {**_forecast("sensor.east", "rows"), "unit": "kWh", "value_kind": "energy"}
    second = {**_forecast("sensor.west", "rows"), "unit": "kWh", "value_kind": "energy"}
    config["sources"]["pv"].update(enabled=True, arrays=[[first], [second]])
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    choices = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    first_group = next(
        choice["value"] for choice in choices if choice["label"] == "PV group 1"
    )
    actions = await hass.config_entries.options.async_configure(
        fid, {"source": first_group}
    )
    assert "source_remove_group" in actions["menu_options"]
    assert "source_edit" not in actions["menu_options"]
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_remove_group"}
    )
    await hass.config_entries.options.async_configure(fid, {"confirm": True})
    flow = hass.config_entries.options._progress[fid]
    assert flow._draft["sources"]["pv"]["arrays"] == [[second]]
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    choices = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    assert any(
        "PV group 1" in item["label"] and "sensor.west" in item["label"]
        for item in choices
    )
    assert not any("PV group 2" in item["label"] for item in choices)


async def test_forged_zero_pv_group_cannot_replace_last_existing_group(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["sources"]["pv"].update(
        enabled=True,
        arrays=[
            [
                {
                    **_forecast("sensor.east", "rows"),
                    "unit": "kWh",
                    "value_kind": "energy",
                }
            ]
        ],
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = hass.config_entries.options._progress[result["flow_id"]]
    flow._source = {"target": "pv", "operation": "replace"}
    flow._binding = None
    rejected = await flow.async_step_source_mode({"mode": "forecast", "group": 0})
    assert rejected["errors"] == {"base": "invalid_input"}
    assert flow._draft["sources"]["pv"]["arrays"] == config["sources"]["pv"]["arrays"]


async def test_polish_source_inventory_labels_bindings_and_back_navigation(
    recorder_mock, hass, enable_custom_integrations
):
    hass.config.language = "pl"
    config = default_configuration("EUR", "UTC")
    config["sources"]["pv"].update(
        enabled=True,
        arrays=[
            [
                {
                    **_forecast("sensor.east", "rows"),
                    "unit": "kWh",
                    "value_kind": "energy",
                }
            ]
        ],
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    options = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    labels = [option["label"] for option in options]
    assert any("Grupa PV 1" in label and "sensor.east" in label for label in labels)
    assert any("Zakup" in label for label in labels)
    assert any("Powrót do źródeł" in label for label in labels)


async def test_polish_required_source_removal_explains_replacement_in_polish(
    recorder_mock, hass, enable_custom_integrations
):
    hass.config.language = "pl"
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    choices = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    buy = next(item["value"] for item in choices if item["label"].startswith("Zakup"))
    await hass.config_entries.options.async_configure(fid, {"source": buy})
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_remove"}
    )
    rejected = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert rejected["errors"] == {"base": "invalid_source"}
    detail = rejected["description_placeholders"]["detail"]
    assert "Zastąp" in detail
    assert "źródło" in detail
