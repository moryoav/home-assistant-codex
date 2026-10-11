"""Check recorder requests against the actual supported Home Assistant command schemas."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import websocket_api

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ha_data import HomeAssistantData
from ha_data_mcp import STATISTIC_TYPES

START = "2026-01-01T00:00:00+00:00"
END = "2026-01-02T00:00:00+00:00"


@pytest.mark.parametrize("period", ["5minute", "hour", "day", "week", "month"])
def test_generated_recorder_requests_match_core_schemas(period, tmp_path):
    """Validate the exact generated requests against both Core versions in the CI matrix."""
    worker = SimpleNamespace(lock=threading.RLock(), tasks={"chat": {"status": "running", "current_turn_id": "turn"}},
                             ha_token=lambda: "private-token", api_token=lambda: "", read_options=lambda: {},
                             redact=lambda text: text)
    engine = HomeAssistantData(worker, tmp_path / "exports")
    capability = engine.begin("chat")
    captured = []

    def recorder(messages):
        """Capture the engine's wire requests while returning an empty recorder response."""
        captured.extend(messages)
        return [], {}

    with patch.object(engine, "ws_read", side_effect=recorder):
        result = engine.dispatch({
            "operation": "statistics", "capability": capability, "statistic_ids": ["sensor.kitchen", "external:meter"],
            "start_time": START, "end_time": END, "period": period, "types": STATISTIC_TYPES,
        })
    assert result["status"] == "observed"
    metadata = websocket_api.ws_get_statistics_metadata._ws_schema({"id": 1, **captured[0]})
    statistics = websocket_api.ws_get_statistics_during_period._ws_schema({"id": 2, **captured[1]})
    assert metadata["statistic_ids"] == statistics["statistic_ids"] == ["sensor.kitchen", "external:meter"]
    assert statistics["types"] == set(STATISTIC_TYPES)
    assert statistics["period"] == period
