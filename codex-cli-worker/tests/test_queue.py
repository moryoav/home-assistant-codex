"""Messages sent while another chat is working wait in a queue and start in order."""
from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode()


class QueueTests(unittest.TestCase):
    def setUp(self):
        """Give each test an empty worker with temporary storage and no real runs or events."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("tasks", {}), ("active_task_runners", set()), ("running_processes", {}), ("message_queue", [])):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "MESSAGE_QUEUE_FILE", self.root / "queue.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.events = []
        self.stack.enter_context(patch.object(
            server, "fire_ha_event", side_effect=lambda _type, data: (self.events.append(data), (True, ""))[1]))
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.runner = self.stack.enter_context(patch.object(server, "start_background_task"))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}
        self.session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        session_dir = server.CODEX_HOME / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        self.session_file = session_dir / f"rollout-2026-09-20T01-00-00-{self.session_id}.jsonl"
        self.session_file.write_text("{}\n")

    def post(self, path, body):
        """Send an authenticated JSON request to the worker."""
        return self.client.post(path, json=body, headers=self.headers)

    def get(self, path):
        """Return the parsed body of an authenticated GET request."""
        return self.client.get(path, headers=self.headers).json

    def create(self, message="Review my automation"):
        """Start a chat directly; it stays active until finish() is called."""
        response = self.post("/tasks", {"prompt": message})
        self.assertEqual(response.status_code, 200)
        return response.json["task_id"]

    def finish(self, task_id, status="completed"):
        """End a run the way the background runner does, which starts the next queued message."""
        server.update_task(task_id, status=status, session_id=self.session_id, summary="Done", completed_at=server.utc_now())
        server.active_task_runners.discard(task_id)
        server.start_next_queued()

    def restart(self):
        """Drop what the worker holds in memory and load the stored chats and queue again."""
        self.runner.reset_mock()
        server.tasks.clear()
        server.active_task_runners.clear()
        server.message_queue.clear()
        server.load_task_index()
        server.load_message_queue()

    def queue_chat(self, message, **extra):
        """Send a new chat that has to wait and return the accepted response."""
        response = self.post("/tasks", {"prompt": message, "queue": True, **extra})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "in_queue")
        return response.json

    def test_new_chats_wait_in_order_and_start_when_the_running_chat_finishes(self):
        """New chats sent behind a running one are listed in order and start one by one."""
        running = self.create()
        first = self.queue_chat("First queued chat")
        second = self.queue_chat("Second queued chat")
        self.assertEqual([first["position"], second["position"]], [1, 2])
        self.assertEqual(first["active_task_id"], running)
        # Nothing ran and no chat was created for them yet.
        self.assertEqual(list(server.tasks), [running])
        self.runner.assert_called_once()
        # A request that does not ask to wait is still refused.
        self.assertEqual(self.post("/tasks", {"prompt": "No waiting"}).status_code, 409)
        listing = self.get("/tasks?summary=true")
        self.assertEqual(listing["active_task_id"], running)
        self.assertEqual([item["task_id"] for item in listing["queue"]], [first["task_id"], second["task_id"]])
        self.assertEqual([item["position"] for item in listing["queue"]], [1, 2])
        self.assertEqual(len(listing["tasks"]), 1)
        self.assertEqual(self.get("/queue")["queue"], listing["queue"])
        self.assertEqual(self.get("/status")["queued_message_count"], 2)
        waiting = self.get(f"/tasks/{first['task_id']}")["task"]
        self.assertEqual(waiting["status"], "in_queue")
        self.assertEqual(waiting["title"], "First queued chat")
        self.assertEqual(waiting["turns"], [])
        self.assertFalse(waiting["can_continue"])
        self.assertEqual(waiting["queued_message"]["message"], "First queued chat")
        self.assertEqual(waiting["queued_message"]["queue_id"], first["queue_id"])

        self.finish(running)
        self.runner.assert_called_with(first["task_id"], "First queued chat")
        started = server.tasks[first["task_id"]]
        self.assertEqual(started["status"], "queued")
        self.assertEqual(started["title"], "First queued chat")
        self.assertEqual([turn["message"] for turn in started["turns"]], ["First queued chat"])
        self.assertEqual(started["turns"][0]["execution_settings"], server.resolve_chat_settings(server.DEFAULT_CHAT_SETTINGS, server.read_options()))
        self.assertIsNone(self.get(f"/tasks/{first['task_id']}")["task"]["queued_message"])
        self.assertEqual([item["task_id"] for item in self.get("/queue")["queue"]], [second["task_id"]])
        self.assertEqual(self.get("/queue")["queue"][0]["position"], 1)
        self.assertNotIn(second["task_id"], server.tasks)

        self.finish(first["task_id"])
        self.runner.assert_called_with(second["task_id"], "Second queued chat")
        self.assertEqual(self.get("/queue")["queue"], [])
        self.assertEqual(json.loads(server.MESSAGE_QUEUE_FILE.read_text(encoding="utf-8")), [])

    def test_queue_request_starts_at_once_when_nothing_is_running(self):
        """Asking to queue changes nothing when the worker is idle."""
        response = self.post("/tasks", {"prompt": "Idle worker", "queue": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "queued")
        self.assertEqual(server.message_queue, [])
        self.assertFalse(server.MESSAGE_QUEUE_FILE.exists())
        self.finish(response.json["task_id"])
        response = self.post(f"/tasks/{response.json['task_id']}/continue", {"message": "More", "queue": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.message_queue, [])

    def test_follow_up_waits_without_touching_the_chat_then_continues_it(self):
        """A queued follow-up leaves its saved chat as it was until the message starts."""
        chat = self.create("Original request")
        self.finish(chat)
        running = self.create("Busy chat")
        before = copy.deepcopy(server.tasks[chat])
        settings = {"model": "gpt-6-astra", "reasoning_effort": "max"}
        # The chat that is working cannot queue behind itself, and plain requests keep failing.
        self.assertEqual(self.post(f"/tasks/{running}/continue", {"message": "More", "queue": True}).status_code, 409)
        self.assertEqual(self.post(f"/tasks/{chat}/continue", {"message": "More"}).status_code, 409)
        self.assertEqual(self.post(f"/tasks/{chat}/continue", {"message": "More", "queue": True, "chat_settings": {"model": "invented"}}).status_code, 400)
        self.assertEqual(server.message_queue, [])
        response = self.post(f"/tasks/{chat}/continue", {"message": "Apply it", "queue": True, "chat_settings": settings})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["task_id"], chat)
        self.assertEqual(server.tasks[chat], before)
        # One waiting message per chat, and its settings stay as sent.
        duplicate = self.post(f"/tasks/{chat}/continue", {"message": "Again", "queue": True})
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("already has a message in the queue", duplicate.json["error"])
        self.assertEqual(self.post(f"/tasks/{chat}/settings", {"chat_settings": {}}).status_code, 409)
        task = self.get(f"/tasks/{chat}")["task"]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["queued_message"]["message"], "Apply it")
        self.assertFalse(task["queued_message"]["new_chat"])
        self.assertEqual(len(task["turns"]), 1)

        self.finish(running)
        self.runner.assert_called_with(chat, "Original request", session_id=self.session_id, reply="Apply it")
        continued = server.tasks[chat]
        self.assertEqual(continued["status"], "queued")
        self.assertEqual(continued["chat_settings"], settings)
        self.assertEqual([turn["message"] for turn in continued["turns"]], ["Original request", "Apply it"])
        self.assertEqual(continued["turns"][0], before["turns"][0])
        self.assertEqual(continued["turns"][1]["execution_settings"], settings)
        self.assertEqual(server.message_queue, [])

    def test_waiting_message_can_be_edited_and_removed(self):
        """A waiting message can be reworded or removed, and only the final text runs."""
        running = self.create()
        queued = self.queue_chat("Frist draft")
        path = f"/queue/{queued['queue_id']}"
        for body in ([], {}, {"message": " "}, {"message": 5}):
            self.assertEqual(self.post(path, body).status_code, 400)
        self.assertEqual(self.post("/queue/missing", {"message": "Text"}).status_code, 404)
        self.assertEqual(self.client.post(path, json={"message": "Text"}).status_code, 401)
        self.assertEqual(self.client.delete(path).status_code, 401)
        self.assertEqual(self.client.get("/queue").status_code, 401)
        response = self.post(path, {"message": "  First draft, corrected  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["queued_message"]["message"], "First draft, corrected")
        # A chat named after its message is renamed with it.
        self.assertEqual(response.json["queued_message"]["title"], "First draft, corrected")
        self.assertEqual(self.get(f"/tasks/{queued['task_id']}")["task"]["title"], "First draft, corrected")
        self.assertEqual(json.loads(server.MESSAGE_QUEUE_FILE.read_text(encoding="utf-8"))[0]["message"], "First draft, corrected")
        named = self.queue_chat("Message", title="Chosen title")
        self.assertEqual(self.post(f"/queue/{named['queue_id']}", {"message": "Other"}).json["queued_message"]["title"], "Chosen title")
        with patch.object(server, "atomic_json_write", side_effect=OSError("disk full")):
            self.assertEqual(self.post(path, {"message": "Lost"}).status_code, 500)
            self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 500)
        self.assertEqual(server.message_queue[0]["message"], "First draft, corrected")

        response = self.client.delete(f"/queue/{named['queue_id']}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "queue_id": named["queue_id"], "task_id": named["task_id"], "deleted": True})
        self.assertEqual(self.client.delete(f"/queue/{named['queue_id']}", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f"/tasks/{named['task_id']}", headers=self.headers).status_code, 404)
        self.finish(running)
        # The edited text is what runs, and the removed message never does.
        self.runner.assert_called_with(queued["task_id"], "First draft, corrected")
        self.assertEqual(server.tasks[queued["task_id"]]["prompt"], "First draft, corrected")
        self.assertNotIn(named["task_id"], server.tasks)
        gone = self.post(path, {"message": "Too late"})
        self.assertEqual(gone.status_code, 404)
        self.assertIn("already started", gone.json["error"])

    def test_attached_images_wait_with_the_message(self):
        """Images are served while their message waits, removed with it, and kept when it starts."""
        running = self.create()
        kept = self.queue_chat("Look at this", attachments=[{"name": "shot.png", "data": PNG}])
        dropped = self.queue_chat("And this", attachments=[{"name": "other.png", "data": PNG}])
        self.assertEqual(self.post("/tasks", {"prompt": "Bad", "queue": True, "attachments": [{"data": "!"}]}).status_code, 400)
        self.assertEqual(len(server.message_queue), 2)
        entry = self.get(f"/tasks/{kept['task_id']}")["task"]["queued_message"]
        attachment = entry["prompt_attachments"][0]
        self.assertEqual(attachment["name"], "shot.png")
        url = f"/tasks/{kept['task_id']}/attachments/{attachment['attachment_id']}"
        self.assertEqual(self.client.get(url).status_code, 401)
        with self.client.get(url, headers=self.headers) as served:
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.mimetype, "image/png")
        dropped_dir = server.get_task_dir(dropped["task_id"])
        self.assertTrue(dropped_dir.exists())
        self.assertEqual(self.client.delete(f"/queue/{dropped['queue_id']}", headers=self.headers).status_code, 200)
        self.assertFalse(dropped_dir.exists())
        self.finish(running)
        turn = server.tasks[kept["task_id"]]["turns"][0]
        self.assertEqual(turn["turn_id"], entry["turn_id"])
        self.assertEqual(turn["prompt_attachments"], entry["prompt_attachments"])
        self.assertEqual(len(server.prompt_attachment_paths(kept["task_id"], turn)), 1)
        with self.client.get(url, headers=self.headers) as served:
            self.assertEqual(served.status_code, 200)

    def test_queue_survives_a_restart_and_resumes(self):
        """Waiting messages are restored after a restart, without stale or malformed entries."""
        chat = self.create("Saved chat")
        self.finish(chat)
        running = self.create("Interrupted")
        new_chat = self.queue_chat("Waiting new chat")
        self.assertEqual(self.post(f"/tasks/{chat}/continue", {"message": "Waiting follow-up", "queue": True}).status_code, 202)
        stored = json.loads(server.MESSAGE_QUEUE_FILE.read_text(encoding="utf-8"))
        # Entries that no longer make sense are skipped: a chat that was deleted,
        # a new chat that already exists, a turn that already started, ids that
        # are not ones the worker generates, and junk.
        stored += [
            {**stored[1], "queue_id": "a" * 32, "task_id": "deleted-chat"},
            {**stored[0], "queue_id": "b" * 32, "task_id": chat},
            {**stored[1], "queue_id": "c" * 32, "turn_id": server.tasks[chat]["turns"][0]["turn_id"]},
            {**stored[0], "queue_id": "e" * 32, "task_id": "../outside"},
            {**stored[0], "queue_id": "f" * 32, "task_id": "another-new-chat", "turn_id": "../.."},
            {"queue_id": "d" * 32}, "junk",
        ]
        server.MESSAGE_QUEUE_FILE.write_text(json.dumps(stored), encoding="utf-8")
        self.restart()
        self.assertEqual(server.tasks[running]["status"], "failed")
        self.assertEqual([entry["message"] for entry in server.message_queue], ["Waiting new chat", "Waiting follow-up"])
        server.start_next_queued()
        self.runner.assert_called_once_with(new_chat["task_id"], "Waiting new chat")
        self.finish(new_chat["task_id"])
        self.runner.assert_called_with(chat, "Saved chat", session_id=self.session_id, reply="Waiting follow-up")
        server.MESSAGE_QUEUE_FILE.write_text("not json", encoding="utf-8")
        server.message_queue.clear()
        server.load_message_queue()
        self.assertEqual(server.message_queue, [])

    def test_worker_stopping_during_the_handoff_does_not_lose_the_message(self):
        """A message stays stored until its exchange is saved, so a stop in between keeps or records it."""
        chat = self.create("Saved chat")
        self.finish(chat)
        running = self.create("Busy chat")
        new_chat = self.queue_chat("New chat that must not get lost")
        follow_up = "Follow-up that must not get lost"
        self.assertEqual(self.post(f"/tasks/{chat}/continue", {"message": follow_up, "queue": True}).status_code, 202)

        # The worker stops after taking the message from the queue, before its chat is saved.
        with patch.object(server, "open_task", side_effect=SystemExit), self.assertRaises(SystemExit):
            self.finish(running)
        self.restart()
        self.assertNotIn(new_chat["task_id"], server.tasks)
        self.assertEqual([entry["message"] for entry in server.message_queue], ["New chat that must not get lost", follow_up])
        server.start_next_queued()
        self.runner.assert_called_once_with(new_chat["task_id"], "New chat that must not get lost")
        self.assertEqual([entry["message"] for entry in json.loads(server.MESSAGE_QUEUE_FILE.read_text(encoding="utf-8"))], [follow_up])

        # The worker stops after the exchange is saved, before the stored queue lets go of it.
        with patch.object(server, "save_message_queue", side_effect=SystemExit), self.assertRaises(SystemExit):
            self.finish(new_chat["task_id"])
        self.restart()
        # The exchange is on record as interrupted and is not sent a second time.
        self.assertEqual(server.message_queue, [])
        self.assertEqual([turn["message"] for turn in server.tasks[chat]["turns"]], ["Saved chat", follow_up])
        self.assertEqual(server.tasks[chat]["turns"][1]["status"], "failed")
        self.assertIn("restarted", server.tasks[chat]["summary"])
        server.start_next_queued()
        self.runner.assert_not_called()

    def test_deleting_a_chat_removes_its_waiting_message(self):
        """Deleting a saved chat also drops the follow-up that was waiting for it."""
        chat = self.create("Saved chat")
        self.finish(chat)
        self.create("Busy chat")
        self.assertEqual(self.post(f"/tasks/{chat}/continue", {"message": "Later", "queue": True, "attachments": [{"data": PNG}]}).status_code, 202)
        other = self.queue_chat("Unrelated")
        self.assertEqual(self.client.delete(f"/tasks/{chat}", headers=self.headers).status_code, 200)
        self.assertEqual([entry["task_id"] for entry in server.message_queue], [other["task_id"]])
        self.assertEqual([entry["task_id"] for entry in json.loads(server.MESSAGE_QUEUE_FILE.read_text(encoding="utf-8"))], [other["task_id"]])
        self.assertFalse(server.get_task_dir(chat).exists())
        # A chat that has not started is removed through the queue, not as a task.
        self.assertEqual(self.client.delete(f"/tasks/{other['task_id']}", headers=self.headers).status_code, 404)

    def test_message_that_cannot_start_fails_in_its_chat_and_the_next_one_runs(self):
        """A follow-up whose session is gone is recorded as failed and does not block the queue."""
        chat = self.create("Saved chat")
        self.finish(chat)
        running = self.create("Busy chat")
        self.assertEqual(self.post(f"/tasks/{chat}/continue", {"message": "Needs the session", "queue": True}).status_code, 202)
        following = self.queue_chat("Runs anyway")
        self.session_file.unlink()
        self.events.clear()
        self.finish(running)
        failed = server.tasks[chat]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("saved Codex session is unavailable", failed["details"])
        self.assertEqual([turn["status"] for turn in failed["turns"]], ["completed", "failed"])
        self.assertEqual(failed["turns"][1]["message"], "Needs the session")
        self.assertEqual([event["task_id"] for event in self.events], [chat])
        self.assertNotIn(chat, server.active_task_runners)
        self.runner.assert_called_with(following["task_id"], "Runs anyway")
        self.assertEqual(server.message_queue, [])

    def test_runner_cancel_and_full_queue(self):
        """The runner and Stop both move the queue on, and a full queue refuses more messages."""
        running = self.create()
        queued = self.queue_chat("After the runner")
        # The background runner starts the next message itself when it ends.
        with patch.object(server, "run_task"), patch.object(server.verification, "end"), patch.object(server, "finish_activity"):
            server.update_task(running, status="completed", session_id=self.session_id)
            server._run_background_task(running, "Review my automation", None, None)
        self.runner.assert_called_with(queued["task_id"], "After the runner")
        # Stopping the running chat lets the queue move on once its runner is gone.
        after_stop = self.queue_chat("After the stop")
        server.active_task_runners.discard(queued["task_id"])
        self.assertEqual(self.post(f"/tasks/{queued['task_id']}/cancel", {}).status_code, 200)
        self.assertEqual(server.tasks[queued["task_id"]]["status"], "cancelled")
        self.runner.assert_called_with(after_stop["task_id"], "After the stop")
        with patch.object(server, "QUEUE_MAX_MESSAGES", 1):
            self.queue_chat("Fills the queue")
            full = self.post("/tasks", {"prompt": "One too many", "queue": True})
        self.assertEqual(full.status_code, 409)
        self.assertIn("queue is full", full.json["error"])
        self.assertEqual(len(server.message_queue), 1)


if __name__ == "__main__":
    unittest.main()
