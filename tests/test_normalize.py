from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_compass.engine.models import (
    InputError,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.normalize import (
    Interval,
    rce_to_kwh,
    resample_energy,
    resample_power,
    resample_prices,
    validate_problem,
)


def at(hour, minute=0):
    return datetime(2026, 9, 17, hour, minute, tzinfo=UTC)


def test_raw_negative_rce_and_mwh_unit():
    assert rce_to_kwh(-200.0, "PLN/MWh") == pytest.approx(-0.2)
    assert rce_to_kwh(200.0, "PLN/kWh") == 200.0


def test_energy_resampling_conserves_energy_for_partial_slots():
    source = (Interval(at(10), at(11), 2.0),)
    slots = ((at(10, 15), at(10, 45)), (at(10, 45), at(11)))
    assert resample_energy(source, slots) == pytest.approx((1.0, 0.5))


def test_power_resampling_multiplies_elapsed_hours():
    source = (Interval(at(10), at(11), 2.0),)
    assert resample_power(source, ((at(10, 15), at(10, 45)),)) == (1.0,)


def test_price_resampling_rejects_coverage_gap_and_mixed_settlements():
    source = (Interval(at(10), at(10, 15), -0.1), Interval(at(10, 30), at(10, 45), 0.2))
    with pytest.raises(InputError, match="coverage"):
        resample_prices(source, ((at(10), at(10, 45)),))
    with pytest.raises(InputError, match="settlement"):
        resample_prices(
            (source[0], Interval(at(10, 15), at(10, 30), 0.2)), ((at(10), at(10, 30)),)
        )


def test_problem_rejects_nonfinite_and_naive_time():
    site = SiteLimits(5, 5, 5, False)
    slot = Slot(at(10), at(11), float("nan"), 0.1, 0, 1)
    with pytest.raises(InputError):
        validate_problem(Problem((slot,), site, None, "preserve_initial", 0, (), "UTC"))
    slot = Slot(datetime(2026, 9, 17, 10), at(11), 0.1, 0.1, 0, 1)  # noqa: DTZ001
    with pytest.raises(InputError):
        validate_problem(Problem((slot,), site, None, "preserve_initial", 0, (), "UTC"))


def test_no_battery_problem_allows_zero_export_limit():
    slot = Slot(at(10), at(11), 0.2, 0, 0, 1)
    validate_problem(
        Problem(
            (slot,), SiteLimits(5, 5, 0, False), None, "preserve_initial", 0, (), "UTC"
        )
    )


def test_problem_accepts_repeated_local_hour_in_utc_order():
    zone = ZoneInfo("Europe/Warsaw")
    first = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    end = datetime(2026, 10, 25, 3, 30, tzinfo=zone)
    slots = (Slot(first, second, 0.2, 0, 0, 1), Slot(second, end, 0.2, 0, 0, 1))
    validate_problem(
        Problem(
            slots,
            SiteLimits(0, 5, 0, False),
            None,
            "preserve_initial",
            0,
            (),
            "Europe/Warsaw",
        )
    )


def test_problem_rejects_actual_gap_between_repeated_local_hours():
    zone = ZoneInfo("Europe/Warsaw")
    first = datetime(2026, 10, 25, 1, 30, tzinfo=zone)
    earlier_fold = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    later_fold = earlier_fold.replace(fold=1)
    end = datetime(2026, 10, 25, 3, 30, tzinfo=zone)
    slots = (
        Slot(first, earlier_fold, 0.2, 0, 0, 1),
        Slot(later_fold, end, 0.2, 0, 0, 1),
    )
    with pytest.raises(InputError, match="contiguous"):
        validate_problem(
            Problem(
                slots,
                SiteLimits(0, 5, 0, False),
                None,
                "preserve_initial",
                0,
                (),
                "Europe/Warsaw",
            )
        )
