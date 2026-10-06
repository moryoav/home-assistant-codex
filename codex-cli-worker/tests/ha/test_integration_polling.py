"""Keep the worker informed while the integration is loaded, even without enabled entities."""
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
spec = importlib.util.spec_from_file_location(
    "polling_ha_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = integration
spec.loader.exec_module(integration)


@pytest.mark.parametrize("disable_polling", [False, True])
async def test_status_polling_without_entities_and_after_unload(hass, disable_polling):
    """Keep polling after the last entity is disabled, respect disabled polling, and stop on unload."""
    entry = MockConfigEntry(
        domain="codex_cli",
        title="Codex",
        data={"base_url": "http://worker"},
        state=ConfigEntryState.SETUP_IN_PROGRESS,
        pref_disable_polling=disable_polling,
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.status.return_value = {"codex_login": {"status_ok": True}}
    worker = SimpleNamespace(base_url="http://worker", api_token="paired-worker")
    broker = SimpleNamespace(setup=AsyncMock(), close=lambda: None)

    with (
        patch.object(integration, "async_discover_worker", return_value=worker),
        patch.object(integration, "CodexCliApiClient", return_value=client),
        patch.object(integration, "BrowserSessions", return_value=broker),
        # No entity listeners are registered, as when every entity is disabled.
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None),
        patch.object(hass.config_entries, "async_unload_platforms", return_value=True),
    ):
        context = current_entry.set(entry)
        try:
            assert await integration.async_setup_entry(hass, entry)
        finally:
            current_entry.reset(context)
        entry._async_set_state(hass, ConfigEntryState.LOADED, None)
        try:
            assert client.status.await_count == 1
            coordinator = entry.runtime_data.coordinator
            # Disabling the last entity removes its coordinator listener.
            remove_entity = coordinator.async_add_listener(lambda: None)
            remove_entity()
            for _ in range(5):
                async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=35))
                await hass.async_block_till_done()
            assert client.status.await_count == (1 if disable_polling else 6)
        finally:
            assert await integration.async_unload_entry(hass, entry)
            await entry._async_process_on_unload(hass)
        calls_after_unload = client.status.await_count
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3))
        await hass.async_block_till_done()
        assert client.status.await_count == calls_after_unload
