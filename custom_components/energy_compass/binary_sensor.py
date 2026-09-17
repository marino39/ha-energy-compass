"""Current input and recommendation validity, separate from future coverage."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.util import dt as dt_util

from .entity import EnergyCompassEntity
from .sources.bindings import parse_timestamp


async def async_setup_entry(hass, entry, async_add_entities):
    """Publish the validity gate used by notification and dashboard consumers."""
    async_add_entities([EnergyCompassValidity(entry.runtime_data, "forecast_valid")])


class EnergyCompassValidity(EnergyCompassEntity, BinarySensorEntity):
    """Never mark a stale generation as actionable advice."""

    @property
    def is_on(self):
        data = self.coordinator.data
        return bool(
            data.get("valid")
            and dt_util.utcnow() < parse_timestamp(data["valid_until"])
        )
