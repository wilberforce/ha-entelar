"""Config flow for the Entelar custom integration.

Single-step wizard: account + password + optional update interval.
On submit, runs a test login to validate the credentials.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    DEFAULT_API_BASE,
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    CONF_ACCOUNT,
    CONF_API_BASE,
    CONF_UPDATE_INTERVAL,
)
from .errors import EntelarLoginError
from .login import login
from .snapshot import discover_site

_LOGGER = logging.getLogger(__name__)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_API_BASE, default=DEFAULT_API_BASE): str,
        vol.Optional(
            CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL_SECONDS
        ): vol.All(int, vol.Range(min=30, max=3600)),
    }
)


class EntelarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for setting up an Entelar account."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "EntelarOptionsFlow":
        # HA 2025+ framework wires `self.config_entry` after instantiation;
        # we don't pass it ourselves. (Older HA used EntelarOptionsFlow(entry).)
        return EntelarOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                # Run the login in an executor so we don't block the loop
                session = await self.hass.async_add_executor_job(
                    login,
                    user_input[CONF_API_BASE],
                    user_input[CONF_ACCOUNT],
                    user_input[CONF_PASSWORD],
                )
                # Discover the site so we can label the device meaningfully
                session = await self.hass.async_add_executor_job(
                    discover_site, session
                )
            except EntelarLoginError as e:
                _LOGGER.warning("Entelar login failed during config: %s", e)
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Entelar config flow")
                errors["base"] = "unknown"
            else:
                # Use the site id as the unique_id so re-adding the same
                # account replaces (rather than duplicates) the entry.
                await self.async_set_unique_id(session.get("siteId_short") or session.get("userId"))
                self._abort_if_unique_id_configured()
                title = session.get("site_name") or f"Entelar ({user_input[CONF_ACCOUNT]})"
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class EntelarOptionsFlow(config_entries.OptionsFlow):
    """Options flow -- lets users change the polling cadence after install
    without removing & re-adding the integration.

    Note: HA 2025+ makes `self.config_entry` a read-only property auto-populated
    by the framework. We must NOT override __init__ to set it.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL,
            self.config_entry.data.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_SECONDS
            ),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_UPDATE_INTERVAL, default=current): vol.All(
                        int, vol.Range(min=30, max=3600)
                    )
                }
            ),
        )
