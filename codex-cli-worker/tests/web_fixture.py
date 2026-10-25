"""Local browser fixture. No Codex process or Home Assistant request is made."""
from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import struct
import tempfile
import threading
import time
import zlib
from pathlib import Path

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple
from werkzeug.wrappers import Response


def preview_png(width=320, height=200):
    """Build a small PNG without third-party libraries."""
    def chunk(tag, data):
        """Frame one PNG chunk with its length and CRC."""
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            sky = y < height * 0.6
            row += bytes((150, 200, 240) if sky else (110, 170, 90))
            if not sky and (x // 16 + y // 16) % 2:
                row[-3:] = bytes((90, 150, 75))
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def main():
    """Serve the chat UI with simulated conversations under an Ingress-style prefix."""
    spec = importlib.util.spec_from_file_location("web_fixture_server", Path(__file__).resolve().parents[1] / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    with tempfile.TemporaryDirectory(prefix="codex-chat-preview-") as temp:
        root = Path(temp)
        server.DATA_ROOT = root
        server.task_root = lambda: root / "tasks"
        server.TASK_STATE_FILE = root / "index.json"
        server.MESSAGE_QUEUE_FILE = root / "queue.json"
        server.AGENTS_PATH = root / "AGENTS.md"
        server.CODEX_HOME = root / "codex-home"
        server.api_token = lambda: "preview-only"
        server.auth_status_payload = lambda: {"status": "authenticated", "message": "Signed in (local preview)"}
        server.fire_ha_event = lambda *args: (True, "")
        server.notify = lambda *args, **kwargs: None
        server.refresh_usage_status_async = lambda **kwargs: None
        from browser_permissions import describe, scope

        permission_scope = scope({"installation_id": "a" * 32, "url": "http://fixture:8123"})
        permission_requests = [
            ("websocket", {"type": "bubble_card_tools/list_modules"}),
            ("websocket", {"type": "subscribe_condition", "condition": {"condition": "state", "entity_id": "light.kitchen", "state": "on"}}),
            ("map_tiles", {"type": "map_tiles/raster"}),
            ("websocket", {"type": "custom/get", "query": "<img src=x onerror=alert(1)>"}),
        ]
        permission_rules = []
        for kind, body in permission_requests:
            # The controller's canonical form: sorted keys, compact, unescaped UTF-8.
            snapshot = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            rule = {"id": hashlib.sha256(f"{kind}\n{snapshot}".encode()).hexdigest(), "kind": kind, "request": snapshot}
            permission_rules.append({**rule, **describe(rule)})

        def permission_check(grants=()):
            """Make fixture evidence while the real worker handles approval and persistence."""
            allowed = {item["id"] for item in grants}
            pending = [item for item in permission_rules if item["id"] not in allowed]
            return {"operation": "dashboard", "check_id": server.uuid.uuid4().hex, "path": "/lovelace/home",
                    "status": "issues" if pending else "captured", "permission_scope": permission_scope,
                    "message": "Fixture dashboard inspection.", "permission_candidates": pending,
                    "permissions_used": sorted(allowed)}

        def permission_capture(task_id, payload, turn_id=None, capability=None, *, retry=None):
            """Simulate only capture, leaving the actual retry ownership and routes intact."""
            time.sleep(0.4)
            remembered = server.verification.permissions.rules(permission_scope, payload["path"])
            return permission_check([*remembered, *((retry or {}).get("grants") or [])])

        server.verification.browser = permission_capture
        # Connected unless POST fixture/integration says otherwise, so the line about
        # the integration stays out of the other checks.
        integration = {"state": "ok", "version": server.MIN_INTEGRATION_VERSION,
                       "minimum_version": server.MIN_INTEGRATION_VERSION}
        server.integration_status = lambda: dict(integration)
        # Plenty left of the 5-hour quota and almost none of the weekly one, so both bar colors show.
        server.usage_state.update(status="ok", five_hour_percent="64", five_hour_reset="19:20",
                                  weekly_percent="3", weekly_reset="12:00 on 8 Oct")
        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        sessions = server.CODEX_HOME / "sessions"
        sessions.mkdir(parents=True)
        (sessions / f"rollout-preview-{session_id}.jsonl").write_text("{}\n")

        def preview_events(text):
            """The `codex exec --json` lines a short review of the config would produce."""
            command = "/bin/sh -lc 'cat /config/automations.yaml'"
            return [
                {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "**Checking the automation**"}},
                {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "I'll read the automation before changing anything."}},
                {"type": "item.started", "item": {"id": "item_2", "type": "command_execution", "command": command,
                                                  "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
                {"type": "item.completed", "item": {"id": "item_2", "type": "command_execution", "command": command,
                                                    "aggregated_output": "- alias: Evening lights\n  trigger:\n    - platform: sun\n      event: sunset\n",
                                                    "exit_code": 0, "status": "completed"}},
                {"type": "item.completed", "item": {"id": "item_3", "type": "file_change", "status": "completed",
                                                    "changes": [{"path": "/config/automations.yaml", "kind": "update"}]}},
                {"type": "item.completed", "item": {"id": "item_4", "type": "agent_message",
                                                    "text": json.dumps({"status": "completed", "summary": text, "question": "", "details": ""})}},
            ]

        release = threading.Event()

        def finish(task_id, prompt, session_id=session_id, reply=None):
            """Simulate a run with recorded steps instead of launching Codex.

            The first chat plays its steps slowly so the browser can show them
            live. A message that starts with "Take your time" keeps its chat
            working until POST fixture/release, so the browser can queue
            messages behind it. A message that starts with "Ask me" is answered
            with a question and two choices. Every other chat completes at once.
            """
            summary = "Preview response: " + (reply or prompt)
            live = task_id == "preview-00"
            held = (reply or prompt).startswith("Take your time")
            asking = (reply or prompt).startswith("Ask me")
            permission = (reply or prompt).startswith("Request permission")

            def run():
                """Play the worker's and Codex's steps for one exchange, then finish it."""
                server.update_task(task_id, status="running", started_at=server.utc_now())
                server.start_activity(task_id, server.tasks[task_id].get("current_turn_id") or "")
                # The worker's own steps come first, as in a real run.
                server.start_phase(task_id, "baseline", "Noting the current state of your configuration files")
                if live:
                    time.sleep(1.5)
                server.finish_phase(task_id, "baseline")
                server.start_phase(task_id, "launch", "Starting Codex")
                started = [{"type": "thread.started", "thread_id": session_id}, {"type": "turn.started"}]
                for event in started + preview_events(summary):
                    if live:
                        time.sleep(0.4)
                    server.record_activity_event(task_id, event)
                    if live and event["type"] == "item.started":
                        # Hold the command open long enough for the browser to show it running.
                        time.sleep(2)
                if permission:
                    answered = threading.Event()

                    class PermissionInput(io.StringIO):
                        """Wake the fixture when the real approval endpoint sends its response."""

                        def write(self, value):
                            """Record the native response and release the simulated command."""
                            result = super().write(value)
                            answered.set()
                            return result

                        def close(self):
                            """Release a cancelled request without granting it."""
                            answered.set()

                    class PermissionProcess:
                        """A live process placeholder that supports normal worker cancellation."""

                        stdin = PermissionInput()
                        returncode = None

                        def poll(self):
                            """Stay live until the fixture or cancel route ends the exchange."""
                            return self.returncode

                        def terminate(self):
                            """Stop the simulated process on cancellation."""
                            self.returncode = -15
                            answered.set()

                        kill = terminate

                        def wait(self, timeout=None):
                            """Return the code after the cancellation route stops the process."""
                            return self.returncode

                    proc = PermissionProcess()
                    server.running_processes[task_id] = proc
                    session = server.create_approval_session(task_id, proc, reply or prompt,
                                                            server.get_run_dir(task_id) / "final.json", None, {}, server.read_options())
                    server.approval_sessions[task_id] = session
                    session.thread_id, session.turn_id = session_id, "preview-native"
                    file_permission = (reply or prompt).startswith("Request permission to rename")
                    hidden_permission = (reply or prompt).startswith("Request permission with hidden")
                    if file_permission:
                        session.receive({"method": "item/started", "params": {
                            "threadId": session_id, "turnId": "preview-native", "item": {
                                "id": "preview-command", "type": "fileChange", "changes": [{
                                    "path": "/other/original.yaml", "diff": "-name: Before\n+name: After",
                                    "kind": {"type": "update", "move_path": "/other/renamed.yaml"}}]}}})
                    session.receive({"id": 0, "method": "item/fileChange/requestApproval" if file_permission else "item/commandExecution/requestApproval", "params": {
                        "threadId": session_id, "turnId": "preview-native", "itemId": "preview-command",
                        "command": "tool --password 'private password'" if hidden_permission else "curl '$HA_URL/api/history/period' -H 'Authorization: Bearer $HA_TOKEN'\n# <img src=x onerror=alert(1)>",
                        "cwd": "/config", "reason": "Read historical sensor states from Home Assistant", "startedAtMs": 1,
                    }})
                    answered.wait(120)
                    session.close()
                    with server.lock:
                        server.approval_sessions.pop(task_id, None)
                        server.running_processes.pop(task_id, None)
                if held:
                    release.wait(120)
                    release.clear()
                server.start_phase(task_id, "review", "Checking the changes")
                if live:
                    time.sleep(0.4)
                server.finish_phase(task_id, "review")
                if asking:
                    server.update_task(
                        task_id, status="waiting_for_input", session_id=session_id, details="",
                        summary="**Evening lights** and **Evening lights 2** do the same thing. I would remove "
                                "`Evening lights 2` from `automations.yaml` and keep the other. Nothing has been changed yet.",
                        question="Remove the duplicate automation?",
                        choices=["Go ahead", "Don't change anything"])
                else:
                    server.update_task(task_id, status="completed", session_id=session_id, summary=summary,
                                       details="", question="", completed_at=server.utc_now())
                    if (reply or prompt).startswith("Inspect with blocked browser requests"):
                        server.update_task(task_id, verification=[permission_check()])
                server.finish_activity(task_id)
                server.active_task_runners.discard(task_id)
                # The real runner starts the next queued message when it ends.
                server.start_next_queued()

            if live or held or permission:
                threading.Thread(target=run, daemon=True).start()
            else:
                run()

        def release_held():
            """Let the chat that is being held finish."""
            release.set()
            return {"ok": True}

        def set_integration():
            """Set the state of the Codex integration that the status reports, from the "state" of the JSON body."""
            integration["state"] = str((server.request.get_json(silent=True) or {}).get("state") or "ok")
            return {"ok": True}

        def corrupt_permissions():
            """Make the private permissions file unreadable, as damage or a downgrade could."""
            (server.DATA_ROOT / "browser-permissions.json").write_text('{"broken": true}', encoding="utf-8")
            return {"ok": True}

        server.start_background_task = finish
        server.app.add_url_rule("/fixture/release", "fixture_release", release_held, methods=["POST"])
        server.app.add_url_rule("/fixture/integration", "fixture_integration", set_integration, methods=["POST"])
        server.app.add_url_rule("/fixture/browser-permissions/corrupt", "fixture_corrupt_permissions",
                                corrupt_permissions, methods=["POST"])
        examples = [
            ("A quieter evening routine", "Review my evening lighting automation. Suggest improvements before making changes.",
             "Your evening routine looks good. There are two small changes that would make it easier to maintain.",
             "1. Use a single scene for the living room lights so brightness and color stay together.\n\n2. Add a condition that checks whether anyone is home before the routine runs.\n\nThe automation currently triggers at sunset. A 20-minute offset would keep the lights from coming on too early."),
            ("Check the energy dashboard", "Check my energy dashboard configuration.", "The dashboard configuration is valid.", "The electricity sensors are reporting the expected units."),
            ("Kitchen motion lights", "Review the kitchen motion sensor.", "The motion sensor is available.", ""),
        ]
        image_example = ("A sheep for the garden dashboard", "Generate a small cartoon image of a sheep on grass for my dashboard.",
                         "Here is a cartoon sheep standing on grass. It is attached below.", "")
        # Markdown in a sent message and in an answer, as the chat formats both.
        markdown_example = (
            "Sunset offset for the evening lights",
            "Make `automation.evening_lights` start **20 minutes before sunset**. This is the trigger I have now:\n\n"
            "```yaml\ntrigger:\n  - platform: sun\n    event: sunset\n```",
            "**Done.** `automation.evening_lights` now starts 20 minutes before sunset.",
            "## What changed\n\n"
            "- Added an `offset` to the sun trigger in `automations.yaml`.\n"
            "- Left the *scene* and brightness settings as they were.\n\n"
            "```yaml\ntrigger:\n  - platform: sun\n    event: sunset\n    offset: \"-00:20:00\"\n"
            "action:\n  - service: notify.mobile_app\n    data:\n"
            "      message: \"The evening lights came on 20 minutes before sunset, as the routine now asks.\"\n```\n\n"
            "| Check | Result |\n|:--|:--|\n| Configuration | Valid |\n| `automation.evening_lights` | `on`<br>since 18:42 |\n\n"
            "### Next steps\n\n"
            "1. Reload automations from **Developer tools**.\n"
            "2. Watch the lights at sunset tonight.\n\n"
            "> A negative offset runs before the event, a positive one after it.\n\n"
            "See the [sun trigger documentation](https://www.home-assistant.io/docs/automation/trigger/#sun-trigger).",
        )
        for index in range(25):
            title, message, summary, details = (
                image_example if index == 2 else markdown_example if index == 4 else examples[index % 3])
            # Each saved chat owns its session, as in production, so deleting one leaves the others resumable.
            task_session = f"{session_id[:-4]}{index:04x}"
            (sessions / f"rollout-preview-{task_session}.jsonl").write_text("{}\n")
            turn = server.new_turn(message)
            turn["prompt_attachments"] = []
            if index == 1:
                # The user attached a screenshot of the card they asked about.
                upload_id = "7a3b9c1d2e4f5a6b7c8d9e0f1a2b3c4d"
                target = root / "tasks" / "preview-01" / "turns" / turn["turn_id"] / "attachments" / f"{upload_id}.png"
                target.parent.mkdir(parents=True)
                target.write_bytes(preview_png(240, 160))
                turn["prompt_attachments"] = [server.attachment_record(
                    "preview-01", upload_id, target, "image/png", name="energy-card.png", origin="user")]
            attachments = []
            if index == 2:
                attachment_id = "5f1d3c9e8b7a4c2d9e0f1a2b3c4d5e6f"
                target = root / "tasks" / "preview-02" / "turns" / turn["turn_id"] / "attachments" / f"{attachment_id}.png"
                target.parent.mkdir(parents=True)
                target.write_bytes(preview_png())
                attachments = [{"attachment_id": attachment_id, "kind": "image", "name": "codex-image-5f1d3c9e.png",
                                "mime_type": "image/png", "size": target.stat().st_size, "sha256": server.file_hash(target),
                                "created_at": "2026-09-18T10:00:00+00:00", "revised_prompt": "A cartoon sheep on grass",
                                "generation_id": "exec-preview", "path": target.relative_to(root / "tasks" / "preview-02").as_posix(),
                                "url": f"/tasks/preview-02/attachments/{attachment_id}"}]
            extra = {}
            if index == 1:
                extra["config_check"] = {"result": "valid", "errors": "", "warnings": ""}
                extra["verification"] = [
                    {"operation": "entity", "status": "passed", "entity_id": "sensor.energy", "state": "42", "expected_state": "42",
                     "message": "Fresh entity state readback.", "attributes": {"unit_of_measurement": "kWh"}},
                    {"operation": "dashboard", "status": "issues", "path": "/lovelace/energy",
                     "message": "Screenshots captured for visual inspection.", "errors": ["Custom element does not exist: sample-card"],
                     "blocked": ["WebSocket call_service"],
                     "findings": [
                         {"kind": "dashboard", "message": "Custom element does not exist: sample-card", "count": 2, "viewports": ["desktop", "mobile"]},
                         {"kind": "policy", "message": "WebSocket call_service (light.turn_on)", "count": 2, "viewports": ["desktop", "mobile"]},
                         {"kind": "diagnostic", "message": "WebSocket call_service (system_log.write)", "count": 4, "viewports": ["desktop", "mobile"]},
                     ]},
                ]
                shot_id = "a" * 32
                shot = root / "tasks" / "preview-01" / "turns" / turn["turn_id"] / "attachments" / f"{shot_id}.png"
                shot.write_bytes(preview_png(390, 844))
                extra["verification_attachments"] = [server.attachment_record(
                    "preview-01", shot_id, shot, "image/png", name="dashboard-mobile.png", origin="verification",
                    viewport="mobile", expires_at=time.time() + 3600)]
            if index == 3:
                # A YAML edit that Home Assistant rejected. Codex saved the previous
                # version of one file it changed, and none of the other.
                saved_copy = "/config/codex_tasks/preview-03/turns/x/backups/automations.yaml"
                extra.update(config_check={"result": "invalid", "warnings": "",
                                           "errors": "Invalid config for 'automation' at automations.yaml, line 12: required key 'trigger' not provided"},
                             validation_errors=["Home Assistant configuration check failed: required key 'trigger' not provided"],
                             recovery_files=[{"path": "automations.yaml", "copy": saved_copy}],
                             backups=[{"path": "automations.yaml", "status": "saved", "copy": saved_copy},
                                      {"path": "scripts.yaml", "status": "missing", "copy": ""}])
            server.update_task(f"preview-{index:02}", title=title if index < 3 or index == 4 else f"Earlier chat {index}",
                               prompt=message, created_at=f"2026-09-{20 - index % 19:02}T10:00:00+00:00",
                               turns=[turn], current_turn_id=turn["turn_id"],
                               status="failed" if index == 3 else "completed",
                               session_id=task_session, summary=summary, question="",
                               details="Validation errors: Home Assistant configuration check failed. Pre-change copies of the affected files are kept for 7 days at: `/config/codex_tasks/preview-03/turns/x/backups/automations.yaml`" if index == 3 else details,
                               attachments=attachments, **extra)
            server.tasks[f"preview-{index:02}"]["updated_at"] = f"2026-09-{20 - index % 19:02}T10:00:00+00:00"
            if index == 0:
                # The first chat keeps the steps of its last exchange, as a finished run would.
                server.start_activity("preview-00", turn["turn_id"])
                for event in preview_events(summary):
                    server.record_activity_event("preview-00", event)
                server.finish_activity("preview-00")
        # A prefix checks that every browser URL works under Home Assistant Ingress.
        mounted = DispatcherMiddleware(Response("Open /preview/", status=404), {"/preview": server.app})

        def ingress(environ, start_response):
            """Mark every request as if it arrived through Home Assistant Ingress."""
            environ["HTTP_X_INGRESS_PATH"] = "/preview"
            environ["REMOTE_ADDR"] = server.INGRESS_PROXY_IP
            return mounted(environ, start_response)

        run_simple("127.0.0.1", 9137, ingress, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
