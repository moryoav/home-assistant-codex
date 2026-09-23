"""Conversation persistence and continuation API regressions."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("tasks", {}), ("active_task_runners", set()), ("running_processes", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.runner = self.stack.enter_context(patch.object(server, "start_background_task"))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}
        self.session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        session_dir = server.CODEX_HOME / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        self.session_file = session_dir / f"rollout-2026-09-20T01-00-00-{self.session_id}.jsonl"
        self.session_file.write_text('{}\n')

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.headers)

    def create(self, message="Review my automation"):
        response = self.post("/tasks", {"prompt": message})
        self.assertEqual(response.status_code, 200)
        return response.json["task_id"]

    def finish(self, task_id, status="completed", summary="First answer"):
        server.update_task(task_id, status=status, session_id=self.session_id, summary=summary, completed_at=server.utc_now())
        server.active_task_runners.discard(task_id)

    def test_continue_preserves_exchanges_and_survives_restart(self):
        task_id = self.create()
        self.finish(task_id)
        original = copy.deepcopy(server.tasks[task_id]["turns"][0])
        response = self.post(f"/tasks/{task_id}/continue", {"message": "Apply that suggestion"})
        self.assertEqual(response.status_code, 200)
        self.runner.assert_called_with(task_id, "Review my automation", session_id=self.session_id, reply="Apply that suggestion")
        self.assertEqual(server.tasks[task_id]["turns"][0], original)
        self.assertEqual(server.tasks[task_id]["summary"], "")
        self.finish(task_id, summary="Second answer")
        server.tasks.clear()
        server.load_task_index()
        task = self.client.get(f"/tasks/{task_id}", headers=self.headers).json["task"]
        self.assertEqual([turn["summary"] for turn in task["turns"]], ["First answer", "Second answer"])
        self.assertEqual([turn["message"] for turn in task["turns"]], ["Review my automation", "Apply that suggestion"])
        self.assertTrue(task["can_continue"])
        self.assertFalse(task["history_incomplete"])

    def test_all_terminal_statuses_can_continue(self):
        for status in server.CONTINUABLE_STATUSES:
            with self.subTest(status=status):
                task_id = self.create()
                self.finish(task_id, status=status)
                if status == "cancelled":
                    server.tasks[task_id]["cancellation_requested"] = True
                response = self.post(f"/tasks/{task_id}/continue", {"message": "Continue"})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(server.tasks[task_id]["cancellation_requested"])
                self.assertEqual(server.tasks[task_id]["status"], "queued")
                self.finish(task_id)

    def test_model_settings_persist_and_apply_to_new_and_resumed_runs(self):
        settings = {"model": "gpt-6-astra", "reasoning_effort": "ultra"}
        response = self.post("/tasks", {"prompt": "Review", "chat_settings": settings})
        self.assertEqual(response.status_code, 200)
        task_id = response.json["task_id"]
        args = server.build_codex_args(task_id, self.root / "prompt", self.root / "final", None)
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-astra")
        self.assertIn('model_reasoning_effort="ultra"', args)
        self.finish(task_id)
        first = copy.deepcopy(server.tasks[task_id]["turns"][0])
        settings = {"model": "gpt-5.6-luna", "reasoning_effort": "max"}
        with patch.object(server, "utc_now", return_value="2027-01-01T00:00:00+00:00"):
            response = self.post(f"/tasks/{task_id}/settings", {"chat_settings": settings})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.tasks[task_id]["turns"][0], first)
        server.tasks.clear()
        server.load_task_index()
        task = self.client.get(f"/tasks/{task_id}", headers=self.headers).json["task"]
        self.assertEqual(task["chat_settings"], settings)
        self.assertEqual(self.post(f"/tasks/{task_id}/continue", {"message": "Next"}).status_code, 200)
        args = server.build_codex_args(task_id, self.root / "prompt", self.root / "final", self.session_id)
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.6-luna")
        self.assertIn('model_reasoning_effort="max"', args)
        self.assertEqual(args[-3:], ["resume", self.session_id, "-"])
        self.assertEqual(server.tasks[task_id]["turns"][0], first)

    def test_settings_inherit_defaults_without_leaking_between_chats(self):
        options = {"codex_model": "gpt-5.6-sol", "model_reasoning_effort": "high"}
        with patch.object(server, "read_options", return_value=options):
            task_id = self.create()
            self.assertEqual(server.tasks[task_id]["turns"][0]["execution_settings"], {"model": "gpt-5.6-sol", "reasoning_effort": "high"})
            # An option edit after queuing must not change the queued run.
            options["codex_model"] = "gpt-5.6-terra"
            args = server.build_codex_args(task_id, self.root / "p", self.root / "f", None)
            self.assertEqual(args[args.index("--model") + 1], "gpt-5.6-sol")
            self.finish(task_id)
            self.post(f"/tasks/{task_id}/settings", {"chat_settings": {"model": "gpt-6-astra", "reasoning_effort": "max"}})
            other = self.create("Separate chat")
            self.assertEqual(server.tasks[other]["chat_settings"], server.DEFAULT_CHAT_SETTINGS)
            self.assertEqual(server.tasks[other]["turns"][0]["execution_settings"]["model"], "gpt-5.6-terra")
            self.finish(other)
            response = self.post(f"/tasks/{task_id}/continue", {"message": "Reset", "chat_settings": {"model": None, "reasoning_effort": None}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(server.tasks[task_id]["turns"][-1]["execution_settings"], {"model": "gpt-5.6-terra", "reasoning_effort": "high"})
            self.assertEqual(options, {"codex_model": "gpt-5.6-terra", "model_reasoning_effort": "high"})

    def test_invalid_settings_are_rejected_without_mutation(self):
        invalid = [None, [], "high", {"model": []}, {"model": "invented"},
                   {"reasoning_effort": {}}, {"reasoning_effort": "minimal"},
                   {"model": "gpt-5.5", "reasoning_effort": "max"},
                   {"model": "gpt-5.6-luna", "reasoning_effort": "ultra"},
                   {"codex_sandbox": "danger-full-access"}]
        for settings in invalid:
            with self.subTest(settings=settings):
                self.assertEqual(self.post("/tasks", {"prompt": "Review", "chat_settings": settings}).status_code, 400)
        self.assertFalse(server.tasks)
        self.runner.assert_not_called()
        task_id = self.create()
        self.finish(task_id)
        before = copy.deepcopy(server.tasks[task_id])
        self.runner.reset_mock()
        for settings in invalid:
            self.assertEqual(self.post(f"/tasks/{task_id}/settings", {"chat_settings": settings}).status_code, 400)
            self.assertEqual(self.post(f"/tasks/{task_id}/continue", {"message": "Next", "chat_settings": settings}).status_code, 400)
        self.assertEqual(server.tasks[task_id], before)
        self.runner.assert_not_called()

    def test_settings_auth_active_task_guard_and_catalog(self):
        self.assertEqual(self.client.get("/chat-options").status_code, 401)
        self.assertEqual(self.client.post("/tasks/missing/settings", json={"chat_settings": {}}).status_code, 401)
        self.assertEqual(self.post("/tasks/missing/settings", {"chat_settings": {}}).status_code, 404)
        task_id = self.create()
        self.assertEqual(self.post(f"/tasks/{task_id}/settings", {"chat_settings": {}}).status_code, 409)
        catalog = self.client.get("/chat-options", headers=self.headers).json
        self.assertEqual(len(catalog["models"]), 5)
        self.assertNotIn("HA_TOKEN", json.dumps(catalog))
        self.assertEqual(catalog["models"][-1]["efforts"], ["low", "medium", "high", "xhigh"])
        with patch.object(server, "read_options", return_value={"codex_model": "gpt-6-astra", "model_reasoning_effort": "minimal"}):
            catalog = self.client.get("/chat-options", headers=self.headers).json
            self.assertEqual(catalog["defaults"]["reasoning_effort"], "medium")

    def test_failed_settings_write_keeps_previous_selection(self):
        task_id = self.create()
        self.finish(task_id)
        before = copy.deepcopy(server.tasks[task_id])
        with patch.object(server, "atomic_json_write", side_effect=OSError("disk full")):
            response = self.post(f"/tasks/{task_id}/settings", {"chat_settings": {"model": "gpt-6-astra"}})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(server.tasks[task_id], before)

    def test_legacy_reply_remains_waiting_only(self):
        task_id = self.create()
        self.finish(task_id)
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"reply": "More"}).status_code, 409)
        self.finish(task_id, "waiting_for_input")
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"reply": "More"}).status_code, 200)

    def test_missing_session_never_starts_fresh_or_changes_history(self):
        task_id = self.create()
        self.finish(task_id)
        self.session_file.unlink()
        before = copy.deepcopy(server.tasks[task_id])
        self.runner.reset_mock()
        response = self.post(f"/tasks/{task_id}/continue", {"message": "More"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("unavailable", response.json["error"])
        self.assertEqual(server.tasks[task_id], before)
        self.runner.assert_not_called()

    def test_active_task_and_duplicate_continue_are_rejected(self):
        task_id = self.create()
        self.assertEqual(self.post(f"/tasks/{task_id}/continue", {"message": "More"}).status_code, 409)
        self.finish(task_id)
        other = self.create("Another request")
        self.assertEqual(self.post(f"/tasks/{task_id}/continue", {"message": "More"}).status_code, 409)
        self.finish(other)
        self.assertEqual(self.post(f"/tasks/{task_id}/continue", {"message": "More"}).status_code, 200)
        self.assertEqual(self.post(f"/tasks/{task_id}/continue", {"message": "Duplicate"}).status_code, 409)
        self.assertEqual(len(server.tasks[task_id]["turns"]), 2)

    def test_recent_summary_pagination_and_status_filters(self):
        ids = []
        for index in range(3):
            task_id = self.create(f"Prompt {index}")
            self.finish(task_id)
            server.tasks[task_id]["updated_at"] = f"2026-09-2{index}T12:00:00+00:00"
            ids.append(task_id)
        page = self.client.get("/tasks?summary=true&order=updated_desc&limit=2", headers=self.headers).json
        self.assertEqual([item["task_id"] for item in page["tasks"]], ids[::-1][:2])
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual(page["total"], 3)
        self.assertNotIn("turns", page["tasks"][0])
        self.assertNotIn("prompt", page["tasks"][0])
        page2 = self.client.get("/tasks?order=updated_desc&limit=2&offset=2", headers=self.headers).json
        self.assertEqual(page2["tasks"][0]["task_id"], ids[0])
        self.assertIsNone(page2["next_offset"])
        empty = self.client.get("/tasks?status=failed", headers=self.headers).json
        self.assertEqual(empty["tasks"], [])
        full = self.client.get("/tasks", headers=self.headers).json
        self.assertEqual(len(full["tasks"]), 3)
        self.assertIn("prompt", full["tasks"][0])

    def test_invalid_filters_and_message_bodies(self):
        for query in ("limit=0", "limit=501", "offset=-1", "limit=no", "status=no", "order=no"):
            self.assertEqual(self.client.get("/tasks?" + query, headers=self.headers).status_code, 400)
        for body in ([], {"prompt": []}, {"prompt": " "}):
            self.assertEqual(self.post("/tasks", body).status_code, 400)
        for body in ([], {"message": {}}, {"message": " "}):
            self.assertEqual(self.post("/tasks/missing/continue", body).status_code, 400)
        self.assertEqual(self.client.get("/tasks").status_code, 401)
        self.assertEqual(self.client.post("/tasks/missing/continue", json={"message": "test"}).status_code, 401)

    def test_legacy_history_is_honest_and_retained_when_continuing(self):
        server.tasks["old"] = {"task_id": "old", "prompt": "Original", "status": "completed", "session_id": self.session_id,
                               "summary": "Latest only", "reply_history": [{"at": "2026-09-19", "reply": "Follow-up"}]}
        data = self.client.get("/tasks/old", headers=self.headers).json["task"]
        self.assertTrue(data["history_incomplete"])
        self.assertNotIn("summary", data["turns"][0])
        self.assertEqual(data["turns"][1]["summary"], "Latest only")
        self.assertEqual(self.post("/tasks/old/continue", {"message": "Next"}).status_code, 200)
        self.assertTrue(server.tasks["old"]["history_incomplete"])
        self.assertEqual(len(server.tasks["old"]["turns"]), 3)

    def test_restart_marks_current_exchange_failed(self):
        task_id = self.create()
        server.tasks.clear()
        server.active_task_runners.clear()
        server.load_task_index()
        self.assertEqual(server.tasks[task_id]["status"], "failed")
        self.assertEqual(server.tasks[task_id]["turns"][0]["status"], "failed")
        self.assertIn("restarted", server.tasks[task_id]["turns"][0]["summary"])

    def test_cancellation_updates_only_current_exchange(self):
        task_id = self.create()
        self.finish(task_id)
        self.post(f"/tasks/{task_id}/continue", {"message": "More"})
        accepted, _, _ = server.request_task_cancellation(task_id)
        self.assertTrue(accepted)
        self.assertEqual(server.tasks[task_id]["turns"][0]["status"], "completed")
        self.assertEqual(server.tasks[task_id]["turns"][1]["status"], "cancelled")

    def test_followup_prompt_does_not_reissue_original_request(self):
        prompt = server.build_prompt("ORIGINAL_REQUEST", "task", reply="CURRENT_MESSAGE")
        self.assertIn("CURRENT_MESSAGE", prompt)
        self.assertNotIn("ORIGINAL_REQUEST", prompt)

    def test_runs_keep_separate_artifacts_and_changes(self):
        task_id = self.create()
        output_dirs = []
        manifests = [{}, {"first.yaml": {"sha256": "one"}}, {"first.yaml": {"sha256": "one"}}, {"first.yaml": {"sha256": "one"}, "second.yaml": {"sha256": "two"}}]

        class FakeProcess:
            def __init__(self, args, **kwargs):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(json.dumps({"type": "thread.started", "thread_id": self_session}) + "\n")
                self.stderr = io.StringIO("")
                self.returncode = 0
                self.output = Path(args[args.index("--output-last-message") + 1])
                output_dirs.append(self.output.parent)
            def poll(self): return 0
            def wait(self, timeout=None):
                self.output.write_text(json.dumps({"status": "completed", "summary": "Saved response", "details": "", "question": ""}))
                return 0

        self_session = self.session_id
        with (
            patch.object(server, "CONFIG_ROOT", self.root),
            patch.object(server, "build_manifest", side_effect=manifests),
            patch.object(server, "sandbox_readiness", return_value={"required": True, "ready": True}),
            patch.object(server, "read_options", return_value={"task_timeout_seconds": 30, "auto_save_lovelace": False}),
            patch.object(server, "validate_changed_files", return_value=[]),
            patch.object(server.subprocess, "Popen", side_effect=FakeProcess),
            patch.object(server, "fire_ha_event", return_value=(True, "")),
            patch.object(server, "notify"), patch.object(server, "refresh_usage_status_async"),
        ):
            server.run_task(task_id, "Review my automation")
            server.active_task_runners.discard(task_id)
            self.post(f"/tasks/{task_id}/continue", {"message": "Next change"})
            server.run_task(task_id, "Review my automation", self.session_id, "Next change")
        self.assertNotEqual(output_dirs[0], output_dirs[1])
        self.assertTrue((output_dirs[0] / "final.json").exists())
        self.assertTrue((output_dirs[1] / "final-resume.json").exists())
        self.assertEqual(server.tasks[task_id]["turns"][0]["changes"]["added"], ["first.yaml"])
        self.assertEqual(server.tasks[task_id]["turns"][1]["changes"]["added"], ["second.yaml"])

    def test_pin_persists_without_reordering_and_lists_first(self):
        """Pinning persists, keeps the recent order, and lists pinned chats first."""
        ids = []
        for index in range(3):
            task_id = self.create(f"Prompt {index}")
            self.finish(task_id)
            server.tasks[task_id]["updated_at"] = f"2026-09-2{index}T12:00:00+00:00"
            ids.append(task_id)
        oldest = ids[0]
        before = copy.deepcopy(server.tasks[oldest])
        for body in ({}, [], {"pinned": "yes"}, {"pinned": 1}):
            self.assertEqual(self.post(f"/tasks/{oldest}/pin", body).status_code, 400)
        self.assertEqual(self.post("/tasks/missing/pin", {"pinned": True}).status_code, 404)
        self.assertEqual(self.client.post(f"/tasks/{oldest}/pin", json={"pinned": True}).status_code, 401)
        self.assertEqual(server.tasks[oldest], before)
        with patch.object(server, "utc_now", return_value="2027-01-01T00:00:00+00:00"):
            response = self.post(f"/tasks/{oldest}/pin", {"pinned": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "task_id": oldest, "pinned": True})
        self.assertEqual(server.tasks[oldest]["updated_at"], before["updated_at"])
        self.assertEqual(server.tasks[oldest]["turns"], before["turns"])
        recent = self.client.get("/tasks?summary=true&order=updated_desc", headers=self.headers).json["tasks"]
        self.assertEqual([item["task_id"] for item in recent], ids[::-1])
        self.assertEqual([item["pinned"] for item in recent], [False, False, True])
        pinned_first = self.client.get("/tasks?summary=true&order=pinned_first&limit=2", headers=self.headers).json
        self.assertEqual([item["task_id"] for item in pinned_first["tasks"]], [oldest, ids[2]])
        self.assertEqual(pinned_first["next_offset"], 2)
        created = self.client.get("/tasks", headers=self.headers).json["tasks"]
        self.assertEqual([item["task_id"] for item in created], sorted(ids, key=lambda i: (server.tasks[i]["created_at"], i)))
        server.tasks.clear()
        server.load_task_index()
        self.assertTrue(server.tasks[oldest]["pinned"])
        self.assertEqual(self.post(f"/tasks/{oldest}/pin", {"pinned": False}).json["pinned"], False)
        server.tasks.clear()
        server.load_task_index()
        self.assertFalse(server.tasks[oldest]["pinned"])
        # Pinning a running chat is allowed and must not disturb its run state.
        running = self.create("Working")
        self.assertEqual(self.post(f"/tasks/{running}/pin", {"pinned": True}).status_code, 200)
        self.assertEqual(server.tasks[running]["status"], "queued")
        self.finish(running)
        self.assertTrue(server.tasks[running]["pinned"])
        self.assertEqual(server.tasks[running]["status"], "completed")

    def test_rename_validates_and_persists_without_touching_history(self):
        """Renaming validates the title and saves it without touching the chat history."""
        task_id = self.create()
        self.finish(task_id)
        before = copy.deepcopy(server.tasks[task_id])
        for body in ({}, [], {"title": ""}, {"title": "   \n\t "}, {"title": 5}, {"title": "x" * 201}):
            self.assertEqual(self.post(f"/tasks/{task_id}/title", body).status_code, 400)
        self.assertEqual(self.post("/tasks/missing/title", {"title": "New"}).status_code, 404)
        self.assertEqual(self.client.post(f"/tasks/{task_id}/title", json={"title": "New"}).status_code, 401)
        self.assertEqual(server.tasks[task_id], before)
        with patch.object(server, "utc_now", return_value="2027-01-01T00:00:00+00:00"):
            response = self.post(f"/tasks/{task_id}/title", {"title": "  Kitchen\n  lights   plan  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["title"], "Kitchen lights plan")
        self.assertEqual(server.tasks[task_id]["updated_at"], before["updated_at"])
        self.assertEqual(server.tasks[task_id]["turns"], before["turns"])
        self.assertEqual(self.post(f"/tasks/{task_id}/title", {"title": "x" * 200}).status_code, 200)
        server.tasks.clear()
        server.load_task_index()
        task = self.client.get(f"/tasks/{task_id}", headers=self.headers).json["task"]
        self.assertEqual(task["title"], "x" * 200)
        summary = self.client.get("/tasks?summary=true", headers=self.headers).json["tasks"][0]
        self.assertEqual(summary["title"], "x" * 200)
        with patch.object(server, "atomic_json_write", side_effect=OSError("disk full")):
            self.assertEqual(self.post(f"/tasks/{task_id}/title", {"title": "Lost"}).status_code, 500)
            self.assertEqual(self.post(f"/tasks/{task_id}/pin", {"pinned": True}).status_code, 500)
        self.assertEqual(server.tasks[task_id]["title"], "x" * 200)
        self.assertNotIn("pinned", server.tasks[task_id])

    def test_delete_removes_files_session_and_index_but_not_active_chats(self):
        """Deleting removes the files, session, and index entry, and refuses active chats."""
        task_id = self.create()
        self.assertEqual(self.client.delete(f"/tasks/{task_id}", headers=self.headers).status_code, 409)
        self.finish(task_id)
        other = self.create("Keep me")
        self.finish(other)
        (server.CODEX_HOME / "generated_images" / self.session_id).mkdir(parents=True)
        task_dir = server.get_task_dir(task_id)
        self.assertTrue((task_dir / "task.json").exists())
        self.assertEqual(self.client.delete(f"/tasks/{task_id}").status_code, 401)
        self.assertEqual(self.client.delete("/tasks/missing", headers=self.headers).status_code, 404)
        with patch.object(server, "active_task_runners", {task_id}):
            self.assertEqual(self.client.delete(f"/tasks/{task_id}", headers=self.headers).status_code, 409)
        self.assertIn(task_id, server.tasks)
        with patch.object(server.shutil, "rmtree", side_effect=OSError("busy")):
            self.assertEqual(self.client.delete(f"/tasks/{task_id}", headers=self.headers).status_code, 500)
        self.assertIn(task_id, server.tasks)
        self.assertTrue(self.session_file.exists())
        response = self.client.delete(f"/tasks/{task_id}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "task_id": task_id, "deleted": True})
        self.assertNotIn(task_id, server.tasks)
        self.assertFalse(task_dir.exists())
        self.assertFalse(self.session_file.exists())
        self.assertFalse((server.CODEX_HOME / "generated_images" / self.session_id).exists())
        self.assertNotIn(task_id, json.loads(server.TASK_STATE_FILE.read_text(encoding="utf-8")))
        self.assertEqual(self.client.get(f"/tasks/{task_id}", headers=self.headers).status_code, 404)
        server.tasks.clear()
        server.load_task_index()
        self.assertEqual(list(server.tasks), [other])
        self.assertTrue(server.get_task_dir(other).exists())

    def test_web_assets_are_packaged_and_load_without_account_calls(self):
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get("/assets/index.html").status_code, 404)
        response = self.client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/test"}, environ_overrides={"REMOTE_ADDR": server.INGRESS_PROXY_IP})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'assets/chat.js', response.data)
        for name in ("chat.js", "chat.css"):
            with self.client.get("/assets/" + name) as asset:
                self.assertEqual(asset.status_code, 200)


if __name__ == "__main__":
    unittest.main()
