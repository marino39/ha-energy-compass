"""Candidate LFP balance windows and their objective weight."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .models import BalanceWindow, Battery, Plan, Problem, Slot

DUE_LOOKAHEAD = timedelta(hours=24)


def _aligned(moment: datetime) -> bool:
    moment = moment.astimezone(UTC)
    return moment.minute == 0 and moment.second == 0 and moment.microsecond == 0


def candidate_windows(
    slots: tuple[Slot, ...],
    *,
    phase: str,
    now: datetime,
    hold_minutes: float,
    remaining_minutes: float,
    grid_charge_ok: tuple[bool, ...],
) -> tuple[BalanceWindow, ...]:
    """Offer hour-aligned hold windows the inverter can execute."""
    if phase == "ok" or not slots:
        return ()
    if phase == "holding":
        starts = [0]
        length = timedelta(minutes=remaining_minutes)
    else:
        length = timedelta(minutes=hold_minutes)
        limit = now + DUE_LOOKAHEAD if phase == "due" else None
        starts = [
            index
            for index, slot in enumerate(slots)
            if _aligned(slot.start) and (limit is None or slot.start < limit)
        ]
    windows = []
    for first in starts:
        end = slots[first].start + length
        members = tuple(
            index for index in range(first, len(slots)) if slots[index].start < end
        )
        if not members or slots[members[-1]].end < end:
            continue
        if all(slots[i].pv_kwh >= slots[i].load_kwh for i in members):
            mode = "CHARGE_PV"
        elif all(grid_charge_ok[i] for i in members):
            mode = "CHARGE_GRID"
        else:
            continue
        windows.append(BalanceWindow(members, mode))
    return tuple(windows)


def miss_cost(phase: str, value: float, days_overdue: int) -> float:
    """Price of skipping a balance in this horizon."""
    if phase == "eligible":
        return 0.1 * value
    if phase == "due":
        return value * (1 + days_overdue)
    if phase == "holding":
        return 10 * value
    return 0.0


def lift_slots(
    windows: tuple[BalanceWindow, ...], battery: Battery, slot_count: int
) -> tuple[int, ...]:
    """Slots whose energy may exceed the configured ceiling, up to capacity.

    From the slot before the earliest window to the horizon end: after a hold
    the pack can only drain back under the ceiling at load pace, so capping the
    following slots would make any window infeasible without battery export.
    """
    if battery.initial_kwh > battery.capacity_kwh * battery.maximum_soc_fraction + 1e-9:
        return tuple(range(slot_count))
    if not windows:
        return ()
    first = max(0, min(window.slots[0] for window in windows) - 1)
    return tuple(range(first, slot_count))


def pin_balance(problem: Problem, plan: Plan) -> Problem:
    """Freeze the dispatch plan's balance choice for consumption probes.

    A probe adds load; letting it re-decide the balance would price the
    balance, not the load. The lift slots stay so energy bounds match.
    """
    if not problem.balance_windows:
        return problem
    if plan.balance_start is None:
        return replace(problem, balance_windows=(), balance_miss_cost=0.0)
    window = next(
        w for w in problem.balance_windows if w.slots[0] == plan.balance_start
    )
    return replace(
        problem, balance_windows=(window,), balance_fixed=True, balance_miss_cost=0.0
    )
