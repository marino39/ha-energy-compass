"""Bounded diagnostics without source entity names or captured payloads."""


async def async_get_config_entry_diagnostics(hass, entry):
    """Report validity and expired-plan metadata without exporting private inputs."""
    coordinator = entry.runtime_data
    data = coordinator.data
    previous = coordinator.previous_plan
    return {
        "status": data.get("status"),
        "valid": data.get("valid"),
        "generated_at": data.get("generated_at"),
        "coverage_end": data.get("quality", {}).get("coverage_end"),
        "classification_mode": data.get("classification_mode"),
        "calibration": data.get("calibration"),
        "expired_previous_plan": {
            "expired": True,
            "generated_at": previous.get("generated_at"),
            "interval_count": len(previous.get("intervals", [])),
        }
        if previous
        else None,
        "selected_measurement_roles": {
            key: "daily_throughput_constraint"
            if key == "throughput_today"
            else "diagnostic_only"
            for key in coordinator.configuration.get("measurements", {})
        },
    }
