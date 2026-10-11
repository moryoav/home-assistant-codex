"""Exercise real pinned-CLI approvals against a local model fixture, without an account."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from approvals import AppServerSession
from server import FINAL_RESPONSE_SCHEMA


def main():
    """Prove allow/deny and session resume preserve the next command's sandbox."""
    binary = os.environ.get("HA_TEST_CODEX", "/usr/local/bin/codex")
    assert "0.160.0" in subprocess.check_output([binary, "--version"], text=True)
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ha-approval-smoke-", dir=cache) as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        requests = []
        probes = []
        stage = 0
        final_only = False
        file_case = False
        outside_file = root / "outside.yaml"
        moved_file = root / "renamed.yaml"

        class Provider(BaseHTTPRequestHandler):
            """Serve deterministic tool calls and count actual shell HTTP requests."""

            def log_message(self, *_args):
                """Keep HTTP fixture logging out of the test output."""

            def do_GET(self):
                """Record a successful shell request, which sandboxing should block."""
                probes.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"network-action-executed")

            def do_POST(self):
                """Return a sandboxed command, an escalation, another sandboxed command, then a final answer."""
                nonlocal stage
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                tools = request.get("tools", [])
                functions = []
                for tool in tools:
                    if tool.get("type") == "namespace":
                        functions.extend((tool["name"], child) for child in tool["tools"])
                    else:
                        functions.append((None, tool))
                if file_case and stage < 2:
                    selected = next(((namespace, tool) for namespace, tool in functions if tool.get("name") == "exec_command"), None)
                    assert selected, [(namespace, tool.get("name")) for namespace, tool in functions]
                    namespace, tool = selected
                    target = outside_file if stage == 0 else moved_file
                    move = f"*** Move to: {moved_file}\n" if stage == 0 else ""
                    patch = f"*** Begin Patch\n*** Update File: {target}\n{move}@@\n-value: {stage}\n+value: {stage + 1}\n*** End Patch"
                    # The pinned CLI recognizes apply_patch inside exec_command
                    # and routes it through its native file-change approval.
                    item = {"type": "function_call", "call_id": f"file_{stage}", "name": tool["name"],
                            "arguments": json.dumps({"cmd": "apply_patch <<'PATCH'\n" + patch + "\nPATCH"})}
                    if namespace:
                        item["namespace"] = namespace
                elif stage < 3 and not final_only and not file_case:
                    selected = next(((namespace, tool) for namespace, tool in functions if tool.get("name") == "exec_command"), None)
                    assert selected, [(namespace, tool.get("name")) for namespace, tool in functions]
                    namespace, tool = selected
                    command = shlex.quote(sys.executable) + " -c " + shlex.quote(
                        f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{provider.server_port}/probe', timeout=3).read().decode())")
                    arguments = {"cmd": command, "max_output_tokens": 1000}
                    if stage == 1:
                        arguments.update(sandbox_permissions="require_escalated", justification="Read the local test endpoint once")
                    item = {"type": "function_call", "call_id": f"call_{stage}", "name": tool["name"], "arguments": json.dumps(arguments)}
                    if namespace:
                        item["namespace"] = namespace
                else:
                    text = json.dumps({"status": "completed", "summary": "Fixture finished", "details": "", "question": "", "choices": []})
                    item = {"type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
                            "content": [{"type": "output_text", "text": text, "annotations": []}]}
                stage += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in (
                    {"type": "response.created", "response": {"id": f"resp_{stage}"}},
                    {"type": "response.output_item.added", "output_index": 0, "item": item},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": f"resp_{stage}", "status": "completed", "output": [item]}},
                ):
                    self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        (root / "codex-home").mkdir()
        environment = {**os.environ, "CODEX_HOME": str(root / "codex-home")}
        settings = {
            "model_provider": "fixture", "model_providers.fixture.name": "Fixture",
            "model_providers.fixture.base_url": f"http://127.0.0.1:{provider.server_port}/v1",
            "model_providers.fixture.wire_api": "responses", "model_providers.fixture.requires_openai_auth": False,
            "check_for_update_on_startup": False,
        }
        config_args = [arg for key, value in settings.items() for arg in ["-c", key + "=" + json.dumps(value)]]
        try:
            # Start in the old transport to prove existing saved chats can resume.
            final_only = True
            legacy = subprocess.run([binary, "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--json",
                                     "--model", "gpt-5.1-codex", *config_args, "Say fixture finished"],
                                    cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
            assert legacy.returncode == 0, (legacy.stdout, legacy.stderr)
            thread_id = next(json.loads(line)["thread_id"] for line in legacy.stdout.splitlines() if json.loads(line).get("type") == "thread.started")
            final_only = False
            for mode, decision, resume_id in [("workspace-write", "allow", thread_id), ("read-only", "deny", None), ("read-only", "file", None)]:
                file_case = decision == "file"
                outside_file.write_text("value: 0\n")
                stage = 0
                requests.clear()
                probes.clear()
                records = []
                finals = []
                events = []
                with (root / "stderr.log").open("w+") as errors:
                    proc = subprocess.Popen([binary, "app-server", *config_args], cwd=workspace, env=environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
                    params = {"cwd": str(workspace), "model": "gpt-5.1-codex", "sandbox": mode,
                              "approvalPolicy": "on-request", "approvalsReviewer": "user"}
                    if resume_id:
                        params["threadId"] = resume_id

                    def approve(published):
                        """Simulate the browser's explicit decision for every live request."""
                        records[:] = published
                        for record in published:
                            if record["status"] == "pending":
                                answer = ("allow" if len(published) == 1 else "deny") if file_case else decision
                                session.decide(record["approval_id"], answer)

                    session = AppServerSession(proc.stdin, lock=threading.RLock(), thread_params=params,
                                               turn_params={"input": [{"type": "text", "text": "Exercise sandbox and approval"}], "outputSchema": FINAL_RESPONSE_SCHEMA},
                                               on_event=events.append, on_approvals=approve, on_final=finals.append,
                                               is_active=lambda: proc.poll() is None, redact=lambda text: text)
                    reader = threading.Thread(target=session.read, args=(proc.stdout,), daemon=True)
                    reader.start()
                    try:
                        session.start()
                        assert session.finished.wait(45), "App-server approval timed out"
                    finally:
                        session.close()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.terminate()
                            proc.wait(timeout=5)
                        reader.join(3)
                    errors.seek(0)
                    diagnostic = (session.error, errors.read()[-6000:], events[-5:])
                assert session.returncode == 0 and finals, diagnostic
                if file_case:
                    assert len(records) == 2 and [record["status"] for record in records] == ["allowed", "denied"], (records, diagnostic)
                    assert all(record["kind"] == "file_change" and record["changes"] for record in records), records
                    assert records[0]["changes"][0]["move_path"] == str(moved_file), records
                    assert not outside_file.exists(), records
                    assert moved_file.read_text() == "value: 1\n", moved_file.read_text()
                    continue
                assert len(records) == 1, (records, diagnostic)
                assert records[0]["status"] == ("allowed" if decision == "allow" else "denied"), records
                assert len(probes) == (1 if decision == "allow" else 0), (probes, diagnostic)
                assert len(requests) == 4, (len(requests), diagnostic)
                # Rejected commands may never start and therefore need not emit
                # command activity items. Their tool responses are authoritative.
                for call_id in ("call_0", "call_2"):
                    outputs = [item for item in requests[-1]["input"]
                               if item.get("type") == "function_call_output" and item.get("call_id") == call_id]
                    assert outputs, (call_id, requests[-1]["input"])
                    assert "network-action-executed" not in json.dumps(outputs), outputs
            print("Pinned CLI approvals passed: legacy sessions resume, command and file allow/deny works, subsequent actions stay sandboxed.")
        finally:
            provider.shutdown()
            provider.server_close()


if __name__ == "__main__":
    main()
