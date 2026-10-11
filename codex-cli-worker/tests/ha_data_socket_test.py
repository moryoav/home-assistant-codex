"""Exercise the private historical-data bridge through real Unix sockets."""
import json
import os
from pathlib import Path
import socket
import stat
import threading
from unittest.mock import patch

import pytest
import pytest_socket

from test_ha_data import START, END, data_worker
from ha_data import HomeAssistantData
from ha_data_mcp import fetch


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix data socket")
def test_mcp_bridge_authenticates_to_owner_only_worker_socket(tmp_path, monkeypatch):
    """Use the actual stdio bridge's forwarding code, rejecting arbitrary or expired credentials."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    worker = data_worker()
    engine = HomeAssistantData(worker, tmp_path / "exports")
    capability = engine.begin("chat")
    path = tmp_path / "data.sock"
    monkeypatch.setenv("HA_DATA_CAPABILITY", capability)
    monkeypatch.setenv("HA_DATA_SOCKET", str(path))
    args = {"entity_ids": ["sensor.kitchen"], "start_time": START, "end_time": END}
    with patch.object(engine, "history", return_value={"status": "observed", "state": "42"}) as history:
        service = engine.serve(path)
        try:
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            receipt = fetch("history", args)
            assert receipt["status"] == "observed" and receipt["delivery"] == "file"
            export = Path(receipt["file"]["path"])
            assert export.is_relative_to(engine.export_root)
            assert json.loads(export.read_bytes()) == {"status": "observed", "state": "42"}
            monkeypatch.setenv("HA_DATA_CAPABILITY", "unrelated-verification-capability")
            assert fetch("history", args)["status"] == "unavailable"
            monkeypatch.setenv("HA_DATA_CAPABILITY", capability)
            engine.end("chat")
            assert not export.exists()
            assert fetch("history", args)["status"] == "unavailable"
            history.assert_called_once()
        finally:
            service.shutdown()
            service.server_close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix data socket")
def test_parallel_query_is_refused_and_cancelled_read_is_discarded(tmp_path, monkeypatch):
    """Keep one active read and discard its late data when the user cancels the exchange."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    worker = data_worker()
    engine = HomeAssistantData(worker, tmp_path / "exports")
    capability = engine.begin("chat")
    path = tmp_path / "data.sock"
    monkeypatch.setenv("HA_DATA_CAPABILITY", capability)
    monkeypatch.setenv("HA_DATA_SOCKET", str(path))
    entered, release = threading.Event(), threading.Event()
    first_result = []
    args = {"entity_ids": ["sensor.kitchen"], "start_time": START, "end_time": END}

    def read(_query):
        """Hold the backend response while another client arrives and the user cancels."""
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test query was not released")
        return {"status": "observed", "state": "private-old-result"}

    with patch.object(engine, "history", side_effect=read) as history:
        service = engine.serve(path)
        first = threading.Thread(target=lambda: first_result.append(fetch("history", args)))
        try:
            first.start()
            assert entered.wait(3)
            assert fetch("history", args) == {"status": "unavailable", "message": "Another historical data query is running"}
            with worker.lock:
                worker.tasks["chat"]["cancellation_requested"] = True
            release.set()
            first.join(3)
            assert not first.is_alive()
            assert first_result == [{"status": "unavailable", "message": "Data capability expired"}]
            assert engine.queries == {} and engine.capabilities == {}
            assert not engine.export_root.exists()
            history.assert_called_once()
        finally:
            release.set()
            first.join(5)
            service.shutdown()
            service.server_close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix data socket")
@pytest.mark.parametrize("raw", [b"{invalid}\n", b"[]\n", b"x" * 16385 + b"\n"])
def test_malformed_socket_requests_cannot_reach_home_assistant(tmp_path, raw):
    """Bound and reject raw transport input before any authenticated API read."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    engine = HomeAssistantData(data_worker(), tmp_path / "exports")
    path = tmp_path / "data.sock"
    with patch.object(engine, "history") as history:
        service = engine.serve(path)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(str(path))
                client.sendall(raw)
                with client.makefile("rb") as response:
                    assert json.loads(response.readline())["status"] == "unavailable"
            history.assert_not_called()
        finally:
            service.shutdown()
            service.server_close()
