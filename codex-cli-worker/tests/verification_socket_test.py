"""Exercise concurrent clients through the real verification Unix socket."""
import json
import socket
import threading
from unittest.mock import patch

import pytest
import pytest_socket

from test_server import server
from verification import Verification


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix verification socket")
def test_parallel_request_is_rejected_while_active_check_is_blocked(tmp_path, monkeypatch):
    """Reject a second request before the first finishes, without recording it."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    monkeypatch.setattr(server, "tasks", {"chat": {"status": "running", "current_turn_id": "turn"}})
    monkeypatch.setattr(server, "save_task_index", lambda: None)
    monkeypatch.setattr(server, "get_task_dir", lambda _task_id: tmp_path / "chat")
    engine = Verification(server)
    engine.capabilities["chat"] = "test-capability"
    entered = threading.Event()
    release = threading.Event()

    def inspect(_payload):
        """Block the first entity check until the test releases it."""
        entered.set()
        if not release.wait(5):
            raise ValueError("Test inspection was not released")
        return {"status": "observed"}

    path = str(tmp_path / "verification.sock")
    with patch.object(engine, "entity", side_effect=inspect) as entity:
        service = engine.serve(path)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first, \
                 socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second:
                request = json.dumps({"capability": "test-capability", "operation": "entity", "entity_id": "light.kitchen"}).encode() + b"\n"
                first.settimeout(3)
                second.settimeout(3)
                first.connect(path)
                first.sendall(request)
                assert entered.wait(3)
                second.connect(path)
                second.sendall(request)
                with second.makefile("rb") as response:
                    assert json.loads(response.readline()) == {
                        "status": "unavailable", "message": "Another verification is running",
                    }
                assert not release.is_set()
                release.set()
                with first.makefile("rb") as response:
                    assert json.loads(response.readline())["status"] == "observed"
                entity.assert_called_once()
                assert len(server.tasks["chat"]["verification"]) == 1
        finally:
            release.set()
            service.shutdown()
            service.server_close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix verification socket")
def test_act_tool_reaches_the_worker_through_the_bridge(tmp_path, monkeypatch):
    """The bridge offers both tools and forwards an act request, with the capability, to the worker's socket."""
    import verification_mcp

    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    monkeypatch.setattr(server, "tasks", {"chat": {"status": "running", "current_turn_id": "turn"}})
    monkeypatch.setattr(server, "save_task_index", lambda: None)
    monkeypatch.setattr(server, "get_task_dir", lambda _task_id: tmp_path / "chat")
    monkeypatch.setattr(server, "read_options", lambda: dict(server.DEFAULT_OPTIONS))
    engine = Verification(server)
    engine.capabilities["chat"] = "test-capability"
    path = str(tmp_path / "verification.sock")
    monkeypatch.setenv("HA_VERIFICATION_SOCKET", path)
    monkeypatch.setenv("HA_VERIFICATION_CAPABILITY", "test-capability")
    assert [tool["name"] for tool in verification_mcp.respond({"method": "tools/list"})["tools"]] == ["verify", "act"]

    def call(name, arguments):
        """Call one tool through the bridge and return the worker's result and the MCP error flag."""
        reply = verification_mcp.respond({"method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return json.loads(reply["content"][0]["text"]), reply["isError"]

    with patch.object(engine, "call_service", return_value="") as service_call:
        service = engine.serve(path)
        try:
            result, is_error = call("act", {"operation": "reload", "domain": "automation"})
            assert (result["status"], result["service"], is_error) == ("done", "automation.reload", False)
            # A refusal is an answer, not a tool error: the reason is in the result.
            result, is_error = call("act", {"operation": "call_service", "service": "light.turn_on", "entity_id": ["light.kitchen"]})
            assert (result["status"], is_error) == ("refused", False)
            # Each tool forwards only its own operations and fields, and a wrong capability gets nowhere.
            rejected = [
                ("act", {"operation": "entity", "entity_id": "light.kitchen"}),
                ("verify", {"operation": "reload"}),
                ("act", {"operation": "reload", "url": "http://example.test"}),
                ("unknown", {"operation": "reload"}),
            ]
            for name, arguments in rejected:
                result, is_error = call(name, arguments)
                assert (result["status"], is_error) == ("unavailable", True), (name, arguments)
            monkeypatch.setenv("HA_VERIFICATION_CAPABILITY", "another-capability")
            result, is_error = call("act", {"operation": "reload"})
            assert (result["status"], is_error) == ("unavailable", True)
            service_call.assert_called_once_with("automation.reload", {})
            recorded = server.tasks["chat"]["verification"]
            assert [(entry["operation"], entry["status"]) for entry in recorded] == [("reload", "done"), ("call_service", "refused")]
        finally:
            service.shutdown()
            service.server_close()
