"""Reusable selector-driven source and helper editors."""

import re
from copy import deepcopy

import voluptuous as vol
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .config_models import LoadSource, NumericSetting, PriceSource
from .daily_export import DAILY_EXPORT_MEASUREMENTS
from .engine.models import InputError
from .flow_schema import entity_binding, number, select, snapshot
from .presets import PRESETS
from .settings import NUMBERS
from .sources.bindings import (
    IntervalBinding,
    field_paths,
    parse_intervals,
    resolve_binding,
)

MEASUREMENTS = (
    "battery_power",
    "battery_charge_power",
    "battery_discharge_power",
    "battery_energy",
    "pv_power",
    "pv_energy",
    "grid_import_power",
    "grid_export_power",
    "grid_import_energy",
    "grid_export_energy",
    "throughput_today",
    *DAILY_EXPORT_MEASUREMENTS,
)
TARGETS = ("buy", "sell", "pv", "load", "soc", "bms_soc", *MEASUREMENTS)


class SourceEditor:
    """Share source transactions across setup and post-setup flows."""

    async def async_step_sources(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._source = dict(user_input)
            target = user_input["target"]
            mode = user_input["mode"]
            if (
                target == "pv"
                and not self._draft["sources"]["pv"]["enabled"]
                or target in ("soc", "bms_soc", "throughput_today")
                and not self._draft["sources"]["battery_enabled"]
            ):
                errors["base"] = "component_disabled"
            elif user_input["operation"] == "remove":
                if target in MEASUREMENTS:
                    self._draft["measurements"].pop(target, None)
                elif target in ("soc", "bms_soc"):
                    self._draft["sources"][target] = None
                elif target == "pv":
                    self._draft["sources"]["pv"]["arrays"] = []
                else:
                    errors["base"] = "invalid_input"
                if not errors:
                    return await self.async_step_menu()
            elif mode == "fixed" and target in ("buy", "sell", "load"):
                if target == "load":
                    self._draft["sources"]["load"] = LoadSource(
                        "daily_estimate",
                        daily_estimate=NumericSetting(
                            fixed=self._draft["settings"]["daily_load_kwh"]
                        ),
                    ).to_dict()
                else:
                    self._draft["sources"][target] = PriceSource(
                        "fixed",
                        fixed=NumericSetting(
                            fixed=self._draft["settings"][f"{target}_rate"],
                            unit=f"{self._draft['currency']}/kWh",
                        ),
                    ).to_dict()
                return await self.async_step_menu()
            elif target in DAILY_EXPORT_MEASUREMENTS and mode != "measurement":
                errors["base"] = "invalid_input"
            elif mode == "statistic" and (target == "load" or target in MEASUREMENTS):
                return await self.async_step_source_statistic()
            elif mode in ("forecast", "measurement", "power_history"):
                if (
                    target in ("buy", "sell", "pv")
                    and mode != "forecast"
                    or target in ("soc", "bms_soc", *MEASUREMENTS)
                    and mode != "measurement"
                    or mode == "power_history"
                    and target != "load"
                ):
                    errors["base"] = "invalid_input"
                else:
                    return await self.async_step_source_entity()
            else:
                errors["base"] = "invalid_input"
        schema = vol.Schema(
            {
                vol.Required("target", default="buy"): select(TARGETS),
                vol.Required("mode", default="forecast"): select(
                    ["forecast", "fixed", "measurement", "power_history", "statistic"]
                ),
                vol.Required("operation", default="replace"): select(
                    ["replace", "append", "remove"]
                ),
                vol.Required("group", default=1): number(1, 8, step=1),
            }
        )
        return self.async_show_form(
            step_id="sources", data_schema=schema, errors=errors
        )

    async def async_step_source_statistic(self, user_input=None):
        if user_input is not None:
            target = self._source["target"]
            if target == "load":
                self._draft["sources"]["load"] = LoadSource(
                    "recorder",
                    statistic_id=user_input["statistic_id"],
                    history_unit=user_input["unit"],
                    history_sign=user_input["sign"],
                ).to_dict()
            else:
                self._draft["measurements"][target] = dict(
                    user_input, usage="diagnostic_only"
                )
            return await self.async_step_menu()
        return self.async_show_form(
            step_id="source_statistic",
            data_schema=vol.Schema(
                {
                    vol.Required("statistic_id"): selector.StatisticSelector(),
                    vol.Required("unit", default="kWh"): select(["kWh", "Wh"]),
                    vol.Required("sign", default=1): number(-1, 1, step=2),
                }
            ),
        )

    async def async_step_source_entity(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._binding = entity_binding(self.hass, user_input["entity_id"])
                if self.hass.states.get(self._binding.entity_id) is None:
                    raise InputError("missing entity")
                return await self.async_step_source_attribute()
            except InputError:
                errors["base"] = "invalid_source"
        return self.async_show_form(
            step_id="source_entity",
            data_schema=vol.Schema(
                {vol.Required("entity_id"): selector.EntitySelector()}
            ),
            errors=errors,
        )

    async def async_step_source_attribute(self, user_input=None):
        if user_input is not None:
            self._binding = entity_binding(
                self.hass, self._binding.entity_id, user_input.get("attribute")
            )
            if self._source["mode"] == "forecast":
                return await self.async_step_source_mapping()
            return await self.async_step_source_measurement()
        preset = PRESETS.get(self._draft.get("preset"))
        attribute = None
        if preset:
            attribute = (
                preset.price_attribute
                if self._source["target"] in ("buy", "sell")
                else preset.pv_attribute
            )
        marker = (
            vol.Optional("attribute", description={"suggested_value": attribute})
            if attribute
            else vol.Optional("attribute")
        )
        return self.async_show_form(
            step_id="source_attribute",
            data_schema=vol.Schema(
                {
                    marker: selector.AttributeSelector(
                        selector.AttributeSelectorConfig(
                            entity_id=self._binding.entity_id
                        )
                    )
                }
            ),
        )

    async def async_step_source_mapping(self, user_input=None):
        errors = {}
        detail = ""
        target = self._source["target"]
        currency = self._draft["currency"]
        state = self.hass.states.get(self._binding.entity_id)
        states = (
            {
                state.entity_id: {
                    "state": state.state,
                    "attributes": dict(state.attributes),
                    "last_updated": state.last_updated.isoformat(),
                }
            }
            if state
            else {}
        )
        try:
            raw = resolve_binding(states, self._binding)
        except InputError:
            raw = None
        paths = (
            field_paths(raw[0])
            if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], dict)
            else ()
        )
        if user_input is not None:
            try:
                data = dict(user_input)
                for key in (
                    "start_path",
                    "end_path",
                    "duration_path",
                    "unit_path",
                    "published_path",
                ):
                    data[key] = data.get(key) or None
                for key in (
                    "value_path",
                    "start_path",
                    "end_path",
                    "duration_path",
                    "unit_path",
                    "published_path",
                ):
                    if data.get(key) and not re.fullmatch(
                        r"[A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*", data[key]
                    ):
                        raise InputError("invalid data path")
                age = data.pop("max_age_hours")
                data["max_age_seconds"] = age * 3600 if data.pop("check_age") else None
                binding = IntervalBinding(self._binding, **data)
                rows = parse_intervals(states, binding, dt_util.utcnow())
                if not rows:
                    raise InputError("empty forecast")
                self._save_interval(binding)
                return await self.async_step_menu()
            except (InputError, ValueError, TypeError) as err:
                errors["base"] = "invalid_source"
                detail = str(err)
        preset = PRESETS.get(self._draft.get("preset"))
        price = target in ("buy", "sell")
        defaults = {
            "value_path": "value",
            "start_path": "start",
            "end_path": "",
            "duration_path": "",
            "unit_path": "",
            "published_path": "",
            "interval_minutes": 60,
            "unit": f"{currency}/kWh" if price else "kWh",
            "value_kind": "price" if price else "energy",
            "value_sign": 1,
            "source_timezone": self._draft["timezone"],
            "check_age": True,
            "max_age_hours": 24,
        }
        if preset:
            if price:
                defaults.update(
                    value_path=preset.price_value_field or "value",
                    start_path=preset.price_start_field or "",
                    end_path=preset.price_end_field or "",
                    unit=preset.price_unit or defaults["unit"],
                    interval_minutes=preset.price_interval_minutes or 60,
                )
            elif target == "pv":
                defaults.update(
                    value_path=preset.pv_value_field or "value",
                    start_path=preset.pv_start_field or "start",
                    unit=preset.pv_unit or "kWh",
                    interval_minutes=preset.pv_interval_minutes or 60,
                    value_kind="power" if preset.pv_unit in ("kW", "W") else "energy",
                )
        defaults.update(user_input or {})
        schema = {}
        for key in (
            "value_path",
            "start_path",
            "end_path",
            "duration_path",
            "unit_path",
        ):
            choices = list(paths)
            if key != "value_path":
                choices.insert(0, "")
            suggested = (
                defaults[key]
                if defaults[key] in choices or not paths
                else (choices[0] if choices else "")
            )
            schema[vol.Required(key, default=suggested)] = (
                select(choices) if paths else selector.TextSelector()
            )
        publication_paths = [
            path
            for path in field_paths(states.get(self._binding.entity_id, {}))
            if path not in ("state",)
        ]
        schema[vol.Optional("published_path", default=defaults["published_path"])] = (
            select(["", *publication_paths])
            if publication_paths
            else selector.TextSelector()
        )
        schema.update(
            {
                vol.Required(
                    "interval_minutes", default=defaults["interval_minutes"]
                ): number(1, 1440, "min"),
                vol.Required("unit", default=defaults["unit"]): select(
                    [f"{currency}/kWh", f"{currency}/MWh"]
                    if price
                    else ["kWh", "Wh", "kW", "W"]
                ),
                vol.Required("value_kind", default=defaults["value_kind"]): select(
                    ["price"] if price else ["energy", "power"]
                ),
                vol.Required("value_sign", default=defaults["value_sign"]): number(
                    -1, 1, step=2
                ),
                vol.Required(
                    "source_timezone", default=defaults["source_timezone"]
                ): selector.TextSelector(),
                vol.Required(
                    "check_age", default=defaults["check_age"]
                ): selector.BooleanSelector(),
                vol.Required(
                    "max_age_hours", default=defaults["max_age_hours"]
                ): number(0.01, 168, "h"),
            }
        )
        return self.async_show_form(
            step_id="source_mapping",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "detail": detail,
                "sample_status": "Observed record fields"
                if paths
                else "No record fields sampled; enter data paths only",
            },
        )

    def _save_interval(self, binding):
        target = self._source["target"]
        data = binding.to_dict()
        if target in ("buy", "sell"):
            old = self._draft["sources"][target]
            rows = (
                old["forecast"]
                if self._source["operation"] == "append" and old["mode"] == "forecast"
                else []
            )
            self._draft["sources"][target] = PriceSource(
                "forecast",
                tuple(IntervalBinding.from_dict(row) for row in [*rows, data]),
            ).to_dict()
        elif target == "load":
            self._draft["sources"]["load"] = LoadSource(
                "forecast", forecast=binding
            ).to_dict()
        else:
            groups = deepcopy(self._draft["sources"]["pv"]["arrays"])
            index = int(self._source["group"]) - 1
            if index > len(groups):
                raise InputError("add PV groups in consecutive order")
            if index == len(groups):
                groups.append([])
            groups[index] = (
                [*groups[index], data]
                if self._source["operation"] == "append"
                else [data]
            )
            self._draft["sources"]["pv"]["arrays"] = groups

    async def async_step_source_measurement(self, user_input=None):
        target = self._source["target"]
        errors = {}
        if user_input is not None:
            if target in ("soc", "bms_soc"):
                if user_input["unit"] not in ("%", "fraction", "kWh"):
                    errors["base"] = "invalid_input"
                else:
                    self._draft["sources"][target] = self._binding.to_dict()
                    prefix = "bms_" if target == "bms_soc" else ""
                    age_key = (
                        "bms_max_age_seconds"
                        if target == "bms_soc"
                        else "soc_max_age_seconds"
                    )
                    self._draft["settings"][age_key] = user_input["max_age_seconds"]
                    self._draft["helpers"].pop(age_key, None)
                    self._draft["soc_options"][prefix + "sign"] = user_input["sign"]
                    self._draft["soc_options"][prefix + "unit"] = user_input["unit"]
                    self._draft["soc_options"][prefix + "timestamp_path"] = user_input[
                        "timestamp_path"
                    ]
                    self._draft["soc_options"][prefix + "timestamp_policy"] = (
                        user_input.get(
                            "timestamp_policy",
                            self._draft["soc_options"].get(
                                prefix + "timestamp_policy", "auto"
                            ),
                        )
                    )
            elif target == "load":
                if user_input["unit"] not in ("W", "kW"):
                    errors["base"] = "invalid_input"
                else:
                    self._draft["sources"]["load"] = LoadSource(
                        "recorder",
                        power=self._binding,
                        history_unit=user_input["unit"],
                        history_sign=user_input["sign"],
                    ).to_dict()
            else:
                output_unit = "kW" if "power" in target else "kWh"
                unit = user_input["unit"]
                if unit not in (("W", "kW") if output_unit == "kW" else ("Wh", "kWh")):
                    errors["base"] = "invalid_input"
                else:
                    self._draft["measurements"][target] = NumericSetting(
                        entity=self._binding,
                        unit=output_unit,
                        source_unit=unit,
                        multiplier=user_input["sign"]
                        * (0.001 if unit in ("W", "Wh") else 1),
                        max_age_seconds=user_input["max_age_seconds"],
                        minimum=0
                        if target in ("throughput_today", *DAILY_EXPORT_MEASUREMENTS)
                        else None,
                    ).to_dict()
            if not errors:
                return await self.async_step_menu()
        soc_source = target in ("soc", "bms_soc")
        prefix = "bms_" if target == "bms_soc" else ""
        options = self._draft["soc_options"]
        fields = {
            vol.Required(
                "unit",
                default=options.get(prefix + "unit", "%")
                if soc_source
                else "kWh"
                if "energy" in target or target == "throughput_today"
                else "W",
            ): select(["%", "fraction", "kWh", "Wh", "kW", "W"]),
            vol.Required(
                "sign", default=options.get(prefix + "sign", 1) if soc_source else 1
            ): number(-1, 1, step=2),
            vol.Required(
                "timestamp_path",
                default=options.get(prefix + "timestamp_path", "last_updated")
                if soc_source
                else "last_updated",
            ): selector.TextSelector(),
            vol.Required(
                "max_age_seconds",
                default=self._draft["settings"][
                    "bms_max_age_seconds" if prefix else "soc_max_age_seconds"
                ]
                if soc_source
                else 86400
                if target in DAILY_EXPORT_MEASUREMENTS
                else 600,
            ): number(1, 86400, "s"),
        }
        if soc_source:
            fields[
                vol.Required(
                    "timestamp_policy",
                    default=options.get(prefix + "timestamp_policy", "auto"),
                )
            ] = select(["auto", "exact_path"])
        return self.async_show_form(
            step_id="source_measurement",
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    async def async_step_helpers(self, user_input=None):
        if user_input is not None:
            self._helper_key = user_input["setting"]
            if user_input["mode"] == "fixed":
                self._draft["helpers"].pop(self._helper_key, None)
                return await self.async_step_menu()
            return await self.async_step_helper_entity()
        return self.async_show_form(
            step_id="helpers",
            data_schema=vol.Schema(
                {
                    vol.Required("setting"): select(NUMBERS),
                    vol.Required("mode", default="entity"): select(["entity", "fixed"]),
                }
            ),
        )

    async def async_step_helper_entity(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._helper_binding = entity_binding(
                    self.hass, user_input["entity_id"]
                )
                return await self.async_step_helper_value()
            except InputError:
                errors["base"] = "invalid_source"
        return self.async_show_form(
            step_id="helper_entity",
            data_schema=vol.Schema(
                {
                    vol.Required("entity_id"): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "number", "input_number"]
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_helper_value(self, user_input=None):
        errors = {}
        detail = ""
        _, _, low, high, unit = NUMBERS[self._helper_key]
        unit = unit.replace("currency", self._draft["currency"])
        if user_input is not None:
            try:
                selected = NumericSetting(
                    entity=entity_binding(
                        self.hass,
                        self._helper_binding.entity_id,
                        user_input.get("attribute"),
                    ),
                    unit=unit,
                    source_unit=user_input["source_unit"] or unit,
                    multiplier=user_input["multiplier"],
                    minimum=low,
                    maximum=high,
                    max_age_seconds=user_input["max_age_hours"] * 3600
                    if user_input["check_age"]
                    else None,
                )
                candidate = deepcopy(self._draft)
                candidate["helpers"][self._helper_key] = selected.to_dict()
                from .settings import validate_configuration

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
            step_id="helper_value",
            data_schema=vol.Schema(
                {
                    vol.Optional("attribute"): selector.AttributeSelector(
                        selector.AttributeSelectorConfig(
                            entity_id=self._helper_binding.entity_id
                        )
                    ),
                    vol.Required("source_unit", default=unit): selector.TextSelector(),
                    vol.Required("multiplier", default=1): number(-1000, 1000),
                    vol.Required("check_age", default=True): selector.BooleanSelector(),
                    vol.Required("max_age_hours", default=24): number(0.01, 8760, "h"),
                }
            ),
            errors=errors,
            description_placeholders={"detail": detail},
        )
