"""Data coordinator for the Codex CLI integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CodexCliApiClient, CodexCliApiError, CodexCliAuthError
from .const import DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_NOT_SIGNED_IN = "codex_not_signed_in"


class CodexCliCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the local Codex CLI Worker."""

    def __init__(self, hass: HomeAssistant, client: CodexCliApiClient) -> None:
        """Set up polling of the worker status through the given client, every DEFAULT_SCAN_INTERVAL_SECONDS."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the worker status and update the sign-in repair issue.

        Rejected worker authentication raises ConfigEntryAuthFailed; any other failure raises UpdateFailed.
        """
        try:
            data = await self.client.status()
        except CodexCliAuthError as exc:
            raise ConfigEntryAuthFailed("Worker authentication failed") from exc
        except CodexCliApiError as exc:
            raise UpdateFailed(str(exc)) from exc
        except Exception as exc:
            raise UpdateFailed(f"Unexpected worker status error: {exc}") from exc

        self._update_repair_issue(data)
        return data

    def _update_repair_issue(self, data: dict[str, Any]) -> None:
        """Create a repair issue if Codex CLI needs sign-in."""
        login = data.get("codex_login") or {}
        if login.get("status_ok"):
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NOT_SIGNED_IN)
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_NOT_SIGNED_IN,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NOT_SIGNED_IN,
        )
