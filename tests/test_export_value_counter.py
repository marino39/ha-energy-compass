"""Run the export value counter blueprint in Home Assistant."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.core import callback
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util

BLUEPRINT = (
    Path(__file__).resolve().parents[1]
    / "blueprints/automation/energy_compass/export_value_counter.yaml"
)
PATH = "energy_compass/export_value_counter.yaml"
EXPORT = "sensor.test_export"
PRICES = "sensor.test_export_prices"
COUNTER = "input_number.test_export_value"
NOW = datetime(2026, 9, 25, 10, 7, tzinfo=UTC)


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


def compact(t):
    return t.strftime("%Y%m%dT%H%MZ")


async def setup(hass, prices, state="0.5", step=5):
    assert await async_setup_component(
        hass,
        "input_number",
        {
            "input_number": {
                "test_export_value": {
                    "min": 0,
                    "max": 1000000,
                    "step": 0.0001,
                    "mode": "box",
                }
            }
        },
    )
    hass.states.async_set(PRICES, state, {"prices": prices})
    hass.states.async_set(EXPORT, "100.0")
    with loaded_blueprint():
        assert await async_setup_component(
            hass,
            automation.DOMAIN,
            {
                automation.DOMAIN: {
                    "alias": "counter",
                    "use_blueprint": {
                        "path": PATH,
                        "input": {
                            "export_energy": EXPORT,
                            "export_prices": PRICES,
                            "counter": COUNTER,
                            "max_step_kwh": step,
                        },
                    },
                }
            },
        )
    await hass.async_block_till_done()


def value(hass):
    return float(hass.states.get(COUNTER).state)


def quarter(start, price, fmt=compact):
    return {
        "start": fmt(start),
        "end": fmt(start + timedelta(minutes=15)),
        "price": price,
    }


@pytest.mark.freeze_time(NOW)
@pytest.mark.parametrize("fmt", [compact, datetime.isoformat], ids=["compact", "iso"])
async def test_increase_is_valued_at_the_current_quarter(hass, fmt):
    q = NOW.replace(minute=0)
    await setup(
        hass,
        [quarter(q - timedelta(minutes=15), 9, fmt), quarter(q, 0.6, fmt)],
        state="9",
    )
    hass.states.async_set(EXPORT, "101.5")
    await hass.async_block_till_done()
    assert value(hass) == pytest.approx(0.9)


@pytest.mark.freeze_time(NOW)
async def test_state_is_the_fallback_price(hass):
    await setup(hass, [], state="0.4")
    hass.states.async_set(EXPORT, "102")
    await hass.async_block_till_done()
    assert value(hass) == pytest.approx(0.8)


@pytest.mark.freeze_time(NOW)
@pytest.mark.parametrize("new", ["unavailable", "99", "120"])
async def test_gaps_decreases_and_jumps_are_not_counted(hass, new):
    await setup(hass, [], state="0.4")
    hass.states.async_set(EXPORT, new)
    await hass.async_block_till_done()
    assert value(hass) == 0


@pytest.mark.freeze_time(NOW)
async def test_missing_price_adds_nothing_and_logs(hass, caplog):
    assert await async_setup_component(hass, "system_log", {})
    await setup(hass, [], state="unknown")
    hass.states.async_set(EXPORT, "101")
    await hass.async_block_till_done()
    assert value(hass) == 0
    assert "no price for 1.0 kWh" in caplog.text
