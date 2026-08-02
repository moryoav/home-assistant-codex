from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SENSOR_PATH = (
    Path(__file__).resolve().parents[2] / "custom_components" / "codex_cli" / "sensor.py"
)


def _load_sensor_module() -> types.ModuleType:
    """Load the real sensor module without requiring Home Assistant."""

    class CoordinatorEntity:
        @classmethod
        def __class_getitem__(cls, _item: Any) -> type[CoordinatorEntity]:
            return cls

        def __init__(self, coordinator: Any, context: Any = None) -> None:
            self.coordinator = coordinator
            self.coordinator_context = context

    class SensorEntity:
        pass

    class SensorDeviceClass:
        TIMESTAMP = "timestamp"

    class SensorStateClass:
        MEASUREMENT = "measurement"

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"

    package_name = "codex_sensor_test_package"
    module_name = f"{package_name}.sensor"
    package = types.ModuleType(package_name)
    package.__path__ = [str(SENSOR_PATH.parent)]

    const_module = types.ModuleType(f"{package_name}.const")
    const_module.CONF_BASE_URL = "base_url"
    const_module.DOMAIN = "codex_cli"

    coordinator_module = types.ModuleType(f"{package_name}.coordinator")
    coordinator_module.CodexCliCoordinator = type("CodexCliCoordinator", (), {})

    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    sensor_component = types.ModuleType("homeassistant.components.sensor")
    sensor_component.SensorDeviceClass = SensorDeviceClass
    sensor_component.SensorEntity = SensorEntity
    sensor_component.SensorStateClass = SensorStateClass

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (), {})

    ha_const = types.ModuleType("homeassistant.const")
    ha_const.EntityCategory = EntityCategory
    ha_const.PERCENTAGE = "%"

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})

    helpers = types.ModuleType("homeassistant.helpers")
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object
    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.CoordinatorEntity = CoordinatorEntity

    stubs = {
        package_name: package,
        f"{package_name}.const": const_module,
        f"{package_name}.coordinator": coordinator_module,
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.sensor": sensor_component,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": ha_const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_platform": entity_platform,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }
    original_modules = {name: sys.modules.get(name) for name in stubs}

    spec = importlib.util.spec_from_file_location(module_name, SENSOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        sys.modules.update(stubs)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    return module


sensor = _load_sensor_module()


def _usage_sensors(usage: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    coordinator = SimpleNamespace(data={"codex_usage": usage})
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={"base_url": "http://codex-worker.test"},
    )
    return (
        sensor.CodexFiveHourLimitSensor(coordinator, entry),
        sensor.CodexFiveHourResetSensor(coordinator, entry),
        sensor.CodexWeeklyLimitSensor(coordinator, entry),
        sensor.CodexWeeklyResetSensor(coordinator, entry),
    )


class UsageSensorTests(unittest.TestCase):
    def test_weekly_only_usage_is_reported_without_five_hour_values(self) -> None:
        five_hour_limit, five_hour_reset, weekly_limit, weekly_reset = _usage_sensors(
            {
                "status": "ok",
                "weekly_percent": "87",
                "weekly_limit": "Weekly limit",
                "weekly_reset": "14:36 on 9 Aug",
                "weekly_reset_at": "2026-08-09T14:36:00+03:00",
                "updated_at": "2026-08-02T15:00:00+03:00",
            }
        )

        self.assertEqual(weekly_limit.native_value, 87)
        self.assertIs(weekly_limit.extra_state_attributes["reported"], True)
        self.assertEqual(weekly_limit.extra_state_attributes["limit"], "Weekly limit")
        self.assertEqual(
            weekly_limit.extra_state_attributes["reset"],
            "2026-08-09T14:36:00+03:00",
        )
        self.assertEqual(
            weekly_reset.native_value,
            datetime.fromisoformat("2026-08-09T14:36:00+03:00"),
        )
        self.assertIs(weekly_reset.extra_state_attributes["reported"], True)

        self.assertIsNone(five_hour_limit.native_value)
        self.assertIs(five_hour_limit.extra_state_attributes["reported"], False)
        self.assertIsNone(five_hour_reset.native_value)
        self.assertIs(five_hour_reset.extra_state_attributes["reported"], False)

    def test_missing_five_hour_metrics_stay_unknown_and_unreported(self) -> None:
        five_hour_limit, five_hour_reset, _, _ = _usage_sensors(
            {
                "status": "ok",
                "five_hour_percent": "",
                "five_hour_reset_at": "",
                "weekly_percent": "42",
            }
        )

        self.assertIsNone(five_hour_limit.native_value)
        self.assertIs(five_hour_limit.extra_state_attributes["reported"], False)
        self.assertIsNone(five_hour_limit.extra_state_attributes["reset"])
        self.assertIsNone(five_hour_reset.native_value)
        self.assertIs(five_hour_reset.extra_state_attributes["reported"], False)

    def test_five_hour_and_weekly_usage_remain_compatible(self) -> None:
        five_hour_limit, five_hour_reset, weekly_limit, weekly_reset = _usage_sensors(
            {
                "status": "ok",
                "five_hour_percent": "64",
                "five_hour_limit": "5h limit",
                "five_hour_reset": "19:20",
                "five_hour_reset_at": "2026-08-02T19:20:00+03:00",
                "weekly_percent": "91",
                "weekly_limit": "Weekly limit",
                "weekly_reset": "12:00 on 8 Aug",
                "weekly_reset_at": "2026-08-08T12:00:00+03:00",
            }
        )

        self.assertEqual(five_hour_limit.native_value, 64)
        self.assertIs(five_hour_limit.extra_state_attributes["reported"], True)
        self.assertEqual(five_hour_limit.extra_state_attributes["weekly_percent"], "91")
        self.assertEqual(
            five_hour_reset.native_value,
            datetime.fromisoformat("2026-08-02T19:20:00+03:00"),
        )
        self.assertIs(five_hour_reset.extra_state_attributes["reported"], True)

        self.assertEqual(weekly_limit.native_value, 91)
        self.assertIs(weekly_limit.extra_state_attributes["reported"], True)
        self.assertEqual(weekly_limit.extra_state_attributes["five_hour_percent"], "64")
        self.assertEqual(
            weekly_reset.native_value,
            datetime.fromisoformat("2026-08-08T12:00:00+03:00"),
        )
        self.assertIs(weekly_reset.extra_state_attributes["reported"], True)


if __name__ == "__main__":
    unittest.main()
