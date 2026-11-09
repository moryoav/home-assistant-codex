"""Exercise real pinned-CLI approvals against a local model fixture, without an account."""

import argparse
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


@contextmanager
def worker_approval_session(proc, workspace, root, mode, resume_id, approve, finals, events):
    """Exercise production session creation, translating only container fixture paths."""
    task_id = "approval-fixture"
    task = {"status": "running", "current_turn_id": "approval-turn", "turns": [{
        "execution_settings": {"model": "gpt-5.1-codex", "reasoning_effort": "low"},
    }]}

    def update_task(_task_id, **fields):
        """Persist approvals in the fixture task as the worker's callback expects."""
        task.update(fields)
        return True

    with (
        patch.object(server, "tasks", {task_id: task}),
        patch.object(server, "running_processes", {task_id: proc}),
        patch.object(server, "api_token", return_value="fixture-worker-token"),
        patch.object(server, "update_task", side_effect=update_task),
        patch.object(server, "write_task_log"),
        patch.object(server, "record_activity_event", side_effect=lambda _task_id, event: events.append(event)),
        patch.object(server, "notify_permission_async"),
    ):
        session = server.create_approval_session(
            task_id, proc, "Exercise sandbox and tool approvals", root / "approval-final.json",
            resume_id, {}, {"codex_sandbox": mode},
        )
        # Production owns the security policy, including on resumed threads.
        policy = session.turn_params["sandboxPolicy"]
        if mode == "danger-full-access":
            assert policy == {"type": "dangerFullAccess"}, policy
        else:
            assert policy["networkAccess"] is False, policy
            assert policy["type"] == ("readOnly" if mode == "read-only" else "workspaceWrite"), policy
        assert session.thread_params["cwd"] == "/config", session.thread_params
        session.thread_params["cwd"] = str(workspace)
        if mode == "workspace-write":
            assert policy["writableRoots"] == ["/config"], policy
            policy["writableRoots"] = [str(workspace)]
        save_approvals = session.on_approvals
        save_final = session.on_final

        def approve_and_persist(published):
            """Keep production approval persistence before the simulated UI decision."""
            save_approvals(published)
            approve(published)

        def finish(text):
            """Keep production final-file persistence and collect the test answer."""
            save_final(text)
            finals.append(text)

        session.on_approvals = approve_and_persist
        session.on_final = finish
        yield session


def main():
    """Prove allow/deny and session resume preserve the next command's sandbox."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", action="store_true", help="Exercise the packaged SSH client against a local fixed-command server")
    args = parser.parse_args()
    binary = os.environ.get("HA_TEST_CODEX", "/usr/local/bin/codex")
    assert "0.165.0" in subprocess.check_output([binary, "--version"], text=True)
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ha-approval-smoke-", dir=cache) as temporary, ExitStack() as resources:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        requests = []
        probes = []
        ssh_command = None
        if args.ssh:
            from ssh_approval_fixture import ssh_probe

            ssh_command = resources.enter_context(ssh_probe(root, probes))
        stage = 0
        final_only = False
        file_case = False
        mcp_case = False
        full_access = False
        tool_names = ("native_gate", "server_gate", "choose_room")
        full_write = root / "outside-workspace.txt"
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
                if mcp_case and stage < len(tool_names) + int(full_access):
                    if full_access and stage == 0:
                        namespace, tool = next((ns, tool) for ns, tool in functions if tool.get("name") == "exec_command")
                        command = shlex.quote(sys.executable) + " -c " + shlex.quote(
                            f"from pathlib import Path; import urllib.request; "
                            f"urllib.request.urlopen('http://127.0.0.1:{provider.server_port}/probe', timeout=3).read(); "
                            f"Path({str(full_write)!r}).write_text('full access')"
                        )
                        arguments = {"cmd": command}
                    else:
                        name = tool_names[stage - int(full_access)]
                        namespace, tool = next((ns, tool) for ns, tool in functions if ns == "mcp__probe" and tool.get("name") == name)
                        arguments = {}
                    item = {"type": "function_call", "call_id": f"probe_{stage}", "name": tool["name"],
                            "arguments": json.dumps(arguments)}
                    if namespace:
                        item["namespace"] = namespace
                elif file_case and stage < 2:
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
                elif stage < 3 and not final_only and not file_case and not mcp_case:
                    selected = next(((namespace, tool) for namespace, tool in functions if tool.get("name") == "exec_command"), None)
                    assert selected, [(namespace, tool.get("name")) for namespace, tool in functions]
                    namespace, tool = selected
                    command = ssh_command or shlex.quote(sys.executable) + " -c " + shlex.quote(
                        f"print('sandbox-command-started', flush=True); import urllib.request; "
                        f"print(urllib.request.urlopen('http://127.0.0.1:{provider.server_port}/probe', timeout=3).read().decode())")
                    arguments = {"cmd": command, "max_output_tokens": 1000}
                    if stage == 1:
                        arguments.update(sandbox_permissions="require_escalated", justification="Run the local test operation once")
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
            "check_for_update_on_startup": False, "features.apps": False,
        }
        config_args = [arg for key, value in settings.items() for arg in ["-c", key + "=" + json.dumps(value)]]
        try:
            # Start in the old transport to prove existing saved chats can resume.
            final_only = True
            legacy_mode = "danger-full-access"
            legacy = subprocess.run([binary, "exec", "--sandbox", legacy_mode, "--skip-git-repo-check", "--json",
                                     "--model", "gpt-5.1-codex", *config_args, "Say fixture finished"],
                                    cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
            assert legacy.returncode == 0, (legacy.stdout, legacy.stderr)
            thread_id = next(json.loads(line)["thread_id"] for line in legacy.stdout.splitlines() if json.loads(line).get("type") == "thread.started")
            final_only = False
            cases = [("workspace-write", "allow", thread_id), ("read-only", "deny", None), ("read-only", "file", None)]
            if args.ssh:
                cases = [(mode, decision, thread_id if (mode, decision) == ("workspace-write", "allow") else None)
                         for mode in ("workspace-write", "read-only") for decision in ("allow", "deny")]
            else:
                cases += [("read-only", "mcp_allow", None), ("workspace-write", "mcp_deny", None),
                          ("danger-full-access", "mcp_auto", thread_id), ("danger-full-access", "mcp_legacy_auto", None)]
            mcp_args = [
                "-c", f"mcp_servers.probe.command={json.dumps(sys.executable)}",
                "-c", f"mcp_servers.probe.args={json.dumps([str(Path(__file__).with_name('mcp_confirmation_fixture.py').resolve()), str(root)])}",
                "-c", 'mcp_servers.probe.default_tools_approval_mode="approve"',
                "-c", 'mcp_servers.probe.tools.native_gate.approval_mode="prompt"',
                "-c", "mcp_servers.probe.required=true",
            ]
            for mode, decision, resume_id in cases:
                file_case = decision == "file"
                mcp_case = decision.startswith("mcp_")
                full_access = mode == "danger-full-access"
                for name in tool_names:
                    (root / name).unlink(missing_ok=True)
                full_write.unlink(missing_ok=True)
                outside_file.write_text("value: 0\n")
                stage = 0
                requests.clear()
                probes.clear()
                records = []
                finals = []
                events = []
                with (root / "stderr.log").open("w+") as errors, ExitStack() as session_context:
                    flags = config_args + (mcp_args if mcp_case else [])
                    if decision == "mcp_legacy_auto":
                        flags += ["-c", "features.tool_call_mcp_elicitation=false"]
                    proc = subprocess.Popen([binary, "app-server", *flags], cwd=workspace, env=environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)

                    def approve(published):
                        """Simulate the browser's explicit decision for every live request."""
                        records[:] = published
                        for record in published:
                            if record["status"] == "pending":
                                if mcp_case:
                                    if record["kind"] == "user_input":
                                        assert record["fields"][0]["id"] == "room", record
                                        session.decide(record["approval_id"], "allow", {"room": "kitchen"})
                                    else:
                                        assert not full_access, ("Unexpected Full access prompt", record)
                                        answer = "allow" if decision == "mcp_allow" else "deny"
                                        session.decide(record["approval_id"], answer)
                                else:
                                    answer = ("allow" if len(published) == 1 else "deny") if file_case else decision
                                    session.decide(record["approval_id"], answer)

                    session = session_context.enter_context(worker_approval_session(
                        proc, workspace, root, mode, resume_id, approve, finals, events,
                    ))
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
                if mcp_case:
                    for name in tool_names:
                        effect = root / name
                        if decision != "mcp_deny" or name == "choose_room":
                            assert effect.read_text() == "executed\n", (mode, name, diagnostic)
                        else:
                            assert not effect.exists(), (mode, name, diagnostic)
                    if full_access:
                        assert full_write.read_text() == "full access" and len(probes) == 1, diagnostic
                        assert [(record["kind"], record["status"]) for record in records] == [("user_input", "allowed")], records
                    else:
                        expected = "allowed" if decision == "mcp_allow" else "denied"
                        assert [record["status"] for record in records] == [expected, expected, "allowed"], records
                    continue
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
                    if not args.ssh:
                        assert "sandbox-command-started" in json.dumps(outputs), outputs
            if args.ssh:
                print("Pinned CLI SSH approvals passed: allow executes once, deny prevents execution, and subsequent SSH commands stay sandboxed in both modes.")
            else:
                print("Pinned CLI passed: sandboxed command, file, and MCP approvals, Full access grants and shell access, genuine input, and legacy resume.")
        finally:
            provider.shutdown()
            provider.server_close()


if __name__ == "__main__":
    main()
