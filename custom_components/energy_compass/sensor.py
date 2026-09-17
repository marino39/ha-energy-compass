"""Advisory sensors with stable entry-scoped unique IDs."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .entity import EnergyCompassEntity, next_change
from .settings import DOMAIN
from .sources.bindings import parse_timestamp

SENSOR_KEYS = (
    "consumption_compass",
    "consumption_cost",
    "next_change",
    "next_boost_start",
    "next_cheap_start",
    "next_limit_start",
    "energy_compass",
    "plan",
    "expected_net_cost",
    "expected_wear_cost",
    "optimizer_status",
)


async def async_setup_entry(hass, entry, async_add_entities):
    """Publish the same contract for every independent installation."""
    registry = er.async_get(hass)
    settings = entry.runtime_data.configuration["settings"]
    for key in SENSOR_KEYS:
        enabled = (
            settings["expose_costs"]
            if key in ("consumption_cost", "expected_net_cost", "expected_wear_cost")
            else settings["expose_windows"]
            if key.startswith("next_")
            else True
        )
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id and (registered := registry.async_get(entity_id)):
            if not enabled and registered.disabled_by is None:
                registry.async_update_entity(
                    entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
                )
            elif (
                enabled
                and registered.disabled_by == er.RegistryEntryDisabler.INTEGRATION
            ):
                registry.async_update_entity(entity_id, disabled_by=None)
    async_add_entities(
        [EnergyCompassSensor(entry.runtime_data, key) for key in SENSOR_KEYS]
    )


class EnergyCompassSensor(EnergyCompassEntity, SensorEntity):
    """Render one public advisory value from the coordinator's current generation."""

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        if key.startswith("next_") or key == "plan":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        if key in ("consumption_cost", "expected_net_cost", "expected_wear_cost"):
            self._attr_native_unit_of_measurement = coordinator.configuration[
                "currency"
            ] + ("/kWh" if key == "consumption_cost" else "")
            self._attr_entity_registry_enabled_default = coordinator.configuration[
                "settings"
            ]["expose_costs"]
        if key.startswith("next_"):
            self._attr_entity_registry_enabled_default = coordinator.configuration[
                "settings"
            ]["expose_windows"]

    @property
    def suggested_display_precision(self):
        if self.key in ("consumption_cost", "expected_net_cost", "expected_wear_cost"):
            return self.coordinator.data.get("presentation", {}).get(
                "cost_precision",
                self.coordinator.configuration["settings"]["cost_precision"],
            )
        return None

    @callback
    def _handle_coordinator_update(self):
        self._update_suggested_precision()
        super()._handle_coordinator_update()

    @property
    def native_value(self):
        data = self.coordinator.data
        if self.key == "optimizer_status":
            return data["status"]
        if not data.get("valid"):
            return None
        if self.key == "consumption_compass":
            return data["outlook"][0]["level"]
        if self.key == "consumption_cost":
            return data["outlook"][0]["cost_per_kwh"]
        if self.key == "energy_compass":
            return data["intervals"][0]["state"]
        if self.key == "plan":
            return parse_timestamp(data["generated_at"])
        if self.key == "next_change":
            row = next_change(data)
            return parse_timestamp(row["start"]) if row else None
        if self.key.startswith("next_"):
            row = self._window()
            return parse_timestamp(row["start"]) if row else None
        return data.get(self.key)
