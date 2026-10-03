"""Stdio MCP bridge for task-scoped checks and actions outside the shell network sandbox."""
from __future__ import annotations

import json
import os
import socket
import sys

VERIFY_TOOL = {
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
ACT_TOOL = {
    "name": "act",
    "description": (
        "Make Home Assistant reload YAML configuration or call a service, through the worker. "
        "The app's Home Assistant actions option decides what is allowed; a refused or failed action "
        "says why in its result. Act only on what the user asked for, then verify with a fresh readback. "
        "No arbitrary URLs and no access to other apps."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["reload", "call_service"]},
            "domain": {"type": "string", "description": "For reload: the domain to reload, for example automation. "
                                                         "Omit it to reload all YAML configuration."},
            "service": {"type": "string", "description": "For call_service: domain.service, for example automation.turn_off"},
            "entity_id": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "data": {"type": "string", "description": "For call_service: other service data as a JSON object in text, "
                                                       "for example {\"brightness_pct\": 40}"},
        },
        "required": ["operation"],
    },
}
TOOLS = {tool["name"]: tool for tool in (VERIFY_TOOL, ACT_TOOL)}


def forward(tool, arguments):
    """Send a tool's supported fields and the process's temporary capability to the worker, and return its answer."""
    schema = tool["inputSchema"]["properties"]
    if not isinstance(arguments, dict) or set(arguments) - set(schema):
        raise ValueError("Unsupported arguments")
    if arguments.get("operation") not in schema["operation"]["enum"]:
        raise ValueError("Unsupported operation")
    payload = {**arguments, "capability": os.environ.get("HA_VERIFICATION_CAPABILITY", "")}
    encoded = json.dumps(payload).encode() + b"\n"
    if len(encoded) > 16384:
        raise ValueError("Request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(230)
        client.connect(os.environ.get("HA_VERIFICATION_SOCKET", "/data/verification.sock"))
        client.sendall(encoded)
        with client.makefile("rb") as response:
            raw = response.readline(128 * 1024 + 1)
    if not raw or len(raw) > 128 * 1024:
        raise ValueError("Invalid response")
    return json.loads(raw)


def respond(request):
    """Implement the MCP initialization, discovery and tool request surface."""
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "home-assistant-verification", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": list(TOOLS.values())}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            tool = TOOLS.get(params.get("name"))
            if tool is None:
                raise ValueError("Unknown tool")
            result = forward(tool, params.get("arguments", {}))
        except (OSError, ValueError, TypeError):
            result = {"status": "unavailable", "message": "The request failed or the task has ended"}
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
