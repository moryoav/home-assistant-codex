"""Task-scoped, read-only recorder access using credentials kept in the worker."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import socketserver
import threading
import time
from urllib.parse import quote

import requests
import websocket

from ha_data_mcp import DEFAULT_POINTS, MAX_IDS, MAX_POINTS, MAX_RESPONSE_BYTES, PERIODS, STATISTIC_TYPES, TOOLS

MAX_QUERIES = 64
MAX_SOURCE_BYTES = 1024 * 1024
ENTITY_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


class DataError(ValueError):
    """Carry a controlled, credential-free explanation suitable for a tool response."""


def timestamp(value):
    """Parse an explicit ISO timestamp and normalize its timezone to UTC."""
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise DataError("Use ISO timestamps with seconds and a timezone, such as 2026-10-01T00:00:00+03:00")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise DataError("Invalid timestamp") from exc


def iso_milliseconds(value):
    """Convert a recorder WebSocket millisecond timestamp to explicit UTC."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DataError("Home Assistant returned an invalid statistics timestamp")
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


class HomeAssistantData:
    """Read only history/statistics for an active turn, without exposing Supervisor credentials."""

    def __init__(self, worker):
        """Bind worker services and initialize private capabilities and query budgets."""
        self.worker = worker
        self.lock = threading.RLock()
        self.execution_lock = threading.Lock()
        self.capabilities = {}
        self.turns = {}
        self.queries = {}

    def begin(self, task_id):
        """Replace the previous turn's credential and reset its independent data-query budget."""
        with self.worker.lock:
            turn = self.worker.tasks.get(task_id, {}).get("current_turn_id")
            with self.lock:
                capability = secrets.token_urlsafe(32)
                self.capabilities[task_id] = capability
                self.turns[task_id] = turn
                self.queries[task_id] = 0
        return capability

    def end(self, task_id):
        """Revoke the credential and budget when the exchange finishes or is cancelled."""
        with self.lock:
            for records in (self.capabilities, self.turns, self.queries):
                records.pop(task_id, None)

    def active(self, task_id, capability):
        """Require the original running turn and refuse cancellation or a replaced credential."""
        with self.worker.lock:
            with self.lock:
                current, turn = self.capabilities.get(task_id), self.turns.get(task_id)
                task = self.worker.tasks.get(task_id, {})
                return bool(current == capability and turn and task.get("current_turn_id") == turn
                            and task.get("status") == "running" and not task.get("cancellation_requested"))

    def validate(self, payload):
        """Validate every field before any network call and bound IDs, time range and output points."""
        operation = payload.get("operation")
        tool = next((tool for tool in TOOLS if tool["name"] == operation), None)
        if tool is None or set(payload) - {"operation", "capability", *tool["inputSchema"]["properties"]}:
            raise DataError("Only history and statistics arguments are supported")
        key = "entity_ids" if operation == "history" else "statistic_ids"
        ids = payload.get(key)
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS:
            raise DataError(f"Request between 1 and {MAX_IDS} explicit IDs")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 255
               or not value.isprintable() or (operation == "history" and not ENTITY_RE.fullmatch(value)) for value in ids):
            raise DataError("Invalid entity or statistic ID")
        if len(set(ids)) != len(ids):
            raise DataError("IDs must be unique")
        start, end = timestamp(payload.get("start_time")), timestamp(payload.get("end_time"))
        days = 7 if operation == "history" else 366
        if start >= end or end - start > timedelta(days=days):
            raise DataError(f"Use an increasing time window of at most {days} days")
        if end > datetime.now(timezone.utc) + timedelta(minutes=1):
            raise DataError("Historical queries cannot end in the future")
        points = payload.get("max_points", DEFAULT_POINTS)
        if type(points) is not int or not len(ids) <= points <= MAX_POINTS:
            raise DataError(f"max_points must be an integer between the number of IDs and {MAX_POINTS}")
        query = {key: list(ids), "start_time": start.isoformat(), "end_time": end.isoformat(), "max_points": points}
        if operation == "statistics":
            period, types = payload.get("period", "hour"), payload.get("types", STATISTIC_TYPES[:-1])
            if not isinstance(period, str) or period not in PERIODS:
                raise DataError("Unsupported statistics period")
            if (not isinstance(types, list) or not 1 <= len(types) <= len(STATISTIC_TYPES)
                    or any(not isinstance(value, str) or value not in STATISTIC_TYPES for value in types)
                    or len(set(types)) != len(types)):
                raise DataError("Unsupported statistics types")
            # Bound work in Core too, before it materializes a large WebSocket response.
            seconds = {"5minute": 300, "hour": 3600, "day": 86400, "week": 604800, "month": 28 * 86400}[period]
            if (math.ceil((end - start).total_seconds() / seconds) + 2) * len(ids) > 10000:
                raise DataError("Statistics query is too large; use fewer IDs, a shorter window or a coarser period")
            query.update(period=period, types=list(types))
        return operation, query

    def dispatch(self, payload):
        """Authenticate, run one bounded query, redact its result and discard data from expired turns."""
        if not isinstance(payload, dict):
            raise DataError("Expected a data request")
        supplied = payload.get("capability")
        if not isinstance(supplied, str) or not supplied or not supplied.isascii():
            raise DataError("No active data capability")
        with self.lock:
            task_id = next((key for key, value in self.capabilities.items() if hmac.compare_digest(value, supplied)), None)
        if not task_id or not self.active(task_id, supplied):
            raise DataError("Data capability expired")
        if not self.execution_lock.acquire(blocking=False):
            return {"status": "unavailable", "message": "Another historical data query is running"}
        try:
            try:
                operation, query = self.validate(payload)
                with self.lock:
                    if self.queries.get(task_id, MAX_QUERIES) >= MAX_QUERIES:
                        raise DataError("Historical data query limit reached for this turn")
                    self.queries[task_id] += 1
                if not self.active(task_id, supplied):
                    raise DataError("Data capability expired")
                result = self.history(query) if operation == "history" else self.statistics(query)
                encoded = json.dumps(result, allow_nan=False)
                # Bare configured credentials may occur as sensor values, not just in labelled fields.
                for value in (self.worker.ha_token(), self.worker.api_token(), self.worker.read_options().get("HA_TOKEN"), supplied):
                    if value:
                        encoded = encoded.replace(json.dumps(str(value))[1:-1], "[redacted]")
                encoded = self.worker.redact(encoded)
                if len(encoded.encode()) + 1 > MAX_RESPONSE_BYTES:
                    raise DataError("Data output is too large; lower max_points, shorten the window or use fewer IDs")
                result = json.loads(encoded)
            except Exception as exc:
                # Network exceptions and Core response bodies can contain private URLs or credentials.
                detail = str(exc) if isinstance(exc, DataError) else "Home Assistant data request failed"
                result = {"status": "unavailable", "message": detail[:220]}
            if not self.active(task_id, supplied):
                return {"status": "unavailable", "message": "Data capability expired"}
            return result
        finally:
            self.execution_lock.release()

    def token(self):
        """Get the worker-only Supervisor credential, failing before network access when unavailable."""
        token = self.worker.ha_token()
        if not token:
            raise DataError("Home Assistant authentication is unavailable")
        return token

    def history(self, query):
        """GET the fixed REST history route, retaining only timestamped state values for requested entities."""
        url = "http://supervisor/core/api/history/period/" + quote(query["start_time"], safe="")
        params = {"filter_entity_id": ",".join(query["entity_ids"]), "end_time": query["end_time"],
                  "minimal_response": "1", "no_attributes": "1", "significant_changes_only": "0"}
        deadline = time.monotonic() + 45
        with requests.get(url, params=params, headers={"Authorization": f"Bearer {self.token()}"},
                          timeout=(5, 20), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise DataError(f"Home Assistant history returned HTTP {response.status_code}; check Recorder and API access")
            content = bytearray()
            for chunk in response.iter_content(8192):
                content.extend(chunk)
                if len(content) > MAX_SOURCE_BYTES or time.monotonic() > deadline:
                    raise DataError("History response exceeds the query limit; use fewer IDs, a shorter window or statistics")
        raw = json.loads(content)
        if not isinstance(raw, list):
            raise DataError("Home Assistant returned invalid history data")
        series = {entity_id: {"points": []} for entity_id in query["entity_ids"]}
        for group in raw:
            if not isinstance(group, list):
                raise DataError("Home Assistant returned invalid history data")
            if not group:
                continue
            if not isinstance(group[0], dict) or not isinstance(group[0].get("entity_id"), str):
                raise DataError("Home Assistant returned invalid history data")
            entity_id = group[0].get("entity_id")
            if entity_id not in series:
                continue
            for state in group:
                if not isinstance(state, dict) or state.get("entity_id", entity_id) != entity_id:
                    raise DataError("Home Assistant returned invalid history data")
                point = {"time": timestamp(state["last_changed"]).isoformat(), "state": state["state"]}
                if not isinstance(point["state"], str):
                    raise DataError("Home Assistant returned an invalid history state")
                # Some domains ignore minimal_response and still include attribute-only updates.
                if series[entity_id]["points"] and series[entity_id]["points"][-1]["state"] == point["state"]:
                    continue
                series[entity_id]["points"].append(point)
        return self.result("history", query, series)

    def ws_read(self, messages):
        """Authenticate once and send only the two fixed recorder read commands with bounded replies."""
        token = self.token()
        if any(message.get("type") not in {"recorder/get_statistics_metadata", "recorder/statistics_during_period"}
               for message in messages):
            raise DataError("Unsupported recorder command")
        deadline = time.monotonic() + 45
        with closing(websocket.create_connection("ws://supervisor/core/websocket", timeout=15)) as ws:
            def receive():
                """Bound every frame, including authentication, and enforce a total query deadline."""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DataError("Statistics query timed out")
                ws.settimeout(min(15, remaining))
                raw = ws.recv()
                if len(raw.encode() if isinstance(raw, str) else raw) > MAX_SOURCE_BYTES:
                    raise DataError("Statistics response exceeds the query limit; use fewer IDs or a coarser period")
                reply = json.loads(raw)
                if not isinstance(reply, dict):
                    raise DataError("Home Assistant returned an invalid statistics reply")
                return reply
            first = receive()
            if first.get("type") != "auth_required":
                raise DataError("Home Assistant WebSocket authentication failed")
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            if receive().get("type") != "auth_ok":
                raise DataError("Home Assistant WebSocket authentication failed")
            results = []
            for request_id, message in enumerate(messages, 1):
                ws.send(json.dumps({**message, "id": request_id}))
                for _ in range(8):
                    reply = receive()
                    if reply.get("id") != request_id or reply.get("type") != "result":
                        continue
                    if not reply.get("success"):
                        raise DataError("Home Assistant could not read statistics; check Recorder and the requested IDs")
                    results.append(reply.get("result"))
                    break
                else:
                    raise DataError("Home Assistant did not return statistics")
            return results

    def statistics(self, query):
        """Read recorder metadata and statistics, preserving missing fields, units and bucket boundaries."""
        ids = query["statistic_ids"]
        metadata, raw = self.ws_read([
            {"type": "recorder/get_statistics_metadata", "statistic_ids": ids},
            {"type": "recorder/statistics_during_period", **{key: query[key] for key in
             ("statistic_ids", "start_time", "end_time", "period", "types")}},
        ])
        if not isinstance(metadata, list) or not isinstance(raw, dict):
            raise DataError("Home Assistant returned invalid statistics data")
        by_id = {item["statistic_id"]: item for item in metadata}
        series = {}
        for statistic_id in ids:
            meta = by_id.get(statistic_id)
            rows = raw.get(statistic_id, [])
            points = []
            for row in rows:
                point = {"start": iso_milliseconds(row["start"]), "end": iso_milliseconds(row["end"])}
                for key in query["types"]:
                    if key in row:
                        value = row[key]
                        if key == "last_reset" and value is not None:
                            value = iso_milliseconds(value)
                        elif value is not None and (type(value) not in (float, int) or not math.isfinite(value)):
                            raise DataError("Home Assistant returned a non-numeric statistic")
                        point[key] = value
                points.append(point)
            series[statistic_id] = {"points": points, "statistics_available": meta is not None,
                                    "unit_of_measurement": meta.get("display_unit_of_measurement") if meta else None,
                                    "has_mean": meta.get("has_mean") if meta else None,
                                    "mean_type": meta.get("mean_type") if meta else None,
                                    "has_sum": meta.get("has_sum") if meta else None}
        return self.result("statistics", query, series)

    def result(self, operation, query, series):
        """Share the point budget fairly, reporting exact truncation and empty/missing series."""
        allowance = query["max_points"] // len(series)
        for item in series.values():
            points = item["points"]
            item["available_points"] = len(points)
            item["points"] = points[:allowance]
            item["returned_points"] = len(item["points"])
            item["truncated"] = len(points) > allowance
            item["status"] = ("no_statistics" if item.get("statistics_available") is False
                              else "observed" if points else "no_data")
        return {"status": "observed", "operation": operation, "start_time": query["start_time"],
                "end_time": query["end_time"], **({"period": query["period"], "types": query["types"]} if operation == "statistics" else {}),
                "truncated": any(item["truncated"] for item in series.values()), "series": series,
                "message": "Truncated series are incomplete. Narrow the window, use fewer IDs or coarser statistics. "
                           "Empty history can mean no recording, no access or purged data; it does not prove a constant state."}

    def serve(self, path):
        """Start an owner-only Unix socket serving one bounded, capability-authenticated query per connection."""
        engine = self

        class Handler(socketserver.StreamRequestHandler):
            """Read one JSON line and return only a bounded, redacted data result."""

            def handle(self):
                """Refuse oversized/malformed requests and keep raw transport failures out of responses."""
                self.request.settimeout(60)
                try:
                    raw = self.rfile.readline(16385)
                    if len(raw) > 16384:
                        raise ValueError("Request too large")
                    result = engine.dispatch(json.loads(raw))
                except (OSError, ValueError):
                    result = {"status": "unavailable", "message": "Invalid or expired data request"}
                try:
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass

        Path(path).unlink(missing_ok=True)
        service = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        service.daemon_threads = True
        os.chmod(path, 0o600)
        threading.Thread(target=service.serve_forever, daemon=True).start()
        return service
