"""Setup, unload and cleanup tests for the integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_energy_jp.const import CONF_ACCOUNT_NUMBER, DOMAIN
from custom_components.octopus_energy_jp.coordinator import OctopusEnergyJpCoordinator
from custom_components.octopus_energy_jp.sensor import SENSORS

ACCOUNT = "A-TEST1234"
ENTRY_DATA = {
    CONF_EMAIL: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_ACCOUNT_NUMBER: ACCOUNT,
}


def _new_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=ENTRY_DATA)


def _api_client_patch():
    """Never construct a real API client during tests."""
    return patch(
        "custom_components.octopus_energy_jp.OctopusEnergyJpApiClient", MagicMock()
    )


def _offline_fetch_patch():
    """Keep the coordinator's first refresh offline."""
    return patch.object(
        OctopusEnergyJpCoordinator, "_async_update_data", AsyncMock(return_value={})
    )


async def test_setup_creates_all_sensors_and_unloads(hass):
    """Setup registers every sensor description and unload tears them down."""
    entry = _new_entry()
    entry.add_to_hass(hass)

    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert len(hass.states.async_entity_ids("sensor")) == len(SENSORS)
        assert entry.runtime_data is not None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_legacy_usage_registry_entry_is_removed(hass):
    """A leftover ``{account}_usage`` entity is cleaned up during setup."""
    registry = er.async_get(hass)
    stale = registry.async_get_or_create("sensor", DOMAIN, f"{ACCOUNT}_usage")
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{ACCOUNT}_usage")
        == stale.entity_id
    )

    entry = _new_entry()
    entry.add_to_hass(hass)

    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert (
            registry.async_get_entity_id("sensor", DOMAIN, f"{ACCOUNT}_usage") is None
        )

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_remove_entry_completes(hass):
    """Deleting the entry runs its cleanup without raising."""
    entry = _new_entry()
    entry.add_to_hass(hass)

    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(entry.entry_id) is None
