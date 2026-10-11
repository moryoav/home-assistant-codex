"""Exercise the pinned CLI's sandbox and MCP path without an account or model call."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verification import Verification
from ha_data import HomeAssistantData
from server import data_mcp_args, verification_mcp_args
from approvals import AppServerSession


def assert_data_results(request):
    """Decode nested MCP text content and require actual history and statistics in the model input."""
    results = {}

    def visit(value):
        """Visit structured content and any JSON nested in an MCP text result."""
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                return
            if not isinstance(decoded, str):
                visit(decoded)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("status") == "observed" and value.get("operation") in ("history", "statistics"):
                results[value["operation"]] = value
            for item in value.values():
                visit(item)

    visit(request["input"])
    assert results["history"]["series"]["sensor.kitchen"]["points"][0]["state"] == "22.5", request
    assert results["statistics"]["series"]["sensor.kitchen"]["points"][0]["mean"] == 22.5, request


def main():
    """Run both CLI transports: private MCP reads work while shell networking stays sandboxed."""
    binary = os.environ.get("HA_TEST_CODEX", "/usr/local/bin/codex")
    assert "0.160.0" in subprocess.check_output([binary, "--version"], text=True)
    with tempfile.TemporaryDirectory(prefix="ha-mcp-test-") as root:
        root = Path(root)
        checks = []
        worker = SimpleNamespace(
            lock=threading.RLock(), tasks={"task": {"status": "running", "current_turn_id": "turn"}},
            task_cancellation_requested=lambda _: False, utc_now=lambda: "fixture", redact=lambda value: value,
            ha_token=lambda: "private-supervisor-token", api_token=lambda: "private-worker-token", read_options=lambda: {},
        )
        def update(task_id, **fields):
            """Apply the fields to the fixture task and collect the verification checks among them."""
            worker.tasks[task_id].update(fields)
            checks.extend(fields.get("verification", []))
        worker.update_task = update
        engine = Verification(worker)
        engine.capabilities["task"] = "fixture-capability"
        engine.core = lambda *_args, **_kwargs: {"state": "on", "attributes": {}}
        path = str(root / "verification.sock")
        service = engine.serve(path)
        data = HomeAssistantData(worker)
        data_capability = data.begin("task")
        data.history = lambda query: data.result("history", query, {
            "sensor.kitchen": {"points": [{"time": query["start_time"], "state": "22.5"}]},
        })
        data.statistics = lambda query: data.result("statistics", query, {
            "sensor.kitchen": {"points": [{"start": query["start_time"], "end": query["end_time"], "mean": 22.5}],
                               "statistics_available": True, "unit_of_measurement": "°C"},
        })
        data_path = str(root / "data.sock")
        data_service = data.serve(data_path)
        received = []

        class Provider(BaseHTTPRequestHandler):
            """Fixture model provider that calls verification, history and statistics before answering."""

            def log_message(self, *_args):
                """Keep request logging out of the smoke test's output."""
                pass

            def do_POST(self):
                """Stream three tool calls and a final message for each CLI run.

                Prewarm requests get an empty completion and are not counted.
                """
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if request.get("generate") is False:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"prewarm"}}\n\n')
                    return
                received.append(request)
                step = len(received) % 4
                if step == 1:
                    namespace = next((tool for tool in request["tools"] if tool.get("name") == "mcp__home_assistant"), None)
                    if not namespace or not any(tool.get("name") == "verify" for tool in namespace["tools"]):
                        self.send_error(400, "Verification tool missing")
                        return
                    item = {"type": "function_call", "call_id": "call_fixture", "namespace": namespace["name"], "name": "verify",
                            "arguments": json.dumps({"operation": "entity", "entity_id": "light.kitchen", "expected_state": "on"})}
                elif step in (2, 3):
                    namespace = next((tool for tool in request["tools"] if tool.get("name") == "mcp__home_assistant_data"), None)
                    name = "history" if step == 2 else "statistics"
                    if not namespace or not any(tool.get("name") == name for tool in namespace["tools"]):
                        self.send_error(400, "Data tool missing")
                        return
                    arguments = {"entity_ids" if step == 2 else "statistic_ids": ["sensor.kitchen"],
                                 "start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-02T00:00:00Z"}
                    item = {"type": "function_call", "call_id": "data_" + name, "namespace": namespace["name"],
                            "name": name, "arguments": json.dumps(arguments)}
                else:
                    item = {"type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "Verified", "annotations": []}]}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in (
                    {"type": "response.created", "response": {"id": "resp_fixture"}},
                    {"type": "response.output_item.added", "output_index": 0, "item": item},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": "resp_fixture", "status": "completed", "output": [item]}},
                ):
                    self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        try:
            for mode in ("workspace-write", "read-only"):
                home = root / mode
                home.mkdir()
                environment = {key: os.environ[key] for key in ("PATH", "LD_LIBRARY_PATH") if key in os.environ}
                environment.update(CODEX_HOME=str(home), HOME=str(home), HA_VERIFICATION_CAPABILITY="fixture-capability",
                                   HA_DATA_CAPABILITY=data_capability)
                for private_path in (path, data_path):
                    probe = subprocess.run([binary, "sandbox", "-c", f'sandbox_mode="{mode}"', "--", sys.executable, "-c",
                        "import socket; s=socket.socket(socket.AF_UNIX); s.connect(" + repr(private_path) + ")"],
                        cwd=root, env=environment, capture_output=True, text=True, timeout=30)
                    assert probe.returncode != 0 and "Operation not permitted" in probe.stderr, probe
                # The test supplies the private socket path to the trusted MCP process.
                config_args = ["-c", 'model_provider="fixture"',
                        "-c", 'model_providers.fixture.name="Fixture"',
                        "-c", f'model_providers.fixture.base_url="http://127.0.0.1:{provider.server_port}/v1"',
                        "-c", 'model_providers.fixture.wire_api="responses"',
                        "-c", 'model_providers.fixture.requires_openai_auth=false',
                        "-c", 'check_for_update_on_startup=false',
                        *verification_mcp_args(), *data_mcp_args(),
                        "-c", 'mcp_servers.home_assistant.env.HA_VERIFICATION_SOCKET=' + json.dumps(path),
                        "-c", 'mcp_servers.home_assistant_data.env.HA_DATA_SOCKET=' + json.dumps(data_path)]
                args = [binary, "exec", "--sandbox", mode, "--skip-git-repo-check", "--json", "--model", "gpt-5.1-codex",
                        "-c", 'approval_policy="never"', *config_args, "Check the kitchen light with the verification tool."]
                result = subprocess.run(args, cwd=root, env=environment, capture_output=True, text=True, timeout=90)
                assert result.returncode == 0, (result.stdout, result.stderr)
                assert "Fresh entity state" in json.dumps(received[-1]["input"]), (result.stdout, result.stderr)
                assert_data_results(received[-1])
                assert checks and checks[-1]["status"] == "passed", (result.stdout, result.stderr)
                assert len(received) % 4 == 0, received
                # Sandboxed production tasks now use app-server. Verify the
                # trusted MCP bridge still works without shell networking.
                with (root / "app-server.log").open("w+") as errors:
                    proc = subprocess.Popen([binary, "app-server", *config_args], cwd=root, env=environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
                    finals = []
                    approvals = []
                    approval = AppServerSession(
                        proc.stdin, lock=threading.RLock(),
                        thread_params={"cwd": str(root), "sandbox": mode, "model": "gpt-5.1-codex",
                                       "approvalPolicy": "on-request", "approvalsReviewer": "user"},
                        turn_params={"input": [{"type": "text", "text": "Check the kitchen light with the verification tool."}]},
                        on_event=lambda _: None, on_approvals=approvals.append, on_final=finals.append,
                        is_active=lambda: proc.poll() is None, redact=lambda text: text,
                    )
                    reader = threading.Thread(target=approval.read, args=(proc.stdout,), daemon=True)
                    reader.start()
                    try:
                        approval.start()
                        assert approval.finished.wait(45), "Verification app-server timed out"
                    finally:
                        approval.close()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.terminate()
                            proc.wait(timeout=5)
                        reader.join(3)
                    errors.seek(0)
                    assert approval.returncode == 0 and finals, (approval.error, errors.read())
                    assert not any(approvals), approvals
                assert "Fresh entity state" in json.dumps(received[-1]["input"]), received[-1]
                assert_data_results(received[-1])
                assert checks[-1]["status"] == "passed", checks
            assert len(received) == 16
            assert data.queries["task"] == 8
            assert "private-supervisor-token" not in json.dumps(received)
            print("Pinned CLI passed: shell sockets denied, verification/history/statistics work without approvals through exec and app-server in both sandbox modes.")
        finally:
            service.shutdown()
            service.server_close()
            data_service.shutdown()
            data_service.server_close()
            provider.shutdown()
            provider.server_close()


if __name__ == "__main__":
    main()
