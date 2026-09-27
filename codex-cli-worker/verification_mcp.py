"""Stdio MCP bridge for task-scoped checks outside the shell network sandbox."""
from __future__ import annotations

import json
import os
import socket
import sys

TOOL = {
    "name": "verify",
    "description": (
        "Check Home Assistant configuration, fresh entity state, Core logs, or dashboard configuration. "
        "Dashboard checks capture desktop/mobile screenshots; inspect their image_paths before visual claims. "
        "save_pending saves only a dashboard edited in this turn when auto-save is enabled. "
        "No service calls, reloads, arbitrary URLs, or other apps' logs."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["entity", "config_check", "logs", "dashboard", "dashboard_readback"]},
            "entity_id": {"type": "string"}, "expected_state": {"type": "string"},
            "attributes": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "path": {"type": "string", "description": "Local dashboard path, for example /lovelace/lights"},
            "save_pending": {"type": "boolean"},
        },
        "required": ["operation"],
    },
}


def verify(arguments):
    """Forward only the supported fields and the process's temporary capability."""
    if not isinstance(arguments, dict) or set(arguments) - set(TOOL["inputSchema"]["properties"]):
        raise ValueError("Unsupported verification arguments")
    if arguments.get("operation") not in TOOL["inputSchema"]["properties"]["operation"]["enum"]:
        raise ValueError("Unsupported verification operation")
    payload = {**arguments, "capability": os.environ.get("HA_VERIFICATION_CAPABILITY", "")}
    encoded = json.dumps(payload).encode() + b"\n"
    if len(encoded) > 16384:
        raise ValueError("Verification request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(230)
        client.connect(os.environ.get("HA_VERIFICATION_SOCKET", "/data/verification.sock"))
        client.sendall(encoded)
        with client.makefile("rb") as response:
            raw = response.readline(128 * 1024 + 1)
    if not raw or len(raw) > 128 * 1024:
        raise ValueError("Invalid verification response")
    return json.loads(raw)


def respond(request):
    """Implement the MCP initialization, discovery and single-tool request surface."""
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "home-assistant-verification", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            if params.get("name") != TOOL["name"]:
                raise ValueError("Unknown verification tool")
            result = verify(params.get("arguments", {}))
        except (OSError, ValueError, TypeError):
            result = {"status": "unavailable", "message": "Verification request failed or the task has ended"}
        return {"content": [{"type": "text", "text": json.dumps(result)}],
                "isError": result.get("status") == "unavailable"}
    raise ValueError("Method not found")


def main():
    """Read bounded newline-delimited JSON-RPC; stdout contains protocol data only."""
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
