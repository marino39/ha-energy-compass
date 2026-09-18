"""Entry-owned scheduling, input generations and coherent result publication."""

import asyncio
import json
import logging
from copy import deepcopy
from functools import partial

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .config_models import SourceConfig
from .daily_export import (
    DAILY_EXPORT_MEASUREMENTS,
    daily_export_active,
    daily_export_observations,
)
from .engine.models import InputError, SolveError
from .flow_schema import entity_ids, rebind_configuration, snapshot
from .runtime import (
    _coverage,
    _soc,
    async_history,
    available_forecasts,
    compute,
    measurement_diagnostics,
)
from .settings import DOMAIN, merged_configuration, validate_configuration
from .sources.bindings import merge_continuations, parse_intervals, parse_timestamp

_LOGGER = logging.getLogger(__name__)


class EnergyCompassCoordinator(DataUpdateCoordinator):
    """Keep one worker per entry and discard superseded or unloaded generations."""

    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.entry = entry
        self.configuration = merged_configuration(entry)
        self.data = {"status": "calculating", "valid": False}
        self._closed = False
        self._generation = 0
        self._pending = False
        self._runner = None
        self._worker = None
        self._debounce = None
        self._boundary = None
        self._source_unsub = None
        self._registry_unsub = None
        self._fingerprint = None
        self._soc_reference = None
        self._previous_soc = None
        self._anchors = {}
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.anchors")
        self._battery_commitment = None
        self._dispatch_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.dispatch")
        self.previous_plan = None

    async def async_start(self):
        """Restore small observation anchors and attach only this entry's listeners."""
        self._anchors = await self._store.async_load() or {}
        self._battery_commitment = await self._dispatch_store.async_load()
        self._registry_unsub = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed
        )
        self._subscribe()
        await self.async_recalculate()

    def _subscribe(self):
        if self._source_unsub:
            self._source_unsub()
            self._source_unsub = None
        try:
            self.configuration = rebind_configuration(
                self.hass, merged_configuration(self.entry)
            )
            ids = entity_ids(self.configuration)
            if ids:
                self._source_unsub = async_track_state_change_event(
                    self.hass, ids, self._source_changed
                )
            ir.async_delete_issue(self.hass, DOMAIN, self.entry.entry_id)
        except InputError as err:
            self._invalidate("invalid_input", str(err))
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.entry.entry_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="source_removed",
                translation_placeholders={"name": self.entry.title},
            )

    @callback
    def _registry_changed(self, event):
        tracked = entity_ids(self.configuration)
        if (
            event.data.get("entity_id") in tracked
            or event.data.get("old_entity_id") in tracked
        ):
            self._generation += 1
            self._fingerprint = None
            self._subscribe()
            self._schedule(0)

    def _content(self, states, values):
        source, missing = available_forecasts(
            SourceConfig.from_dict(self.configuration["sources"]), states
        )
        selected = [missing]

        def visit(value):
            if isinstance(value, dict):
                if "entity_id" in value:
                    state = states.get(value["entity_id"], {})
                    raw = (
                        state.get("attributes", {}).get(value.get("attribute"))
                        if value.get("attribute")
                        else state.get("state")
                    )
                    try:
                        raw = float(raw)
                    except TypeError, ValueError:
                        pass
                    if (
                        source.soc
                        and value["entity_id"] == source.soc.entity_id
                        and value.get("attribute") == source.soc.attribute
                    ):
                        energy, _ = _soc(
                            self.configuration,
                            source,
                            values,
                            states,
                            dt_util.utcnow(),
                            self._previous_soc,
                        )
                        percent = energy / values["capacity_kwh"] * 100
                        if (
                            self._soc_reference is not None
                            and abs(percent - self._soc_reference)
                            < values["soc_trigger_percent"]
                        ):
                            raw = self._soc_reference
                        else:
                            self._soc_reference = percent
                            raw = percent
                    selected.append((value["entity_id"], value.get("attribute"), raw))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        planning_inputs = {"helpers": self.configuration["helpers"]}
        if source.soc:
            planning_inputs["soc"] = source.soc.to_dict()
        if daily_export_active(values):
            for name in DAILY_EXPORT_MEASUREMENTS:
                planning_inputs[name] = self.configuration.get("measurements", {}).get(
                    name
                )
        if source.battery_enabled and values["daily_cycles"]:
            planning_inputs["throughput_today"] = self.configuration.get(
                "measurements", {}
            ).get("throughput_today")
        visit(planning_inputs)
        groups = [
            price.forecast
            for price in (source.buy, source.sell)
            if price.mode == "forecast"
        ]
        groups.extend(source.pv.arrays)
        if source.load.forecast:
            groups.append((source.load.forecast,))
        for group in groups:
            rows = merge_continuations(
                tuple(
                    parse_intervals(states, binding, dt_util.utcnow())
                    for binding in group
                )
            )
            selected.append(
                [
                    (row.start.isoformat(), row.end.isoformat(), row.value)
                    for row in rows
                ]
            )
        return json.dumps(selected, sort_keys=True, default=str)

    def _inputs(self):
        config = rebind_configuration(self.hass, merged_configuration(self.entry))
        self.configuration = config
        states = snapshot(self.hass, config)
        now = dt_util.utcnow()
        values = validate_configuration(config, states, now)
        daily_export_observations(config, states, values, now)
        source, _ = available_forecasts(
            SourceConfig.from_dict(config["sources"]), states
        )
        required = entity_ids(
            {"sources": source.to_dict(), "helpers": config["helpers"]}
        )
        for entity_id in required:
            if entity_id not in states or states[entity_id]["state"] in (
                "unknown",
                "unavailable",
            ):
                raise InputError(f"missing or unavailable source: {entity_id}")
        groups = [
            price.forecast
            for price in (source.buy, source.sell)
            if price.mode == "forecast"
        ]
        groups.extend(source.pv.arrays)
        if source.load.forecast:
            groups.append((source.load.forecast,))
        for group in groups:
            rows = merge_continuations(
                tuple(parse_intervals(states, binding, now) for binding in group)
            )
            _coverage(rows, now)
        if source.battery_enabled:
            _soc(config, source, values, states, now, self._previous_soc)
        return config, states, now, values

    @callback
    def _source_changed(self, event):
        if self._closed:
            return
        try:
            _, states, _, values = self._inputs()
            content = self._content(states, values)
        except (InputError, KeyError, ValueError) as err:
            self._generation += 1
            self._fingerprint = None
            self._invalidate("invalid_input", str(err))
            self._schedule(0)
            return
        if content == self._fingerprint:
            if self.data.get("valid"):
                self.async_set_updated_data(
                    {
                        **self.data,
                        "measurements": measurement_diagnostics(
                            self.configuration, states, dt_util.utcnow()
                        ),
                    }
                )
            return
        self._fingerprint = content
        self._generation += 1
        self._pending = True
        self._invalidate("calculating", "inputs_changed")
        self._schedule(values["debounce_seconds"])

    def _schedule(self, delay):
        if self._closed or self._debounce is not None:
            return

        @callback
        def run():
            self._debounce = None
            if not self._closed:
                self.hass.async_create_task(self.async_recalculate())

        if delay == 0:
            run()
        else:
            self._debounce = self.hass.loop.call_later(delay, run)

    def _next_boundary(self, values):
        if self._boundary:
            self._boundary.cancel()
        now = dt_util.utcnow()
        seconds = values.get("refresh_minutes", 15) * 60
        delay = seconds - now.timestamp() % seconds
        if self.data.get("valid_until"):
            delay = min(
                delay,
                max(
                    0.1,
                    (parse_timestamp(self.data["valid_until"]) - now).total_seconds(),
                ),
            )
        source = SourceConfig.from_dict(self.configuration["sources"])
        freshness = (
            [values.get("soc_max_age_seconds", 600)] if source.battery_enabled else []
        )
        freshness.extend(
            item.get("max_age_seconds")
            for item in self.configuration.get("helpers", {}).values()
            if item.get("max_age_seconds")
        )
        if freshness:
            delay = min(delay, max(1, min(freshness) / 2))

        @callback
        def refresh():
            self._boundary = None
            self._generation += 1
            self._invalidate("calculating", "interval_boundary")
            self._schedule(0)

        self._boundary = self.hass.loop.call_later(delay, refresh)

    def _invalidate(self, status, reason):
        """Revoke advice validity; keep its display only while a replacement runs."""
        retained = (
            self.data
            if status == "calculating"
            and (self.data.get("valid") or self.data.get("refreshing"))
            else {}
        )
        if self.data.get("valid"):
            self.previous_plan = {**deepcopy(self.data), "expired": True}
        self.async_set_updated_data(
            {
                **retained,
                "status": status,
                "valid": False,
                "refreshing": bool(retained),
                "reason": reason,
                "expired_previous_generated_at": self.previous_plan.get("generated_at")
                if self.previous_plan
                else None,
            }
        )

    def _anchor_windows(self, result):
        now = parse_timestamp(result["generated_at"])
        updated = {}
        for key, windows in result["windows"].items():
            active = next(
                (
                    window
                    for window in windows
                    if parse_timestamp(window["start"])
                    <= now
                    < parse_timestamp(window["end"])
                ),
                None,
            )
            if active:
                previous = self._anchors.get(key)
                start = (
                    previous["start"]
                    if previous and parse_timestamp(previous["end"]) >= now
                    else active["start"]
                )
                active["start"] = start
                active["start_basis"] = "first_observed"
                updated[key] = {"start": start, "end": active["end"]}
        self._anchors = updated
        self._store.async_delay_save(lambda: self._anchors, 1)

    async def async_recalculate(self) -> None:
        """Snapshot on the event loop, calculate off-loop, and publish one generation."""
        if self._closed:
            return
        if self._runner is not None:
            self._pending = True
            return
        self._runner = asyncio.current_task()
        try:
            while not self._closed:
                self._pending = False
                generation = self._generation
                values = self.configuration["settings"]
                try:
                    config, states, now, values = self._inputs()
                    self._fingerprint = self._content(states, values)
                    self._invalidate("calculating", "calculating")
                    history, _ = await async_history(self.hass, config, states, now)
                    if self._closed:
                        return
                    self._worker = self.hass.async_add_executor_job(
                        partial(
                            compute,
                            deepcopy(config),
                            states,
                            now,
                            previous_soc=self._previous_soc,
                            battery_commitment=deepcopy(self._battery_commitment),
                            **history,
                        )
                    )
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(self._worker),
                            timeout=values["total_time_limit_s"] + 1,
                        )
                    except TimeoutError:
                        self._invalidate("timeout", "worker_deadline")
                        # Executor threads cannot be cancelled; retain ownership until completion.
                        await asyncio.shield(self._worker)
                        raise SolveError("timeout") from None
                    finally:
                        self._worker = None
                    if self._closed:
                        return
                    if generation == self._generation:
                        self._previous_soc = result["quality"].pop(
                            "soc_observation", None
                        )
                        self._anchor_windows(result)
                        self._commit_battery_direction(result)
                        self.async_set_updated_data(result)
                except InputError as err:
                    if not self._closed and generation == self._generation:
                        self._invalidate("invalid_input", str(err))
                except SolveError as err:
                    if not self._closed and generation == self._generation:
                        self._invalidate(
                            err.reason
                            if err.reason in ("timeout", "infeasible")
                            else "error",
                            err.reason,
                        )
                except Exception as err:
                    if not self._closed and generation == self._generation:
                        _LOGGER.exception("Advisory calculation failed")
                        self._invalidate("error", type(err).__name__)
                if not self._pending and generation == self._generation:
                    break
            if not self._closed:
                self._next_boundary(values)
        finally:
            self._runner = None

    def _commit_battery_direction(self, result):
        """Persist published advice, never pretend forecast energy was measured."""
        rows = result.get("intervals", [])
        mode = rows[0].get("battery_mode") if rows else None
        if mode is None or not result.get("dispatch_policy", {}).get(
            "minimum_mode_minutes"
        ):
            self._battery_commitment = None
        elif not self._battery_commitment or self._battery_commitment["mode"] != mode:
            self._battery_commitment = {"mode": mode, "since": result["generated_at"]}
        self._dispatch_store.async_delay_save(lambda: self._battery_commitment, 1)

    async def async_stop(self):
        """Detach listeners and prevent late worker results from owning entities."""
        self._closed = True
        self._generation += 1
        for handle in (self._debounce, self._boundary):
            if handle:
                handle.cancel()
        self._debounce = self._boundary = None
        for unsubscribe in (self._source_unsub, self._registry_unsub):
            if unsubscribe:
                unsubscribe()
        self._source_unsub = self._registry_unsub = None
        if self._worker is not None:
            try:
                await asyncio.shield(self._worker)
            except Exception:
                _LOGGER.debug(
                    "Discarded failed advisory worker during unload", exc_info=True
                )
        await self._store.async_save(self._anchors)
        await self._dispatch_store.async_save(self._battery_commitment)
