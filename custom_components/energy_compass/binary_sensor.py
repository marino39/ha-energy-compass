"""Current input and recommendation validity, separate from future coverage."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.util import dt as dt_util

from .entity import EnergyCompassEntity
from .sources.bindings import parse_timestamp


async def async_setup_entry(hass, entry, async_add_entities):
    """Publish the validity gate used by notification and dashboard consumers."""
    async_add_entities(
        [
            EnergyCompassValidity(entry.runtime_data, "forecast_valid"),
            EnergyCompassAlert(entry.runtime_data, "alert"),
        ]
    )


class EnergyCompassAlert(EnergyCompassEntity, BinarySensorEntity):
    """Expose calculation and input failures independently of retained advice."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self):
        return bool(self.coordinator.data.get("alert"))

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        alert = data.get("alert") or {}
        return {
            "code": alert.get("code"),
            "reason": alert.get("reason"),
            "since": alert.get("since"),
            "plan_retained": data.get("plan_retained", False),
            "last_successful_plan_at": self.coordinator.last_successful_plan_at,
        }


class EnergyCompassValidity(EnergyCompassEntity, BinarySensorEntity):
    """Mark advice available only within its current plan coverage."""

    @property
    def is_on(self):
        data = self.coordinator.data
        return bool(
            data.get("valid")
            and dt_util.utcnow() < parse_timestamp(data["valid_until"])
        )
