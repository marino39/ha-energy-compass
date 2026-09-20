"""Autonomy-reserve SOC floor: pure math over plain numeric sequences.

Deliberately has no dependency on `models.py` (or any other engine module):
every input is a plain tuple of numbers/datetimes and every output is plain
floats and ints, so this module stays usable regardless of how `Problem`
grows elsewhere.
"""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

_EPSILON = 1e-9


@dataclass(frozen=True)
class AutonomyResult:
    """Per-slot SOC floor and the window partition that produced it."""

    targets_kwh: tuple[float, ...]
    window_ids: tuple[int, ...]


def autonomy_targets(
    starts: tuple[datetime, ...],
    durations_h: tuple[float, ...],
    load_kwh: tuple[float, ...],
    pv_kwh: tuple[float, ...],
    *,
    reserve_kwh: float,
    usable_capacity_kwh: float,
    eta_discharge: float,
    timezone: str,
) -> AutonomyResult:
    """Compute the autonomy-reserve SOC floor and its window partition.

    A window is a maximal run of slots over which the running net surplus
    (pv - load), accumulated from the window's own start, first reaches zero
    and then never dips below zero again through the end of the local
    calendar day the recovery slot falls in (or the horizon end, whichever
    comes first). This is the *cumulative* partition, not a per-slot-sign
    one: it only ever inspects sums over elapsed real time, which is what
    makes it invariant under subdividing a slot into finer intervals — a
    per-slot-sign rule would not be, since a subdivided slot can straddle
    the sign change a coarser slot hides.

    Window ids are assigned from that partition alone, before any target is
    computed, so clamping a target to `reserve_kwh` or `usable_capacity_kwh`
    can never move a window boundary.

    `soc_target_t = min(usable_capacity_kwh, reserve_kwh + (1/eta_discharge)
    * sum of max(0, load - pv) over the slots strictly after t through the
    end of t's window)`. Empty inputs return an empty result. If the
    running surplus never recovers, the window runs to the horizon end.
    """
    slot_count = len(starts)
    if slot_count == 0:
        return AutonomyResult((), ())
    if not (len(durations_h) == len(load_kwh) == len(pv_kwh) == slot_count):
        raise ValueError(
            "starts, durations_h, load_kwh and pv_kwh must have equal length"
        )
    if any(duration <= 0 for duration in durations_h):
        raise ValueError("durations_h must be strictly positive")
    if eta_discharge <= 0:
        raise ValueError("eta_discharge must be strictly positive")

    tz = ZoneInfo(timezone)
    local_dates = [start.astimezone(tz).date() for start in starts]
    day_end = _day_end_indices(local_dates)
    net = [pv_kwh[i] - load_kwh[i] for i in range(slot_count)]

    windows = _partition_windows(net, day_end)

    window_ids = [0] * slot_count
    for window_id, (window_start, window_end) in enumerate(windows):
        for i in range(window_start, window_end + 1):
            window_ids[i] = window_id

    targets = [0.0] * slot_count
    for window_start, window_end in windows:
        length = window_end - window_start + 1
        deficits = [
            max(0.0, load_kwh[window_start + j] - pv_kwh[window_start + j])
            for j in range(length)
        ]
        suffix = [0.0] * (length + 1)
        for j in range(length - 1, -1, -1):
            suffix[j] = suffix[j + 1] + deficits[j]
        for j in range(length):
            remaining_deficit = suffix[j + 1]
            targets[window_start + j] = min(
                usable_capacity_kwh, reserve_kwh + remaining_deficit / eta_discharge
            )

    return AutonomyResult(tuple(targets), tuple(window_ids))


def _day_end_indices(local_dates: list[date]) -> list[int]:
    """Index of the last slot sharing each slot's local calendar date."""
    slot_count = len(local_dates)
    day_end = [0] * slot_count
    day_end[slot_count - 1] = slot_count - 1
    for i in range(slot_count - 2, -1, -1):
        day_end[i] = day_end[i + 1] if local_dates[i] == local_dates[i + 1] else i
    return day_end


def _partition_windows(net: list[float], day_end: list[int]) -> list[tuple[int, int]]:
    """Split the horizon into cumulative-surplus windows."""
    slot_count = len(net)
    windows: list[tuple[int, int]] = []
    window_start = 0
    while window_start < slot_count:
        window_end = _find_recovery_slot(net, day_end, window_start, slot_count)
        windows.append((window_start, window_end))
        window_start = window_end + 1
    return windows


def _find_recovery_slot(
    net: list[float], day_end: list[int], window_start: int, slot_count: int
) -> int:
    """First slot where the window's running surplus recovers and holds.

    A candidate at `t` only closes the window if the running sum (from
    `window_start`) stays non-negative through the end of `t`'s local day —
    a single positive slot inside an otherwise deficient day does not close
    it. Falls back to the horizon end when the surplus never recovers.
    """
    running = 0.0
    cumulative: list[float] = []
    for i in range(window_start, slot_count):
        running += net[i]
        cumulative.append(running)
    for offset, value in enumerate(cumulative):
        if value < -_EPSILON:
            continue
        t = window_start + offset
        day_boundary = min(day_end[t], slot_count - 1)
        segment = cumulative[offset : day_boundary - window_start + 1]
        if min(segment) >= -_EPSILON:
            return t
    return slot_count - 1
