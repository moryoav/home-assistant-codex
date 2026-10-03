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
from server import verification_mcp_args


def main():
    binary = os.environ.get("HA_TEST_CODEX", "/usr/local/bin/codex")
    assert "0.160.0" in subprocess.check_output([binary, "--version"], text=True)
    with tempfile.TemporaryDirectory(prefix="ha-mcp-test-") as root:
        root = Path(root)
        checks = []
        worker = SimpleNamespace(
            lock=threading.RLock(), tasks={"task": {"status": "running", "current_turn_id": "turn"}},
            task_cancellation_requested=lambda _: False, utc_now=lambda: "fixture", redact=lambda value: value,
        )
        def update(task_id, **fields):
            worker.tasks[task_id].update(fields)
            checks.extend(fields.get("verification", []))
        worker.update_task = update
        engine = Verification(worker)
        engine.capabilities["task"] = "fixture-capability"
        engine.core = lambda *_args, **_kwargs: {"state": "on", "attributes": {}}
        path = str(root / "verification.sock")
        service = engine.serve(path)
        received = []

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if request.get("generate") is False:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"prewarm"}}\n\n')
                    return
                received.append(request)
                if len(received) % 2:
                    namespace = next((tool for tool in request["tools"] if tool.get("name") == "mcp__home_assistant"), None)
                    if not namespace or not any(tool.get("name") == "verify" for tool in namespace["tools"]):
                        self.send_error(400, "Verification tool missing")
                        return
                    item = {"type": "function_call", "call_id": "call_fixture", "namespace": namespace["name"], "name": "verify",
                            "arguments": json.dumps({"operation": "entity", "entity_id": "light.kitchen", "expected_state": "on"})}
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
                environment.update(CODEX_HOME=str(home), HOME=str(home), HA_VERIFICATION_CAPABILITY="fixture-capability")
                probe = subprocess.run([binary, "sandbox", "-c", f'sandbox_mode="{mode}"', "--", sys.executable, "-c",
                    "import socket; s=socket.socket(socket.AF_UNIX); s.connect(" + repr(path) + ")"],
                    cwd=root, env=environment, capture_output=True, text=True, timeout=30)
                assert probe.returncode != 0 and "Operation not permitted" in probe.stderr, probe
                # The test supplies the private socket path to the trusted MCP process.
                args = [binary, "exec", "--sandbox", mode, "--skip-git-repo-check", "--json", "--model", "gpt-5.1-codex",
                        "-c", 'approval_policy="never"', "-c", 'model_provider="fixture"',
                        "-c", 'model_providers.fixture.name="Fixture"',
                        "-c", f'model_providers.fixture.base_url="http://127.0.0.1:{provider.server_port}/v1"',
                        "-c", 'model_providers.fixture.wire_api="responses"',
                        "-c", 'model_providers.fixture.requires_openai_auth=false',
                        "-c", 'check_for_update_on_startup=false',
                        *verification_mcp_args(),
                        "-c", 'mcp_servers.home_assistant.env.HA_VERIFICATION_SOCKET=' + json.dumps(path),
                        "Check the kitchen light with the verification tool."]
                result = subprocess.run(args, cwd=root, env=environment, capture_output=True, text=True, timeout=90)
                assert result.returncode == 0, (result.stdout, result.stderr)
                assert "Fresh entity state" in json.dumps(received[-1]["input"]), (result.stdout, result.stderr)
                assert checks and checks[-1]["status"] == "passed", (result.stdout, result.stderr)
                assert len(received) % 2 == 0, received
            assert len(received) == 4
            print("Pinned CLI verification passed: shell sockets denied, MCP checks work in workspace-write and read-only.")
        finally:
            service.shutdown()
            service.server_close()
            provider.shutdown()
            provider.server_close()


if __name__ == "__main__":
    main()
