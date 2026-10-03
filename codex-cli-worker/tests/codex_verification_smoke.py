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
    """Run the pinned CLI in both sandbox modes: the shell cannot reach the verification socket, the MCP tools can.

    The verify tool reads an entity in both modes. The act tool reloads in workspace-write and is refused in read-only.
    """
    binary = os.environ.get("HA_TEST_CODEX", "/usr/local/bin/codex")
    assert "0.160.0" in subprocess.check_output([binary, "--version"], text=True)
    with tempfile.TemporaryDirectory(prefix="ha-mcp-test-") as root:
        root = Path(root)
        checks = []
        calls = []
        sandbox = {"mode": ""}
        worker = SimpleNamespace(
            lock=threading.RLock(), tasks={"task": {"status": "running", "current_turn_id": "turn"}},
            task_cancellation_requested=lambda _: False, utc_now=lambda: "fixture", redact=lambda value: value,
            read_options=lambda: {"codex_sandbox": sandbox["mode"]},
        )
        def update(task_id, **fields):
            """Apply the fields to the fixture task and collect the verification checks among them."""
            worker.tasks[task_id].update(fields)
            checks.extend(fields.get("verification", []))
        worker.update_task = update
        engine = Verification(worker)
        engine.capabilities["task"] = "fixture-capability"
        engine.core = lambda *_args, **_kwargs: {"state": "on", "attributes": {}}
        engine.call_service = lambda service, data: calls.append((service, data)) or ""
        path = str(root / "verification.sock")
        service = engine.serve(path)
        received = []

        class Provider(BaseHTTPRequestHandler):
            """Fixture model provider that requests one tool call and then gives the final answer."""

            def log_message(self, *_args):
                """Keep request logging out of the smoke test's output."""
                pass

            def do_POST(self):
                """Stream a tool call for each odd model request and the final message for each even one.

                The call is to act when the prompt carries the ACT_FIXTURE marker, otherwise to verify.
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
                if len(received) % 2:
                    namespace = next((tool for tool in request["tools"] if tool.get("name") == "mcp__home_assistant"), None)
                    names = {tool.get("name") for tool in namespace["tools"]} if namespace else set()
                    if not {"verify", "act"} <= names:
                        self.send_error(400, "Home Assistant tools missing")
                        return
                    if "ACT_FIXTURE" in json.dumps(request["input"]):
                        name, arguments = "act", {"operation": "reload", "domain": "automation"}
                    else:
                        name, arguments = "verify", {"operation": "entity", "entity_id": "light.kitchen", "expected_state": "on"}
                    item = {"type": "function_call", "call_id": "call_fixture", "namespace": namespace["name"], "name": name,
                            "arguments": json.dumps(arguments)}
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
                sandbox["mode"] = mode
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
                        "-c", 'mcp_servers.home_assistant.env.HA_VERIFICATION_SOCKET=' + json.dumps(path)]
                result = subprocess.run([*args, "Check the kitchen light with the verification tool."],
                                        cwd=root, env=environment, capture_output=True, text=True, timeout=90)
                assert result.returncode == 0, (result.stdout, result.stderr)
                assert "Fresh entity state" in json.dumps(received[-1]["input"]), (result.stdout, result.stderr)
                assert checks and checks[-1]["status"] == "passed", (result.stdout, result.stderr)
                assert len(received) % 2 == 0, received
                # The act tool is approved for the non-interactive run too; the worker decides what it does.
                before = len(calls)
                result = subprocess.run([*args, "ACT_FIXTURE Reload the automations with the act tool."],
                                        cwd=root, env=environment, capture_output=True, text=True, timeout=90)
                assert result.returncode == 0, (result.stdout, result.stderr)
                assert len(received) % 2 == 0, received
                if mode == "read-only":
                    assert checks[-1]["status"] == "refused" and len(calls) == before, (checks[-1], calls)
                    assert "read-only mode" in json.dumps(received[-1]["input"]), (result.stdout, result.stderr)
                else:
                    assert checks[-1]["status"] == "done" and calls[before:] == [("automation.reload", {})], (checks[-1], calls)
                    assert "accepted the call" in json.dumps(received[-1]["input"]), (result.stdout, result.stderr)
            assert len(received) == 8
            print("Pinned CLI verification passed: shell sockets denied, MCP checks work in workspace-write and "
                  "read-only, and the act tool reloads in workspace-write and is refused in read-only.")
        finally:
            service.shutdown()
            service.server_close()
            provider.shutdown()
            provider.server_close()


if __name__ == "__main__":
    main()
