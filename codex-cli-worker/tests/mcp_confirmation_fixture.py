"""Local MCP fixture that records effects only after a valid tool response."""

import json
from pathlib import Path
import sys


def send(**message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def serve(directory):
    pending = {}
    for line in sys.stdin:
        request = json.loads(line)
        method, request_id = request.get("method"), request.get("id")
        if method == "initialize":
            send(id=request_id, result={
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "approval-fixture", "version": "1"},
            })
        elif method == "tools/list":
            send(id=request_id, result={"tools": [
                {"name": name, "description": f"Exercise {name}", "inputSchema": {"type": "object", "properties": {}}}
                for name in ("native_gate", "server_gate", "choose_room")
            ]})
        elif method == "tools/call":
            name = request["params"]["name"]
            if name == "native_gate":
                complete(directory, request_id, name, True)
                continue
            properties = {"room": {"type": "string", "enum": ["kitchen", "office"]}} if name == "choose_room" else {}
            pending[name] = request_id
            send(id=name, method="elicitation/create", params={
                "mode": "form", "message": "Choose a room" if properties else "Run the fixture action?",
                "requestedSchema": {"type": "object", "properties": properties, "required": list(properties)},
            })
        elif request_id in pending:
            result = request.get("result") or {}
            content = result.get("content") or {}
            accepted = result.get("action") == "accept" and (request_id != "choose_room" or content.get("room") == "kitchen")
            complete(directory, pending.pop(request_id), request_id, accepted)
        elif request_id is not None:
            send(id=request_id, error={"code": -32601, "message": "Unsupported fixture method"})


def complete(directory, request_id, name, accepted):
    if accepted:
        with (directory / name).open("a", encoding="utf-8") as handle:
            handle.write("executed\n")
    send(id=request_id, result={
        "content": [{"type": "text", "text": "Fixture executed" if accepted else "Fixture declined"}],
        "isError": not accepted,
    })


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
