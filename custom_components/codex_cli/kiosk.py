"""Read screenshots only from Fully Kiosk devices displaying the requested view."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from aiohttp import web

from homeassistant.components.image import async_get_image
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import is_hass_url

from .browser_auth import DiagnosticView

SCREENSHOT_MAX_BYTES = 5 * 1024 * 1024
PATH_RE = re.compile(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)?/?")


def matching_screenshots(hass: HomeAssistant, path: str) -> list[dict[str, str]]:
    """Find enabled Fully screenshots whose current-page sensor matches this HA view."""
    registry = er.async_get(hass)
    matches = []
    for entry in registry.entities.values():
        if (entry.platform != "fully_kiosk" or entry.domain != "sensor" or entry.disabled
            or not entry.device_id or not entry.config_entry_id or not entry.unique_id.endswith("-currentPage")):
            continue
        state = hass.states.get(entry.entity_id)
        if state is None or state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN}:
            continue
        current_url = state.attributes.get("full_url") or state.state
        if not isinstance(current_url, str):
            continue
        try:
            current = urlsplit(current_url)
            if (current.scheme not in {"http", "https"} or current.username or current.password
                or current.path.rstrip("/") != path.rstrip("/") or not is_hass_url(hass, current_url)):
                continue
        except ValueError:
            continue
        unique_id = entry.unique_id.removesuffix("-currentPage") + "-screenshot"
        entity_id = registry.async_get_entity_id("image", "fully_kiosk", unique_id)
        screenshot = registry.async_get(entity_id) if entity_id else None
        if (screenshot is None or screenshot.disabled or screenshot.device_id != entry.device_id
            or screenshot.config_entry_id != entry.config_entry_id):
            continue
        image_state = hass.states.get(screenshot.entity_id)
        name = (image_state.attributes.get("friendly_name") if image_state else None) or screenshot.name or screenshot.original_name or screenshot.entity_id
        matches.append({"entity_id": screenshot.entity_id, "name": name})
    return matches


class KioskScreenshotsView(DiagnosticView):
    """Let the paired worker discover and fetch screenshots for matching kiosk views."""

    url = "/api/codex_cli/kiosk_screenshots"
    name = "api:codex_cli:kiosk_screenshots"

    async def post(self, request: web.Request) -> web.Response:
        """List matching screenshots, or return one freshly fetched, bounded image."""
        data = await self.payload(request)
        self.broker(request, data)
        path = data.get("path")
        if not isinstance(path, str) or len(path) > 512 or not PATH_RE.fullmatch(path):
            raise web.HTTPBadRequest()
        screenshots = matching_screenshots(self.hass, path)
        if "entity_id" not in data:
            return self.json({"screenshots": screenshots}, headers={"Cache-Control": "no-store"})
        entity_id = data["entity_id"]
        if not isinstance(entity_id, str) or not any(shot["entity_id"] == entity_id for shot in screenshots):
            raise web.HTTPNotFound()
        try:
            image = await async_get_image(self.hass, entity_id, timeout=10)
        except HomeAssistantError as err:
            raise web.HTTPBadGateway() from err
        if len(image.content) > SCREENSHOT_MAX_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=SCREENSHOT_MAX_BYTES, actual_size=len(image.content))
        return web.Response(body=image.content, content_type=image.content_type, headers={"Cache-Control": "no-store"})
