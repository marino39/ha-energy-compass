"""Native setup, atomic reconfiguration and operating preferences."""

import json
from copy import deepcopy

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .engine.models import InputError, SolveError
from .engine.optimize import solve
from .flow_schema import (
    currency_review_schema,
    rebind_configuration,
    select,
    settings_schema,
    snapshot,
)
from .presets import PRESETS
from .runtime import async_history, build_problem
from .settings import (
    DOMAIN,
    GROUPS,
    default_configuration,
    merged_configuration,
    validate_configuration,
)
from .source_flow import SourceEditor


def _preview_assumptions(config, problem, values, quality):
    buy = config["sources"]["buy"]
    currency = config["currency"]
    if buy["mode"] == "fixed":
        helper = config.get("helpers", {}).get("buy_rate")
        origin = (
            f"helper {helper['entity']['entity_id']}"
            + (
                f" attribute {helper['entity']['attribute']}"
                if helper["entity"].get("attribute")
                else ""
            )
            if helper
            else "fixed setting"
        )
        source = f"Buy source: fixed, {origin}; resolved {values['buy_rate']:g} {currency}/kWh"
        if values["buy_rate"] == 0:
            source += " (intentional free import rate)"
    else:
        bindings = ", ".join(
            row["entity"]["entity_id"]
            + (
                f" attribute {row['entity']['attribute']}"
                if row["entity"].get("attribute")
                else ""
            )
            for row in buy["forecast"]
        )
        source = f"Buy source: forecast {bindings}; first resolved {problem.slots[0].buy_per_kwh:g} {currency}/kWh"
    source += (
        f"; multiplier {values['buy_multiplier']:g}, addition {values['buy_addition']:g} {currency}/kWh, "
        f"VAT {'applied' if values['buy_apply_vat'] else 'not applied'} at {values['vat_percent']:g}%."
    )
    load = config["sources"]["load"]
    method = quality.get("load", {}).get("method", load["mode"])
    if load["mode"] == "daily_estimate":
        helper = config.get("helpers", {}).get("daily_load_kwh")
        origin = (
            f"helper {helper['entity']['entity_id']}" if helper else "fixed setting"
        )
        load_text = f"Household load: daily estimate from {origin}, resolved {values['daily_load_kwh']:g} kWh/day"
    elif load["mode"] == "recorder":
        origin = load.get("statistic_id") or load.get("power", {}).get("entity_id")
        fallback = (
            f"fallback daily estimate {values['fallback_daily_kwh']:g} kWh/day"
            if values["allow_fallback"]
            else "fallback disabled"
        )
        load_text = (
            f"Household load: recorder {origin}, actual method {method}; {fallback}"
        )
    else:
        binding = load["forecast"]["entity"]
        load_text = (
            f"Household load: forecast {binding['entity_id']}, actual method {method}"
        )
    load_quality = quality.get("load")
    if load_quality:
        load_text += (
            f"; fallback {load_quality['fallback_coverage_hours']:g} h "
            f"({load_quality['fallback_fraction']:.1%} of elapsed forecast time); "
            f"samples available/required by segment: "
            + ", ".join(
                f"{row['samples_available']}/{row['samples_required']}"
                for row in load_quality["coverage"]
                if row["samples_available"] is not None
            )
        )
    return source + "\n" + load_text + "."


def _solver_failure_detail(problem, values, error):
    if error.reason == "timeout":
        return "Feasibility is unknown; retry or increase the bounded solve time limit in Performance."
    if error.reason != "infeasible":
        return f"Optimizer error: {error}. Review Planning and retry."
    detail = (
        f"Review Hardware, Battery and Planning settings: grid import/export "
        f"{values['grid_import_kw']:g}/{values['grid_export_kw']:g} kW, "
        f"inverter {values['inverter_kw']:g} kW, curtailment "
        f"{'enabled' if values['allow_curtailment'] else 'disabled'}."
    )
    if problem.battery:
        detail += (
            f" Battery capacity {values['capacity_kwh']:g} kWh, floor "
            f"{values['operating_floor']:g}%, ceiling {values['soc_ceiling']:g}%, "
            f"charge/discharge {values['charge_kw']:g}/{values['discharge_kw']:g} kW."
        )
    if (
        any(slot.load_kwh > 0 for slot in problem.slots)
        and not problem.battery
        and values["grid_import_kw"] == 0
        and all(slot.pv_kwh == 0 for slot in problem.slots)
    ):
        detail += " Positive household load has no grid import, PV, or battery supply."
    if (
        any(slot.pv_kwh > 0 for slot in problem.slots)
        and values["inverter_kw"] == 0
        and not values["allow_curtailment"]
    ):
        detail += " Positive PV has zero inverter capacity and curtailment is disabled."
    return detail


class Editor(SourceEditor):
    """Keep unfinished edits separate until a validated preview is accepted."""

    _existing_installation = False
    _currency_review_pending = False

    async def async_step_menu(self, user_input=None):
        return self.async_show_menu(
            step_id="menu",
            menu_options=["installation", "sources", *GROUPS, "helpers", "preview"],
        )

    async def async_step_installation(self, user_input=None):
        errors = {}
        if user_input is not None:
            candidate = deepcopy(self._draft)
            candidate.update(
                {
                    key: user_input[key]
                    for key in ("name", "currency", "timezone", "preset")
                }
            )
            candidate["sources"]["currency"] = candidate["currency"]
            candidate["sources"]["pv"]["enabled"] = user_input["pv_enabled"]
            candidate["sources"]["battery_enabled"] = user_input["battery_enabled"]
            if not user_input["pv_enabled"]:
                candidate["sources"]["pv"]["arrays"] = []
            if not user_input["battery_enabled"]:
                candidate["sources"].update(soc=None, bms_soc=None)
            currency_changed = candidate["currency"] != self._draft["currency"]
            if currency_changed:
                candidate["settings"]["calibration"] = "unvalidated"
            try:
                validate_configuration(
                    {**candidate, "helpers": {}} if currency_changed else candidate,
                    snapshot(self.hass, candidate),
                    dt_util.utcnow(),
                    sources=False,
                )
                self._draft = candidate
                if currency_changed and self._existing_installation:
                    self._currency_review_pending = True
                    return await self.async_step_currency_review()
                return await self.async_step_menu()
            except InputError:
                errors["base"] = "invalid_input"
        return self.async_show_form(
            step_id="installation",
            data_schema=self._installation_schema(),
            errors=errors,
        )

    async def async_step_currency_review(self, user_input=None):
        errors = {}
        detail = ""
        if user_input is not None:
            if user_input.get("confirm_currency_values") is not True:
                errors["base"] = "currency_review_required"
            else:
                candidate = deepcopy(self._draft)
                try:
                    reviewed = currency_review_schema(
                        candidate["settings"], candidate["currency"]
                    )(user_input)
                    reviewed.pop("confirm_currency_values")
                    candidate["settings"].update(reviewed)
                    # Old helper units remain visible until rebound in the helper editor.
                    validate_configuration(
                        {**candidate, "helpers": {}},
                        {},
                        dt_util.utcnow(),
                        sources=False,
                    )
                    self._draft = candidate
                    self._currency_review_pending = False
                    return await self.async_step_menu()
                except (InputError, vol.Invalid) as err:
                    errors["base"] = "invalid_input"
                    detail = str(err)
        return self.async_show_form(
            step_id="currency_review",
            data_schema=currency_review_schema(
                self._draft["settings"], self._draft["currency"]
            ),
            errors=errors,
            description_placeholders={
                "currency": self._draft["currency"],
                "detail": detail,
            },
        )

    def _installation_schema(self):
        return vol.Schema(
            {
                vol.Required(
                    "name", default=self._draft["name"]
                ): selector.TextSelector(),
                vol.Required(
                    "currency", default=self._draft["currency"]
                ): selector.TextSelector(),
                vol.Required(
                    "timezone", default=self._draft["timezone"]
                ): selector.TextSelector(),
                vol.Required(
                    "preset", default=self._draft.get("preset", "generic")
                ): select(
                    [
                        {"value": "generic", "label": "Generic"},
                        *(
                            {"value": key, "label": preset.name}
                            for key, preset in PRESETS.items()
                        ),
                    ]
                ),
                vol.Required(
                    "pv_enabled", default=self._draft["sources"]["pv"]["enabled"]
                ): selector.BooleanSelector(),
                vol.Required(
                    "battery_enabled", default=self._draft["sources"]["battery_enabled"]
                ): selector.BooleanSelector(),
            }
        )

    async def _settings_step(self, group, user_input):
        errors = {}
        detail = ""
        if user_input is not None:
            candidate = deepcopy(self._draft)
            candidate["settings"].update(user_input)
            try:
                validate_configuration(
                    candidate,
                    snapshot(self.hass, candidate),
                    dt_util.utcnow(),
                    sources=False,
                )
                self._draft = candidate
                return await self.async_step_menu()
            except InputError as err:
                errors["base"] = "invalid_input"
                detail = str(err)
        return self.async_show_form(
            step_id=group,
            data_schema=settings_schema(
                group, self._draft["settings"], self._draft["currency"]
            ),
            errors=errors,
            description_placeholders={"detail": detail},
        )

    async def async_step_battery(self, user_input=None):
        return await self._settings_step("battery", user_input)

    async def async_step_hardware(self, user_input=None):
        return await self._settings_step("hardware", user_input)

    async def async_step_tariffs(self, user_input=None):
        return await self._settings_step("tariffs", user_input)

    async def async_step_forecast(self, user_input=None):
        return await self._settings_step("forecast", user_input)

    async def async_step_planning(self, user_input=None):
        return await self._settings_step("planning", user_input)

    async def async_step_compass(self, user_input=None):
        return await self._settings_step("compass", user_input)

    async def async_step_performance(self, user_input=None):
        return await self._settings_step("performance", user_input)

    async def async_step_presentation(self, user_input=None):
        return await self._settings_step("presentation", user_input)

    async def async_step_notifications(self, user_input=None):
        return await self._settings_step("notifications", user_input)

    async def async_step_preview(self, user_input=None):
        if self._currency_review_pending:
            return await self.async_step_currency_review()
        errors = {}
        preview = "Source inputs: failed. Base plan: not checked. Extra-consumption guidance: not checked in preview; computed after saving."
        try:
            candidate = rebind_configuration(self.hass, self._draft)
            now = dt_util.utcnow()
            states = snapshot(self.hass, candidate)
            history, _ = await async_history(self.hass, candidate, states, now)
            problem, values, quality = await self.hass.async_add_executor_job(
                lambda: build_problem(candidate, states, now, **history)
            )
            solver_error = None
            plan_status = "feasible"
            try:
                await self.hass.async_add_executor_job(
                    lambda: solve(problem, time_limit_s=values["solve_time_limit_s"])
                )
            except SolveError as err:
                solver_error = err
                if err.reason == "infeasible":
                    errors["base"] = "plan_infeasible"
                    plan_status = "infeasible"
                elif err.reason == "timeout":
                    errors["base"] = "plan_timeout"
                    plan_status = "timed out; feasibility unknown"
                else:
                    errors["base"] = "optimizer_error"
                    plan_status = "optimizer error; feasibility unknown"
            rows = "\n".join(
                f"{slot.start.isoformat()} → {slot.end.isoformat()}: import {slot.buy_per_kwh:g}, export {slot.sell_per_kwh:g} {candidate['currency']}/kWh; PV {slot.pv_kwh:.3f}, household {slot.load_kwh:.3f} kWh"
                for slot in problem.slots[:4]
            )
            preview = (
                f"Source inputs: validated. Base plan: {plan_status}. Extra-consumption guidance: not checked in preview; computed after saving.\n"
                f"{_preview_assumptions(candidate, problem, values, quality)}\n"
                f"{rows}\n\nCoverage: {problem.slots[0].start.isoformat()} → {quality['coverage_end']} ({len(problem.slots)} native intervals).\n"
                f"Warnings: {', '.join(quality['warnings']) or 'none'}.\n"
                f"Input ages (seconds): {json.dumps(quality['input_ages'])}.\n"
                f"Battery: {'disabled' if problem.battery is None else str(values['capacity_kwh']) + ' kWh; floor ' + str(values['operating_floor']) + '%; ceiling ' + str(values['soc_ceiling']) + '%'} .\n"
                f"Grid import/export: {values['grid_import_kw']}/{values['grid_export_kw']} kW. Currency conversion is not supported."
            )
            if solver_error:
                preview += "\n" + _solver_failure_detail(problem, values, solver_error)
            elif user_input is not None and user_input.get("confirm") is True:
                if not self._existing_installation and (
                    user_input.get("confirm_buy_source") is not True
                    or user_input.get("confirm_load_source") is not True
                ):
                    errors["base"] = "input_acknowledgement_required"
                else:
                    self._draft = candidate
                    return self._finish()
        except (InputError, ValueError, KeyError) as err:
            errors["base"] = "invalid_source"
            preview += "\n" + str(err)
        schema = {vol.Required("confirm", default=False): selector.BooleanSelector()}
        if not self._existing_installation:
            schema.update(
                {
                    vol.Required(
                        "confirm_buy_source", default=False
                    ): selector.BooleanSelector(),
                    vol.Required(
                        "confirm_load_source", default=False
                    ): selector.BooleanSelector(),
                }
            )
        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"preview": preview},
        )


class EnergyCompassConfigFlow(Editor, config_entries.ConfigFlow, domain=DOMAIN):
    """Configure provider-independent advisory inputs through native HA forms."""

    VERSION = 2

    async def async_step_user(self, user_input=None):
        self._draft = default_configuration(
            getattr(self.hass.config, "currency", "EUR") or "EUR",
            self.hass.config.time_zone,
        )
        if user_input is not None:
            if user_input["currency"] == "PLN" and user_input["preset"] in (
                "pse_solcast",
                "pse",
            ):
                self._draft["settings"].update(boost_ceiling=0.05, limit_floor=0.80)
            preset = PRESETS.get(user_input["preset"])
            if preset and preset.soc_unit:
                self._draft["soc_options"]["unit"] = preset.soc_unit
            result = await self.async_step_installation(user_input)
            if result["type"] != "form":
                return result
            return self.async_show_form(
                step_id="user",
                data_schema=self._installation_schema(),
                errors=result["errors"],
            )
        return self.async_show_form(
            step_id="user", data_schema=self._installation_schema()
        )

    async def async_step_reconfigure(self, user_input=None):
        self._entry = self._get_reconfigure_entry()
        self._draft = merged_configuration(self._entry)
        self._existing_installation = True
        return await self.async_step_menu()

    @callback
    def _finish(self):
        if hasattr(self, "_entry"):
            return self.async_update_reload_and_abort(
                self._entry, data=self._draft, options={}, title=self._draft["name"]
            )
        return self.async_create_entry(title=self._draft["name"], data=self._draft)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EnergyCompassOptionsFlow()


class EnergyCompassOptionsFlow(Editor, config_entries.OptionsFlowWithReload):
    """Validate a complete preferences transaction before automatic entry reload."""

    async def async_step_init(self, user_input=None):
        self._draft = merged_configuration(self.config_entry)
        self._existing_installation = True
        return await self.async_step_menu()

    @callback
    def _finish(self):
        self.hass.config_entries.async_update_entry(
            self.config_entry, title=self._draft["name"]
        )
        return self.async_create_entry(title="", data={"configuration": self._draft})
