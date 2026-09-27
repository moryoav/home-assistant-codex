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
