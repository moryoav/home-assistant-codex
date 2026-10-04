"""Config flow for the Codex integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CodexCliApiError, CodexCliAuthError
from .const import CONF_BASE_URL, DOMAIN
from .discovery import async_discover_worker


def _schema() -> vol.Schema:
    """Return the empty form schema; the worker is discovered, so the forms ask for no input."""
    return vol.Schema({})


async def _discover_input(hass: HomeAssistant) -> dict[str, Any]:
    """Discover and validate the local worker app."""
    worker = await async_discover_worker(hass, async_get_clientsession(hass))
    return {CONF_BASE_URL: worker.base_url}


def _error_for_exception(exc: CodexCliApiError) -> str:
    """Map API exceptions to config-flow error keys."""
    if isinstance(exc, CodexCliAuthError):
        return "invalid_auth"
    return "cannot_connect"


class CodexCliConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Codex."""

    VERSION = 1

    async def _async_discover_entry_data(self) -> dict[str, Any]:
        """Discover the worker and guard against duplicate entries."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await _discover_input(self.hass)

    async def async_step_hassio(self, discovery_info: dict[str, Any]):
        """Set up the integration from Supervisor app discovery."""
        try:
            data = await self._async_discover_entry_data()
        except CodexCliApiError:
            return self.async_abort(reason="cannot_connect")
        return self.async_create_entry(title="Codex", data=data)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Set up the integration from the Home Assistant UI."""
        errors: dict[str, str] = {}
        try:
            data = await self._async_discover_entry_data()
        except CodexCliApiError as exc:
            errors["base"] = _error_for_exception(exc)
        else:
            return self.async_create_entry(title="Codex", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Handle reauthentication requests."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Confirm new worker credentials."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                data = await _discover_input(self.hass)
            except CodexCliApiError as exc:
                errors["base"] = _error_for_exception(exc)
            else:
                self.hass.config_entries.async_update_entry(entry, data=data, options={})
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_schema(),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Handle UI reconfiguration."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                data = await _discover_input(self.hass)
            except CodexCliApiError as exc:
                errors["base"] = _error_for_exception(exc)
            else:
                self.hass.config_entries.async_update_entry(entry, data=data, options={})
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(),
            errors=errors,
        )
