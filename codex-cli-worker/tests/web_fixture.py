"""Local browser fixture. No Codex process or Home Assistant request is made."""
from __future__ import annotations

import importlib.util
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
        server.task_root = lambda: root / "tasks"
        server.TASK_STATE_FILE = root / "index.json"
        server.AGENTS_PATH = root / "AGENTS.md"
        server.CODEX_HOME = root / "codex-home"
        server.api_token = lambda: "preview-only"
        server.auth_status_payload = lambda: {"status": "authenticated", "message": "Signed in (local preview)"}
        server.fire_ha_event = lambda *args: (True, "")
        server.notify = lambda *args: None
        server.refresh_usage_status_async = lambda **kwargs: None
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

        def finish(task_id, prompt, session_id=session_id, reply=None):
            """Simulate a run with recorded steps instead of launching Codex.

            The first chat plays its steps slowly so the browser can show them
            live; every other chat completes at once, as the earlier fixture did.
            """
            summary = "Preview response: " + (reply or prompt)
            live = task_id == "preview-00"

            def run():
                server.update_task(task_id, status="running", started_at=server.utc_now())
                server.start_activity(task_id, server.tasks[task_id].get("current_turn_id") or "")
                for event in preview_events(summary):
                    if live:
                        time.sleep(0.4)
                    server.record_activity_event(task_id, event)
                    if live and event["type"] == "item.started":
                        # Hold the command open long enough for the browser to show it running.
                        time.sleep(2)
                server.update_task(task_id, status="completed", session_id=session_id, summary=summary,
                                   details="", question="", completed_at=server.utc_now())
                server.finish_activity(task_id)
                server.active_task_runners.discard(task_id)

            if live:
                threading.Thread(target=run, daemon=True).start()
            else:
                run()

        server.start_background_task = finish
        examples = [
            ("A quieter evening routine", "Review my evening lighting automation. Suggest improvements before making changes.",
             "Your evening routine looks good. There are two small changes that would make it easier to maintain.",
             "1. Use a single scene for the living room lights so brightness and color stay together.\n\n2. Add a condition that checks whether anyone is home before the routine runs.\n\nThe automation currently triggers at sunset. A 20-minute offset would keep the lights from coming on too early."),
            ("Check the energy dashboard", "Check my energy dashboard configuration.", "The dashboard configuration is valid.", "The electricity sensors are reporting the expected units."),
            ("Kitchen motion lights", "Review the kitchen motion sensor.", "The motion sensor is available.", ""),
        ]
        image_example = ("A sheep for the garden dashboard", "Generate a small cartoon image of a sheep on grass for my dashboard.",
                         "Here is a cartoon sheep standing on grass. It is attached below.", "")
        for index in range(25):
            title, message, summary, details = image_example if index == 2 else examples[index % 3]
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
            server.update_task(f"preview-{index:02}", title=title if index < 3 else f"Earlier chat {index}",
                               prompt=message, created_at=f"2026-09-{20 - index % 19:02}T10:00:00+00:00",
                               turns=[turn], current_turn_id=turn["turn_id"], status="completed",
                               session_id=task_session, summary=summary, details=details, question="",
                               attachments=attachments)
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
