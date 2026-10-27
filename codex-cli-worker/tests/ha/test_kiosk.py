"""Fully Kiosk screenshot discovery and paired-worker HTTP boundaries."""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.image import Image, ImageEntity
from homeassistant.components.image.const import DATA_COMPONENT
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
package = types.ModuleType("kiosk_ha_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("kiosk_ha_test.kiosk", ROOT / "kiosk.py")
kiosk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kiosk)
auth = importlib.import_module("kiosk_ha_test.browser_auth")

VIEW = "/dashboard-home/default_view"
URL = "http://192.168.1.229:8123" + VIEW + "?kiosk"
PNG = b"\x89PNG\r\n\x1a\n" + b"test screenshot"


def tablet(hass, key, url=URL):
    """Register a real Fully device and its independently renamed sensor and image."""
    config = MockConfigEntry(domain="fully_kiosk", title=key)
    config.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=config.entry_id, identifiers={("fully_kiosk", key)})
    registry = er.async_get(hass)
    sensor = registry.async_get_or_create("sensor", "fully_kiosk", f"{key}-currentPage", config_entry=config, device_id=device.id)
    image = registry.async_get_or_create("image", "fully_kiosk", f"{key}-screenshot", config_entry=config, device_id=device.id)
    sensor = registry.async_update_entity(sensor.entity_id, new_entity_id=f"sensor.renamed_{key}")
    image = registry.async_update_entity(image.entity_id, new_entity_id=f"image.other_name_{key}")
    hass.states.async_set(sensor.entity_id, url)
    hass.states.async_set(image.entity_id, "2026-10-27T00:00:00+00:00", {"friendly_name": f"{key} tablet screenshot"})
    return sensor, image


@pytest.fixture
async def kiosk_view(hass):
    """Register the actual HTTP view and paired-worker broker with HA authentication."""
    assert await async_setup_component(hass, "http", {})
    hass.config.internal_url = "http://192.168.1.229:8123"
    sessions = auth.BrowserSessions(hass, "paired-worker")
    await sessions.setup()
    hass.data.setdefault(auth.DOMAIN, {})["browser_sessions"] = sessions
    hass.http.register_view(kiosk.KioskScreenshotsView(hass))
    yield sessions
    sessions.close()


async def test_discovery_matches_each_device_and_exact_view(hass):
    """Discover both matching tablets despite entity renames and a truncated sensor state."""
    hass.config.internal_url = "http://192.168.1.229:8123"
    hass.config.external_url = "https://ha.example.test"
    first, first_image = tablet(hass, "first")
    second, second_image = tablet(hass, "second", "https://ha.example.test" + VIEW)
    full_url = URL + "&display=" + "a" * 300
    hass.states.async_set(first.entity_id, full_url[:255], {"full_url": full_url, "truncated": True})
    tablet(hass, "other_view", "http://192.168.1.229:8123/dashboard-home/kitchen")
    tablet(hass, "other_origin", "https://other.example.test" + VIEW)
    tablet(hass, "malformed", "http://[invalid" + VIEW)
    disabled, _ = tablet(hass, "disabled")
    er.async_get(hass).async_update_entity(disabled.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    unavailable, _ = tablet(hass, "unavailable")
    hass.states.async_set(unavailable.entity_id, "unavailable", {"full_url": URL})
    _, disabled_image = tablet(hass, "disabled_image")
    er.async_get(hass).async_update_entity(disabled_image.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    assert kiosk.matching_screenshots(hass, VIEW) == [
        {"entity_id": first_image.entity_id, "name": "first tablet screenshot"},
        {"entity_id": second_image.entity_id, "name": "second tablet screenshot"},
    ]


async def test_http_requires_both_credentials_and_skips_no_matches(hass, hass_client, hass_client_no_auth, kiosk_view):
    """Return an empty list silently and enforce HA administrator authentication plus pairing."""
    client = await hass_client()
    response = await client.post(kiosk.KioskScreenshotsView.url, json={"worker_token": "paired-worker", "path": VIEW})
    assert response.status == 200
    assert await response.json() == {"screenshots": []}
    assert response.headers["Cache-Control"] == "no-store"
    response = await client.post(kiosk.KioskScreenshotsView.url, json={"worker_token": "wrong", "path": VIEW})
    assert response.status == 403
    response = await client.delete(kiosk.KioskScreenshotsView.url)
    assert response.status == 405
    no_auth = await hass_client_no_auth()
    response = await no_auth.post(kiosk.KioskScreenshotsView.url, json={"worker_token": "paired-worker", "path": VIEW})
    assert response.status == 401
    token = await hass.auth.async_create_refresh_token(kiosk_view.user)
    read_only = await hass_client(access_token=hass.auth.async_create_access_token(token))
    response = await read_only.post(kiosk.KioskScreenshotsView.url, json={"worker_token": "paired-worker", "path": VIEW})
    assert response.status == 403


async def test_http_revalidates_view_and_limits_image(hass, hass_client, kiosk_view):
    """Reject arbitrary images, changed views, unsafe paths, and oversized image content."""
    client = await hass_client()
    sensor, image = tablet(hass, "first")
    body = {"worker_token": "paired-worker", "path": VIEW, "entity_id": image.entity_id}
    with patch.object(kiosk, "async_get_image", new_callable=AsyncMock) as fetch:
        for path in ("https://example.test/", "//example.test", "/dashboard-home/../other", VIEW + "?kiosk", VIEW + "#other", "/dashboard/%2e%2e"):
            response = await client.post(kiosk.KioskScreenshotsView.url, json={**body, "path": path})
            assert response.status == 400
        response = await client.post(kiosk.KioskScreenshotsView.url, json={**body, "entity_id": "image.private_camera"})
        assert response.status == 404
        hass.states.async_set(sensor.entity_id, "http://192.168.1.229:8123/dashboard-home/kitchen")
        response = await client.post(kiosk.KioskScreenshotsView.url, json=body)
        assert response.status == 404
        fetch.assert_not_called()
        hass.states.async_set(sensor.entity_id, URL)
        fetch.return_value = Image("image/png", b"x" * (kiosk.SCREENSHOT_MAX_BYTES + 1))
        response = await client.post(kiosk.KioskScreenshotsView.url, json=body)
        assert response.status == 413
        fetch.side_effect = HomeAssistantError("Device unavailable")
        response = await client.post(kiosk.KioskScreenshotsView.url, json=body)
        assert response.status == 502


async def test_http_fetches_fresh_image_bytes(hass, hass_client, kiosk_view):
    """Use HA's actual image API and fetch new screenshot bytes on every request."""
    assert await async_setup_component(hass, "image", {})
    _, image = tablet(hass, "first")

    class Screenshot(ImageEntity):
        """Return changing screen content through the real image entity component."""

        _attr_content_type = "image/png"
        _attr_unique_id = "first-screenshot"
        _attr_device_info = {"identifiers": {("fully_kiosk", "first")}}

        def __init__(self):
            """Initialize an image with the registered Fully screenshot entity ID."""
            super().__init__(hass)
            self.entity_id = image.entity_id
            self.calls = 0

        async def async_image(self):
            """Return distinct bytes for each actual image request."""
            self.calls += 1
            return PNG + str(self.calls).encode()

    entity = Screenshot()
    hass.states.async_remove(image.entity_id)
    component = hass.data[DATA_COMPONENT]
    platform = component._async_init_entity_platform("fully_kiosk", None)
    platform.config_entry = hass.config_entries.async_get_entry(image.config_entry_id)
    component._platforms["fully_kiosk"] = platform
    await platform.async_add_entities([entity])
    client = await hass_client()
    body = {"worker_token": "paired-worker", "path": VIEW, "entity_id": image.entity_id}
    for expected in (1, 2):
        response = await client.post(kiosk.KioskScreenshotsView.url, json=body)
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.read() == PNG + str(expected).encode()
    assert entity.calls == 2
