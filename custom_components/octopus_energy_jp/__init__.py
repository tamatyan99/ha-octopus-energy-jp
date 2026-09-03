"""Octopus Energy Japan integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OctopusEnergyJpApiClient
from .const import DOMAIN
from .coordinator import OctopusEnergyJpCoordinator
from .statistics import OctopusStatisticsImporter

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    api = OctopusEnergyJpApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
    )
    coordinator = OctopusEnergyJpCoordinator(hass, entry, api)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()

    importer = OctopusStatisticsImporter(hass, entry.entry_id)
    await importer.async_load()
    if coordinator.data:
        await importer.async_import(coordinator.data["hourly"])

    def _import_on_update() -> None:
        if coordinator.data:
            hass.async_create_task(importer.async_import(coordinator.data["hourly"]))

    entry.async_on_unload(coordinator.async_add_listener(_import_on_update))

    entry.runtime_data = {"coordinator": coordinator, "importer": importer}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
