"""Exercise the pinned CLI's sandbox and MCP path without an account or model call."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
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


def data_receipts(request):
    """Decode nested MCP content and locate the small file receipts actually delivered to the model."""
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
            if value.get("delivery") == "file" and value.get("operation") in ("history", "statistics"):
                results[value["operation"]] = value
            for item in value.values():
                visit(item)

    visit(request["input"])
    assert set(results) == {"history", "statistics"}, request
    assert "tokens truncated" not in json.dumps(request["input"]), request
    return results


def analysis_command(receipts):
    """Load every saved point in the sandbox and return counts, full-series sums and middle values."""
    code = """import hashlib, json
from pathlib import Path
receipts = RECEIPTS
answer = {}
for operation, receipt in receipts.items():
    content = Path(receipt['file']['path']).read_bytes()
    assert len(content) == receipt['file']['size_bytes']
    assert hashlib.sha256(content).hexdigest() == receipt['file']['sha256']
    data = json.loads(content)
    points = data['series']['sensor.kitchen']['points']
    assert not data['truncated'] and not receipt['truncated']
    values = [int(point['state'] if operation == 'history' else point['mean']) for point in points]
    answer[operation] = {'count': len(points), 'sum': sum(values), 'middle': values[len(values) // 2]}
print(json.dumps(answer, sort_keys=True))
""".replace("RECEIPTS", repr(receipts))
    return shlex.quote(sys.executable) + " -c " + shlex.quote(code)


def assert_data_results(request):
    """Require full-series calculations from the real sandbox, rather than trusting file metadata alone."""
    receipts = data_receipts(request)
    assert receipts["history"]["returned_points"] == 1000, receipts
    assert receipts["statistics"]["returned_points"] == 5000, receipts
    # Look at actual shell output separately from the echoed command text.
    outputs = [item for item in request["input"] if item.get("type") == "function_call_output"
               and item.get("call_id") == "analyze_files"]
    assert len(outputs) == 1, request
    text = json.dumps(outputs[0])
    assert '\\"count\\": 1000' in text and '\\"count\\": 5000' in text, outputs
    assert "499500" in text and "12497500" in text, outputs
    assert '\\"middle\\": 500' in text and '\\"middle\\": 2500' in text, outputs


def main():
    """Run both CLI transports: private MCP reads work while shell networking stays sandboxed."""
    binary = os.environ.get("HA_TEST_CODEX", "/usr/local/bin/codex")
    assert "0.160.0" in subprocess.check_output([binary, "--version"], text=True)
    # Private files must be outside both the workspace and writable /tmp, just
    # like /data/ha-data is outside /config in the app container.
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ha-mcp-test-", dir=cache) as root:
        root = Path(root)
        workspace = root / "workspace"
        workspace.mkdir()
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
        data = HomeAssistantData(worker, root / "exports")
        data_capability = data.begin("task")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        data.history = lambda query: data.result("history", query, {
            "sensor.kitchen": {"points": [{"time": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00", "state": str(index)}
                                          for index in range(1000)]},
        })
        data.statistics = lambda query: data.result("statistics", query, {
            "sensor.kitchen": {"points": [{"start": (start + timedelta(hours=index)).isoformat(),
                                           "end": (start + timedelta(hours=index + 1)).isoformat(), "mean": index,
                                           "min": index - 1, "max": index + 1, "sum": index * 2,
                                           "state": index * 2, "change": 2, "last_reset": query["start_time"]}
                                          for index in range(5000)],
                               "statistics_available": True, "unit_of_measurement": "°C"},
        })
        data_path = str(root / "data.sock")
        data_service = data.serve(data_path)
        received = []

        class Provider(BaseHTTPRequestHandler):
            """Call both data tools, then analyze their large files through a real sandboxed shell."""

            def log_message(self, *_args):
                """Keep request logging out of the smoke test's output."""
                pass

            def do_POST(self):
                """Stream four tool calls and a final message for each CLI run.

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
                step = len(received) % 5
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
                                 "start_time": "2026-01-01T00:00:00Z",
                                 "end_time": "2026-01-02T00:00:00Z" if step == 2 else "2026-09-01T00:00:00Z",
                                 "max_points": 1000 if step == 2 else 5000}
                    if step == 3:
                        arguments["types"] = ["mean", "min", "max", "sum", "state", "change", "last_reset"]
                    item = {"type": "function_call", "call_id": "data_" + name, "namespace": namespace["name"],
                            "name": name, "arguments": json.dumps(arguments)}
                elif step == 4:
                    receipts = data_receipts(request)
                    functions = [(tool["name"], child) for tool in request["tools"] if tool.get("type") == "namespace"
                                 for child in tool["tools"]]
                    functions += [(None, tool) for tool in request["tools"] if tool.get("type") != "namespace"]
                    namespace, tool = next((namespace, tool) for namespace, tool in functions if tool.get("name") == "exec_command")
                    item = {"type": "function_call", "call_id": "analyze_files", "name": tool["name"],
                            "arguments": json.dumps({"cmd": analysis_command(receipts), "max_output_tokens": 1000})}
                    if namespace:
                        item["namespace"] = namespace
                else:
                    assert_data_results(request)
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
                        cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
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
                result = subprocess.run(args, cwd=workspace, env=environment, capture_output=True, text=True, timeout=90)
                assert result.returncode == 0, (result.stdout, result.stderr)
                assert "Fresh entity state" in json.dumps(received[-1]["input"]), (result.stdout, result.stderr)
                assert_data_results(received[-1])
                assert checks and checks[-1]["status"] == "passed", (result.stdout, result.stderr)
                assert len(received) % 5 == 0, received
                export = Path(data_receipts(received[-1])["history"]["file"]["path"])
                probe = subprocess.run([binary, "sandbox", "-c", f'sandbox_mode="{mode}"', "--", sys.executable, "-c",
                    "from pathlib import Path; Path(" + repr(str(export)) + ").write_text('tampered')"],
                    cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
                assert probe.returncode != 0 and export.read_text().startswith('{"status": "observed"'), probe
                # Sandboxed production tasks now use app-server. Verify the
                # trusted MCP bridge still works without shell networking.
                with (root / "app-server.log").open("w+") as errors:
                    proc = subprocess.Popen([binary, "app-server", *config_args], cwd=workspace, env=environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
                    finals = []
                    approvals = []
                    approval = AppServerSession(
                        proc.stdin, lock=threading.RLock(),
                        thread_params={"cwd": str(workspace), "sandbox": mode, "model": "gpt-5.1-codex",
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
            assert len(received) == 20
            assert data.queries["task"] == 8
            assert "private-supervisor-token" not in json.dumps(received)
            data.end("task")
            assert not any(data.export_root.rglob("*.json"))
            print("Pinned CLI passed: private sockets and export writes denied; all 1,000 history and 5,000 statistics points analyzed without approvals through exec and app-server in both sandbox modes.")
        finally:
            service.shutdown()
            service.server_close()
            data_service.shutdown()
            data_service.server_close()
            provider.shutdown()
            provider.server_close()


if __name__ == "__main__":
    main()
