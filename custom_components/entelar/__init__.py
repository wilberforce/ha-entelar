"""The Entelar Energy (Univers EMS) custom integration.

Entry point. Builds the coordinator from stored config-entry data,
performs a first refresh (fails fast on bad creds), and forwards setup
to the sensor platform. Also registers the `backfill_statistics` service.
"""
from __future__ import annotations

import logging
from datetime import date

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    DOMAIN,
    DEFAULT_API_BASE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    CONF_ACCOUNT,
    CONF_API_BASE,
    CONF_HISTORY_START,
    CONF_UPDATE_INTERVAL,
)
from .coordinator import EntelarCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_BACKFILL = "backfill_statistics"
ATTR_DAYS_TO_FETCH = "days_to_fetch"
BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DAYS_TO_FETCH, default=DEFAULT_HISTORY_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=DEFAULT_HISTORY_DAYS)
        )
    }
)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when options (e.g. update_interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register integration-wide services once."""
    if hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        return

    async def _handle_backfill(call: ServiceCall) -> None:
        days = call.data[ATTR_DAYS_TO_FETCH]
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            await coordinator.async_backfill(days)

    hass.services.async_register(
        DOMAIN, SERVICE_BACKFILL, _handle_backfill, schema=BACKFILL_SCHEMA
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Entelar from a config entry."""
    # Options take precedence over original config entry data
    update_interval = entry.options.get(
        CONF_UPDATE_INTERVAL,
        entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_SECONDS),
    )
    # A stored anchor is reused as-is; otherwise the coordinator resolves it from
    # the portal's operativeDate on first login and persists it (see below).
    stored = entry.data.get(CONF_HISTORY_START)
    history_start = date.fromisoformat(stored) if stored else None

    coordinator = EntelarCoordinator(
        hass,
        config_entry=entry,
        account=entry.data[CONF_ACCOUNT],
        password=entry.data[CONF_PASSWORD],
        history_start=history_start,
        api_base=entry.data.get(CONF_API_BASE, DEFAULT_API_BASE),
        update_interval_seconds=update_interval,
    )

    # Pulls the first snapshot; fails the setup if login is broken.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _register_services(hass)
    # Note: historical statistics flow via the coordinator on every
    # daily-history refresh (statistics_manager.py -> external statistic_ids
    # under the `entelar:` source prefix).
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Entelar config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL)
    return unload_ok
