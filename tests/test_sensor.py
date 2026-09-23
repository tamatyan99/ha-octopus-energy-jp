"""Sensor descriptions and entity behaviour tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_energy_jp.const import CONF_ACCOUNT_NUMBER, DOMAIN
from custom_components.octopus_energy_jp.coordinator import OctopusEnergyJpCoordinator
from custom_components.octopus_energy_jp.sensor import SENSORS, OctopusSensor

ACCOUNT = "A-TEST1234"
ACCOUNT_SLUG = ACCOUNT.lower().replace("-", "_")
ENTRY_DATA = {
    CONF_EMAIL: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_ACCOUNT_NUMBER: ACCOUNT,
}

TOTAL_KEYS = {"today_kwh", "month_kwh", "cost_today", "cost_month"}
SNAPSHOT_KEYS = [
    "yesterday_kwh",
    "prev_month_kwh",
    "billing_kwh",
    "cost_yesterday",
    "prev_month_cost",
    "billing_cost",
]
DIFF_KEYS = ["diff_kwh", "month_diff_kwh"]


def _by_key():
    """Index sensor descriptions by key."""
    return {description.key: description for description in SENSORS}


def _payload():
    """Offline coordinator payload covering every sensor value function."""
    now = dt_util.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    return {
        "yesterday_kwh": 10.5,
        "today_kwh": 3.2,
        "month_kwh": 120.4,
        "diff_kwh": 1.1,
        "diff_pct": 11.0,
        "avg_rate": 31.25,
        "cost_yesterday": 326,
        "cost_today": 100,
        "cost_month": 3760,
        "prev_month_kwh": 200.1,
        "prev_month_cost": 6200,
        "month_diff_kwh": -5.5,
        "month_diff_pct": -4.0,
        "daily": [{"d": "2026-09-01", "kwh": 10.0, "cost": 300}],
        "yesterday_series": [],
        "today_series": [],
        "hourly": [],
        "plan_name": "Standard Plan",
        "billing_period": None,
        "billing": None,
        "last_update": now.isoformat(),
        "today_start": today_start.isoformat(),
        "month_start": month_start.isoformat(),
    }


async def _setup_with_payload(hass, payload):
    """Set up the entry with the API client and fetch kept offline."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=dict(ENTRY_DATA))
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.octopus_energy_jp.OctopusEnergyJpApiClient",
            MagicMock(),
        ),
        patch.object(
            OctopusEnergyJpCoordinator,
            "_async_update_data",
            AsyncMock(return_value=payload),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def test_keys_unique_and_legacy_usage_absent():
    """The legacy usage key stays gone and every key is unique."""
    keys = [description.key for description in SENSORS]
    assert "usage" not in keys
    assert len(keys) == len(set(keys))


def test_last_reset_fn_exactly_matches_total_sensors():
    """Running totals carry last_reset; TOTAL state class matches exactly."""
    by_key = _by_key()
    with_reset = {k for k, d in by_key.items() if d.last_reset_fn is not None}
    assert with_reset == TOTAL_KEYS
    total = {k for k, d in by_key.items() if d.state_class == SensorStateClass.TOTAL}
    assert total == TOTAL_KEYS


@pytest.mark.parametrize("key", SNAPSHOT_KEYS)
def test_snapshot_sensors_have_no_state_class(key):
    """Point-in-time snapshots must not declare a state class."""
    assert _by_key()[key].state_class is None


@pytest.mark.parametrize("key", DIFF_KEYS)
def test_diff_sensors_are_diagnostic_measurements(key):
    """Comparison sensors are diagnostic MEASUREMENT values."""
    description = _by_key()[key]
    assert description.state_class == SensorStateClass.MEASUREMENT
    assert description.entity_category == EntityCategory.DIAGNOSTIC


def test_only_diff_sensors_are_diagnostic():
    """No other sensor may gain an entity category unnoticed."""
    diagnostic = {
        k
        for k, d in _by_key().items()
        if d.entity_category == EntityCategory.DIAGNOSTIC
    }
    assert diagnostic == set(DIFF_KEYS)


def test_no_sensor_is_disabled_by_default():
    """Every sensor stays user-facing; none hides on new installs."""
    for description in SENSORS:
        assert description.entity_registry_enabled_default is True


async def test_today_sensor_reports_local_midnight_last_reset(hass):
    """The today sensor exposes last_reset at today's local midnight."""
    payload = _payload()
    await _setup_with_payload(hass, payload)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ACCOUNT}_today_kwh")
    assert entity_id == f"sensor.octopus_energy_{ACCOUNT_SLUG}_today_usage"

    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == payload["today_kwh"]

    raw = state.attributes.get("last_reset")
    assert raw is not None
    parsed = dt_util.parse_datetime(raw) if isinstance(raw, str) else raw
    assert parsed is not None
    assert parsed.utcoffset() is not None
    expected = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    assert parsed == expected


async def test_yesterday_sensor_has_no_last_reset_or_state_class(hass):
    """Snapshot sensors expose neither last_reset nor state_class."""
    await _setup_with_payload(hass, _payload())

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{ACCOUNT}_yesterday_kwh"
    )
    assert entity_id == f"sensor.octopus_energy_{ACCOUNT_SLUG}_yesterday_usage"

    state = hass.states.get(entity_id)
    assert state is not None
    assert "last_reset" not in state.attributes
    assert "state_class" not in state.attributes


async def test_yesterday_sensor_carries_aggregate_attributes(hass):
    """The v0.3.0 attribute move puts aggregates on yesterday_kwh."""
    payload = _payload()
    await _setup_with_payload(hass, payload)

    state = hass.states.get(f"sensor.octopus_energy_{ACCOUNT_SLUG}_yesterday_usage")
    assert state is not None
    attrs = state.attributes
    for key in (
        "avg_rate",
        "plan_name",
        "today_kwh",
        "month_kwh",
        "diff_kwh",
        "cost_today",
        "daily",
        "last_update",
    ):
        assert key in attrs
    assert attrs["avg_rate"] == payload["avg_rate"]
    assert attrs["plan_name"] == payload["plan_name"]
    assert attrs["today_kwh"] == payload["today_kwh"]


async def test_diff_sensors_registered_as_diagnostic(hass):
    """The comparison sensors show entity_category diagnostic in the registry."""
    await _setup_with_payload(hass, _payload())

    registry = er.async_get(hass)
    for key in DIFF_KEYS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ACCOUNT}_{key}")
        assert entity_id is not None
        reg_entry = registry.async_get(entity_id)
        assert reg_entry is not None
        assert reg_entry.entity_category == "diagnostic"


@pytest.mark.parametrize("key", [description.key for description in SENSORS])
def test_no_data_yields_none_without_error(key):
    """With no coordinator data every sensor reports None, never raises."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=dict(ENTRY_DATA))
    coordinator = MagicMock()
    coordinator.data = None
    sensor = OctopusSensor(coordinator, _by_key()[key], entry)
    assert sensor.native_value is None
    assert sensor.last_reset is None
