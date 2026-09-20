"""Home Assistant integration for the Codex CLI Worker add-on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CodexCliApiClient, CodexCliApiError
from .const import (
    ATTR_PROMPT,
    ATTR_REPLY,
    ATTR_TASK_ID,
    ATTR_FORCE,
    CONF_BASE_URL,
    DOMAIN,
    SERVICE_CANCEL_TASK,
    SERVICE_CONTINUE_TASK,
    SERVICE_GET_LOGIN_STATUS,
    SERVICE_GET_TASK,
    SERVICE_LOGOUT,
    SERVICE_LIST_TASKS,
    SERVICE_REPLY_TASK,
    SERVICE_START_LOGIN,
    SERVICE_START_TASK,
)
from .coordinator import CodexCliCoordinator
from .discovery import async_discover_worker

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

START_TASK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PROMPT): str,
    }
)
TASK_ID_SCHEMA = vol.Schema({vol.Required(ATTR_TASK_ID): str})
REPLY_TASK_SCHEMA = vol.Schema({vol.Required(ATTR_TASK_ID): str, vol.Required(ATTR_REPLY): str})
CONTINUE_TASK_SCHEMA = vol.Schema({vol.Required(ATTR_TASK_ID): str, vol.Required("message"): str})
LIST_TASKS_SCHEMA = vol.Schema({
    vol.Optional("limit"): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
    vol.Optional("offset"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("status"): vol.In(["queued", "running", "waiting_for_input", "completed", "failed", "cancelled"]),
    vol.Optional("order"): vol.In(["created_asc", "updated_desc"]),
    vol.Optional("summary"): bool,
})
START_LOGIN_SCHEMA = vol.Schema({vol.Optional(ATTR_FORCE, default=False): bool})


@dataclass(slots=True)
class CodexCliRuntimeData:
    """Runtime objects for one Codex config entry."""

    client: CodexCliApiClient
    coordinator: CodexCliCoordinator


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Codex CLI integration."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Codex CLI from a config entry."""
    session = async_get_clientsession(hass)
    try:
        worker = await async_discover_worker(hass, session)
    except CodexCliApiError as exc:
        raise ConfigEntryNotReady(str(exc)) from exc

    _async_update_discovered_entry(hass, entry, worker.base_url)
    client = CodexCliApiClient(session, worker.base_url, worker.api_token)
    coordinator = CodexCliCoordinator(hass, client)
    entry.runtime_data = CodexCliRuntimeData(client=client, coordinator=coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Codex CLI config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry.runtime_data = None
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_update_discovered_entry(hass: HomeAssistant, entry: ConfigEntry, base_url: str) -> None:
    """Persist only non-secret discovered connection metadata."""
    data = {CONF_BASE_URL: base_url}
    if dict(entry.data) != data or dict(entry.options):
        hass.config_entries.async_update_entry(entry, data=data, options={})


def _first_runtime(hass: HomeAssistant) -> CodexCliRuntimeData:
    """Return the first loaded Codex runtime."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime_data = getattr(entry, "runtime_data", None)
        if isinstance(runtime_data, CodexCliRuntimeData):
            return runtime_data
    raise ServiceValidationError("Codex integration is not configured or is not loaded")


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_START_TASK):
        return

    register_kwargs: dict[str, Any] = {}
    try:
        from homeassistant.core import SupportsResponse

        register_kwargs["supports_response"] = SupportsResponse.OPTIONAL
    except ImportError:
        pass

    async def handle_start_task(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.start_task(call.data[ATTR_PROMPT])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_start_login(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.start_login(bool(call.data.get(ATTR_FORCE, False)))
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_get_login_status(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            return await runtime_data.client.login_status()
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_logout(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.logout()
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_get_task(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            return await runtime_data.client.get_task(call.data[ATTR_TASK_ID])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_list_tasks(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            return await runtime_data.client.list_tasks(**call.data)
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_cancel_task(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.cancel_task(call.data[ATTR_TASK_ID])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_reply_task(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.reply_task(call.data[ATTR_TASK_ID], call.data[ATTR_REPLY])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    async def handle_continue_task(call: ServiceCall) -> dict[str, Any]:
        runtime_data = _first_runtime(hass)
        try:
            result = await runtime_data.client.continue_task(call.data[ATTR_TASK_ID], call.data["message"])
        except CodexCliApiError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await runtime_data.coordinator.async_request_refresh()
        return result

    hass.services.async_register(
        DOMAIN, SERVICE_CONTINUE_TASK, handle_continue_task,
        schema=CONTINUE_TASK_SCHEMA, **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_TASK,
        handle_start_task,
        schema=START_TASK_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_LOGIN,
        handle_start_login,
        schema=START_LOGIN_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_LOGIN_STATUS,
        handle_get_login_status,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOGOUT,
        handle_logout,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TASK,
        handle_get_task,
        schema=TASK_ID_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_TASKS,
        handle_list_tasks,
        schema=LIST_TASKS_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_TASK,
        handle_cancel_task,
        schema=TASK_ID_SCHEMA,
        **register_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPLY_TASK,
        handle_reply_task,
        schema=REPLY_TASK_SCHEMA,
        **register_kwargs,
    )
