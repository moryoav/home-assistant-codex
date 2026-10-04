"""Answer a waiting Codex question from a button on a mobile app notification."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.core import Event, HomeAssistant, callback

from .api import CodexCliApiClient, CodexCliApiError

if TYPE_CHECKING:
    from .coordinator import CodexCliCoordinator

# The companion apps fire this when a notification button is tapped.
EVENT_NOTIFICATION_ACTION = "mobile_app_notification_action"
# Written by the worker: the choice's position, the turn that asked, and the task. Keep the two in step.
CHOICE_ACTION_RE = re.compile(r"CODEX_CLI_CHOICE_(\d{1,2})_([0-9a-f]{32})_([A-Za-z0-9][A-Za-z0-9._-]{0,127})")
NOTIFICATION_ID = "codex_cli_choice"


def parse_choice_action(action: Any) -> tuple[str, str, int] | None:
    """Return the task ID, turn ID and choice position a Codex notification button names, or None for any other action."""
    match = CHOICE_ACTION_RE.fullmatch(action) if isinstance(action, str) else None
    if match is None:
        return None
    return match[3], match[2], int(match[1])


@callback
def async_listen_for_choices(
    hass: HomeAssistant, client: CodexCliApiClient, coordinator: CodexCliCoordinator
) -> Callable[[], None]:
    """Send the choice tapped on a Codex notification to the worker; return the function that stops listening."""

    async def handle_action(event: Event) -> None:
        """Reply with the tapped choice, and say so in a notification when the worker does not take it."""
        parsed = parse_choice_action(event.data.get("action"))
        if parsed is None:
            return
        task_id, turn_id, choice = parsed
        try:
            await client.reply_choice(task_id, turn_id, choice)
        except CodexCliApiError as exc:
            persistent_notification.async_create(
                hass,
                f"Codex did not get the answer you picked: {exc}",
                title="Codex",
                notification_id=NOTIFICATION_ID,
            )
            return
        await coordinator.async_request_refresh()

    return hass.bus.async_listen(EVENT_NOTIFICATION_ACTION, handle_action)
