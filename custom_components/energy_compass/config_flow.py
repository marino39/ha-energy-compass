"""Native setup, atomic reconfiguration and operating preferences."""

import json
from copy import deepcopy

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .engine.models import InputError
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
        preview = ""
        try:
            candidate = rebind_configuration(self.hass, self._draft)
            now = dt_util.utcnow()
            states = snapshot(self.hass, candidate)
            history, _ = await async_history(self.hass, candidate, states, now)
            problem, values, quality = await self.hass.async_add_executor_job(
                lambda: build_problem(candidate, states, now, **history)
            )
            rows = "\n".join(
                f"{slot.start.isoformat()} → {slot.end.isoformat()}: import {slot.buy_per_kwh:g}, export {slot.sell_per_kwh:g} {candidate['currency']}/kWh; PV {slot.pv_kwh:.3f}, household {slot.load_kwh:.3f} kWh"
                for slot in problem.slots[:4]
            )
            preview = f"{rows}\n\nCoverage: {problem.slots[0].start.isoformat()} → {quality['coverage_end']} ({len(problem.slots)} native intervals).\nWarnings: {', '.join(quality['warnings']) or 'none'}.\nInput ages (seconds): {json.dumps(quality['input_ages'])}.\nBattery: {'disabled' if problem.battery is None else str(values['capacity_kwh']) + ' kWh; floor ' + str(values['operating_floor']) + '%; ceiling ' + str(values['soc_ceiling']) + '%'}.\nGrid import/export: {values['grid_import_kw']}/{values['grid_export_kw']} kW. Currency conversion is not supported."
            if user_input is not None and user_input.get("confirm"):
                self._draft = candidate
                return self._finish()
        except (InputError, ValueError, KeyError) as err:
            errors["base"] = "invalid_source"
            preview = str(err)
        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): selector.BooleanSelector()}
            ),
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
