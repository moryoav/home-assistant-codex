"""A tapped choice on a Codex notification, handled on Home Assistant's real event bus."""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.components import persistent_notification
from homeassistant.setup import async_setup_component

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
PACKAGE = "notification_actions_ha_test"


def _load(name):
    """Load one module of the integration under a stand-in package, so its relative imports resolve."""
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    full_name = f"{PACKAGE}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


api = _load("api")
actions = _load("notification_actions")

TURN_ID = "0123456789abcdef0123456789abcdef"
TASK_ID = "20261004T101500Z-1a2b3c4d"


def test_only_codex_choice_actions_are_parsed():
    """The worker's button action gives the task, turn and position; any other action is left alone."""
    assert actions.parse_choice_action(f"CODEX_CLI_CHOICE_2_{TURN_ID}_{TASK_ID}") == (TASK_ID, TURN_ID, 2)
    # A task id may contain underscores, dots and dashes.
    assert actions.parse_choice_action(f"CODEX_CLI_CHOICE_0_{TURN_ID}_my_task.1-a") == ("my_task.1-a", TURN_ID, 0)
    for other in ("OPEN_DOOR", f"CODEX_CLI_CHOICE_x_{TURN_ID}_{TASK_ID}", f"CODEX_CLI_CHOICE_0_short_{TASK_ID}",
                  f"CODEX_CLI_CHOICE_0_{TURN_ID}_", f"CODEX_CLI_CHOICE_0_{TURN_ID}_bad/task", "", None, 7):
        assert actions.parse_choice_action(other) is None


async def test_tapped_choice_is_sent_to_the_worker(hass):
    """Tapping a choice replies to that turn's question and refreshes the sensors; other buttons are ignored."""
    client = AsyncMock()
    coordinator = AsyncMock()
    stop = actions.async_listen_for_choices(hass, client, coordinator)
    hass.bus.async_fire("mobile_app_notification_action", {"action": "OPEN_DOOR"})
    await hass.async_block_till_done()
    client.reply_choice.assert_not_called()

    hass.bus.async_fire("mobile_app_notification_action", {"action": f"CODEX_CLI_CHOICE_1_{TURN_ID}_{TASK_ID}"})
    await hass.async_block_till_done()
    client.reply_choice.assert_awaited_once_with(TASK_ID, TURN_ID, 1)
    coordinator.async_request_refresh.assert_awaited_once()

    # After unloading, a tap is no longer handled.
    stop()
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"CODEX_CLI_CHOICE_0_{TURN_ID}_{TASK_ID}"})
    await hass.async_block_till_done()
    assert client.reply_choice.await_count == 1


async def test_refused_choice_is_reported_in_a_notification(hass):
    """When the worker does not take the answer, for example because the question was answered, the user is told why."""
    assert await async_setup_component(hass, "persistent_notification", {})
    shown = {}
    persistent_notification.async_register_callback(hass, lambda _update, notifications: shown.update(notifications))
    client = AsyncMock()
    client.reply_choice.side_effect = api.CodexCliApiError("This question is no longer waiting for an answer.", status=409)
    coordinator = AsyncMock()
    actions.async_listen_for_choices(hass, client, coordinator)
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"CODEX_CLI_CHOICE_0_{TURN_ID}_{TASK_ID}"})
    await hass.async_block_till_done()

    assert shown[actions.NOTIFICATION_ID]["title"] == "Codex"
    assert shown[actions.NOTIFICATION_ID]["message"] == (
        "Codex did not get the answer you picked: This question is no longer waiting for an answer."
    )
    coordinator.async_request_refresh.assert_not_called()
