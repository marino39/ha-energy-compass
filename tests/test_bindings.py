from datetime import UTC, datetime

import pytest

from custom_components.energy_compass.config_models import (
    LoadSource,
    NumericSetting,
    PriceSource,
    PvSource,
    SourceConfig,
    resolve_numeric,
    validate_sources,
)
from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    field_paths,
    rebind_entity,
    resolve_binding,
    validate_dependencies,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
STATES = {
    "sensor.forecast": {
        "state": "42",
        "last_updated": NOW.isoformat(),
        "attributes": {"series": [{"at": NOW.isoformat(), "value": 1}]},
    },
    "input_number.limit": {
        "state": "5.5",
        "last_updated": NOW.isoformat(),
        "attributes": {},
    },
}


def test_state_and_attribute_bindings_resolve_distinct_values():
    assert resolve_binding(STATES, EntityBinding("sensor.forecast")) == "42"
    assert (
        resolve_binding(STATES, EntityBinding("sensor.forecast", attribute="series"))[
            0
        ]["value"]
        == 1
    )


@pytest.mark.parametrize("unavailable", ("unknown", "unavailable"))
def test_attribute_binding_rejects_invalid_entity_state(unavailable):
    states = {"sensor.forecast": {**STATES["sensor.forecast"], "state": unavailable}}
    with pytest.raises(InputError, match="unavailable"):
        resolve_binding(states, EntityBinding("sensor.forecast", attribute="series"))


def test_binding_roundtrip_keeps_registry_identity_and_record_selector():
    selected = EntityBinding(
        "sensor.forecast",
        registry_id="stable-1",
        attribute="series",
        record_value_path="price.amount",
    )
    assert EntityBinding.from_dict(selected.to_dict()) == selected


def test_removed_entity_with_registry_identity_does_not_rebind():
    with pytest.raises(InputError, match="missing"):
        resolve_binding(STATES, EntityBinding("sensor.renamed", registry_id="stable-1"))


def test_own_output_feedback_is_rejected():
    with pytest.raises(InputError, match="feedback"):
        validate_dependencies(
            (EntityBinding("sensor.energy_compass_buy_price"),),
            {"sensor.energy_compass_buy_price"},
        )


def test_helper_numeric_value_is_validated_after_resolution():
    setting = NumericSetting(
        entity=EntityBinding("input_number.limit"), unit="kW", minimum=1, maximum=10
    )
    assert resolve_numeric(setting, STATES, NOW) == 5.5
    with pytest.raises(InputError):
        resolve_numeric(
            NumericSetting(
                entity=EntityBinding("sensor.forecast", attribute="series"), unit="kW"
            ),
            STATES,
            NOW,
        )
    with pytest.raises(InputError, match="stale"):
        resolve_numeric(setting, STATES, NOW.replace(day=18), max_age_seconds=60)


def test_entity_numeric_setting_serializes_and_enforces_its_own_age_limit():
    setting = NumericSetting(
        entity=EntityBinding("input_number.limit"), unit="kW", max_age_seconds=60
    )
    assert NumericSetting.from_dict(setting.to_dict()) == setting
    with pytest.raises(InputError, match="stale"):
        resolve_numeric(setting, STATES, NOW.replace(minute=2))


def test_numeric_helper_unit_conversion_and_sign():
    states = {
        "sensor.power": {
            "state": "-500",
            "last_updated": NOW.isoformat(),
            "attributes": {"unit_of_measurement": "W"},
        }
    }
    setting = NumericSetting(
        entity=EntityBinding("sensor.power"),
        unit="kW",
        source_unit="W",
        multiplier=-0.001,
    )
    assert resolve_numeric(setting, states, NOW) == 0.5
    states["sensor.power"]["attributes"]["unit_of_measurement"] = "kWh"
    with pytest.raises(InputError, match="unit"):
        resolve_numeric(setting, states, NOW)


def test_registry_rename_uses_stable_identity_and_missing_is_error():
    binding = EntityBinding("sensor.old", registry_id="stable-1")
    assert rebind_entity(binding, {"stable-1": "sensor.new"}).entity_id == "sensor.new"
    with pytest.raises(InputError, match="missing"):
        rebind_entity(binding, {})


def test_observed_field_paths_exclude_arrays_and_unsafe_expressions():
    assert field_paths({"price": {"amount": 2}, "records": [1]}) == ("price.amount",)


def test_source_config_roundtrip_and_enabled_component_validation():
    source = SourceConfig(
        buy=PriceSource("fixed", fixed=NumericSetting(fixed=0.5, unit="PLN/kWh")),
        sell=PriceSource("fixed", fixed=NumericSetting(fixed=0.1, unit="PLN/kWh")),
        pv=PvSource(False),
        load=LoadSource("daily_estimate", daily_estimate=NumericSetting(fixed=24)),
        battery_enabled=False,
        numeric={"grid_import_kw": NumericSetting(fixed=6, unit="kW")},
    )
    assert SourceConfig.from_dict(source.to_dict()) == source
    validate_sources(source, set())
    with pytest.raises(InputError, match="enabled PV"):
        validate_sources(
            SourceConfig(source.buy, source.sell, PvSource(True), source.load, False),
            set(),
        )


def test_foreign_source_name_is_not_an_ownership_check():
    binding = EntityBinding("sensor.energy_compass_user_template")
    validate_dependencies((binding,), set())
    assert (
        resolve_binding({binding.entity_id: {"state": "5", "attributes": {}}}, binding)
        == "5"
    )
