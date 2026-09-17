from collections.abc import Mapping
from datetime import datetime
from itertools import pairwise
from typing import Any

from ..engine.models import InputError
from ..engine.normalize import Interval, resample_energy
from .bindings import IntervalBinding, merge_continuations, parse_intervals


def pv_series(
    states: Mapping[str, Any],
    binding: IntervalBinding,
    now: datetime,
    *,
    continuations: tuple[Mapping[str, Any], ...] = (),
    required_slots: tuple[tuple[datetime, datetime], ...] = (),
) -> tuple[Interval, ...]:
    """Normalize one PV array/site and deduplicate its forecast continuations."""
    if binding.value_kind not in ("energy", "power"):
        raise InputError("PV values must be energy or average power")
    all_series = (parse_intervals(states, binding, now),) + tuple(
        parse_intervals(snapshot, binding, now) for snapshot in continuations
    )
    merged = merge_continuations(all_series)
    if any(row.value < 0 for row in merged):
        raise InputError("PV energy must be nonnegative")
    if required_slots:
        resample_energy(merged, required_slots)
    return merged


def sum_pv_arrays(
    arrays: tuple[tuple[Interval, ...], ...],
    slots: tuple[tuple[datetime, datetime], ...] | None = None,
) -> tuple[Interval, ...]:
    """Sum only arrays explicitly supplied as independent additive sources."""
    if not arrays:
        return ()
    if slots is not None:
        values = tuple(resample_energy(array, slots) for array in arrays)
        return tuple(
            Interval(start, end, sum(series[index] for series in values))
            for index, (start, end) in enumerate(slots)
        )
    boundaries = sorted(
        {edge for array in arrays for row in array for edge in (row.start, row.end)}
    )
    slices = tuple(pairwise(boundaries))
    return sum_pv_arrays(arrays, slices)
