"""Sensors for the Codex CLI integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BASE_URL, DOMAIN
from .coordinator import CodexCliCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Codex CLI sensors."""
    coordinator: CodexCliCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            CodexLastTaskSensor(coordinator, entry),
            CodexTaskCountSensor(coordinator, entry),
            CodexAuthSensor(coordinator, entry),
            CodexFiveHourLimitSensor(coordinator, entry),
            CodexFiveHourResetSensor(coordinator, entry),
            CodexWeeklyLimitSensor(coordinator, entry),
            CodexWeeklyResetSensor(coordinator, entry),
        ]
    )


class _CodexSensor(CoordinatorEntity[CodexCliCoordinator], SensorEntity):
    """Base for the Codex sensors: diagnostic entities on the Codex device that read the coordinator's worker status."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry, key: str) -> None:
        """Set the unique ID from the entry ID and key, and attach the sensor to the Codex device of the config entry."""
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Codex",
            "manufacturer": "OpenAI",
            "model": "Codex CLI Worker",
            "configuration_url": entry.data.get(CONF_BASE_URL),
        }


class CodexLastTaskSensor(_CodexSensor):
    """Show the latest Codex task status."""

    _attr_icon = "mdi:clipboard-text-clock-outline"
    _attr_translation_key = "last_task"

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor with the unique ID key "last_task"."""
        super().__init__(coordinator, entry, "last_task")

    @property
    def native_value(self) -> str:
        """Return the status of the worker's latest task, or "unknown" when there is none."""
        latest = (self.coordinator.data or {}).get("latest_task") or {}
        return latest.get("status") or "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the latest task's details and error, plus the active task ID from the worker status."""
        latest = (self.coordinator.data or {}).get("latest_task") or {}
        return {
            "task_id": latest.get("task_id"),
            "title": latest.get("title"),
            "summary": latest.get("summary"),
            "question": latest.get("question"),
            "updated_at": latest.get("updated_at"),
            "active_task_id": (self.coordinator.data or {}).get("active_task_id"),
            "error": latest.get("error"),
        }


class CodexTaskCountSensor(_CodexSensor):
    """Show how many Codex tasks are active."""

    _attr_icon = "mdi:progress-clock"
    _attr_translation_key = "active_tasks"

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor with the unique ID key "task_count"."""
        super().__init__(coordinator, entry, "task_count")

    @property
    def native_value(self) -> int:
        """Return the worker's active task count, falling back to its task_count field, or 0 when neither is set."""
        data = self.coordinator.data or {}
        return int(data.get("active_task_count") or data.get("task_count") or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the active task ID and the total number of tasks the worker knows."""
        data = self.coordinator.data or {}
        return {
            "active_task_id": data.get("active_task_id"),
            "total_task_count": data.get("total_task_count"),
        }


class CodexAuthSensor(_CodexSensor):
    """Show Codex login status."""

    _attr_icon = "mdi:account-key-outline"
    _attr_translation_key = "auth_status"

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor with the unique ID key "auth_status"."""
        super().__init__(coordinator, entry, "auth_status")

    @property
    def native_value(self) -> str:
        """Return "logged_in", "auth_file_present" (saved credentials that fail the login check) or "not_logged_in"."""
        login = (self.coordinator.data or {}).get("codex_login") or {}
        if login.get("status_ok"):
            return "logged_in"
        if login.get("has_auth_file"):
            return "auth_file_present"
        return "not_logged_in"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the login check message and auth file flag, plus the login flow's status, URL, user code and QR URL."""
        login = (self.coordinator.data or {}).get("codex_login") or {}
        auth_flow = (self.coordinator.data or {}).get("auth_flow") or {}
        return {
            "message": login.get("message"),
            "has_auth_file": login.get("has_auth_file"),
            "auth_flow_status": auth_flow.get("status"),
            "verification_url": auth_flow.get("verification_url"),
            "user_code": auth_flow.get("user_code"),
            "qr_url": auth_flow.get("qr_url"),
        }


class CodexFiveHourLimitSensor(_CodexSensor):
    """Show Codex 5-hour usage line from interactive status."""

    _attr_icon = "mdi:timer-sand"
    _attr_translation_key = "five_hour_limit"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor with the unique ID key "five_hour_limit"."""
        super().__init__(coordinator, entry, "five_hour_limit")

    @property
    def native_value(self) -> int | None:
        """Return the percentage of the 5-hour limit that is left, or None when it is missing or not an integer."""
        usage = (self.coordinator.data or {}).get("codex_usage") or {}
        try:
            return int(str(usage.get("five_hour_percent") or ""))
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the usage refresh state, the 5-hour, weekly and context figures, and the raw usage excerpt."""
        usage = (self.coordinator.data or {}).get("codex_usage") or {}
        return {
            "usage_status": usage.get("status"),
            "reported": bool(usage.get("five_hour_percent")),
            "updated_at": usage.get("updated_at"),
            "error": usage.get("error"),
            "limit": usage.get("five_hour_limit"),
            "reset": usage.get("five_hour_reset_at") or usage.get("five_hour_reset"),
            "reset_text": usage.get("five_hour_reset"),
            "weekly_limit": usage.get("weekly_limit"),
            "weekly_percent": usage.get("weekly_percent"),
            "weekly_reset": usage.get("weekly_reset_at") or usage.get("weekly_reset"),
            "weekly_reset_text": usage.get("weekly_reset"),
            "context_remaining": usage.get("context_remaining"),
            "context_percent": usage.get("context_percent"),
            "raw_excerpt": usage.get("raw_excerpt"),
        }


def _parse_timestamp(value: Any) -> datetime | None:
    """Return a datetime for an ISO 8601 value, or None when it is empty, invalid or has no timezone.

    A value that is already a datetime is returned unchanged.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


class CodexFiveHourResetSensor(_CodexSensor):
    """Show when the Codex 5-hour usage limit resets."""

    _attr_icon = "mdi:clock-time-five-outline"
    _attr_translation_key = "five_hour_reset"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor with the unique ID key "five_hour_reset"."""
        super().__init__(coordinator, entry, "five_hour_reset")

    @property
    def native_value(self) -> datetime | None:
        """Return when the 5-hour limit resets, from the worker's five_hour_reset_at, or None when it is not known."""
        usage = (self.coordinator.data or {}).get("codex_usage") or {}
        return _parse_timestamp(usage.get("five_hour_reset_at"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the usage refresh state, plus the 5-hour reset text, limit text and percentage Codex reported."""
        usage = (self.coordinator.data or {}).get("codex_usage") or {}
        return {
            "usage_status": usage.get("status"),
            "reported": bool(usage.get("five_hour_reset_at")),
            "updated_at": usage.get("updated_at"),
            "error": usage.get("error"),
            "reset_text": usage.get("five_hour_reset"),
            "limit": usage.get("five_hour_limit"),
            "percent": usage.get("five_hour_percent"),
        }


class CodexWeeklyLimitSensor(_CodexSensor):
    """Show Codex weekly usage line from interactive status."""

    _attr_icon = "mdi:calendar-week"
    _attr_translation_key = "weekly_limit"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor with the unique ID key "weekly_limit"."""
        super().__init__(coordinator, entry, "weekly_limit")

    @property
    def native_value(self) -> int | None:
        """Return the percentage of the weekly limit that is left, or None when it is missing or not an integer."""
        usage = (self.coordinator.data or {}).get("codex_usage") or {}
        try:
            return int(str(usage.get("weekly_percent") or ""))
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the usage refresh state, the weekly, 5-hour and context figures, and the raw usage excerpt."""
        usage = (self.coordinator.data or {}).get("codex_usage") or {}
        return {
            "usage_status": usage.get("status"),
            "reported": bool(usage.get("weekly_percent")),
            "updated_at": usage.get("updated_at"),
            "error": usage.get("error"),
            "limit": usage.get("weekly_limit"),
            "reset": usage.get("weekly_reset_at") or usage.get("weekly_reset"),
            "reset_text": usage.get("weekly_reset"),
            "five_hour_limit": usage.get("five_hour_limit"),
            "five_hour_percent": usage.get("five_hour_percent"),
            "five_hour_reset": usage.get("five_hour_reset_at") or usage.get("five_hour_reset"),
            "five_hour_reset_text": usage.get("five_hour_reset"),
            "context_remaining": usage.get("context_remaining"),
            "context_percent": usage.get("context_percent"),
            "raw_excerpt": usage.get("raw_excerpt"),
        }


class CodexWeeklyResetSensor(_CodexSensor):
    """Show when the Codex weekly usage limit resets."""

    _attr_icon = "mdi:calendar-clock"
    _attr_translation_key = "weekly_reset"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: CodexCliCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor with the unique ID key "weekly_reset"."""
        super().__init__(coordinator, entry, "weekly_reset")

    @property
    def native_value(self) -> datetime | None:
        """Return when the weekly limit resets, from the worker's weekly_reset_at, or None when it is not known."""
        usage = (self.coordinator.data or {}).get("codex_usage") or {}
        return _parse_timestamp(usage.get("weekly_reset_at"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the usage refresh state, plus the weekly reset text, limit text and percentage Codex reported."""
        usage = (self.coordinator.data or {}).get("codex_usage") or {}
        return {
            "usage_status": usage.get("status"),
            "reported": bool(usage.get("weekly_reset_at")),
            "updated_at": usage.get("updated_at"),
            "error": usage.get("error"),
            "reset_text": usage.get("weekly_reset"),
            "limit": usage.get("weekly_limit"),
            "percent": usage.get("weekly_percent"),
        }
