"""Bounded advisory dispatch for grid, solar, and optional battery energy."""

from dataclasses import replace
from datetime import UTC
from math import isfinite
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .charge_benefit import constrain_charge_benefit, validate_charge_benefit
from .charge_price import price_allows_grid_charge
from .daily_energy import daily_export_rows, day_fractions
from .dispatch_policy import MODES, constrain_modes, validate_modes
from .export_benefit import constrain_export_benefit, validate_export_benefit
from .idle_drain import cumulative, drain_schedule
from .models import (
    FlexibleLoadPlan,
    FlexibleLoadRequest,
    Flow,
    InputError,
    Plan,
    Problem,
    SolveError,
)
from .normalize import validate_problem

_TOL = 1e-6
_UTC = UTC


class _Model:
    def __init__(self) -> None:
        self.exact_mip_gap = False
        self.cost: list[float] = []
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.integrality: list[int] = []
        self.rows: list[tuple[dict[int, float], float, float]] = []

    def variable(
        self, *, upper: float = np.inf, cost: float = 0.0, binary: bool = False
    ) -> int:
        index = len(self.cost)
        self.cost.append(cost)
        self.lower.append(0.0)
        self.upper.append(1.0 if binary else upper)
        self.integrality.append(int(binary))
        return index

    def constrain(self, terms: dict[int, float], lower: float, upper: float) -> None:
        self.rows.append((terms, lower, upper))

    def solve(self, time_limit_s: float) -> np.ndarray:
        entries = [
            (row_index, column, coefficient)
            for row_index, (terms, _, _) in enumerate(self.rows)
            for column, coefficient in terms.items()
            if coefficient
        ]
        matrix = coo_matrix(
            (
                [item[2] for item in entries],
                ([item[0] for item in entries], [item[1] for item in entries]),
            ),
            shape=(len(self.rows), len(self.cost)),
        ).tocsc()
        result = milp(
            c=np.asarray(self.cost),
            integrality=np.asarray(self.integrality),
            bounds=Bounds(self.lower, self.upper),
            constraints=LinearConstraint(
                matrix,
                [row[1] for row in self.rows],
                [row[2] for row in self.rows],
            ),
            options={
                "time_limit": time_limit_s,
                **({"mip_rel_gap": 0} if self.exact_mip_gap else {}),
            },
        )
        if result.status != 0 or result.x is None:
            reasons = {1: "timeout", 2: "infeasible", 3: "unbounded"}
            raise SolveError(
                reasons.get(result.status, "solver_failure"), result.message
            )
        return np.asarray(result.x)


def _check(condition: bool, detail: str) -> None:
    if not condition:
        raise SolveError("solver_failure", f"invalid solver result: {detail}")


def _close(actual: float, expected: float, detail: str) -> None:
    _check(abs(actual - expected) <= _TOL, detail)


def _floor_windows(window_ids: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    """Group slot indices by the window id the autonomy module assigned them."""
    windows: list[list[int]] = []
    for index, window_id in enumerate(window_ids):
        if windows and window_id == window_ids[index - 1]:
            windows[-1].append(index)
        else:
            windows.append([index])
    return tuple(tuple(window) for window in windows)


def _validate_solution(
    problem: Problem,
    vectors: list[dict[str, int]],
    values: np.ndarray,
    daily_fractions: list[dict[str, float]],
    budgets: dict[str, float],
) -> None:
    flexible_load = "flex_load" in vectors[0]
    _check(
        len(values) == max(max(vector.values()) for vector in vectors) + 1,
        "variable count",
    )
    _check(bool(np.all(np.isfinite(values))), "nonfinite variables")
    validate_export_benefit(problem, vectors, values)
    validate_charge_benefit(problem, vectors, values)
    battery = problem.battery
    previous_energy = battery.initial_kwh if battery else 0.0
    drains = drain_schedule(problem)
    spent = {day: 0.0 for day in budgets}
    floor_windows = (
        _floor_windows(problem.soc_target_window)
        if battery and problem.soc_target_kwh and problem.soc_target_weight > 0
        else ()
    )
    slack_index_by_slot = {
        t: vectors[window[0]]["soc_slack"] for window in floor_windows for t in window
    }
    peak_index = vectors[0].get("peak_import")

    for index, (slot, variables, fractions) in enumerate(
        zip(problem.slots, vectors, daily_fractions, strict=True)
    ):

        def value(name, variables=variables):
            return float(values[variables[name]])

        duration = (
            slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)
        ).total_seconds() / 3600
        for name in variables:
            _check(value(name) >= -_TOL, f"negative {name}")
        for name in (
            "grid_mode",
            "battery_mode",
            "reserve_discharge",
            "load_above_solar",
            *(f"mode_{mode}" for mode in MODES),
        ):
            if name in variables:
                _check(
                    min(abs(value(name)), abs(value(name) - 1)) <= _TOL,
                    f"fractional {name}",
                )
        if "mode_HOLD" in variables:
            _close(
                sum(value(f"mode_{mode}") for mode in MODES), 1, "one operating mode"
            )
        gin, gout, curt = value("gin"), value("gout"), value("curt")
        if index in slack_index_by_slot:
            target = problem.soc_target_kwh[index]
            if target > 0:
                slack = float(values[slack_index_by_slot[index]])
                _check(slack >= target - value("energy") - _TOL, "autonomy floor slack")
        if peak_index is not None:
            _check(float(values[peak_index]) >= gin / duration - _TOL, "peak import")
        if "cap_import" in variables:
            cap = problem.soft_import_cap_kw * duration
            _check(gin - value("cap_import") <= cap + _TOL, "soft import cap")
        if "cap_export" in variables:
            cap = problem.soft_export_cap_kw * duration
            _check(gout - value("cap_export") <= cap + _TOL, "soft export cap")
        extra_load = value("flex_load") if flexible_load else 0.0
        charge = value("bc") if battery else 0.0
        discharge = value("bd") if battery else 0.0
        _check(
            curt <= (slot.pv_kwh if problem.site.allow_curtailment else 0) + _TOL,
            "curtailment limit",
        )
        _check(
            gin <= problem.site.grid_import_kw * duration + _TOL, "grid import limit"
        )
        _check(
            gout <= problem.site.grid_export_kw * duration + _TOL, "grid export limit"
        )
        _check(min(gin, gout) <= _TOL, "simultaneous grid directions")
        _check(
            gin <= problem.site.grid_import_kw * duration * value("grid_mode") + _TOL,
            "grid import direction",
        )
        _check(
            gout
            <= problem.site.grid_export_kw * duration * (1 - value("grid_mode")) + _TOL,
            "grid export direction",
        )
        _check(
            abs(slot.pv_kwh - curt + discharge - charge)
            <= problem.site.inverter_kw * duration + _TOL,
            "inverter limit",
        )
        _close(
            slot.pv_kwh - curt + gin + discharge,
            slot.load_kwh + extra_load + gout + charge,
            "site balance",
        )
        _close(
            slot.pv_kwh - curt,
            value("pv_load")
            + value("pv_grid")
            + (value("pv_battery") if battery else 0),
            "solar allocation",
        )
        _close(
            gin,
            value("grid_load") + (value("grid_battery") if battery else 0),
            "grid allocation",
        )
        _close(
            slot.load_kwh + extra_load,
            value("pv_load")
            + value("grid_load")
            + (value("battery_load") if battery else 0),
            "load allocation",
        )
        _close(
            gout,
            value("pv_grid") + (value("battery_grid") if battery else 0),
            "export allocation",
        )

        if battery:
            _close(
                discharge,
                value("battery_load") + value("battery_grid"),
                "discharge allocation",
            )
            _close(
                charge, value("pv_battery") + value("grid_battery"), "charge allocation"
            )
            _check(
                charge <= battery.charge_kw * duration * value("battery_mode") + _TOL,
                "charge direction",
            )
            _check(
                discharge
                <= battery.discharge_kw * duration * (1 - value("battery_mode")) + _TOL,
                "discharge direction",
            )
            _check(min(charge, discharge) <= _TOL, "simultaneous battery directions")
            if not battery.allow_grid_charge or not price_allows_grid_charge(
                problem, slot
            ):
                _check(value("grid_battery") <= _TOL, "grid charging capability")
                _check(
                    charge <= max(slot.pv_kwh - slot.load_kwh - extra_load, 0) + _TOL,
                    "solar surplus charging",
                )
            if not price_allows_grid_charge(problem, slot):
                _check(
                    charge
                    <= max(
                        slot.pv_kwh - curt - slot.load_kwh - extra_load,
                        0,
                    )
                    + _TOL,
                    "price ceiling after curtailment",
                )
            if not battery.allow_battery_export:
                _check(value("battery_grid") <= _TOL, "battery export capability")
                _check(
                    discharge
                    <= max(slot.load_kwh + extra_load - slot.pv_kwh, 0) + _TOL,
                    "residual load discharge",
                )
            discharge_floor = min(
                battery.capacity_kwh * battery.minimum_soc_fraction, previous_energy
            )
            previous_energy += (
                battery.eta_charge * charge
                - discharge / battery.eta_discharge
                - drains[index]
            )
            discharge_floor = max(0.0, discharge_floor - drains[index])
            _close(value("energy"), previous_energy, "battery energy")
            _check(
                discharge_floor - _TOL
                <= previous_energy
                <= battery.capacity_kwh * battery.maximum_soc_fraction + _TOL,
                "battery SOC",
            )
            for day, fraction in fractions.items():
                if day in spent:
                    spent[day] += fraction * (charge + discharge)
    if problem.limit_export_to_pv:
        for row in daily_export_rows(problem):
            planned = sum(
                fraction.get(row["date"], 0)
                * (float(values[v["gout"]]) + float(values[v["curt"]]))
                for v, fraction in zip(vectors, daily_fractions, strict=True)
            )
            _check(
                planned <= max(0.0, row["remaining_export_kwh"]) + _TOL,
                f"daily export exceeds PV generation: {row['date']}",
            )
    if battery:
        if problem.terminal_mode == "preserve_initial":
            # Standby loss is a tax, not a decision, so "leave it as you found
            # it" is measured against what the plan actually controls. Holding
            # the raw initial SOC here would be infeasible for an install that
            # can neither grid-charge nor see PV inside the horizon.
            terminal = battery.initial_kwh - sum(drains)
            _check(previous_energy >= terminal - _TOL, "terminal SOC")
        for day, total in spent.items():
            _check(total <= budgets[day] + _TOL, f"daily throughput {day}")
    if flexible_load:
        _close(
            sum(float(values[v["flex_load"]]) for v in vectors),
            float(values[vectors[0]["flex_target"]]),
            "flexible load energy",
        )


def solve(problem: Problem, *, time_limit_s: float = 10.0) -> Plan:
    """Find a bounded least-cost advisory plan or raise a typed solver error."""
    return _solve(problem, flexible_load=None, time_limit_s=time_limit_s).plan


def solve_flexible_load(
    problem: Problem,
    request: FlexibleLoadRequest,
    *,
    time_limit_s: float = 10.0,
) -> FlexibleLoadPlan:
    """Find least-cost dispatch and schedule for one flexible energy target."""
    try:
        energy_kwh = float(request.energy_kwh)
        max_power_kw = float(request.max_power_kw)
    except (AttributeError, TypeError, ValueError) as err:
        raise InputError("flexible load values must be finite and positive") from err
    if (
        not isfinite(energy_kwh)
        or energy_kwh <= 0
        or not isfinite(max_power_kw)
        or max_power_kw <= 0
    ):
        raise InputError("flexible load values must be finite and positive")
    return _solve(
        problem,
        flexible_load=FlexibleLoadRequest(energy_kwh, max_power_kw),
        time_limit_s=time_limit_s,
    )


def _solve(
    problem: Problem,
    *,
    flexible_load: FlexibleLoadRequest | None,
    time_limit_s: float,
) -> FlexibleLoadPlan:
    validate_problem(problem)
    try:
        time_limit = float(time_limit_s)
    except (TypeError, ValueError) as err:
        raise InputError("time limit must be finite and positive") from err
    if not isfinite(time_limit) or time_limit <= 0:
        raise InputError("time limit must be finite and positive")

    budgets = dict(problem.remaining_daily_throughput_kwh)
    if len(budgets) != len(problem.remaining_daily_throughput_kwh):
        raise InputError("duplicate daily throughput date")
    zone = ZoneInfo(problem.timezone)
    model = _Model()
    vectors: list[dict[str, int]] = []
    daily_fractions: list[dict[str, float]] = []
    previous_energy_index: int | None = None
    battery = problem.battery
    drains = drain_schedule(problem)
    drained = cumulative(drains)

    for index, slot in enumerate(problem.slots):
        duration = (
            slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)
        ).total_seconds() / 3600
        variables = {
            "gin": model.variable(
                upper=problem.site.grid_import_kw * duration,
                cost=problem.import_weight * slot.buy_per_kwh
                + problem.import_kwh_weight,
            ),
            "gout": model.variable(
                upper=problem.site.grid_export_kw * duration,
                cost=-problem.export_weight
                * (slot.sell_per_kwh - problem.pv_export_margin),
            ),
            "curt": model.variable(
                upper=slot.pv_kwh if problem.site.allow_curtailment else 0
            ),
            "pv_load": model.variable(upper=slot.pv_kwh),
            "pv_grid": model.variable(upper=slot.pv_kwh),
            "grid_load": model.variable(upper=problem.site.grid_import_kw * duration),
            "grid_mode": model.variable(binary=True),
        }
        if flexible_load:
            variables["flex_load"] = model.variable(
                upper=flexible_load.max_power_kw * duration
            )
        if battery:
            variables.update(
                {
                    "bc": model.variable(
                        upper=battery.charge_kw * duration,
                        cost=battery.wear_per_kwh / 2,
                    ),
                    "bd": model.variable(
                        upper=battery.discharge_kw * duration,
                        cost=battery.wear_per_kwh / 2,
                    ),
                    "energy": model.variable(
                        upper=battery.capacity_kwh * battery.maximum_soc_fraction,
                        cost=-problem.terminal_value_per_kwh
                        if problem.terminal_mode == "value"
                        and slot is problem.slots[-1]
                        else 0,
                    ),
                    "pv_battery": model.variable(upper=slot.pv_kwh),
                    "grid_battery": model.variable(
                        upper=problem.site.grid_import_kw * duration
                        if battery.allow_grid_charge
                        and price_allows_grid_charge(problem, slot)
                        else 0
                    ),
                    "battery_load": model.variable(
                        upper=battery.discharge_kw * duration
                    ),
                    "battery_grid": model.variable(
                        upper=battery.discharge_kw * duration
                        if battery.allow_battery_export
                        else 0,
                        cost=problem.battery_export_penalty_per_kwh,
                    ),
                    "battery_mode": model.variable(binary=True),
                }
            )
            reserve = battery.capacity_kwh * battery.minimum_soc_fraction
            floor = max(0.0, min(reserve, battery.initial_kwh) - drained[index])
            model.lower[variables["energy"]] = floor
            if floor < reserve:
                # Below reserve, waiting or gradual charging remains feasible.
                # Any discharge must finish at/above the full reserve; a fixed
                # lowered energy bound alone would allow spending partial
                # recovery, or let standby loss open the floor to discharge.
                gate = variables["reserve_discharge"] = model.variable(binary=True)
                model.constrain(
                    {variables["bd"]: 1, gate: -battery.discharge_kw * duration},
                    -np.inf,
                    0,
                )
                model.constrain({variables["energy"]: 1, gate: -reserve}, 0, np.inf)
        v = variables
        model.constrain(
            {v["gin"]: 1, v["grid_mode"]: -problem.site.grid_import_kw * duration},
            -np.inf,
            0,
        )
        model.constrain(
            {v["gout"]: 1, v["grid_mode"]: problem.site.grid_export_kw * duration},
            -np.inf,
            problem.site.grid_export_kw * duration,
        )
        if problem.soft_import_cap_kw is not None and problem.cap_violation_weight > 0:
            cap = problem.soft_import_cap_kw * duration
            slack = max(0.0, problem.site.grid_import_kw * duration - cap)
            u = v["cap_import"] = model.variable(
                upper=slack, cost=problem.cap_violation_weight
            )
            model.constrain({v["gin"]: 1, u: -1}, -np.inf, cap)
        if problem.soft_export_cap_kw is not None and problem.cap_violation_weight > 0:
            cap = problem.soft_export_cap_kw * duration
            slack = max(0.0, problem.site.grid_export_kw * duration - cap)
            u = v["cap_export"] = model.variable(
                upper=slack, cost=problem.cap_violation_weight
            )
            model.constrain({v["gout"]: 1, u: -1}, -np.inf, cap)
        model.constrain(
            {v["curt"]: -1, **({v["bd"]: 1, v["bc"]: -1} if battery else {})},
            -problem.site.inverter_kw * duration - slot.pv_kwh,
            problem.site.inverter_kw * duration - slot.pv_kwh,
        )
        solar = {v["curt"]: 1, v["pv_load"]: 1, v["pv_grid"]: 1}
        if battery:
            solar[v["pv_battery"]] = 1
        model.constrain(solar, slot.pv_kwh, slot.pv_kwh)
        grid = {v["gin"]: 1, v["grid_load"]: -1}
        load = {v["pv_load"]: 1, v["grid_load"]: 1}
        if flexible_load:
            load[v["flex_load"]] = -1
        export = {v["gout"]: 1, v["pv_grid"]: -1}
        if battery:
            grid[v["grid_battery"]] = -1
            load[v["battery_load"]] = 1
            export[v["battery_grid"]] = -1
            model.constrain(
                {v["bd"]: 1, v["battery_load"]: -1, v["battery_grid"]: -1}, 0, 0
            )
            model.constrain(
                {v["bc"]: 1, v["pv_battery"]: -1, v["grid_battery"]: -1}, 0, 0
            )
            model.constrain(
                {v["bc"]: 1, v["battery_mode"]: -battery.charge_kw * duration},
                -np.inf,
                0,
            )
            model.constrain(
                {v["bd"]: 1, v["battery_mode"]: battery.discharge_kw * duration},
                -np.inf,
                battery.discharge_kw * duration,
            )
            energy = {
                v["energy"]: 1,
                v["bc"]: -battery.eta_charge,
                v["bd"]: 1 / battery.eta_discharge,
            }
            if previous_energy_index is not None:
                energy[previous_energy_index] = -1
            initial = battery.initial_kwh if previous_energy_index is None else 0
            # Leave the row untouched when standby loss is off, so a disabled
            # setting keeps the solver model byte-identical to 0.1.18.
            balance = initial - drains[index] if drains[index] else initial
            model.constrain(energy, balance, balance)
            previous_energy_index = v["energy"]
            solar_surplus = slot.pv_kwh - slot.load_kwh
            load_above_solar = None
            if (
                flexible_load
                and solar_surplus > 0
                and (
                    not battery.allow_grid_charge
                    or not price_allows_grid_charge(problem, slot)
                    or not battery.allow_battery_export
                )
            ):
                load_above_solar = model.variable(binary=True)
                v["load_above_solar"] = load_above_solar
                model.constrain(
                    {
                        v["flex_load"]: 1,
                        load_above_solar: -model.upper[v["flex_load"]],
                    },
                    -np.inf,
                    solar_surplus,
                )
                model.constrain(
                    {v["flex_load"]: 1, load_above_solar: -solar_surplus},
                    0,
                    np.inf,
                )
            if not battery.allow_grid_charge or not price_allows_grid_charge(
                problem, slot
            ):
                if not flexible_load or solar_surplus <= 0:
                    model.constrain({v["bc"]: 1}, -np.inf, max(solar_surplus, 0))
                else:
                    charge_cap = model.upper[v["bc"]]
                    model.constrain(
                        {
                            v["bc"]: 1,
                            v["flex_load"]: 1,
                            load_above_solar: -model.upper[v["flex_load"]],
                        },
                        -np.inf,
                        solar_surplus,
                    )
                    model.constrain(
                        {v["bc"]: 1, load_above_solar: charge_cap},
                        -np.inf,
                        charge_cap,
                    )
            if (
                not price_allows_grid_charge(problem, slot)
                and problem.site.allow_curtailment
            ):
                # Curtailing solar cannot create an apparent surplus for charging.
                # With no charge, the idle/discharge direction permits curtailment.
                model.constrain(
                    {v["bc"]: 1, v["curt"]: 1, v["battery_mode"]: slot.pv_kwh},
                    -np.inf,
                    max(slot.pv_kwh - slot.load_kwh, 0) + slot.pv_kwh,
                )
            if not battery.allow_battery_export:
                if not flexible_load:
                    model.constrain(
                        {v["bd"]: 1},
                        -np.inf,
                        max(slot.load_kwh - slot.pv_kwh, 0),
                    )
                elif solar_surplus <= 0:
                    model.constrain(
                        {v["bd"]: 1, v["flex_load"]: -1},
                        -np.inf,
                        -solar_surplus,
                    )
                else:
                    discharge_cap = model.upper[v["bd"]]
                    model.constrain(
                        {v["bd"]: 1, load_above_solar: -discharge_cap},
                        -np.inf,
                        0,
                    )
                    model.constrain(
                        {
                            v["bd"]: 1,
                            v["flex_load"]: -1,
                            load_above_solar: discharge_cap + solar_surplus,
                        },
                        -np.inf,
                        discharge_cap,
                    )
        model.constrain(grid, 0, 0)
        model.constrain(load, slot.load_kwh, slot.load_kwh)
        model.constrain(export, 0, 0)
        vectors.append(variables)
        daily_fractions.append(day_fractions(slot.start, slot.end, zone))

    if battery and problem.soc_target_kwh and problem.soc_target_weight > 0:
        for window in _floor_windows(problem.soc_target_window):
            slack = model.variable(
                upper=max(problem.soc_target_kwh[t] for t in window),
                cost=problem.soc_target_weight,
            )
            vectors[window[0]]["soc_slack"] = slack
            for t in window:
                target = problem.soc_target_kwh[t]
                if target > 0:
                    model.constrain({vectors[t]["energy"]: 1, slack: 1}, target, np.inf)

    if problem.peak_import_weight > 0:
        peak = model.variable(
            upper=problem.site.grid_import_kw, cost=problem.peak_import_weight
        )
        vectors[0]["peak_import"] = peak
        for slot, v in zip(problem.slots, vectors, strict=True):
            duration = (
                slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)
            ).total_seconds() / 3600
            model.constrain({v["gin"]: 1 / duration, peak: -1}, -np.inf, 0)

    if flexible_load:
        target = model.variable(upper=flexible_load.energy_kwh)
        model.lower[target] = flexible_load.energy_kwh
        vectors[0]["flex_target"] = target
        model.constrain(
            {
                **{variables["flex_load"]: 1 for variables in vectors},
                target: -1,
            },
            0,
            0,
        )

    if problem.limit_export_to_pv:
        for row in daily_export_rows(problem):
            model.constrain(
                {
                    v[name]: fraction[row["date"]]
                    for v, fraction in zip(vectors, daily_fractions, strict=True)
                    if row["date"] in fraction
                    for name in ("gout", "curt")
                },
                -np.inf,
                # Export already above today's PV cannot be undone; it only
                # forbids more. A negative bound would make every plan infeasible.
                max(0.0, row["remaining_export_kwh"]),
            )
    if battery:
        constrain_modes(
            model, problem, vectors, flexible_load=flexible_load is not None
        )
        if problem.terminal_mode == "preserve_initial":
            model.constrain(
                {previous_energy_index: 1},
                battery.initial_kwh - sum(drains),
                np.inf,
            )
        for day, budget in budgets.items():
            terms = {}
            for variables, fractions in zip(vectors, daily_fractions, strict=True):
                fraction = fractions.get(day, 0)
                if fraction:
                    terms[variables["bc"]] = fraction
                    terms[variables["bd"]] = fraction
            if terms:
                model.constrain(terms, -np.inf, budget)

    constrain_export_benefit(model, problem, vectors)
    constrain_charge_benefit(model, problem, vectors)
    values = model.solve(time_limit)
    _validate_solution(problem, vectors, values, daily_fractions, budgets)
    flows = tuple(
        Flow(
            float(values[v["gin"]]),
            float(values[v["gout"]]),
            float(values[v["bc"]]) if battery else 0.0,
            float(values[v["bd"]]) if battery else 0.0,
            float(values[v["curt"]]),
            float(values[v["energy"]]) if battery else 0.0,
            ("charge" if values[v["battery_mode"]] > 0.5 else "discharge")
            if battery
            else None,
            next(
                (
                    mode
                    for mode in MODES
                    if f"mode_{mode}" in v and values[v[f"mode_{mode}"]] > 0.5
                ),
                None,
            ),
        )
        for v in vectors
    )
    mode_problem = (
        replace(
            problem,
            slots=tuple(
                replace(slot, load_kwh=slot.load_kwh + float(values[v["flex_load"]]))
                for slot, v in zip(problem.slots, vectors, strict=True)
            ),
        )
        if flexible_load
        else problem
    )
    validate_modes(mode_problem, flows)
    grid_cost = sum(
        slot.buy_per_kwh * flow.grid_import_kwh
        - slot.sell_per_kwh * flow.grid_export_kwh
        for slot, flow in zip(problem.slots, flows, strict=True)
    )
    wear_cost = (
        sum(
            battery.wear_per_kwh * (flow.charge_kwh + flow.discharge_kwh) / 2
            for flow in flows
        )
        if battery
        else 0.0
    )
    terminal_credit = (
        problem.terminal_value_per_kwh * flows[-1].end_soc_kwh
        if battery and problem.terminal_mode == "value"
        else 0.0
    )
    objective = grid_cost + wear_cost - terminal_credit
    _check(isfinite(objective), "nonfinite objective")
    autonomy_shortfall_kwh = sum(
        float(values[v["soc_slack"]]) for v in vectors if "soc_slack" in v
    )
    cap_violation_kwh = sum(
        float(values[v[name]])
        for v in vectors
        for name in ("cap_import", "cap_export")
        if name in v
    )
    peak_import_kw = (
        float(values[vectors[0]["peak_import"]]) if "peak_import" in vectors[0] else 0.0
    )
    episodes, reserve = validate_export_benefit(problem, vectors, values)
    charge_episodes, charge_reserve = validate_charge_benefit(problem, vectors, values)
    return FlexibleLoadPlan(
        Plan(
            flows,
            objective,
            grid_cost,
            wear_cost,
            terminal_credit,
            episodes,
            reserve,
            charge_episodes,
            charge_reserve,
            autonomy_shortfall_kwh,
            cap_violation_kwh,
            peak_import_kw,
        ),
        tuple(float(values[v["flex_load"]]) for v in vectors) if flexible_load else (),
    )
