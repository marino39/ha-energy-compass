from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.helpers import selector

from custom_components.energy_compass.engine.models import InputError, SolveError
from custom_components.energy_compass.flow_schema import settings_schema, snapshot
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


def test_snapshot_preserves_identical_native_soc_report_time(hass, freezer):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    hass.states.async_set("sensor.soc", "50")
    first_updated = hass.states.get("sensor.soc").last_updated
    first_reported = hass.states.get("sensor.soc").last_reported
    freezer.move_to("2026-09-17T10:11:00+00:00")
    hass.states.async_set("sensor.soc", "50")
    repeated = hass.states.get("sensor.soc")
    assert repeated.last_updated == first_updated
    assert repeated.last_reported > first_reported
    config = default_configuration("EUR", "UTC")
    config["sources"].update(battery_enabled=True, soc={"entity_id": "sensor.soc"})
    copied = snapshot(hass, config)["sensor.soc"]
    assert copied["last_updated"] == first_updated.isoformat()
    assert copied["last_reported"] == repeated.last_reported.isoformat()


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


async def _setup_preview(hass):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Fresh",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    return fid, await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "preview"}
    )


@pytest.mark.parametrize(
    "acknowledgements",
    [
        {"confirm": True},
        {"confirm": True, "confirm_buy_source": True},
        {"confirm": True, "confirm_load_source": True},
    ],
)
async def test_new_installation_requires_both_input_acknowledgements(
    recorder_mock, hass, enable_custom_integrations, acknowledgements
):
    fid, preview = await _setup_preview(hass)
    fields = {str(key): key.default() for key in preview["data_schema"].schema}
    assert fields == {
        "confirm": False,
        "confirm_buy_source": False,
        "confirm_load_source": False,
    }
    assert "0 EUR/kWh" in preview["description_placeholders"]["preview"]
    assert "10 kWh" in preview["description_placeholders"]["preview"]
    result = await hass.config_entries.flow.async_configure(fid, acknowledgements)
    assert result["type"] == "form"
    assert result["errors"] == {"base": "input_acknowledgement_required"}
    assert not hass.config_entries.async_entries("energy_compass")


async def test_explicit_zero_and_daily_estimate_can_be_saved_with_acknowledgements(
    recorder_mock, hass, enable_custom_integrations
):
    fid, preview = await _setup_preview(hass)
    summary = preview["description_placeholders"]["preview"]
    assert "free import" in summary
    assert "daily estimate" in summary
    result = await hass.config_entries.flow.async_configure(
        fid,
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
    )
    assert result["type"] == "create_entry"
    assert all(not key.startswith("confirm") for key in result["data"])
    assert result["data"]["settings"]["buy_rate"] == 0
    assert result["data"]["settings"]["daily_load_kwh"] == 10


async def test_draft_preview_restarts_acknowledgements(
    recorder_mock, hass, enable_custom_integrations
):
    fid, _ = await _setup_preview(hass)
    await hass.config_entries.flow.async_configure(
        fid, {"confirm_buy_source": True, "confirm_load_source": True}
    )
    flow = hass.config_entries.flow._progress[fid]
    await flow.async_step_tariffs({"buy_rate": 0.2})
    preview = await flow.async_step_preview()
    assert {str(key): key.default() for key in preview["data_schema"].schema} == {
        "confirm": False,
        "confirm_buy_source": False,
        "confirm_load_source": False,
    }
    assert "0.2 EUR/kWh" in preview["description_placeholders"]["preview"]
    other_fid, other = await _setup_preview(hass)
    assert other_fid != fid
    assert all(key.default() is False for key in other["data_schema"].schema)


@pytest.mark.parametrize("kind", ["load", "pv"])
async def test_preview_rejects_real_infeasible_base_plan(
    recorder_mock, hass, enable_custom_integrations, kind
):
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["settings"]["grid_import_kw"] = 0
    if kind == "pv":
        from custom_components.energy_compass.config_models import PvSource
        from custom_components.energy_compass.sources.bindings import (
            EntityBinding,
            IntervalBinding,
        )

        flow._draft["settings"]["daily_load_kwh"] = 0
        flow._draft["sources"]["pv"] = PvSource(
            True,
            (
                (
                    IntervalBinding(
                        EntityBinding("sensor.pv", attribute="rows"),
                        start_path="start",
                        end_path="end",
                        value_path="energy",
                    ),
                ),
            ),
        ).to_dict()
        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()
        hass.states.async_set(
            "sensor.pv",
            "ok",
            {
                "rows": [
                    {
                        "start": (now - timedelta(minutes=1)).isoformat(),
                        "end": (now + timedelta(hours=24)).isoformat(),
                        "energy": 24,
                    }
                ]
            },
        )
    result = await flow.async_step_preview(
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "plan_infeasible"}
    summary = result["description_placeholders"]["preview"]
    assert "Source inputs: validated" in summary
    assert "Base plan: infeasible" in summary
    assert "Hardware" in summary
    assert not hass.config_entries.async_entries("energy_compass")


@pytest.mark.parametrize(
    "reason,error", [("timeout", "plan_timeout"), ("solver_failure", "optimizer_error")]
)
async def test_preview_maps_solver_failure_without_saving(
    recorder_mock, hass, enable_custom_integrations, reason, error
):
    fid, _ = await _setup_preview(hass)
    from custom_components.energy_compass import config_flow

    with patch.object(config_flow, "solve", side_effect=SolveError(reason)):
        result = await hass.config_entries.flow.async_configure(
            fid,
            {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
        )
    assert result["errors"] == {"base": error}
    assert "Source inputs: validated" in result["description_placeholders"]["preview"]
    assert (
        "Extra-consumption guidance: not checked"
        in result["description_placeholders"]["preview"]
    )
    assert not hass.config_entries.async_entries("energy_compass")


async def test_preview_uses_resolved_solver_budget_without_consumption_probes(
    recorder_mock, hass, enable_custom_integrations
):
    from custom_components.energy_compass import config_flow, runtime

    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["settings"]["solve_time_limit_s"] = 0.5
    with (
        patch.object(config_flow, "solve", wraps=config_flow.solve) as base_solve,
        patch.object(
            runtime, "analyze_consumption", side_effect=AssertionError("probe ran")
        ),
    ):
        result = await flow.async_step_preview()
    assert not result["errors"]
    assert (
        "Extra-consumption guidance: not checked"
        in result["description_placeholders"]["preview"]
    )
    assert base_solve.call_args.kwargs["time_limit_s"] == 0.5


async def test_feasible_base_without_extra_import_headroom_can_save(
    recorder_mock, hass, enable_custom_integrations
):
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["settings"].update(grid_import_kw=1, daily_load_kwh=24)
    preview = await flow.async_step_preview()
    assert "Base plan: feasible" in preview["description_placeholders"]["preview"]
    assert "guidance: not checked" in preview["description_placeholders"]["preview"]
    saved = await flow.async_step_preview(
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True}
    )
    assert saved["type"] == "create_entry"


async def test_source_failure_marks_base_plan_unchecked(
    recorder_mock, hass, enable_custom_integrations
):
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["sources"]["pv"]["enabled"] = True
    result = await flow.async_step_preview(
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True}
    )
    assert result["errors"] == {"base": "invalid_source"}
    summary = result["description_placeholders"]["preview"]
    assert "Source inputs: failed" in summary
    assert "Base plan: not checked" in summary
