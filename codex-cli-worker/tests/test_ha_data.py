"""Recorder API contracts, bounded analysis, and the historical-data credential boundary."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_server import server
from ha_data import HomeAssistantData, MAX_EXPORT_BYTES, MAX_QUERIES, MAX_SOURCE_BYTES, MAX_TURN_BYTES
from ha_data_mcp import MAX_RESPONSE_BYTES, STATISTIC_TYPES, TOOLS, respond

START = "2026-01-01T00:00:00+00:00"
END = "2026-01-02T00:00:00+00:00"
START_MS = 1767225600000


def data_worker():
    """Supply one active turn and private credentials without an HA installation."""
    return SimpleNamespace(
        lock=threading.RLock(), tasks={"chat": {"status": "running", "current_turn_id": "turn"}},
        ha_token=Mock(return_value="supervisor-secret"), api_token=lambda: "worker-secret",
        read_options=lambda: {"HA_TOKEN": "configured-secret"}, redact=lambda value: value,
    )


class HistoricalDataTests(unittest.TestCase):
    """Exercise the worker's authenticated reads using realistic REST and WebSocket responses."""

    def setUp(self):
        """Begin an isolated turn with its own data capability."""
        self.worker = data_worker()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = HomeAssistantData(self.worker, Path(temporary.name) / "exports")
        self.addCleanup(self.engine.end, "chat")
        self.capability = self.engine.begin("chat")

    def saved_result(self, receipt):
        """Read the full export and verify the model-visible receipt identifies its exact bytes."""
        self.assertEqual(receipt["delivery"], "file")
        self.assertLessEqual(len(json.dumps(receipt).encode()) + 1, MAX_RESPONSE_BYTES)
        content = Path(receipt["file"]["path"]).read_bytes()
        self.assertEqual(len(content), receipt["file"]["size_bytes"])
        self.assertEqual(hashlib.sha256(content).hexdigest(), receipt["file"]["sha256"])
        return json.loads(content)

    def payload(self, operation="history", **fields):
        """Build a valid scoped request and allow a test to change one boundary."""
        key = "entity_ids" if operation == "history" else "statistic_ids"
        return {"operation": operation, "capability": self.capability, key: ["sensor.kitchen"],
                "start_time": START, "end_time": END, **fields}

    def rest_response(self, raw, status=200):
        """Provide a streaming requests response with automatic connection cleanup."""
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps(raw).encode()]
        return response

    def ws_response(self, metadata, statistics):
        """Use websocket-client's actual close/send/recv interface and recorder result envelopes."""
        ws = Mock(spec=["recv", "send", "close", "settimeout"])
        ws.recv.side_effect = [json.dumps(item) for item in [
            {"type": "auth_required"}, {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": metadata},
            {"id": 2, "type": "result", "success": True, "result": statistics},
        ]]
        return ws

    def test_untrusted_arguments_fail_before_network_access(self):
        """Reject unscoped queries, arbitrary operations, malformed windows and oversized work."""
        invalid = [
            {"entity_ids": []}, {"entity_ids": None}, {"entity_ids": ["sensor.kitchen"] * 2},
            {"entity_ids": [f"sensor.item_{index}" for index in range(17)]},
            {"entity_ids": ["sensor.kitchen,light.bedroom"]}, {"entity_ids": ["../states"]},
            {"entity_ids": [True]}, {"entity_ids": ["sensor." + "x" * 256]},
            {"start_time": "2026-01-01"}, {"start_time": "2026-01-01T00:00:00"},
            {"start_time": "2026-02-30T00:00:00Z"}, {"start_time": END},
            {"end_time": "2026-01-08T00:00:01Z"}, {"max_points": True}, {"max_points": 0},
            {"max_points": 5001}, {"entity_ids": ["sensor.a", "sensor.b"], "max_points": 1},
            {"url": "https://example.test/"}, {"headers": {"Authorization": "x"}},
            {"operation": "call_service"}, {"operation": "sql"},
            {"end_time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        ]
        with patch("ha_data.requests.get") as rest, patch("ha_data.websocket.create_connection") as ws:
            for fields in invalid:
                with self.subTest(fields=fields):
                    self.assertEqual(self.engine.dispatch(self.payload(**fields))["status"], "unavailable")
        rest.assert_not_called()
        ws.assert_not_called()
        self.assertEqual(self.engine.queries["chat"], 0)

    def test_statistics_scope_and_estimated_bucket_limit_are_enforced(self):
        """Require explicit unique IDs and supported aggregation fields, bounding Core's work too."""
        invalid = [
            {"statistic_ids": []}, {"statistic_ids": ["sensor.kitchen", "sensor.kitchen"]},
            {"statistic_ids": ["line\nbreak"]}, {"statistic_ids": ["x" * 256]},
            {"period": "year"}, {"period": {}}, {"types": []}, {"types": ["mean", "mean"]},
            {"types": ["median"]}, {"types": "mean"}, {"units": {"energy": "Wh"}},
            {"end_time": "2027-01-03T00:00:00Z"},
            {"end_time": "2026-03-01T00:00:00Z", "period": "5minute"},
        ]
        with patch.object(self.engine, "ws_read") as read:
            for fields in invalid:
                with self.subTest(fields=fields):
                    self.assertEqual(self.engine.dispatch(self.payload("statistics", **fields))["status"], "unavailable")
        read.assert_not_called()
        operation, query = self.engine.validate(self.payload(
            "statistics", statistic_ids=["external:meter"], end_time="2026-10-01T00:00:00Z", period="day"))
        self.assertEqual(operation, "statistics")
        self.assertEqual(query["statistic_ids"], ["external:meter"])

    def test_ended_cancelled_completed_and_replaced_turns_cannot_query(self):
        """Refuse stale credentials before HA reads, even when an old task id is reused."""
        for outcome in ("ended", "cancelled", "completed", "new_turn", "new_capability", "restarted"):
            with self.subTest(outcome=outcome):
                self.worker.tasks["chat"] = {"status": "running", "current_turn_id": "turn"}
                self.capability = self.engine.begin("chat")
                if outcome == "ended":
                    self.engine.end("chat")
                elif outcome == "cancelled":
                    self.worker.tasks["chat"]["cancellation_requested"] = True
                elif outcome == "completed":
                    self.worker.tasks["chat"]["status"] = "completed"
                elif outcome == "new_turn":
                    self.worker.tasks["chat"]["current_turn_id"] = "replacement"
                elif outcome == "new_capability":
                    self.engine.begin("chat")
                else:
                    self.engine = HomeAssistantData(self.worker, self.engine.export_root)
                with patch.object(self.engine, "history") as read, self.assertRaises(ValueError):
                    self.engine.dispatch(self.payload())
                read.assert_not_called()

    def test_missing_or_malformed_capabilities_cannot_query(self):
        """Reject Unicode, non-string and unrelated credentials without reaching HA."""
        with patch.object(self.engine, "history") as read:
            for value in (None, "", "wrong", "שלום", [], 1):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.engine.dispatch(self.payload(capability=value))
        read.assert_not_called()

    def test_late_results_are_discarded_after_turn_replacement_or_cancellation(self):
        """Prevent a completed read from returning data to a task that ceased to own the capability."""
        for outcome in ("cancelled", "replaced", "ended", "completed"):
            with self.subTest(outcome=outcome):
                self.worker.tasks["chat"] = {"status": "running", "current_turn_id": "turn"}
                self.capability = self.engine.begin("chat")
                def late_result(_query):
                    """End the active exchange while its backend read is in progress."""
                    if outcome == "cancelled":
                        self.worker.tasks["chat"]["cancellation_requested"] = True
                    elif outcome == "replaced":
                        self.engine.begin("chat")
                    elif outcome == "ended":
                        self.engine.end("chat")
                    else:
                        self.worker.tasks["chat"]["status"] = "completed"
                    return {"status": "observed", "private": "old-turn-data"}
                with patch.object(self.engine, "history", side_effect=late_result):
                    result = self.engine.dispatch(self.payload())
                self.assertEqual(result, {"status": "unavailable", "message": "Data capability expired"})

    def test_query_budget_is_shared_by_data_tools_and_resets_for_a_new_turn(self):
        """Count history and statistics together without borrowing verification's budget."""
        with patch("ha_data.MAX_QUERIES", 2), \
             patch.object(self.engine, "history", return_value={"status": "observed"}) as history, \
             patch.object(self.engine, "statistics", return_value={"status": "observed"}) as statistics:
            self.assertEqual(self.engine.dispatch(self.payload())["status"], "observed")
            self.assertEqual(self.engine.dispatch(self.payload("statistics"))["status"], "observed")
            self.assertIn("query limit", self.engine.dispatch(self.payload())["message"])
            history.assert_called_once()
            statistics.assert_called_once()
            self.capability = self.engine.begin("chat")
            self.assertEqual(self.engine.dispatch(self.payload())["status"], "observed")
        self.assertEqual(MAX_QUERIES, 64)

    def test_history_uses_only_supervisor_get_with_all_state_changes(self):
        """Normalize minimal REST history, retaining initial/unknown states and excluding attributes and other IDs."""
        raw = [[
            {"entity_id": "sensor.kitchen", "last_changed": "2026-01-01T03:00:00+03:00", "state": "19.5",
             "attributes": {"token": "private"}, "context": {"id": "private-context"}},
            {"last_changed": "2026-01-01T01:00:00+00:00", "state": "unknown"},
            {"last_changed": "2026-01-01T02:00:00+00:00", "state": "unavailable"},
        ], [{"entity_id": "sensor.unrequested", "last_changed": START, "state": "private"}]]
        response = self.rest_response(raw)
        with patch("ha_data.requests.get", return_value=response) as get:
            result = self.saved_result(self.engine.dispatch(self.payload(entity_ids=["sensor.kitchen", "sensor.empty"])))
        call = get.call_args
        self.assertEqual(call.args, ("http://supervisor/core/api/history/period/2026-01-01T00%3A00%3A00%2B00%3A00",))
        self.assertEqual(call.kwargs["params"], {"filter_entity_id": "sensor.kitchen,sensor.empty", "end_time": END,
                                               "minimal_response": "1", "no_attributes": "1", "significant_changes_only": "0"})
        self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer supervisor-secret"})
        self.assertIs(call.kwargs["allow_redirects"], False)
        self.assertIs(call.kwargs["stream"], True)
        self.assertEqual([point["state"] for point in result["series"]["sensor.kitchen"]["points"]],
                         ["19.5", "unknown", "unavailable"])
        self.assertEqual(result["series"]["sensor.kitchen"]["points"][0]["time"], START)
        self.assertEqual(result["series"]["sensor.empty"]["status"], "no_data")
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(set(result["series"]), {"sensor.kitchen", "sensor.empty"})
        response.__exit__.assert_called_once()

    def test_truncation_is_explicit_for_every_requested_series(self):
        """Preserve source order and allocate points to both sensors instead of silently losing one."""
        raw = [[{"entity_id": entity, "last_changed": START, "state": str(index)} for index in range(3)]
               for entity in ("sensor.a", "sensor.b")]
        with patch("ha_data.requests.get", return_value=self.rest_response(raw)):
            result = self.saved_result(self.engine.dispatch(self.payload(entity_ids=["sensor.a", "sensor.b"], max_points=4)))
        self.assertTrue(result["truncated"])
        for item in result["series"].values():
            self.assertEqual(item["available_points"], 3)
            self.assertEqual(item["returned_points"], 2)
            self.assertTrue(item["truncated"])
            self.assertEqual([point["state"] for point in item["points"]], ["0", "1"])
        self.assertIn("incomplete", result["message"])

    def test_full_history_for_attribute_domains_keeps_only_state_changes(self):
        """Treat repeated climate states as attribute updates while preserving actual state transitions."""
        raw = [[{"entity_id": "climate.kitchen", "last_changed": changed, "state": state,
                 "last_updated": START, "attributes": {"temperature": index}}
                for index, (changed, state) in enumerate([
                    (START, "heat"), ("2025-12-31T23:00:00Z", "heat"),
                    ("2026-01-01T01:00:00Z", "off"), ("2026-01-01T02:00:00Z", "heat"),
                ])]]
        with patch("ha_data.requests.get", return_value=self.rest_response(raw)):
            result = self.saved_result(self.engine.dispatch(self.payload(entity_ids=["climate.kitchen"])))
        item = result["series"]["climate.kitchen"]
        self.assertEqual([point["state"] for point in item["points"]], ["heat", "off", "heat"])
        self.assertEqual(item["available_points"], 3)
        self.assertNotIn("attributes", json.dumps(result))

    def test_http_errors_redirects_and_bad_or_oversized_history_fail_closed(self):
        """Never forward redirects or arbitrary error bodies, and cap source bytes before parsing."""
        responses = [self.rest_response([], status=code) for code in (302, 401, 404, 500)]
        responses += [self.rest_response({"token": "private"}), self.rest_response([[{"state": 1}]])]
        oversized = self.rest_response([])
        oversized.iter_content.return_value = [b"x" * (MAX_SOURCE_BYTES + 1)]
        responses.append(oversized)
        for response in responses:
            with self.subTest(response=response), patch("ha_data.requests.get", return_value=response):
                result = self.engine.dispatch(self.payload())
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("private", json.dumps(result))
            response.__exit__.assert_called_once()

    def test_statistics_use_filtered_metadata_and_millisecond_bucket_times(self):
        """Read display units and cumulative/consumption fields while preserving HA's actual bucket boundaries."""
        metadata = [{"statistic_id": "sensor.kitchen", "display_unit_of_measurement": "kWh",
                     "statistics_unit_of_measurement": "Wh", "has_mean": False, "mean_type": 0, "has_sum": True},
                    {"statistic_id": "sensor.empty", "display_unit_of_measurement": "°C", "has_mean": True,
                     "mean_type": 1, "has_sum": False}]
        raw = {"sensor.kitchen": [{"start": START_MS - 3600000, "end": START_MS,
                                    "sum": 134.6, "change": 1.2, "state": None, "last_reset": START_MS,
                                    "unrequested": "private"}], "sensor.other": [{"private": "hidden"}]}
        ws = self.ws_response(metadata, raw)
        with patch("ha_data.websocket.create_connection", return_value=ws) as connect:
            result = self.saved_result(self.engine.dispatch(self.payload(
                "statistics", statistic_ids=["sensor.kitchen", "sensor.empty", "external:missing"],
                types=["mean", "sum", "change", "state", "last_reset"])))
        connect.assert_called_once_with("ws://supervisor/core/websocket", timeout=15)
        sent = [json.loads(call.args[0]) for call in ws.send.call_args_list]
        self.assertEqual(sent[0], {"type": "auth", "access_token": "supervisor-secret"})
        self.assertEqual(sent[1], {"id": 1, "type": "recorder/get_statistics_metadata",
                                  "statistic_ids": ["sensor.kitchen", "sensor.empty", "external:missing"]})
        self.assertEqual(sent[2], {"id": 2, "type": "recorder/statistics_during_period", "period": "hour",
                                  "statistic_ids": sent[1]["statistic_ids"], "start_time": START, "end_time": END,
                                  "types": ["mean", "sum", "change", "state", "last_reset"]})
        item = result["series"]["sensor.kitchen"]
        self.assertEqual(item["unit_of_measurement"], "kWh")
        self.assertEqual(item["points"], [{"start": "2025-12-31T23:00:00+00:00", "end": START,
                                           "sum": 134.6, "change": 1.2, "state": None, "last_reset": START}])
        self.assertEqual(result["series"]["sensor.empty"]["status"], "no_data")
        self.assertEqual(result["series"]["external:missing"]["status"], "no_statistics")
        self.assertNotIn("private", json.dumps(result))
        ws.close.assert_called_once()

    def test_statistics_authentication_and_recorder_errors_do_not_leak_details(self):
        """Hide backend diagnostics and close connections on failed authentication or recorder responses."""
        for replies in (
            [{"type": "auth_invalid", "message": "private"}],
            [{"type": "auth_required"}, {"type": "auth_invalid", "message": "private"}],
            [{"type": "auth_required"}, {"type": "auth_ok"},
             {"id": 1, "type": "result", "success": False, "error": {"message": "private"}}],
            [{"type": "auth_required"}, {"type": "auth_ok"}] + [{"id": 99, "type": "result"}] * 8,
        ):
            with self.subTest(replies=replies):
                ws = self.ws_response([], {})
                ws.recv.side_effect = [json.dumps(reply) for reply in replies]
                with patch("ha_data.websocket.create_connection", return_value=ws):
                    result = self.engine.dispatch(self.payload("statistics"))
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("private", json.dumps(result))
                ws.close.assert_called_once()

    def test_statistics_source_byte_limit_includes_unicode(self):
        """Count UTF-8 bytes rather than characters when bounding a metadata frame."""
        ws = self.ws_response([], {})
        ws.recv.side_effect = ['{"type":"auth_required","padding":"' + "א" * (MAX_SOURCE_BYTES // 2) + '"}']
        with patch("ha_data.websocket.create_connection", return_value=ws):
            result = self.engine.dispatch(self.payload("statistics"))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("query limit", result["message"])
        ws.send.assert_not_called()
        ws.close.assert_called_once()

    def test_invalid_statistics_numbers_and_timestamps_are_unavailable(self):
        """Do not fabricate numeric data from strings, booleans, NaN or invalid timestamps."""
        for fields in ({"mean": "22"}, {"mean": True}, {"mean": float("nan")},
                       {"start": None}, {"end": float("inf")}, {"last_reset": "2026-01-01"}):
            with self.subTest(fields=fields), patch.object(self.engine, "ws_read", return_value=[
                [{"statistic_id": "sensor.kitchen"}],
                {"sensor.kitchen": [{"start": START_MS, "end": START_MS + 3600000, **fields}]},
            ]):
                result = self.engine.dispatch(self.payload("statistics", types=["mean", "last_reset"]))
            self.assertEqual(result["status"], "unavailable")

    def test_absent_credentials_do_not_open_any_network_connection(self):
        """Fail locally when automatic HA authentication is unavailable."""
        self.worker.ha_token.return_value = ""
        with patch("ha_data.requests.get") as rest, patch("ha_data.websocket.create_connection") as ws:
            for operation in ("history", "statistics"):
                self.assertIn("authentication", self.engine.dispatch(self.payload(operation))["message"])
        rest.assert_not_called()
        ws.assert_not_called()

    def test_credentials_in_state_values_are_redacted_including_json_escapes(self):
        """Protect bare configured credentials and temporary capabilities even inside ordinary sensor values."""
        for value in ("supervisor-secret", "worker-secret", "configured-secret", self.capability, 'secret"\nvalue'):
            with self.subTest(value=value):
                self.worker.ha_token.return_value = value
                with patch.object(self.engine, "history", return_value={"status": "observed", "state": "prefix " + value}):
                    result = self.saved_result(self.engine.dispatch(self.payload()))
                self.assertEqual(result, {"status": "observed", "state": "prefix [redacted]"})

    def test_transport_exceptions_and_oversized_outputs_are_safe_tool_errors(self):
        """Do not expose an exception's private URL or return unbounded state values."""
        for error in (OSError("private-url supervisor-secret"), ValueError("private-url configured-secret")):
            with self.subTest(error=error), patch.object(self.engine, "history", side_effect=error):
                result = self.engine.dispatch(self.payload())
            self.assertEqual(result, {"status": "unavailable", "message": "Home Assistant data request failed"})
        with patch.object(self.engine, "history", return_value={"status": "observed", "state": "x" * MAX_EXPORT_BYTES}):
            result = self.engine.dispatch(self.payload())
        self.assertIn("too large", result["message"])
        # The execution lock must also be released after a backend failure.
        with patch.object(self.engine, "history", return_value={"status": "observed"}):
            self.assertEqual(self.engine.dispatch(self.payload())["status"], "observed")

    def test_large_history_is_saved_completely_without_points_in_the_receipt(self):
        """Keep the middle of a large series accessible without sending it through CLI output truncation."""
        raw = [[{"entity_id": "sensor.kitchen", "last_changed": (datetime.fromisoformat(START) + timedelta(seconds=index)).isoformat(),
                 "state": str(index)} for index in range(1000)]]
        with patch("ha_data.requests.get", return_value=self.rest_response(raw)):
            receipt = self.engine.dispatch(self.payload())
        result = self.saved_result(receipt)
        self.assertLess(len(json.dumps(receipt).encode()), 2000)
        self.assertNotIn("points", receipt)
        self.assertNotIn("sensor.kitchen", json.dumps(receipt))
        self.assertEqual(receipt["returned_points"], 1000)
        self.assertFalse(receipt["truncated"])
        self.assertEqual([point["state"] for point in result["series"]["sensor.kitchen"]["points"]],
                         [str(index) for index in range(1000)])

    def test_maximum_statistics_points_fit_in_a_file_with_all_supported_fields(self):
        """Deliver 5,000 full statistics buckets, exceeding the old 256 KiB output cap."""
        rows = [{"start": START_MS + index * 3600000, "end": START_MS + (index + 1) * 3600000,
                 "mean": index / 4, "min": index / 4 - 1, "max": index / 4 + 1,
                 "sum": index * 2, "state": index * 2, "change": 2, "last_reset": START_MS}
                for index in range(5000)]
        ws = self.ws_response([{"statistic_id": "sensor.kitchen", "display_unit_of_measurement": "kWh"}],
                              {"sensor.kitchen": rows})
        with patch("ha_data.websocket.create_connection", return_value=ws):
            receipt = self.engine.dispatch(self.payload("statistics", max_points=5000, types=STATISTIC_TYPES,
                                                        end_time="2026-09-01T00:00:00Z"))
        result = self.saved_result(receipt)
        points = result["series"]["sensor.kitchen"]["points"]
        self.assertGreater(receipt["file"]["size_bytes"], 256 * 1024)
        self.assertEqual(len(points), 5000)
        self.assertEqual(points[2500]["mean"], 625)
        self.assertEqual(sum(point["change"] for point in points), 10000)
        self.assertFalse(receipt["truncated"])
        self.assertEqual(receipt["returned_points"], 5000)

    def test_point_budget_redistributes_unused_shares_and_integer_remainders(self):
        """Use the whole budget without starving quiet or empty sensors when one records frequently."""
        counts = [9117, 150, 9, 0, 0, 0, 0, 0]
        series = {f"sensor.item_{index}": {"points": [{"state": str(point)} for point in range(count)]}
                  for index, count in enumerate(counts)}
        result = self.engine.result("history", {"max_points": 1000, "start_time": START, "end_time": END}, series)
        self.assertEqual([item["returned_points"] for item in result["series"].values()], [841, 150, 9, 0, 0, 0, 0, 0])
        self.assertTrue(result["series"]["sensor.item_0"]["truncated"])
        self.assertFalse(result["series"]["sensor.item_1"]["truncated"])
        series = {f"sensor.item_{index}": {"points": [0] * 10} for index in range(3)}
        result = self.engine.result("history", {"max_points": 5, "start_time": START, "end_time": END}, series)
        self.assertEqual([item["returned_points"] for item in result["series"].values()], [2, 2, 1])

    def test_receipt_size_does_not_grow_with_long_unicode_ids_or_sensor_values(self):
        """Keep long untrusted IDs and states in the file rather than exhausting the output budget."""
        ids = [str(index) + "漢" * 250 for index in range(16)]
        with patch.object(self.engine, "ws_read", return_value=[[], {}]):
            receipt = self.engine.dispatch(self.payload("statistics", statistic_ids=ids))
        result = self.saved_result(receipt)
        self.assertEqual(set(result["series"]), set(ids))
        self.assertEqual(receipt["requested_series"], 16)
        self.assertEqual(receipt["series_statuses"]["no_statistics"], 16)
        self.assertLess(len(json.dumps(receipt).encode()), 2000)

    def test_exports_are_private_and_removed_on_end_replacement_and_restart(self):
        """Keep data outside task files, isolate turns, and remove exports left by a stopped worker."""
        with patch.object(self.engine, "history", return_value={"status": "observed"}):
            for outcome in ("ended", "replaced", "restarted"):
                with self.subTest(outcome=outcome):
                    self.capability = self.engine.begin("chat")
                    receipt = self.engine.dispatch(self.payload())
                    path = Path(receipt["file"]["path"])
                    self.assertEqual(path.parent.parent, self.engine.export_root)
                    self.assertNotIn(self.capability, str(path))
                    if os.name == "posix":
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                        self.assertEqual(self.engine.export_root.stat().st_mode & 0o777, 0o700)
                    if outcome == "ended":
                        self.engine.end("chat")
                    elif outcome == "replaced":
                        self.engine.begin("chat")
                    else:
                        restarted = HomeAssistantData(self.worker, self.engine.export_root)
                        restarted.cleanup()
                    self.assertFalse(path.exists())
                    self.assertFalse(path.parent.exists())

    def test_storage_quota_refuses_additional_files_without_dropping_prior_results(self):
        """Bound per-turn disk use and restore the allowance only when a new exchange starts."""
        with patch.object(self.engine, "history", return_value={"status": "observed"}):
            first = self.engine.dispatch(self.payload())
            with patch("ha_data.MAX_TURN_BYTES", first["file"]["size_bytes"]):
                self.assertIn("storage limit", self.engine.dispatch(self.payload())["message"])
                self.assertTrue(Path(first["file"]["path"]).exists())
                self.assertEqual(len(list(self.engine.export_root.rglob("*.json"))), 1)
                self.capability = self.engine.begin("chat")
                self.assertEqual(self.engine.dispatch(self.payload())["status"], "observed")
        self.assertLess(MAX_EXPORT_BYTES, MAX_TURN_BYTES)

    def test_write_failure_removes_partial_exports_and_keeps_the_budget_available(self):
        """Do not return a dangling receipt or charge storage when publishing a file fails."""
        with patch.object(self.engine, "history", return_value={"status": "observed"}), \
             patch("ha_data.Path.replace", side_effect=OSError("private storage path")):
            self.assertEqual(self.engine.dispatch(self.payload()),
                             {"status": "unavailable", "message": "Home Assistant data request failed"})
        self.assertEqual(list(self.engine.export_root.rglob("*.tmp")), [])
        self.assertEqual(list(self.engine.export_root.rglob("*.json")), [])
        self.assertEqual(self.engine.export_bytes["chat"], 0)

    @unittest.skipUnless(os.name == "posix", "Unix symlink boundary")
    def test_storage_and_cleanup_never_follow_symlinks(self):
        """Refuse a replaced storage root and unlink orphan symlinks without touching their targets."""
        outside = self.engine.export_root.parent / "outside"
        outside.mkdir()
        marker = outside / "keep.json"
        marker.write_text("private", encoding="utf-8")
        self.engine.export_root.symlink_to(outside, target_is_directory=True)
        with patch.object(self.engine, "history", return_value={"status": "observed"}):
            self.assertEqual(self.engine.dispatch(self.payload())["message"], "Historical data storage is unavailable")
        self.engine.export_root.unlink()
        self.engine.export_root.mkdir()
        (self.engine.export_root / "turn-orphan").symlink_to(outside, target_is_directory=True)
        self.engine.cleanup()
        self.assertEqual(marker.read_text(encoding="utf-8"), "private")
        self.assertFalse((self.engine.export_root / "turn-orphan").exists())

    @unittest.skipUnless(os.name == "posix", "Unix symlink boundary")
    def test_startup_cleanup_skips_replaced_storage_without_unblocking_queries(self):
        """Keep worker startup available without following a symlink or exporting data through it."""
        outside = self.engine.export_root.parent / "outside"
        orphan = outside / "turn-keep"
        orphan.mkdir(parents=True)
        marker = orphan / "result.json"
        marker.write_text("private", encoding="utf-8")
        self.engine.export_root.symlink_to(outside, target_is_directory=True)
        with patch("ha_data.sys.stderr") as diagnostics:
            self.engine.cleanup()
        diagnostics.write.assert_any_call("Historical data storage was replaced; skipping cleanup")
        self.assertEqual(marker.read_text(encoding="utf-8"), "private")
        with patch.object(self.engine, "history", return_value={"status": "observed"}):
            result = self.engine.dispatch(self.payload())
        self.assertEqual(result, {"status": "unavailable", "message": "Historical data storage is unavailable"})
        self.assertEqual(list(outside.glob("result-*")), [])

    @unittest.skipUnless(os.name == "posix", "Unix symlink boundary")
    def test_end_never_deletes_through_a_replaced_storage_root(self):
        """Revoke the credential even when a replacement root makes file cleanup unsafe."""
        with patch.object(self.engine, "history", return_value={"status": "observed"}):
            receipt = self.engine.dispatch(self.payload())
        directory = Path(receipt["file"]["path"]).parent
        moved = self.engine.export_root.with_name("moved")
        self.engine.export_root.rename(moved)
        self.engine.export_root.symlink_to(moved, target_is_directory=True)
        with patch("ha_data.sys.stderr"):
            self.engine.end("chat")
        self.assertTrue((moved / directory.name / Path(receipt["file"]["path"]).name).exists())
        self.assertEqual(self.engine.capabilities, {})
        self.engine.export_root.unlink()

    def test_cleanup_preserves_files_belonging_to_an_active_exchange(self):
        """Allow orphan cleanup without removing the files a current query has delivered."""
        with patch.object(self.engine, "history", return_value={"status": "observed"}):
            receipt = self.engine.dispatch(self.payload())
        orphan = self.engine.export_root / "turn-orphan"
        orphan.mkdir()
        (orphan / "result.json").write_text("{}", encoding="utf-8")
        self.engine.cleanup()
        self.assertTrue(Path(receipt["file"]["path"]).exists())
        self.assertFalse(orphan.exists())

    def test_cancellation_while_publishing_removes_files_and_refuses_the_receipt(self):
        """Discard even a successfully written result if the exchange ends before dispatch returns it."""
        replace = Path.replace

        def cancelled_replace(path, target):
            """Publish the file, then simulate cancellation before the final capability check."""
            result = replace(path, target)
            self.worker.tasks["chat"]["cancellation_requested"] = True
            return result

        with patch.object(self.engine, "history", return_value={"status": "observed"}), \
             patch("ha_data.Path.replace", cancelled_replace):
            result = self.engine.dispatch(self.payload())
        self.assertEqual(result, {"status": "unavailable", "message": "Data capability expired"})
        self.assertEqual(list(self.engine.export_root.glob("turn-*")), [])
        self.assertEqual(self.engine.capabilities, {})


class DataMcpTests(unittest.TestCase):
    """The trusted bridge exposes two reads and requires no model-visible credentials."""

    def test_stdio_discovery_and_protocol_errors(self):
        """Discover read-only tools through real JSON-RPC and reject malformed input without diagnostics on stdout."""
        requests = [{"jsonrpc": "2.0", "id": index, "method": method} for index, method in enumerate(
            ("initialize", "tools/list", "ping", "unknown"))]
        result = subprocess.run([sys.executable, str(Path(server.__file__).with_name("ha_data_mcp.py"))],
                                input="\n".join(json.dumps(request) for request in requests) + "\n{invalid}\n",
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual({tool["name"] for tool in replies[1]["result"]["tools"]}, {"history", "statistics"})
        self.assertTrue(all(tool["annotations"]["readOnlyHint"] for tool in TOOLS))
        self.assertEqual(replies[3]["error"]["code"], -32601)
        self.assertEqual(replies[4]["error"]["code"], -32700)

    def test_stdio_rejects_oversized_lines(self):
        """Stop an oversized protocol request instead of accepting another chunk as a new request."""
        result = subprocess.run([sys.executable, str(Path(server.__file__).with_name("ha_data_mcp.py"))],
                                input=b"x" * 65537, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")

    def test_tools_refuse_arbitrary_operations_and_capability_overrides(self):
        """Reject tools and privileged arguments supplied by the model before connecting to the worker."""
        with patch("ha_data_mcp.socket.socket") as connect:
            for name, arguments in (("call_service", {}), ("history", {"capability": "chosen"}),
                                    ("statistics", {"operation": "sql"}), ("history", {"url": "http://example.test"})):
                with self.subTest(name=name, arguments=arguments):
                    result = respond({"method": "tools/call", "params": {"name": name, "arguments": arguments}})
                    self.assertTrue(result["isError"])
        connect.assert_not_called()

    def test_missing_bridge_is_an_mcp_tool_error_without_transport_details(self):
        """Return an actionable tool failure rather than leaking socket paths or network diagnostics."""
        with patch("ha_data_mcp.fetch", side_effect=OSError("private-socket-path")):
            result = respond({"method": "tools/call", "params": {"name": "history", "arguments": {}}})
        self.assertTrue(result["isError"])
        self.assertNotIn("private", json.dumps(result))

    def test_worker_strips_inherited_credentials_and_redacts_tool_capabilities(self):
        """Keep Supervisor credentials out of subprocess environments and data capabilities out of worker logs."""
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "supervisor", "HASSIO_TOKEN": "supervisor-legacy",
                                   "HA_DATA_CAPABILITY": "inherited", "HA_DATA_SOCKET": "/untrusted.sock"}), \
             patch.object(server, "read_options", return_value={"HA_TOKEN": ""}):
            environment = server.codex_env()
        for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HA_DATA_CAPABILITY", "HA_DATA_SOCKET", "HA_TOKEN"):
            self.assertNotIn(key, environment)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        engine = HomeAssistantData(data_worker(), Path(temporary.name) / "exports")
        self.addCleanup(engine.end, "chat")
        capability = engine.begin("chat")
        with patch.object(server, "ha_data", engine):
            self.assertEqual(server.redact("temporary " + capability), "temporary [redacted]")

    def test_worker_revokes_data_access_on_success_and_startup_failure(self):
        """Exercise the background runner's cleanup and refuse its old credential after either outcome."""
        for failure in (None, RuntimeError("Fixture startup failure")):
            with self.subTest(failure=failure), ExitStack() as stack:
                temporary = stack.enter_context(tempfile.TemporaryDirectory())
                engine = HomeAssistantData(data_worker(), Path(temporary) / "exports")
                capability = engine.begin("chat")
                with patch.object(engine, "history", return_value={"status": "observed"}):
                    receipt = engine.dispatch({"operation": "history", "capability": capability, "entity_ids": ["sensor.kitchen"],
                                               "start_time": START, "end_time": END})
                export = Path(receipt["file"]["path"])
                self.assertTrue(export.exists())
                stack.enter_context(patch.object(server, "ha_data", engine))
                stack.enter_context(patch.object(server, "run_task", side_effect=failure))
                failed = stack.enter_context(patch.object(server, "fail_task_launch"))
                stack.enter_context(patch.object(server.verification, "end"))
                for name in ("running_processes", "approval_sessions"):
                    stack.enter_context(patch.object(server, name, {}))
                for name in ("finish_activity", "remove_run_artifacts", "start_next_queued", "cleanup_backups"):
                    stack.enter_context(patch.object(server, name))
                stack.enter_context(patch.object(server, "get_run_dir", return_value=Path("unused")))
                server._run_background_task("chat", "Read history", None, None)
                self.assertEqual(failed.call_count, int(failure is not None))
                with self.assertRaises(ValueError):
                    engine.dispatch({"operation": "history", "capability": capability, "entity_ids": ["sensor.kitchen"],
                                     "start_time": START, "end_time": END})
                self.assertEqual(engine.capabilities, {})
                self.assertFalse(export.exists())

    def test_both_cli_paths_register_automatically_approved_data_tools(self):
        """Use the same required bridge in exec and app-server so changing sandbox mode cannot hide it."""
        options = {**server.DEFAULT_OPTIONS, "codex_sandbox": "workspace-write"}
        with patch.object(server, "read_options", return_value=options), patch.object(server, "tasks", {}):
            exec_args = server.build_codex_args("chat", Path("prompt"), Path("final"), None)
            app_args = server.build_app_server_args()
        for args in (exec_args, app_args):
            self.assertIn('mcp_servers.home_assistant_data.tools.history.approval_mode="approve"', args)
            self.assertIn('mcp_servers.home_assistant_data.tools.statistics.approval_mode="approve"', args)
            self.assertIn('mcp_servers.home_assistant_data.required=true', args)
            self.assertNotIn("SUPERVISOR_TOKEN", " ".join(args))


if __name__ == "__main__":
    unittest.main()
