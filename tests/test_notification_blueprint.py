"""Exercise the published blueprint through Home Assistant automation scripts."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.core import callback
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

BLUEPRINT = (
    Path(__file__).resolve().parents[1]
    / "blueprints/automation/energy_compass/notifications.yaml"
)
PATH = "energy_compass/notifications.yaml"
ENTITIES = {
    "plan": "sensor.test_plan",
    "compass": "sensor.test_consumption_compass",
    "forecast_valid": "binary_sensor.test_forecast_valid",
    "next_boost": "sensor.test_next_boost_start",
    "next_cheap": "sensor.test_next_cheap_start",
    "next_limit": "sensor.test_next_limit_start",
    "last_favorable": "input_text.test_last_favorable",
    "last_limit": "input_text.test_last_limit",
    "daily_count": "input_number.test_daily_count",
    "daily_date": "input_datetime.test_daily_date",
    "last_sent": "input_datetime.test_last_sent",
}
DEFAULT_PREFERENCES = {
    "notify_enabled": True,
    "notify_events": ["favorable", "limit"],
    "notify_minimum_hours": 2,
    "notify_limit_lead_minutes": 30,
    "notify_daily_max": 3,
    "notify_cooldown_minutes": 60,
    "quiet_start": "22:00:00",
    "quiet_end": "08:00:00",
    "actions_configured": False,
}


@pytest.fixture(autouse=True)
async def stop_automation(hass):
    yield
    if hass.states.get("automation.test_compass") is not None:
        await hass.services.async_call(
            "automation",
            "turn_off",
            {"entity_id": "automation.test_compass"},
            blocking=True,
        )
        await hass.async_block_till_done()


@contextmanager
def loaded_blueprint():
    original = models.DomainBlueprints._load_blueprint

    @callback
    def load(self, path):
        if path != PATH:
            return original(self, path)
        return models.Blueprint(
            yaml_util.load_yaml(BLUEPRINT),
            expected_domain=self.domain,
            path=path,
            schema=automation.config.AUTOMATION_BLUEPRINT_SCHEMA,
        )

    with patch.object(models.DomainBlueprints, "_load_blueprint", load):
        yield


def window(start, end, level):
    return {"start": start.isoformat(), "end": end.isoformat(), "level": level}


def update_plan(hass, now, changes):
    attributes = dict(hass.states.get(ENTITIES["plan"]).attributes)
    attributes.update(changes)
    hass.states.async_set(ENTITIES["plan"], now.isoformat(), attributes)


async def setup_blueprint(
    hass,
    now,
    *,
    inputs=None,
    favorable=(),
    limit=(),
    valid=True,
    preferences=DEFAULT_PREFERENCES,
):
    await hass.config.async_set_time_zone("Europe/Warsaw")
    assert await async_setup_component(
        hass,
        "input_text",
        {
            "input_text": {
                "test_last_favorable": {"max": 64},
                "test_last_limit": {"max": 64},
            }
        },
    )
    assert await async_setup_component(
        hass,
        "input_number",
        {"input_number": {"test_daily_count": {"min": 0, "max": 100, "step": 1}}},
    )
    assert await async_setup_component(
        hass,
        "input_datetime",
        {
            "input_datetime": {
                "test_daily_date": {"has_date": True, "has_time": False},
                "test_last_sent": {"has_date": True, "has_time": True},
            }
        },
    )
    hass.states.async_set(ENTITIES["forecast_valid"], "on" if valid else "off")
    hass.states.async_set(ENTITIES["compass"], "NORMAL")
    plan_attributes = {
        "favorable_windows": list(favorable),
        "windows": {"limit": list(limit)},
        "window_status": {
            "boost": "none_in_coverage",
            "cheap": "none_in_coverage",
            "limit": "upcoming" if limit else "none_in_coverage",
        },
    }
    if preferences is not None:
        plan_attributes["notification_preferences"] = {
            **DEFAULT_PREFERENCES,
            **preferences,
        }
    hass.states.async_set(ENTITIES["plan"], now.isoformat(), plan_attributes)
    for key in ("next_boost", "next_cheap", "next_limit"):
        hass.states.async_set(ENTITIES[key], "unavailable")
    received = []
    hass.bus.async_listen("energy_compass_test_notification", received.append)
    selections = {
        **ENTITIES,
        "notification_actions": [
            {
                "event": "energy_compass_test_notification",
                "event_data": {"kind": "{{ event_kind }}"},
            }
        ],
        **(inputs or {}),
    }
    with loaded_blueprint():
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "alias": "Test compass",
                    "use_blueprint": {"path": PATH, "input": selections},
                }
            },
        )
    await hass.async_block_till_done()
    return received


async def trigger(hass):
    await hass.services.async_call(
        "automation",
        "trigger",
        {"entity_id": "automation.test_compass"},
        blocking=True,
    )
    await hass.async_block_till_done()


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_integration_notify_enabled_is_master_opt_in(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=3), "BOOST")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[candidate],
        preferences={"notify_enabled": False},
    )
    await trigger(hass)
    assert received == []
    assert hass.states.get(ENTITIES["daily_count"]).state == "0.0"


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_changed_integration_preferences_apply_without_automation_reload(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=3), "CHEAP")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[candidate],
        preferences={"notify_minimum_hours": 4},
    )
    await trigger(hass)
    assert received == []
    attributes = dict(hass.states.get(ENTITIES["plan"]).attributes)
    attributes["notification_preferences"] = {
        **attributes["notification_preferences"],
        "notify_minimum_hours": 2,
    }
    hass.states.async_set(ENTITIES["plan"], now.isoformat(), attributes)
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_integration_event_filter_changes_without_automation_reload(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=3), "CHEAP")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[candidate],
        preferences={"notify_events": ["limit"]},
    )
    await trigger(hass)
    assert received == []
    preferences = dict(
        hass.states.get(ENTITIES["plan"]).attributes["notification_preferences"]
    )
    update_plan(
        hass,
        now,
        {"notification_preferences": {**preferences, "notify_events": ["favorable"]}},
    )
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_resolved_limit_lead_can_exceed_blueprint_default(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now + timedelta(minutes=45), now + timedelta(hours=2), "LIMIT")
    received = await setup_blueprint(
        hass,
        now,
        limit=[candidate],
        preferences={"notify_limit_lead_minutes": 60},
    )
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["limit"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_resolved_quiet_hours_change_without_automation_reload(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=3), "BOOST")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[candidate],
        preferences={"quiet_start": "11:00:00", "quiet_end": "13:00:00"},
    )
    await trigger(hass)
    assert received == []
    preferences = dict(
        hass.states.get(ENTITIES["plan"]).attributes["notification_preferences"]
    )
    update_plan(
        hass,
        now,
        {
            "notification_preferences": {
                **preferences,
                "quiet_start": "14:00:00",
                "quiet_end": "15:00:00",
            }
        },
    )
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_resolved_daily_cap_and_cooldown_limit_new_windows(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    first = window(now, now + timedelta(hours=4), "BOOST")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[first],
        preferences={"notify_daily_max": 2, "notify_cooldown_minutes": 120},
    )
    await trigger(hass)
    assert len(received) == 1
    freezer.move_to(now + timedelta(hours=1))
    second = window(now + timedelta(hours=1), now + timedelta(hours=5), "CHEAP")
    update_plan(hass, now, {"favorable_windows": [second]})
    await trigger(hass)
    assert len(received) == 1
    freezer.move_to(now + timedelta(hours=2))
    await trigger(hass)
    assert len(received) == 2
    freezer.move_to(now + timedelta(hours=3))
    third = window(now + timedelta(hours=3), now + timedelta(hours=7), "BOOST")
    update_plan(hass, now, {"favorable_windows": [third]})
    await trigger(hass)
    assert len(received) == 2


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_blueprint_override_changes_threshold_but_not_master_opt_in(
    hass, freezer
):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    first = window(now, now + timedelta(hours=3), "BOOST")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[first],
        preferences={"notify_minimum_hours": 4},
        inputs={"use_blueprint_overrides": True, "minimum_hours": 2},
    )
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable"]
    freezer.move_to(now + timedelta(hours=1))
    second = window(now + timedelta(hours=1), now + timedelta(hours=4), "CHEAP")
    preferences = dict(
        hass.states.get(ENTITIES["plan"]).attributes["notification_preferences"]
    )
    update_plan(
        hass,
        now,
        {
            "favorable_windows": [second],
            "notification_preferences": {**preferences, "notify_enabled": False},
        },
    )
    await trigger(hass)
    assert len(received) == 1


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_missing_notification_preferences_fail_closed(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=3), "BOOST")
    received = await setup_blueprint(hass, now, favorable=[candidate], preferences=None)
    await trigger(hass)
    assert received == []


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_incomplete_notification_preferences_fail_closed(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=3), "BOOST")
    received = await setup_blueprint(hass, now, favorable=[candidate])
    preferences = dict(
        hass.states.get(ENTITIES["plan"]).attributes["notification_preferences"]
    )
    del preferences["notify_daily_max"]
    update_plan(hass, now, {"notification_preferences": preferences})
    await trigger(hass)
    assert received == []


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_empty_actions_do_not_consume_window_or_daily_cap(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=3), "BOOST")
    received = await setup_blueprint(
        hass, now, favorable=[candidate], inputs={"notification_actions": []}
    )
    await trigger(hass)
    assert received == []
    assert hass.states.get(ENTITIES["daily_count"]).state == "0.0"
    assert hass.states.get(ENTITIES["last_favorable"]).state in ("unknown", "")
    actions = [
        {
            "event": "energy_compass_test_notification",
            "event_data": {"kind": "{{ event_kind }}"},
        }
    ]
    config = {
        "automation": {
            "alias": "Test compass",
            "use_blueprint": {
                "path": PATH,
                "input": {**ENTITIES, "notification_actions": actions},
            },
        }
    }
    with (
        loaded_blueprint(),
        patch(
            "homeassistant.config.async_hass_config_yaml",
            new=AsyncMock(return_value=config),
        ),
    ):
        await hass.services.async_call("automation", "reload", blocking=True)
    await hass.async_block_till_done()
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_favorable_window_deduplicates_and_reschedules(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    first = window(now, now + timedelta(hours=3), "BOOST")
    received = await setup_blueprint(hass, now, favorable=[first])
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable"]
    await trigger(hass)
    assert len(received) == 1
    moved = window(now + timedelta(hours=1), now + timedelta(hours=4), "BOOST")
    update_plan(hass, now, {"favorable_windows": [moved], "windows": {"limit": []}})
    freezer.move_to(now + timedelta(hours=1))
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable", "favorable"], {
        key: hass.states.get(ENTITIES[key]).state
        for key in ("last_favorable", "last_sent", "daily_date", "daily_count")
    }


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_limit_lead_time_and_cooldown(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now + timedelta(minutes=45), now + timedelta(hours=2), "LIMIT")
    received = await setup_blueprint(hass, now, limit=[candidate])
    await trigger(hass)
    assert received == []
    freezer.move_to(now + timedelta(minutes=15))
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["limit"]
    await trigger(hass)
    assert len(received) == 1


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_minute_trigger_reaches_limit_lead_without_forecast_change(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now + timedelta(minutes=45), now + timedelta(hours=2), "LIMIT")
    received = await setup_blueprint(hass, now, limit=[candidate])
    assert received == []
    boundary = now + timedelta(minutes=15)
    freezer.move_to(boundary)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()
    assert [event.data["kind"] for event in received] == ["limit"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
@pytest.mark.parametrize("first_end_minutes", [20, 21])
async def test_minute_trigger_advances_to_later_limit_without_plan_refresh(
    hass, freezer, first_end_minutes
):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    first = window(
        now + timedelta(minutes=10), now + timedelta(minutes=first_end_minutes), "LIMIT"
    )
    second = window(now + timedelta(minutes=50), now + timedelta(hours=2), "LIMIT")
    received = await setup_blueprint(
        hass,
        now,
        limit=[first, second],
        inputs={"use_blueprint_overrides": True, "cooldown_minutes": 0},
    )
    await trigger(hass)
    assert len(received) == 1
    boundary = now + timedelta(minutes=20)
    freezer.move_to(boundary)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()
    assert [event.data["kind"] for event in received] == ["limit", "limit"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_recomputed_window_and_automation_reload_keep_one_identity(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    first = window(now, now + timedelta(hours=4), "BOOST")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[first],
        inputs={"use_blueprint_overrides": True, "cooldown_minutes": 0},
    )
    await trigger(hass)
    assert len(received) == 1
    await hass.services.async_call(
        "automation",
        "turn_off",
        {"entity_id": "automation.test_compass"},
        blocking=True,
    )
    await hass.services.async_call(
        "automation", "turn_on", {"entity_id": "automation.test_compass"}, blocking=True
    )
    freezer.move_to(now + timedelta(minutes=10))
    shifted = window(
        now + timedelta(minutes=10), now + timedelta(hours=4, minutes=10), "BOOST"
    )
    update_plan(hass, now, {"favorable_windows": [shifted], "windows": {"limit": []}})
    await trigger(hass)
    assert len(received) == 1


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_known_window_remains_actionable_with_short_future_coverage(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    current = window(now, now + timedelta(hours=3), "CHEAP")
    received = await setup_blueprint(hass, now, favorable=[current])
    hass.states.async_set(
        ENTITIES["forecast_valid"],
        "on",
        {"coverage_complete": False, "missing_sources": ["tomorrow_tariff"]},
    )
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable"]


@pytest.mark.freeze_time("2026-09-17T20:30:00+00:00")
async def test_quiet_hours_cross_midnight(hass, freezer):
    now = datetime(2026, 9, 17, 20, 30, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=12), "CHEAP")
    received = await setup_blueprint(hass, now, favorable=[candidate])
    await trigger(hass)
    assert received == []
    freezer.move_to(now + timedelta(hours=9, minutes=30))
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_local_date_cap_resets(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    first = window(now, now + timedelta(hours=20), "BOOST")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[first],
        inputs={
            "use_blueprint_overrides": True,
            "daily_cap": 1,
            "cooldown_minutes": 0,
            "quiet_start": "00:00:00",
            "quiet_end": "00:00:00",
        },
    )
    await trigger(hass)
    assert len(received) == 1
    second = window(now + timedelta(hours=1), now + timedelta(hours=21), "CHEAP")
    update_plan(hass, now, {"favorable_windows": [second], "windows": {"limit": []}})
    freezer.move_to(now + timedelta(hours=1))
    await trigger(hass)
    assert len(received) == 1
    freezer.move_to(now + timedelta(hours=12))
    await trigger(hass)
    assert len(received) == 2, {
        key: hass.states.get(ENTITIES[key]).state
        for key in ("last_favorable", "last_sent", "daily_date", "daily_count")
    }


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
@pytest.mark.parametrize("helper", ["daily_count", "daily_date", "last_sent"])
async def test_unknown_bookkeeping_after_first_send_suppresses_action(
    hass, freezer, helper
):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    first = window(now, now + timedelta(hours=3), "BOOST")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[first],
        inputs={"use_blueprint_overrides": True, "cooldown_minutes": 120}
        if helper == "last_sent"
        else None,
    )
    await trigger(hass)
    assert len(received) == 1
    freezer.move_to(now + timedelta(hours=1))
    second = window(now + timedelta(hours=1), now + timedelta(hours=4), "CHEAP")
    hass.states.async_set(ENTITIES[helper], "unknown")
    update_plan(hass, now, {"favorable_windows": [second], "windows": {"limit": []}})
    await trigger(hass)
    assert len(received) == 1


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
@pytest.mark.parametrize("case", ["no_window", "short", "stale", "unavailable"])
async def test_missing_or_invalid_advice_never_dispatches(hass, case):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    candidate = window(now, now + timedelta(hours=1), "BOOST")
    received = await setup_blueprint(
        hass,
        now,
        favorable=[] if case == "no_window" else [candidate],
        valid=case not in ("stale", "unavailable"),
    )
    if case == "unavailable":
        hass.states.async_set(ENTITIES["plan"], "unavailable")
    await trigger(hass)
    assert received == []
