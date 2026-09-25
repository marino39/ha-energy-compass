"""Exercise the RCE sell-price example with Home Assistant template rendering."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
import yaml
from homeassistant.helpers.template import Template

from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)
from custom_components.energy_compass.sources.prices import price_series

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "rce-sell-price.yaml"
RAW = "sensor.energy_compass_rce_raw"
ENTITY_ID = "sensor.energy_compass_rce_export_forecast"
NOW = datetime(2026, 9, 25, 10, 7, tzinfo=UTC)


@pytest.fixture
def example():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def raw_rows(
    start=datetime(2026, 9, 25, tzinfo=UTC), count=192, price=lambda i: 400 + i
):
    """PSE rows: dtime_utc is the END of each quarter hour, prices in PLN/MWh."""
    return [
        {
            "dtime_utc": (start + timedelta(minutes=15 * (i + 1))).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "rce_pln": price(i),
            "business_date": "2026-09-25",
        }
        for i in range(count)
    ]


def render(hass, freezer, example, rows, fetched=NOW, **settings):
    freezer.move_to(NOW)
    hass.states.async_set(RAW, fetched.isoformat(), {"value": rows})
    block = example["template"][0]
    variables = dict(block["variables"]) | settings
    for name, value in variables.copy().items():
        if isinstance(value, str) and "{" in value:
            variables[name] = Template(value, hass).async_render(
                variables, parse_result=True
            )
    sensor = block["sensor"][0]
    available = Template(sensor["availability"], hass).async_render(
        variables, parse_result=True
    )
    return available, variables["current_price"], variables["sell_rows"]


def test_rest_sensor_matches_raw_entity(example):
    rest = example["sensor"][0]
    assert rest["platform"] == "rest"
    assert rest["unique_id"] == "energy_compass_rce_raw"
    assert example["template"][0]["variables"]["source"] == RAW


def test_prices_apply_floor_and_multiplier(hass, freezer, example):
    rows = raw_rows(price=lambda i: -50 if i == 41 else 400)
    available, current, prices = render(hass, freezer, example, rows)
    assert available is True
    assert len(prices) == 192
    # 10:07 UTC is inside the quarter ending 10:15, row index 40.
    assert current == pytest.approx(400 * 1.23 / 1000)
    assert prices[41]["price"] == 0  # negative RCE floored at zero
    assert prices[40] == {
        "start": "20260925T1000Z",
        "end": "20260925T1015Z",
        "price": pytest.approx(0.492),
    }


def test_multiplier_and_floor_are_settings(hass, freezer, example):
    _, current, _ = render(
        hass, freezer, example, raw_rows(price=lambda i: -50), multiplier=1, floor=-100
    )
    assert current == pytest.approx(-0.05)


def test_consumer_contract(hass, freezer, example):
    _, current, prices = render(hass, freezer, example, raw_rows())
    snapshot = {
        ENTITY_ID: {
            "state": str(current),
            "attributes": {"prices": prices, "published_at": NOW.isoformat()},
            "last_updated": NOW.isoformat(),
        }
    }
    binding = IntervalBinding(
        entity=EntityBinding(ENTITY_ID, attribute="prices"),
        value_path="price",
        start_path="start",
        end_path="end",
        unit="PLN/kWh",
        value_kind="price",
        published_path="attributes.published_at",
        max_age_seconds=4500,
    )
    parsed = price_series(snapshot, binding, NOW, multiplier=1, addition_per_kwh=0)
    covering = [row for row in parsed if row.start <= NOW < row.end]
    assert len(covering) == 1
    assert covering[0].value == pytest.approx(current)
    assert all(right.start == left.end for left, right in pairwise(parsed))
    assert all(row.end - row.start == timedelta(minutes=15) for row in parsed)


@pytest.mark.parametrize(
    "case",
    ["stale", "no_current_row", "bad_row", "empty"],
)
def test_unavailable_instead_of_a_wrong_price(hass, freezer, example, case):
    rows = raw_rows()
    fetched = NOW
    if case == "stale":
        fetched = NOW - timedelta(hours=2)
    elif case == "no_current_row":
        rows = raw_rows(start=datetime(2026, 9, 26, tzinfo=UTC))
    elif case == "bad_row":
        rows[3]["rce_pln"] = "n/a"
    elif case == "empty":
        rows = []
    available, _, prices = render(hass, freezer, example, rows, fetched=fetched)
    assert available is False
    if case in ("bad_row", "empty"):
        assert prices == []
