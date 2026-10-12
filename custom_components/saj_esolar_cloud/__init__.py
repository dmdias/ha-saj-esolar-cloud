"""The SAJ eSolar integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import DOMAIN, CONF_REGION, CONF_MONITORED_PLANTS
from .coordinator import SAJeSolarDataUpdateCoordinator
from .history import async_run_import

PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_IMPORT_HISTORY = "import_history"
IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Optional("plant_uid"): cv.string,
        vol.Optional("start_date"): cv.date,
    }
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the integration actions."""

    async def handle_import_history(call: ServiceCall) -> None:
        coordinators: list[SAJeSolarDataUpdateCoordinator] = list(hass.data.get(DOMAIN, {}).values())
        wanted = call.data.get("plant_uid")
        targets = [
            (coordinator, plant_uid)
            for coordinator in coordinators
            for plant_uid in coordinator.monitored_plants
            if wanted in (None, plant_uid)
        ]
        if not targets:
            raise ServiceValidationError(f"No monitored plant {wanted or ''} found")
        for coordinator, plant_uid in targets:
            hass.async_create_background_task(
                async_run_import(hass, coordinator, plant_uid, call.data.get("start_date")),
                f"{DOMAIN} import history {plant_uid}",
            )

    hass.services.async_register(DOMAIN, SERVICE_IMPORT_HISTORY, handle_import_history, schema=IMPORT_HISTORY_SCHEMA)
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SAJ eSolar from a config entry."""
    session = async_get_clientsession(hass)
    coordinator = SAJeSolarDataUpdateCoordinator(
        hass,
        session,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_REGION, "eu"),
        entry.data.get(CONF_MONITORED_PLANTS, []),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
