"""Read-only, configurable household energy advice."""

from copy import deepcopy

from homeassistant.const import Platform

from .coordinator import EnergyCompassCoordinator
from .settings import default_configuration, explicit_strategy_fields

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass, entry) -> bool:
    """Own all scheduling and source subscriptions through the config entry."""
    coordinator = EnergyCompassCoordinator(hass, entry)
    entry.runtime_data = coordinator
    try:
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_stop()
        raise
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload entities before releasing the entry's runtime ownership."""
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
        return True
    return False


async def async_migrate_entry(hass, entry) -> bool:
    """Fill absent configuration fields while retaining every existing choice."""
    if entry.version > 3:
        return False
    if entry.version < 3:
        current = deepcopy(dict(entry.data))
        defaults = default_configuration(
            current.get("currency", "EUR"),
            current.get("timezone", hass.config.time_zone),
        )

        def fill(target, fallback):
            for key, value in fallback.items():
                if key not in target:
                    target[key] = deepcopy(value)
                elif isinstance(value, dict) and isinstance(target[key], dict):
                    fill(target[key], value)

        fill(current, defaults)
        options = deepcopy(dict(entry.options))
        if "configuration" in options:
            fill(options["configuration"], defaults)
        for target in (
            current,
            *([options["configuration"]] if "configuration" in options else []),
        ):
            target["explicit_strategy_fields"] = explicit_strategy_fields(
                target["settings"]
            )
        hass.config_entries.async_update_entry(
            entry, data=current, options=options, version=3
        )
    return True
