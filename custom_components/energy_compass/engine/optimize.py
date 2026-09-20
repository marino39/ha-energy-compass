"""Bounded advisory dispatch for grid, solar, and optional battery energy."""

from dataclasses import replace
from datetime import UTC
from math import isfinite
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .charge_price import price_allows_grid_charge
from .daily_energy import daily_export_rows, day_fractions
from .dispatch_policy import MODES, constrain_modes, validate_modes
from .export_benefit import constrain_export_benefit, validate_export_benefit
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
    battery = problem.battery
    previous_energy = battery.initial_kwh if battery else 0.0
    spent = {day: 0.0 for day in budgets}

    for slot, variables, fractions in zip(
        problem.slots, vectors, daily_fractions, strict=True
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
                battery.eta_charge * charge - discharge / battery.eta_discharge
            )
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
                planned <= row["remaining_export_kwh"] + _TOL,
                f"daily export exceeds PV generation: {row['date']}",
            )
    if battery:
        if problem.terminal_mode == "preserve_initial":
            _check(previous_energy >= battery.initial_kwh - _TOL, "terminal SOC")
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

    for slot in problem.slots:
        duration = (
            slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)
        ).total_seconds() / 3600
        variables = {
            "gin": model.variable(
                upper=problem.site.grid_import_kw * duration, cost=slot.buy_per_kwh
            ),
            "gout": model.variable(
                upper=problem.site.grid_export_kw * duration, cost=-slot.sell_per_kwh
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
                        else 0
                    ),
                    "battery_mode": model.variable(binary=True),
                }
            )
            reserve = battery.capacity_kwh * battery.minimum_soc_fraction
            model.lower[variables["energy"]] = min(reserve, battery.initial_kwh)
            if battery.initial_kwh < reserve:
                # Below reserve, waiting or gradual charging remains feasible.
                # Any discharge must finish at/above the full reserve; a fixed
                # lowered energy bound alone would allow spending partial recovery.
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
            model.constrain(energy, initial, initial)
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
                row["remaining_export_kwh"],
            )
    if battery:
        constrain_modes(
            model, problem, vectors, flexible_load=flexible_load is not None
        )
        if problem.terminal_mode == "preserve_initial":
            model.constrain({previous_energy_index: 1}, battery.initial_kwh, np.inf)
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
    episodes, reserve = validate_export_benefit(problem, vectors, values)
    return FlexibleLoadPlan(
        Plan(
            flows,
            objective,
            grid_cost,
            wear_cost,
            terminal_credit,
            episodes,
            reserve,
        ),
        tuple(float(values[v["flex_load"]]) for v in vectors) if flexible_load else (),
    )
