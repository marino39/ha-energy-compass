"""Render the optional card with real Home Assistant template semantics."""

from pathlib import Path

import pytest
from homeassistant.helpers.template import Template
from homeassistant.util import yaml as yaml_util

CARD = Path(__file__).resolve().parents[1] / "examples/dashboard.yaml"


@pytest.fixture
async def card(hass):
    await hass.config.async_set_time_zone("Europe/Warsaw")
    config = yaml_util.load_yaml(CARD)
    assert config["type"] == "markdown"
    return Template(config["content"], hass)


def set_entity(hass, suffix, state, attributes=None):
    domain = "binary_sensor" if suffix == "forecast_valid" else "sensor"
    hass.states.async_set(f"{domain}.replace_with_{suffix}", state, attributes or {})


async def test_future_boost_and_limit_are_visible_before_their_starts(hass, card):
    set_entity(hass, "forecast_valid", "on", {"coverage_complete": True})
    set_entity(hass, "consumption_compass", "NORMAL")
    set_entity(
        hass, "next_change", "2026-09-17T11:00:00+00:00", {"next_level": "BOOST"}
    )
    set_entity(
        hass,
        "next_boost_start",
        "2026-09-17T11:00:00+00:00",
        {"end": "2026-09-17T13:00:00+00:00"},
    )
    set_entity(hass, "next_cheap_start", "unavailable")
    set_entity(
        hass,
        "next_limit_start",
        "2026-09-17T18:00:00+00:00",
        {"end": "2026-09-17T20:00:00+00:00"},
    )
    set_entity(
        hass,
        "plan",
        "2026-09-17T10:00:00+00:00",
        {"window_status": {"cheap": "none_in_coverage"}},
    )
    rendered = card.async_render(parse_result=False)
    assert "**Now:** NORMAL" in rendered
    assert "Next different level:** BOOST at" in rendered
    assert "| Window | Start | End |\n|:--|:--|:--|\n| BOOST |" in rendered
    assert "BOOST |" in rendered and "13:00" in rendered and "15:00" in rendered
    assert "LIMIT |" in rendered and "20:00" in rendered and "22:00" in rendered
    assert "CHEAP | No window in known coverage" in rendered


async def test_repeated_local_hour_displays_distinct_offsets(hass, card):
    set_entity(hass, "forecast_valid", "on", {"coverage_complete": True})
    set_entity(hass, "consumption_compass", "NORMAL")
    set_entity(hass, "next_change", "unavailable")
    set_entity(
        hass,
        "next_boost_start",
        "2026-10-25T00:30:00+00:00",
        {"end": "2026-10-25T01:00:00+00:00"},
    )
    set_entity(hass, "next_cheap_start", "unavailable")
    set_entity(
        hass,
        "next_limit_start",
        "2026-10-25T01:30:00+00:00",
        {"end": "2026-10-25T02:00:00+00:00"},
    )
    set_entity(hass, "plan", "2026-10-25T00:00:00+00:00")
    rendered = card.async_render(parse_result=False)
    assert "02:30 CEST" in rendered
    assert "02:30 CET" in rendered


@pytest.mark.parametrize("case", ["no_window", "missing_tomorrow", "stale"])
async def test_missing_and_stale_render_honestly(hass, card, case):
    set_entity(
        hass,
        "forecast_valid",
        "off" if case == "stale" else "on",
        {
            "coverage_complete": case == "no_window",
            "missing_sources": ["tomorrow_tariff"]
            if case == "missing_tomorrow"
            else [],
        },
    )
    set_entity(hass, "consumption_compass", "NORMAL")
    for suffix in (
        "next_change",
        "next_boost_start",
        "next_cheap_start",
        "next_limit_start",
    ):
        set_entity(hass, suffix, "unavailable")
    set_entity(
        hass,
        "plan",
        "2026-09-17T10:00:00+00:00" if case != "stale" else "unavailable",
        {
            "window_status": {
                "boost": "none_in_coverage",
                "cheap": "none_in_coverage",
                "limit": "none_in_coverage",
            }
        },
    )
    rendered = card.async_render(parse_result=False)
    if case == "stale":
        assert "Forecast is stale or unavailable" in rendered
        assert "**Now:** NORMAL" not in rendered
    else:
        assert "BOOST | No window in known coverage" in rendered
        assert "LIMIT | No window in known coverage" in rendered
        if case == "missing_tomorrow":
            assert "coverage ends before" in rendered
            assert "tomorrow_tariff" in rendered
