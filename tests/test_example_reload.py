"""Trigger-based example sensors publish again right after a Template reload."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 25, 10, 7, tzinfo=UTC)


@pytest.fixture
def expected_lingering_timers() -> bool:
    return True


def rce_rows():
    return [
        {
            "dtime_utc": (
                NOW.replace(hour=0, minute=0) + timedelta(minutes=15 * (i + 1))
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "rce_pln": 400,
        }
        for i in range(96)
    ]


@pytest.mark.freeze_time(NOW)
@pytest.mark.parametrize(
    ("example", "entity_id"),
    [
        ("tariff-helper.yaml", "sensor.energy_compass_tariff_price"),
        ("rce-sell-price.yaml", "sensor.energy_compass_rce_export_forecast"),
    ],
)
async def test_state_is_restored_by_the_reload_event(hass, example, entity_id):
    await hass.config.async_set_time_zone("Europe/Warsaw")
    hass.states.async_set(
        "sensor.energy_compass_rce_raw", NOW.isoformat(), {"value": rce_rows()}
    )
    config = yaml_util.load_yaml(ROOT / "examples" / example)
    assert await async_setup_component(
        hass, "template", {"template": config["template"]}
    )
    await hass.async_block_till_done()
    with patch(
        "homeassistant.config.load_yaml_config_file",
        return_value={"template": config["template"]},
    ):
        await hass.services.async_call("template", "reload", blocking=True)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None and state.state not in ("unknown", "unavailable"), state
