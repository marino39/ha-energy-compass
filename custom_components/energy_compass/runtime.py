"""Immutable source assembly and bounded advisory computation."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import pairwise
from time import perf_counter
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period

from .config_models import NumericSetting, SourceConfig, resolve_numeric
from .daily_export import (
    DAILY_EXPORT_MEASUREMENTS,
    daily_export_active,
    daily_export_observations,
)
from .engine.consumption import (
    analyze_consumption,
    machine_snapshot,
    merge_windows,
    serialize_opportunity,
)
from .engine.daily_energy import daily_export_rows
from .engine.dispatch_policy import MODES, safety_exception
from .engine.models import (
    Battery,
    CompassSettings,
    ForecastSettings,
    InputError,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from .engine.normalize import finite, validate_problem
from .engine.optimize import solve
from .settings import validate_configuration
from .sources.battery import SocSettings, validate_soc
from .sources.bindings import (
    field,
    merge_continuations,
    parse_intervals,
    parse_timestamp,
    resolve_binding,
)
from .sources.history import load_for_slots_with_quality
from .sources.prices import price_for_slots
from .sources.pv import sum_pv_arrays
from .sources.throughput import resolve_daily_throughput


def restore_commitment(raw, now):
    """Discard malformed or future clocks without donating legacy direction age."""
    if not isinstance(raw, dict) or raw.get("mode") not in (
        *MODES,
        "charge",
        "discharge",
    ):
        return None
    try:
        since = parse_timestamp(raw.get("since"))
    except InputError:
        return None
    if since > now:
        return None
    result = {"mode": raw["mode"], "since": since.isoformat()}
    legacy = raw.get("legacy_direction")
    if isinstance(legacy, dict) and legacy.get("mode") in ("charge", "discharge"):
        restored = restore_commitment(legacy, now)
        if restored:
            result["legacy_direction"] = {
                key: restored[key] for key in ("mode", "since")
            }
    return result


def restore_export_commitment(raw, now):
    """Only a bounded, current published export period proves continuity."""
    if not isinstance(raw, dict):
        return None
    try:
        generated = parse_timestamp(raw.get("generated_at"))
        until = parse_timestamp(raw.get("until"))
    except InputError, OverflowError, ValueError:
        return None
    if not generated <= now < until or not timedelta(
        0
    ) < until - generated <= timedelta(hours=48):
        return None
    return {"generated_at": generated.isoformat(), "until": until.isoformat()}


def _coverage(rows, start):
    cursor = start
    for row in rows:
        if row.end <= cursor:
            continue
        if row.start > cursor:
            break
        cursor = max(cursor, row.end)
    if cursor <= start:
        raise InputError("source has no current interval coverage")
    return cursor


def available_forecasts(source, states):
    """Omit unpublished continuations without making any independent source optional."""
    missing = set()

    def select(bindings):
        available = []
        for binding in bindings:
            entity = binding.entity
            state = states.get(entity.entity_id)
            raw = (
                None
                if state is None
                else state.get("attributes", {}).get(entity.attribute)
                if entity.attribute
                else state.get("state")
            )
            if (
                state is None
                or state.get("state") in (None, "unknown", "unavailable")
                or raw is None
                or isinstance(raw, (list, tuple, dict))
                and not raw
            ):
                missing.add(entity.entity_id)
            else:
                available.append(binding)
        return tuple(available)

    return replace(
        source,
        buy=replace(source.buy, forecast=select(source.buy.forecast)),
        sell=replace(source.sell, forecast=select(source.sell.forecast)),
        pv=replace(
            source.pv, arrays=tuple(select(group) for group in source.pv.arrays)
        ),
    ), sorted(missing)


def _soc_value(states, binding, unit):
    raw = resolve_binding(states, binding)
    actual = states[binding.entity_id].get("attributes", {}).get("unit_of_measurement")
    if binding.attribute is None and actual and actual != unit:
        raise InputError("SOC source unit mismatch")
    return finite(raw, "SOC")


def _soc_timestamp(
    config: dict, states: dict, binding, *, prefix: str = ""
) -> datetime:
    options = config["soc_options"]
    path = options[prefix + "timestamp_path"]
    policy = options.get(prefix + "timestamp_policy", "auto")
    state = states[binding.entity_id]
    if policy == "auto" and path in ("last_updated", "last_reported"):
        path = "last_reported" if "last_reported" in state else "last_updated"
    return parse_timestamp(field(state, path), config["timezone"])


def _soc(config, source, values, states, now, previous_soc=None):
    options = config["soc_options"]
    binding = source.soc
    stamp = _soc_timestamp(config, states, binding)
    settings = SocSettings(
        values["capacity_kwh"],
        max(values["charge_kw"], values["discharge_kw"], 0.000001),
        values["soc_max_age_seconds"],
        values["soc_disagreement_percent"] / 100,
        values["soc_jump_percent"] / 100,
    )
    bms = None
    if source.bms_soc:
        bms_stamp = _soc_timestamp(config, states, source.bms_soc, prefix="bms_")
        bms = validate_soc(
            _soc_value(states, source.bms_soc, options["bms_unit"])
            * options.get("bms_sign", 1),
            options["bms_unit"],
            bms_stamp,
            now,
            replace(settings, max_age_seconds=values["bms_max_age_seconds"]),
        )
    prior = previous_soc if previous_soc and stamp != previous_soc[0] else None
    energy = validate_soc(
        _soc_value(states, binding, options["unit"]) * options.get("sign", 1),
        options["unit"],
        stamp,
        now,
        settings,
        previous=prior,
        bms_kwh=bms,
    )
    return energy, (stamp, energy)


def freshness_deadline(config, states, values, now):
    """End validity at the first required input's actual freshness deadline."""
    source, _ = available_forecasts(SourceConfig.from_dict(config["sources"]), states)
    deadlines = [now + timedelta(minutes=values["refresh_minutes"])]
    _, _, export_deadline = daily_export_observations(config, states, values, now)
    if export_deadline:
        deadlines.append(export_deadline)
    bindings = [
        *source.buy.forecast,
        *source.sell.forecast,
        *(binding for group in source.pv.arrays for binding in group),
    ]
    if source.load.forecast:
        bindings.append(source.load.forecast)
    for binding in bindings:
        if binding.max_age_seconds is not None:
            stamp = parse_timestamp(states[binding.entity.entity_id]["last_updated"])
            deadlines.append(stamp + timedelta(seconds=binding.max_age_seconds))
            if binding.published_path:
                published = parse_timestamp(
                    field(states[binding.entity.entity_id], binding.published_path),
                    binding.source_timezone,
                )
                deadlines.append(published + timedelta(seconds=binding.max_age_seconds))
    for selected in config.get("helpers", {}).values():
        setting = NumericSetting.from_dict(selected)
        if setting.entity and setting.max_age_seconds is not None:
            stamp = parse_timestamp(states[setting.entity.entity_id]["last_updated"])
            deadlines.append(stamp + timedelta(seconds=setting.max_age_seconds))
    if source.battery_enabled:
        for binding, prefix, setting in (
            (source.soc, "", "soc_max_age_seconds"),
            (source.bms_soc, "bms_", "bms_max_age_seconds"),
        ):
            if binding:
                stamp = _soc_timestamp(config, states, binding, prefix=prefix)
                deadlines.append(stamp + timedelta(seconds=values[setting]))
        if values["daily_cycles"]:
            selected = NumericSetting.from_dict(
                config["measurements"]["throughput_today"]
            )
            if selected.max_age_seconds is not None:
                deadlines.append(
                    parse_timestamp(states[selected.entity.entity_id]["last_updated"])
                    + timedelta(seconds=selected.max_age_seconds)
                )
    return min(deadlines)


def measurement_diagnostics(config, states, now):
    """Describe diagnostic measurements and counters used by active constraints."""
    result = {}
    values = validate_configuration(config, states, now, sources=False)
    for name, selected in config.get("measurements", {}).items():
        item = {
            "usage": "diagnostic_only",
            "value": None,
            "unit": selected.get("unit", ""),
        }
        if (
            name == "throughput_today"
            and config["sources"]["battery_enabled"]
            and config["settings"]["daily_cycles"]
        ):
            item["usage"] = "daily_throughput_constraint"
        if name in DAILY_EXPORT_MEASUREMENTS and daily_export_active(values):
            item["usage"] = "daily_export_constraint"
            try:
                observed = daily_export_observations(config, states, values, now)
                item.update(
                    value=observed[DAILY_EXPORT_MEASUREMENTS.index(name)],
                    status="available",
                )
            except InputError:
                item["status"] = "unavailable"
            result[name] = item
            continue
        if selected.get("_missing_registry"):
            item["status"] = "unavailable"
        elif selected.get("statistic_id"):
            item["status"] = "external_statistic_not_queried"
        else:
            try:
                item["value"] = resolve_numeric(
                    NumericSetting.from_dict(selected), states, now
                )
                item["status"] = "available"
            except InputError:
                item["status"] = "unavailable"
        result[name] = item
    return result


def _load_quality(source_mode, result):
    coverage = result.coverage
    duration = sum((row.end - row.start).total_seconds() for row in coverage)
    history = sum(
        (row.end - row.start).total_seconds()
        for row in coverage
        if row.method == "history"
    )
    fallback = sum(
        (row.end - row.start).total_seconds()
        for row in coverage
        if row.method == "fallback"
    )
    if source_mode == "recorder":
        method = (
            "history_with_fallback"
            if history and fallback
            else "fallback"
            if fallback
            else "history"
        )
        insufficient = fallback / 3600
    else:
        method = source_mode
        insufficient = None
    return {
        "source_mode": source_mode,
        "method": method,
        "coverage_hours": duration / 3600,
        "history_coverage_hours": history / 3600,
        "fallback_coverage_hours": fallback / 3600,
        "fallback_fraction": fallback / duration if duration else 0,
        "insufficient_history_hours": insufficient,
        "coverage": [
            {
                "start": row.start.isoformat(),
                "end": row.end.isoformat(),
                "method": row.method,
                "samples_available": row.samples_available,
                "samples_required": row.samples_required,
            }
            for row in coverage
        ],
    }


def build_problem(
    config: dict,
    states: dict,
    now: datetime,
    *,
    statistics=(),
    power_samples=(),
    previous_soc=None,
    battery_commitment=None,
    export_commitment=None,
):
    """Preserve native boundaries and stop at actual contiguous source coverage."""
    values = validate_configuration(config, states, now)
    pv_today, export_today, _ = daily_export_observations(config, states, values, now)
    source, missing = available_forecasts(
        SourceConfig.from_dict(config["sources"]), states
    )
    requested_end = now + timedelta(hours=values["horizon_hours"])
    groups = []
    for price in (source.buy, source.sell):
        if price.mode == "forecast":
            groups.append(
                merge_continuations(
                    tuple(
                        parse_intervals(states, binding, now)
                        for binding in price.forecast
                    )
                )
            )
    pv_groups = tuple(
        merge_continuations(
            tuple(parse_intervals(states, binding, now) for binding in group)
        )
        for group in source.pv.arrays
    )
    groups.extend(pv_groups)
    if source.load.mode == "forecast":
        groups.append(
            merge_continuations((parse_intervals(states, source.load.forecast, now),))
        )
    end = min([requested_end, *(_coverage(rows, now) for rows in groups)])
    boundaries = {now, end}
    cursor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while cursor < end:
        boundaries.add(cursor)
        cursor += timedelta(hours=1)
    boundaries.update(
        edge
        for rows in groups
        for row in rows
        for edge in (row.start, row.end)
        if now < edge < end
    )
    edges = sorted(boundaries)
    intervals = tuple(pairwise(edges))
    if len(intervals) > 384:
        raise InputError(
            "more than 384 native intervals; reduce horizon without coarsening settlements"
        )
    tariffs = []
    for name, price in (("buy", source.buy), ("sell", source.sell)):
        factor = values[f"{name}_multiplier"] * (
            1 + values["vat_percent"] / 100 if values[f"{name}_apply_vat"] else 1
        )
        addition = values[f"{name}_addition"]
        if price.mode == "fixed":
            price = replace(
                price,
                fixed=NumericSetting(
                    fixed=values[f"{name}_rate"] * factor + addition,
                    unit=f"{config['currency']}/kWh",
                ),
            )
        else:
            price = replace(
                price,
                multiplier=NumericSetting(fixed=factor),
                addition_per_kwh=NumericSetting(fixed=addition),
            )
        tariffs.append(
            price_for_slots(price, states, now, intervals, config["currency"])
        )
    if any(row.value < 0 for rows in pv_groups for row in rows):
        raise InputError("PV energy must be nonnegative")
    pv = (
        tuple(row.value for row in sum_pv_arrays(pv_groups, intervals))
        if source.pv.enabled
        else (0.0,) * len(intervals)
    )
    load_source = replace(
        source.load, power_max_gap_minutes=values["power_max_gap_minutes"]
    )
    if load_source.mode == "daily_estimate":
        load_source = replace(
            load_source, daily_estimate=NumericSetting(fixed=values["daily_load_kwh"])
        )
    forecast = ForecastSettings(
        values["lookback_days"],
        values["group_weekends"],
        values["minimum_samples"],
        values["allow_fallback"],
    )
    load_result = load_for_slots_with_quality(
        load_source,
        states,
        now,
        intervals,
        config["timezone"],
        forecast,
        statistics=statistics,
        power_samples=power_samples,
        fallback_daily_kwh=values["fallback_daily_kwh"]
        if values["allow_fallback"]
        else None,
    )
    loads = load_result.values
    slots = tuple(
        Slot(
            start, finish, tariffs[0][index], tariffs[1][index], pv[index], loads[index]
        )
        for index, (start, finish) in enumerate(intervals)
    )
    battery = None
    soc_observation = None
    budgets = ()
    if source.battery_enabled:
        energy, soc_observation = _soc(
            config, source, values, states, now, previous_soc
        )
        battery = Battery(
            values["capacity_kwh"],
            values["operating_floor"] / 100,
            values["soc_ceiling"] / 100,
            energy,
            values["charge_kw"],
            values["discharge_kw"],
            values["eta_charge"],
            values["eta_discharge"],
            values["wear_per_kwh"],
            values["allow_grid_charge"],
            values["allow_battery_export"],
        )
        if values["daily_cycles"]:
            observed = resolve_daily_throughput(config, values, states, now)
            local_today = now.astimezone(ZoneInfo(config["timezone"])).date()
            cap = 2 * values["capacity_kwh"] * values["daily_cycles"]
            days = {
                slot.start.astimezone(ZoneInfo(config["timezone"])).date()
                for slot in slots
            }
            budgets = tuple(
                (day.isoformat(), max(0, cap - observed) if day == local_today else cap)
                for day in sorted(days)
            )
    commitment = restore_commitment(battery_commitment, now) if battery else None
    named = commitment if commitment and commitment["mode"] in MODES else None
    legacy = (commitment.get("legacy_direction") if named else commitment) or {}
    problem = Problem(
        slots,
        SiteLimits(
            values["inverter_kw"],
            values["grid_import_kw"],
            values["grid_export_kw"],
            values["allow_curtailment"],
        ),
        battery,
        values["terminal_mode"],
        values["terminal_value_per_kwh"],
        budgets,
        config["timezone"],
        minimum_export_episode_benefit=values["minimum_export_episode_benefit"],
        initial_export_active=bool(
            battery and restore_export_commitment(export_commitment, now)
        ),
        minimum_mode_minutes=values["minimum_mode_minutes"],
        limit_export_to_pv=values["limit_export_to_pv"],
        pv_generated_today_kwh=pv_today,
        grid_exported_today_kwh=export_today,
        minimum_mode_power_kw=values["minimum_mode_power_kw"],
        initial_dispatch_mode=named["mode"] if named else None,
        initial_dispatch_mode_since=parse_timestamp(named["since"]) if named else None,
        initial_battery_mode=legacy.get("mode"),
        initial_battery_mode_since=parse_timestamp(legacy["since"]) if legacy else None,
    )
    validate_problem(problem)
    ages = {
        key: max(0, (now - parse_timestamp(state["last_updated"])).total_seconds())
        for key, state in states.items()
    }
    quality = {
        "coverage_end": end.isoformat(),
        "requested_end": requested_end.isoformat(),
        "coverage_complete": end >= requested_end,
        "input_ages": ages,
        "missing_sources": missing,
        "load": _load_quality(source.load.mode, load_result),
        "soc_observation": soc_observation,
        "warnings": ["unvalidated_tariff"]
        if values["calibration"] != "verified"
        else [],
    }
    if missing:
        quality["warnings"].append("missing_forecast_continuation")
    if quality["load"]["fallback_coverage_hours"] > 0:
        quality["warnings"].append("load_history_fallback")
    if battery and values["capacity_calibration"] != "verified":
        quality["warnings"].append("unvalidated_capacity")
    if end < requested_end:
        quality["warnings"].append("short_source_coverage")
    return problem, values, quality


async def async_history(hass, config, states, now):
    """Use recorder's own executor for history and external statistic IDs."""
    source = SourceConfig.from_dict(config["sources"]).load
    if source.mode != "recorder":
        return {}, ()
    values = validate_configuration(config, states, now)
    start = now - timedelta(days=values["lookback_days"] + 1)
    recorder = get_instance(hass)
    if source.statistic_id:
        query = partial(
            statistics_during_period,
            hass,
            start,
            now,
            {source.statistic_id},
            "hour",
            None,
            {"sum"},
        )
        result = await recorder.async_add_executor_job(query)
        rows = result.get(source.statistic_id, [])
        return {
            "statistics": tuple(
                {
                    **row,
                    "start": datetime.fromtimestamp(row["start"], UTC).isoformat()
                    if isinstance(row["start"], (int, float))
                    else row["start"],
                }
                for row in rows
            )
        }, ()
    query = partial(
        get_significant_states,
        hass,
        start,
        now,
        [source.power.entity_id],
        significant_changes_only=False,
        minimal_response=False,
        no_attributes=False,
    )
    result = await recorder.async_add_executor_job(query)
    samples = []
    for state in result.get(source.power.entity_id, []):
        value = (
            None
            if state.state in ("unknown", "unavailable")
            else state.attributes.get(source.power.attribute)
            if source.power.attribute
            else state.state
        )
        if value in ("unknown", "unavailable"):
            value = None
        samples.append((state.last_updated, value))
    return {"power_samples": tuple(samples)}, ()


def compute(config: dict, states: dict, now: datetime, **history) -> dict:
    """Use one snapshot and a single deadline for dispatch and consumption probes."""
    started = perf_counter()
    problem, values, quality = build_problem(config, states, now, **history)
    plan = solve(
        problem,
        time_limit_s=min(values["solve_time_limit_s"], values["total_time_limit_s"]),
    )
    remaining = values["total_time_limit_s"] - (perf_counter() - started)
    if remaining <= 0:
        raise SolveError("timeout")
    compass = CompassSettings(
        **{key: values[key] for key in CompassSettings.__dataclass_fields__}
    )
    analysis = analyze_consumption(problem, plan, settings=compass, budget_s=remaining)
    if perf_counter() - started > values["total_time_limit_s"]:
        raise SolveError("timeout")
    zone = ZoneInfo(config["timezone"])
    detailed = [
        {
            **machine_snapshot(slot, flow),
            "start": slot.start.astimezone(zone).isoformat(),
            "end": slot.end.astimezone(zone).isoformat(),
            "buy_per_kwh": slot.buy_per_kwh,
            "sell_per_kwh": slot.sell_per_kwh,
            **asdict(flow),
        }
        for slot, flow in zip(problem.slots, plan.flows)
    ]
    windows = {}
    for key, levels in [
        ("boost", ("BOOST",)),
        ("cheap", ("CHEAP",)),
        ("limit", ("LIMIT",)),
        ("favorable", ("BOOST", "CHEAP")),
    ]:
        windows[key] = [
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "levels": list(item.levels),
            }
            for item in merge_windows(analysis.opportunities, levels)
        ]
    display_end = now + timedelta(hours=values["display_horizon_hours"])
    net_cost = wear_cost = 0
    for slot, flow in zip(problem.slots, plan.flows):
        fraction = (
            max(0, (min(slot.end, display_end) - slot.start).total_seconds())
            / (slot.end - slot.start).total_seconds()
        )
        net_cost += fraction * (
            flow.grid_import_kwh * slot.buy_per_kwh
            - flow.grid_export_kwh * slot.sell_per_kwh
        )
        wear_cost += (
            fraction
            * (flow.charge_kwh + flow.discharge_kwh)
            / 2
            * (problem.battery.wear_per_kwh if problem.battery else 0)
        )
    outlook = [serialize_opportunity(item) for item in analysis.opportunities]
    guidance_valid = bool(outlook and outlook[0]["level"] is not None)
    if not guidance_valid:
        quality["warnings"].append("current_guidance_unavailable")
    return {
        "status": "ready",
        "valid": True,
        "guidance_valid": guidance_valid,
        "generated_at": now.isoformat(),
        "valid_until": min(
            problem.slots[0].end, freshness_deadline(config, states, values, now)
        ).isoformat(),
        "intervals": detailed,
        "outlook": outlook,
        "windows": windows,
        "favorable_windows": windows["favorable"],
        "classification_mode": analysis.classification_mode,
        "coverage_reason": analysis.coverage_reason,
        "calibration": values["calibration"],
        "capacity_calibration": values["capacity_calibration"],
        "probe_kwh": values["probe_kwh"],
        "currency": config["currency"],
        "expected_net_cost": net_cost,
        "expected_wear_cost": wear_cost,
        "cost_coverage_hours": (
            min(display_end, problem.slots[-1].end) - now
        ).total_seconds()
        / 3600,
        "monthly_charge_reporting_only": values["monthly_charge"],
        "quality": quality,
        "dispatch_policy": {
            "minimum_export_episode_benefit": values["minimum_export_episode_benefit"],
            "new_export_episodes": plan.new_export_episodes,
            "export_episode_reserve": plan.export_episode_reserve,
            "export_benefit_scope": "additional_battery_export_period",
            "minimum_mode_minutes": values["minimum_mode_minutes"],
            "minimum_mode_power_kw": values["minimum_mode_power_kw"],
            "mode_scope": "actual_operating_mode",
            "enabled": problem.battery is not None
            and values["minimum_mode_minutes"] > 0,
            "safety_exception": safety_exception(problem),
            "limit_export_to_pv": values["limit_export_to_pv"],
            "export_limit_scope": "local_day",
            "timezone": config["timezone"],
            "daily_balances": daily_export_rows(problem, plan.flows),
        },
        "measurements": measurement_diagnostics(config, states, now),
        "presentation": {
            key: values[key]
            for key in (
                "cost_precision",
                "window_label",
                "boost_color",
                "cheap_color",
                "normal_color",
                "limit_color",
                "expose_costs",
                "expose_windows",
            )
        },
        "notification_preferences": {
            **{
                key: values[key]
                for key in (
                    "notify_enabled",
                    "notify_events",
                    "notify_minimum_hours",
                    "notify_limit_lead_minutes",
                    "notify_daily_max",
                    "notify_cooldown_minutes",
                    "quiet_start",
                    "quiet_end",
                )
            },
            "actions_configured": bool(values["notify_actions"]),
        },
    }
