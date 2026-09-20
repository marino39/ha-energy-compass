from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Level = Literal["BOOST", "CHEAP", "NORMAL", "LIMIT"]
MachineState = Literal[
    "CHARGE_GRID", "CHARGE_PV", "SELF_CONSUME", "DISCHARGE_GRID", "HOLD", "CURTAIL"
]


class InputError(ValueError):
    """Source or domain data cannot produce a trustworthy plan."""


class SolveError(RuntimeError):
    """The optimizer could not produce a usable plan."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    buy_per_kwh: float
    sell_per_kwh: float
    pv_kwh: float
    load_kwh: float


@dataclass(frozen=True)
class SiteLimits:
    inverter_kw: float
    grid_import_kw: float
    grid_export_kw: float
    allow_curtailment: bool


@dataclass(frozen=True)
class Battery:
    capacity_kwh: float
    minimum_soc_fraction: float
    maximum_soc_fraction: float
    initial_kwh: float
    charge_kw: float
    discharge_kw: float
    eta_charge: float
    eta_discharge: float
    wear_per_kwh: float
    allow_grid_charge: bool
    allow_battery_export: bool


@dataclass(frozen=True)
class Problem:
    slots: tuple[Slot, ...]
    site: SiteLimits
    battery: Battery | None
    terminal_mode: Literal["preserve_initial", "value"]
    terminal_value_per_kwh: float
    remaining_daily_throughput_kwh: tuple[tuple[str, float], ...]
    timezone: str
    minimum_mode_minutes: float = 60
    limit_export_to_pv: bool = True
    initial_battery_mode: Literal["charge", "discharge"] | None = None
    initial_battery_mode_since: datetime | None = None
    pv_generated_today_kwh: float = 0.0
    grid_exported_today_kwh: float = 0.0
    minimum_mode_power_kw: float = 0.1
    initial_dispatch_mode: MachineState | None = None
    initial_dispatch_mode_since: datetime | None = None
    minimum_export_episode_benefit: float = 1.0
    initial_export_active: bool = False
    maximum_grid_charge_price: float | None = None


@dataclass(frozen=True)
class Flow:
    grid_import_kwh: float
    grid_export_kwh: float
    charge_kwh: float
    discharge_kwh: float
    curtail_kwh: float
    end_soc_kwh: float
    battery_mode: Literal["charge", "discharge"] | None = None
    dispatch_mode: MachineState | None = None


@dataclass(frozen=True)
class Plan:
    flows: tuple[Flow, ...]
    objective: float
    grid_cost: float
    wear_cost: float
    terminal_credit: float
    new_export_episodes: int = 0
    export_episode_reserve: float = 0.0


@dataclass(frozen=True)
class FlexibleLoadRequest:
    energy_kwh: float
    max_power_kw: float


@dataclass(frozen=True)
class FlexibleLoadPlan:
    plan: Plan
    schedule_kwh: tuple[float, ...]


@dataclass(frozen=True)
class Opportunity:
    start: datetime
    end: datetime
    cost_per_kwh: float | None
    level: Level | None


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    levels: tuple[Level, ...]


@dataclass(frozen=True)
class ForecastSettings:
    lookback_days: int = 14
    group_weekends: bool = True
    minimum_samples: int = 2
    allow_fallback: bool = True


@dataclass(frozen=True)
class LoadCoverageSegment:
    start: datetime
    end: datetime
    method: Literal["history", "fallback", "daily_estimate", "forecast"]
    samples_available: int | None
    samples_required: int | None


@dataclass(frozen=True)
class LoadForecastResult:
    values: tuple[float, ...]
    coverage: tuple[LoadCoverageSegment, ...]


@dataclass(frozen=True)
class CompassSettings:
    display_horizon_hours: int = 24
    display_interval_minutes: int = 60
    reference_horizon_hours: int = 24
    probe_kwh: float = 1.0
    boost_ceiling: float = 0.01
    cheap_percentile: float = 25.0
    limit_percentile: float = 75.0
    limit_floor: float = 0.80
    short_coverage: Literal["absolute_fallback", "unavailable"] = "absolute_fallback"
    probe_time_limit_s: float = 2.0
    flexible_load_enabled: bool = True
    flexible_load_max_power_kw: float = 3.0
    flexible_price_degradation_percent: float = 15.0


def plan_monetary_cost(plan: Plan) -> float:
    """Real currency cost of a plan, free of any strategy weighting or penalty."""
    return plan.grid_cost + plan.wear_cost - plan.terminal_credit
