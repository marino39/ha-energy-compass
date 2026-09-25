"""Load the Deye controller blueprint and its package in Home Assistant itself."""

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.core import callback
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "deye_controller_build", ROOT / "tools/deye_controller/build.py"
)
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
BLUEPRINT = ROOT / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"
PACKAGE = ROOT / "packages/energy_compass_deye.yaml"
PATH = "energy_compass/deye_solarman_controller.yaml"
PREFIX = "inverter_deye_program_"
INPUTS = {
    "plan_entity": "sensor.test_plan",
    "optimizer_entity": "sensor.test_optimizer_status",
    "valid_entity": "binary_sensor.test_forecast_valid",
    "alert_entity": "binary_sensor.test_alert",
    "compass_entity": "sensor.test_consumption_compass",
    "solarman_device": "test_solarman_device",
    "charge_entity": "number.inverter_deye_battery_max_charging_current",
    "discharge_entity": "number.inverter_deye_battery_max_discharging_current",
    "grid_entity": "number.inverter_deye_battery_grid_charging_current",
    "operation_entity": "select.inverter_deye_battery_operation_mode",
    "soc_entity": "sensor.inverter_deye_battery",
    "voltage_entity": "sensor.inverter_deye_battery_voltage",
    "telemetry_entities": ["sensor.inverter_deye_battery"],
}


def program_entities(prefix=PREFIX):
    for i in range(1, 7):
        yield f"number.{prefix}{i}_power", "8000"
        yield f"number.{prefix}{i}_voltage", "49.6"
        yield f"number.{prefix}{i}_soc", "10"
        yield f"select.{prefix}{i}_charging", "Disabled"
        yield f"time.{prefix}{i}_time", f"{(i - 1) * 4:02d}:00:00"


@pytest.fixture
def expected_lingering_timers() -> bool:
    """The minute trigger and now()-based package templates keep timers."""
    return True


@contextmanager
def loaded_blueprint():
    original = models.DomainBlueprints._load_blueprint

    @callback
    def load(self, path):
        if path != PATH:
            return original(self, path)
        return models.Blueprint(
            yaml_util.load_yaml(BLUEPRINT),
            expected_domain=self.domain,
            path=path,
            schema=automation.config.AUTOMATION_BLUEPRINT_SCHEMA,
        )

    with patch.object(models.DomainBlueprints, "_load_blueprint", load):
        yield


async def setup_package(hass):
    package = yaml.safe_load(PACKAGE.read_text())
    for domain in ["input_select", "input_boolean", "input_datetime", "template"]:
        assert await async_setup_component(hass, domain, {domain: package[domain]})
    await hass.async_block_till_done()


async def setup_controller(hass, **inputs):
    with loaded_blueprint():
        assert await async_setup_component(
            hass,
            automation.DOMAIN,
            {
                automation.DOMAIN: {
                    "id": "test_deye",
                    "alias": "test_deye",
                    "use_blueprint": {"path": PATH, "input": INPUTS | inputs},
                }
            },
        )
    await hass.async_block_till_done()


def test_committed_outputs_match_generator():
    stale = [
        path.relative_to(ROOT)
        for path, text in build.outputs().items()
        if path.read_text() != text
    ]
    assert not stale, "run python tools/deye_controller/build.py"


@pytest.mark.parametrize("old_writers", [None, ["automation.legacy_charging"]])
async def test_blueprint_instance_is_valid(hass, old_writers):
    await setup_package(hass)
    inputs = {} if old_writers is None else {"old_writers": old_writers}
    await setup_controller(hass, **inputs)
    state = hass.states.get("automation.test_deye")
    assert state is not None and state.state == "on"


async def test_package_publishes_tou_settings(hass):
    for entity_id, value in program_entities():
        hass.states.async_set(entity_id, value)
    await setup_package(hass)
    state = hass.states.get("sensor.energy_compass_deye_tou_settings")
    assert state.state == "30"
    assert state.attributes["prefix"] == PREFIX
    assert state.attributes["values"][f"number.{PREFIX}3_soc"] == "10"
    assert state.attributes["values"][f"time.{PREFIX}6_time"] == "20:00:00"
    assert hass.states.get("sensor.energy_compass_deye_next_tou").state not in [
        "unknown",
        "unavailable",
    ]

    hass.states.async_set(f"number.{PREFIX}2_soc", "40")
    await hass.async_block_till_done()
    state = hass.states.get("sensor.energy_compass_deye_tou_settings")
    assert state.attributes["values"][f"number.{PREFIX}2_soc"] == "40"


async def test_tou_setting_change_runs_controller(hass):
    for entity_id, value in program_entities():
        hass.states.async_set(entity_id, value)
    await setup_package(hass)
    await setup_controller(hass)
    before = hass.states.get("automation.test_deye").attributes["last_triggered"]

    hass.states.async_set(f"number.{PREFIX}4_power", "5000")
    await hass.async_block_till_done()

    after = hass.states.get("automation.test_deye").attributes["last_triggered"]
    assert after is not None and after != before


def test_custom_prefix_package():
    package = build.package("inverter_2_program_")
    text = yaml.dump(package)
    assert "inverter_2_program_" in text
    assert PREFIX not in text
