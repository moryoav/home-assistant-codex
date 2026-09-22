"""Generated image capture, storage, serving and persistence regressions."""
from __future__ import annotations

import base64
import io
import json
import struct
import tempfile
import unittest
import zlib
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server

SESSION_ID = "019fc242-910a-7c92-a17d-54c014e19fc4"


def png_bytes(width=4, height=3, shade=200):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes([shade, 80, 40]) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def rollout_line(item_id, saved_path="", result=b"", status="completed", failure=None, revised="A cartoon sheep"):
    """Mirror the item_completed record Codex CLI 0.154.0 writes for image_gen.generation."""
    item = {"type": "Extension", "kind": "image_gen.generation", "id": item_id, "status": status,
            "revisedPrompt": revised, "result": base64.b64encode(result).decode() if result else "",
            "transparentBackground": False, "failure": failure, "savedPath": saved_path}
    return json.dumps({"timestamp": "2026-09-22T09:21:54.198Z", "ordinal": 22, "type": "event_msg",
                       "payload": {"type": "item_completed", "thread_id": SESSION_ID, "turn_id": "turn-1", "item": item},
                       "started_at_ms": 1}) + "\n"


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("tasks", {}), ("active_task_runners", set()), ("running_processes", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.stack.enter_context(patch.object(server, "CONFIG_ROOT", self.root / "config"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "start_background_task"))
        self.stack.enter_context(patch.object(server, "sandbox_readiness", return_value={"required": True, "ready": True}))
        self.stack.enter_context(patch.object(server, "read_options", return_value={"task_timeout_seconds": 30, "auto_save_lovelace": False}))
        self.stack.enter_context(patch.object(server, "build_manifest", return_value={}))
        self.stack.enter_context(patch.object(server, "validate_changed_files", return_value=[]))
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.events = []
        self.stack.enter_context(patch.object(server, "fire_ha_event", side_effect=lambda _event, data: (self.events.append(data) or True, "")))
        (self.root / "config").mkdir()
        self.rollout = server.CODEX_HOME / "sessions" / "2026" / "09" / "22" / f"rollout-2026-09-22T12-20-55-{SESSION_ID}.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_text("", encoding="utf-8")
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}

    def save_generated(self, name, data):
        directory = server.generated_images_dir(SESSION_ID)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        return path

    def append_rollout(self, *lines):
        with self.rollout.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))

    def create(self, prompt="Draw a sheep"):
        response = self.client.post("/tasks", json={"prompt": prompt}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.json["task_id"]

    def run_codex(self, task_id, *, on_wait=None, session_id=None, reply=None, final=None):
        final = final or {"status": "completed", "summary": "Here is your sheep.", "details": "", "question": ""}

        class FakeProcess:
            def __init__(self, args, **kwargs):
                self.stdin = io.StringIO()
                self.stderr = io.StringIO("")
                self.stdout = io.StringIO(json.dumps({"type": "thread.started", "thread_id": SESSION_ID}) + "\n")
                self.returncode = 0
                self.output = Path(args[args.index("--output-last-message") + 1])

            def poll(self):
                return 0

            def wait(self, timeout=None):
                if on_wait:
                    on_wait()
                self.output.write_text(json.dumps(final), encoding="utf-8")
                return 0

        with patch.object(server.subprocess, "Popen", side_effect=FakeProcess):
            server.run_task(task_id, "Draw a sheep", session_id, reply)
        server.active_task_runners.discard(task_id)

    def generate_first_image(self, task_id, name="exec-1.png", data=None):
        data = data or png_bytes()

        def on_wait():
            path = self.save_generated(name, data)
            self.append_rollout(rollout_line(name.removesuffix(".png"), saved_path=str(path)))

        self.run_codex(task_id, on_wait=on_wait)
        return data

    def test_generated_image_is_attached_served_and_survives_restart(self):
        task_id = self.create()
        png = self.generate_first_image(task_id)
        task = server.tasks[task_id]
        self.assertEqual(task["status"], "completed")
        attachments = task["attachments"]
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertEqual(attachment["kind"], "image")
        self.assertEqual(attachment["mime_type"], "image/png")
        self.assertEqual(attachment["size"], len(png))
        self.assertEqual(attachment["revised_prompt"], "A cartoon sheep")
        self.assertEqual(attachment["generation_id"], "exec-1")
        self.assertEqual(attachment["url"], f"/tasks/{task_id}/attachments/{attachment['attachment_id']}")
        self.assertTrue(attachment["path"].startswith(f"turns/{task['current_turn_id']}/attachments/"))
        stored = server.get_task_dir(task_id) / attachment["path"]
        self.assertEqual(stored.read_bytes(), png)
        self.assertEqual(task["turns"][0]["attachments"], attachments)
        self.assertEqual(self.events[-1]["attachments"], attachments)
        self.assertNotIn("attachments", self.events[-1]["response"])
        # The original stays where Codex saved it so follow-up edits keep working.
        self.assertTrue((server.generated_images_dir(SESSION_ID) / "exec-1.png").exists())

        response = self.client.get(attachment["url"], headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, png)
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("attachment", response.headers.get("Content-Disposition", ""))
        download = self.client.get(attachment["url"] + "?download=1", headers=self.headers)
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        self.assertIn(attachment["name"], download.headers["Content-Disposition"])
        self.assertEqual(self.client.get(attachment["url"]).status_code, 401)
        self.assertEqual(self.client.get(f"/tasks/{task_id}/attachments/{'0' * 32}", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f"/tasks/{task_id}/attachments/not-an-id", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f"/tasks/missing/attachments/{attachment['attachment_id']}", headers=self.headers).status_code, 404)

        server.tasks.clear()
        server.load_task_index()
        loaded = self.client.get(f"/tasks/{task_id}", headers=self.headers).json["task"]
        self.assertEqual(loaded["turns"][0]["attachments"], attachments)
        self.assertEqual(loaded["attachments"], attachments)
        self.assertEqual(self.client.get(attachment["url"], headers=self.headers).status_code, 200)
        summary = self.client.get("/tasks?summary=true", headers=self.headers).json["tasks"][0]
        self.assertNotIn("attachments", summary)

        stored.unlink()
        missing = self.client.get(attachment["url"], headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        self.assertIn("missing", missing.json["error"])
        stored.write_bytes(b"not an image any more")
        self.assertEqual(self.client.get(attachment["url"], headers=self.headers).status_code, 404)

    def test_resumed_turn_attaches_only_new_images_and_keeps_history(self):
        task_id = self.create()
        self.generate_first_image(task_id)
        first = server.tasks[task_id]["turns"][0]["attachments"]
        response = self.client.post(f"/tasks/{task_id}/continue", json={"message": "Make it blue"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.tasks[task_id]["attachments"], [])
        self.assertEqual(server.tasks[task_id]["turns"][0]["attachments"], first)
        blue = png_bytes(shade=20)

        def on_wait():
            path = self.save_generated("exec-2.png", blue)
            self.append_rollout(rollout_line("exec-2", saved_path=str(path), revised="A blue sheep"))

        self.run_codex(task_id, on_wait=on_wait, session_id=SESSION_ID, reply="Make it blue")
        task = server.tasks[task_id]
        self.assertEqual([item["generation_id"] for item in task["turns"][1]["attachments"]], ["exec-2"])
        self.assertEqual(task["turns"][0]["attachments"], first)
        self.assertEqual(task["attachments"], task["turns"][1]["attachments"])
        self.assertEqual(self.client.get(task["attachments"][0]["url"], headers=self.headers).data, blue)
        self.assertEqual(self.client.get(first[0]["url"], headers=self.headers).status_code, 200)
        first_dir = server.get_task_dir(task_id) / Path(first[0]["path"]).parent
        second_dir = server.get_task_dir(task_id) / Path(task["attachments"][0]["path"]).parent
        self.assertNotEqual(first_dir, second_dir)

    def test_inline_result_fallback_and_unusable_items_are_skipped(self):
        task_id = self.create()
        png = png_bytes()
        outside = self.root / "config" / "secrets.yaml"
        outside.write_bytes(png)

        def on_wait():
            text_file = self.save_generated("exec-text.png", b"not an image at all")
            self.append_rollout(
                "not json\n",
                json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "id": "m"}}}) + "\n",
                rollout_line("exec-inline", saved_path=str(self.root / "codex" / "generated_images" / SESSION_ID / "gone.png"), result=png),
                rollout_line("exec-outside", saved_path=str(outside)),
                rollout_line("exec-text", saved_path=str(text_file)),
                rollout_line("exec-failed", saved_path=str(text_file), status="failed", failure={"message": "blocked"}),
                rollout_line("exec-empty"),
            )

        self.run_codex(task_id, on_wait=on_wait)
        task = server.tasks[task_id]
        self.assertEqual([item["generation_id"] for item in task["attachments"]], ["exec-inline"])
        self.assertEqual(self.client.get(task["attachments"][0]["url"], headers=self.headers).data, png)
        self.assertEqual(task["status"], "completed")
        log = (server.get_task_dir(task_id) / "codex.log").read_text(encoding="utf-8")
        for item_id in ("exec-outside", "exec-text", "exec-failed", "exec-empty"):
            self.assertIn(f"Skipped image generation {item_id}", log)

    def test_size_and_count_limits_are_enforced(self):
        task_id = self.create()
        small = png_bytes()
        large = png_bytes(width=64, height=64)

        def on_wait():
            paths = [self.save_generated(name, data) for name, data in (("a.png", small), ("b.png", large), ("c.png", small))]
            self.append_rollout(*(rollout_line(path.stem, saved_path=str(path)) for path in paths))

        with patch.object(server, "ATTACHMENT_MAX_BYTES", len(large) - 1), patch.object(server, "ATTACHMENTS_PER_TURN_MAX", 1):
            self.run_codex(task_id, on_wait=on_wait)
        self.assertEqual([item["generation_id"] for item in server.tasks[task_id]["attachments"]], ["a"])
        log = (server.get_task_dir(task_id) / "codex.log").read_text(encoding="utf-8")
        self.assertIn("Skipped image generation b: attachment limit reached", log)

    def test_attachment_paths_outside_the_task_directory_are_rejected(self):
        task_id = self.create()
        server.active_task_runners.discard(task_id)
        png = png_bytes()
        (self.root / "outside.png").write_bytes(png)
        attempts = ["../../outside.png", str(self.root / "outside.png"), ""]
        for index, relative in enumerate(attempts):
            attachment_id = f"{index:032x}"
            server.update_task(task_id, status="completed", session_id=SESSION_ID, attachments=[{
                "attachment_id": attachment_id, "kind": "image", "mime_type": "image/png", "name": "x.png", "path": relative,
            }])
            response = self.client.get(f"/tasks/{task_id}/attachments/{attachment_id}", headers=self.headers)
            self.assertEqual(response.status_code, 404, relative)

    def test_rollout_parsing_and_prompt_guidance(self):
        self.append_rollout(rollout_line("exec-1", saved_path="/x/1.png", status="generating"), rollout_line("exec-1", saved_path="/x/1.png"))
        items = server.image_generation_items(SESSION_ID)
        self.assertEqual([(item["id"], item["status"], item["saved_path"]) for item in items], [("exec-1", "completed", "/x/1.png")])
        self.assertEqual(server.image_generation_items("not-a-session"), [])
        self.assertEqual(server.image_generation_items(""), [])
        self.assertIsNone(server.sniff_image(b"plain text"))
        self.assertEqual(server.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ("image/webp", ".webp"))
        prompt = server.build_prompt("Draw a sheep", "task")
        self.assertIn("image generation tool", prompt)
        self.assertIn("attached to this conversation", prompt)

    def test_cancellation_and_launch_failure_clear_attachments(self):
        task_id = self.create()
        self.generate_first_image(task_id)
        self.assertEqual(len(server.tasks[task_id]["attachments"]), 1)
        self.client.post(f"/tasks/{task_id}/continue", json={"message": "Again"}, headers=self.headers)
        accepted, _, _ = server.request_task_cancellation(task_id)
        self.assertTrue(accepted)
        self.assertEqual(server.tasks[task_id]["attachments"], [])
        self.assertEqual(len(server.tasks[task_id]["turns"][0]["attachments"]), 1)
        self.assertEqual(server.tasks[task_id]["turns"][1]["attachments"], [])


if __name__ == "__main__":
    unittest.main()
