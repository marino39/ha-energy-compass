"""Pure valuation steps of tools/export_value_backfill.py (no network)."""

import datetime as dt
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "export_value_backfill", ROOT / "tools/export_value_backfill.py"
)
tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tool)
WARSAW = ZoneInfo("Europe/Warsaw")


def pse_rows(count, price=lambda i: 400.0):
    return [
        {"dtime": f"x{i:03d}", "period": str(i), "rce_pln": price(i)}
        for i in range(count)
    ]


@pytest.mark.parametrize(
    ("day", "quarters"), [("2026-09-25", 96), ("2026-10-25", 100), ("2026-03-29", 92)]
)
def test_quarters_follow_local_day_length(day, quarters):
    day = dt.date.fromisoformat(day)
    prices = tool.quarter_prices(pse_rows(quarters), day, WARSAW)
    assert len(prices) == quarters
    first = min(prices)
    assert first == dt.datetime.combine(day, dt.time(), WARSAW).astimezone(dt.UTC)


def test_wrong_quarter_count_is_rejected():
    with pytest.raises(ValueError, match="expected 96"):
        tool.quarter_prices(pse_rows(95), dt.date(2026, 9, 25), WARSAW)


def test_floor_and_multiplier():
    prices = tool.quarter_prices(
        pse_rows(96, lambda i: -50.0 if i else 400.0), dt.date(2026, 9, 25), WARSAW
    )
    values = [prices[k] for k in sorted(prices)]
    assert values[0] == pytest.approx(0.492)
    assert values[1] == 0


def ms(t):
    return int(t.timestamp() * 1000)


def test_hourly_mean_and_five_minute_override():
    h0 = dt.datetime(2026, 9, 25, 8, tzinfo=dt.UTC)
    prices = {
        h0 + dt.timedelta(minutes=15 * i): p
        for i, p in enumerate([0.4, 0.4, 0.8, 0.8, 1, 1, 1, 1])
    }
    hours = [
        {"start": ms(h0), "change": 2.0},
        {"start": ms(h0 + dt.timedelta(hours=1)), "change": 1.0},
    ]
    five = [
        {"start": ms(h0 + dt.timedelta(hours=1, minutes=5 * i)), "change": 1 / 12}
        for i in range(12)
    ]
    rows = tool.value_hours(hours, prices, h0 + dt.timedelta(hours=2), five)
    assert (
        rows[0]["value"] == pytest.approx(2 * 0.6)
        and rows[0]["method"] == "hourly_mean"
    )
    assert rows[1]["value"] == pytest.approx(1.0) and rows[1]["method"] == "quarter"


def test_missing_price_for_export_is_an_error():
    h0 = dt.datetime(2026, 9, 25, 8, tzinfo=dt.UTC)
    with pytest.raises(ValueError, match="missing price"):
        tool.value_hours(
            [{"start": ms(h0), "change": 1.0}], {}, h0 + dt.timedelta(hours=1)
        )


def test_statistic_rows_are_cumulative_from_zero():
    rows = [
        {"start": "2026-09-25T08:00:00+00:00", "value": 1.5},
        {"start": "2026-09-25T09:00:00+00:00", "value": 2.0},
    ]
    stats, total = tool.statistic_rows(rows)
    assert stats[0] == {"start": "2026-09-25T07:00:00+00:00", "sum": 0.0, "state": 0.0}
    assert [s["sum"] for s in stats[1:]] == [1.5, 3.5] and total == 3.5
