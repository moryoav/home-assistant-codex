"""Stdio MCP bridge for bounded, read-only Home Assistant recorder data."""
from __future__ import annotations

import json
import os
import socket
import sys

MAX_IDS = 16
MAX_POINTS = 5000
DEFAULT_POINTS = 1000
MAX_RESPONSE_BYTES = 256 * 1024
PERIODS = ["5minute", "hour", "day", "week", "month"]
STATISTIC_TYPES = ["mean", "min", "max", "sum", "state", "change", "last_reset"]
COMMON_PROPERTIES = {
    "start_time": {"type": "string", "format": "date-time", "description": "ISO timestamp with timezone, inclusive."},
    "end_time": {"type": "string", "format": "date-time", "description": "ISO timestamp with timezone, exclusive; no future dates."},
    "max_points": {"type": "integer", "minimum": 1, "maximum": MAX_POINTS, "default": DEFAULT_POINTS,
                   "description": "Total point budget divided between requested IDs; at least one per ID. Truncation is explicit."},
}
TOOLS = [
    {
        "name": "history",
        "description": "Read timestamped Home Assistant states for explicit entity IDs over at most seven days. "
                       "Includes the initial state and preserves unknown/unavailable values. No attributes or device actions. "
                       "Truncated data is incomplete: narrow the window or use statistics before drawing conclusions.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {**COMMON_PROPERTIES, "entity_ids": {"type": "array", "minItems": 1,
                                       "maxItems": MAX_IDS, "uniqueItems": True, "items": {"type": "string"}}},
                        "required": ["entity_ids", "start_time", "end_time"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "statistics",
        "description": "Read Home Assistant recorder statistics and units for explicit statistic IDs over at most 366 days. "
                       "Use 5minute, hour, day, week or month; fields exist only where supported by the sensor. "
                       "sum is cumulative; use change for period consumption. Buckets follow Home Assistant's timezone. "
                       "Missing statistics differ from an empty period. Narrow/coarsen truncated queries before analysis.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {**COMMON_PROPERTIES, "statistic_ids": {"type": "array", "minItems": 1,
                                       "maxItems": MAX_IDS, "uniqueItems": True, "items": {"type": "string", "maxLength": 255}},
                                       "period": {"type": "string", "enum": PERIODS, "default": "hour"},
                                       "types": {"type": "array", "minItems": 1, "maxItems": len(STATISTIC_TYPES),
                                                 "uniqueItems": True, "items": {"type": "string", "enum": STATISTIC_TYPES}}},
                        "required": ["statistic_ids", "start_time", "end_time"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def fetch(name, arguments):
    """Forward an allowlisted operation to the worker using this turn's temporary credential."""
    tool = next((tool for tool in TOOLS if tool["name"] == name), None)
    if tool is None or not isinstance(arguments, dict) or set(arguments) - set(tool["inputSchema"]["properties"]):
        raise ValueError("Unsupported data request")
    payload = {**arguments, "operation": name, "capability": os.environ.get("HA_DATA_CAPABILITY", "")}
    encoded = json.dumps(payload).encode() + b"\n"
    if len(encoded) > 16384:
        raise ValueError("Data request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        # Leave room for an in-flight HTTP read timeout after the backend's query deadline.
        client.settimeout(75)
        client.connect(os.environ.get("HA_DATA_SOCKET", "/data/ha-data.sock"))
        client.sendall(encoded)
        with client.makefile("rb") as response:
            raw = response.readline(MAX_RESPONSE_BYTES + 1)
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Invalid data response")
    return json.loads(raw)


def respond(request):
    """Expose initialization, discovery and the two read-only recorder tools."""
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "home-assistant-data", "version": "1.0"},
                "instructions": "Read-only history and statistics through the worker, without shell networking or API tokens. "
                                "Request only relevant IDs and time windows. Truncated results are incomplete; narrow the "
                                "query or use coarser statistics before analysis. Empty history may be purged or unrecorded. "
                                "Statistics sum is cumulative, while change describes period consumption."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            params = request.get("params", {})
            result = fetch(params.get("name"), params.get("arguments", {}))
        except (OSError, ValueError, TypeError, AttributeError):
            result = {"status": "unavailable", "message": "Data request failed or the task has ended"}
        return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": result.get("status") == "unavailable"}
    raise ValueError("Method not found")


def main():
    """Serve bounded JSON-RPC lines, keeping all diagnostics out of stdout."""
    while raw := sys.stdin.buffer.readline(65537):
        if len(raw) > 65536:
            return 1
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"]}
            try:
                response["result"] = respond(request)
            except (ValueError, TypeError, AttributeError):
                response["error"] = {"code": -32601, "message": "Unsupported request"}
            print(json.dumps(response), flush=True)
        except (ValueError, UnicodeError):
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
