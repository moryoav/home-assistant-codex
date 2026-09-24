"""Live activity steps for the running exchange."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server


def item(kind, item_id, **fields):
    return {"type": "item.completed", "item": {"id": item_id, "type": kind, **fields}}


def command(item_id, cmd, output="", exit_code=0, status="completed"):
    return {"type": "item.completed" if status != "in_progress" else "item.started",
            "item": {"id": item_id, "type": "command_execution", "command": cmd,
                     "aggregated_output": output, "exit_code": exit_code, "status": status}}


FINAL_ANSWER = json.dumps({"status": "completed", "summary": "Done", "question": "", "details": ""})


class ActivityStepTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("tasks", {}), ("task_activity", {}), ("active_task_runners", set())):
            self.stack.enter_context(patch.object(server, name, value))
        server.tasks["t"] = {"task_id": "t", "status": "running", "current_turn_id": "turn-1"}
        server.start_activity("t", "turn-1")

    def steps(self, after=0):
        with server.lock:
            return server.activity_payload_locked(server.task_activity["t"], after)

    def test_items_map_to_steps(self):
        events = [
            item("reasoning", "item_0", text="**Checking the automation**"),
            item("agent_message", "item_1", text="I'll read the automation first."),
            command("item_2", "/bin/sh -lc 'cat /config/automations.yaml'", output="x" * 5000),
            command("item_3", "/bin/sh -lc \"rg -n foo /config\"", output="", exit_code=1),
            item("file_change", "item_4", status="completed",
                 changes=[{"path": "/config/automations.yaml", "kind": "update"}, {"path": "/config/new.yaml", "kind": "add"}]),
            item("web_search", "item_5", query="", action={"type": "other"}),
            item("mcp_tool_call", "item_6", server="codex_apps", tool="github.get_repo", status="completed", error=None),
            item("todo_list", "item_7", items=[{"text": "Read config", "completed": True}, {"text": "Edit", "completed": False}]),
            item("agent_message", "item_8", text=FINAL_ANSWER),
            item("agent_message", "item_9", text=""),
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ]
        for event in events:
            server.record_activity_event("t", event)
        payload = self.steps()
        self.assertTrue(payload["running"])
        self.assertEqual(payload["turn_id"], "turn-1")
        self.assertEqual([step["kind"] for step in payload["steps"]],
                         ["reasoning", "message", "command", "command", "file_change", "web_search", "tool_call", "plan"])
        self.assertEqual([step["index"] for step in payload["steps"]], list(range(8)))
        reasoning, message, cat, rg, edit, search, tool, plan = payload["steps"]
        self.assertEqual(reasoning["text"], "Checking the automation")
        self.assertEqual(message["text"], "I'll read the automation first.")
        self.assertEqual(cat["text"], "cat /config/automations.yaml")
        self.assertTrue(cat["output_truncated"])
        self.assertEqual(len(cat["output"]), server.ACTIVITY_OUTPUT_MAX + 1)
        self.assertEqual(cat["status"], "done")
        self.assertEqual(rg["text"], "rg -n foo /config")
        self.assertEqual((rg["status"], rg["exit_code"]), ("failed", 1))
        self.assertEqual(edit["text"], "Edited automations.yaml, Added new.yaml")
        self.assertEqual(edit["files"], [{"path": "automations.yaml", "kind": "update"}, {"path": "new.yaml", "kind": "add"}])
        self.assertEqual(search["text"], "Searched the web")
        self.assertEqual(tool["text"], "codex_apps · github.get_repo")
        self.assertEqual(plan["text"], "☑ Read config\n☐ Edit")
        self.assertEqual(reasoning["id"], "item_0")

    def test_started_items_update_in_place_with_duration(self):
        server.record_activity_event("t", command("item_1", "/bin/sh -lc 'sleep 1'", status="in_progress", exit_code=None))
        first = self.steps()
        self.assertEqual(first["steps"][0]["status"], "running")
        self.assertIsNone(first["steps"][0]["duration_ms"])
        server.record_activity_event("t", command("item_1", "/bin/sh -lc 'sleep 1'", output="done"))
        second = self.steps(after=first["seq"])
        self.assertEqual(second["total"], 1)
        self.assertEqual(len(second["steps"]), 1)
        self.assertEqual(second["steps"][0]["status"], "done")
        self.assertEqual(second["steps"][0]["output"], "done")
        self.assertIsInstance(second["steps"][0]["duration_ms"], int)
        self.assertEqual(self.steps(after=second["seq"])["steps"], [])

    def test_repeated_error_reports_become_one_step(self):
        message = "Your access token could not be refreshed."
        server.record_activity_event("t", item("error", "item_0", message=message))
        server.record_activity_event("t", {"type": "error", "message": message})
        server.record_activity_event("t", {"type": "turn.failed", "error": {"message": message}})
        payload = self.steps()
        self.assertEqual([(s["kind"], s["status"], s["text"]) for s in payload["steps"]], [("error", "failed", message)])

    def test_secrets_are_redacted_and_steps_are_capped(self):
        server.record_activity_event("t", command("item_0", "/bin/sh -lc 'curl -H \"Authorization: Bearer abcdefghijklmnop123456\" x'",
                                                  output="token=abcdefghijklmnop123456"))
        step = self.steps()["steps"][0]
        self.assertNotIn("abcdefghijklmnop123456", step["text"] + step["output"])
        for index in range(server.ACTIVITY_MAX_STEPS + 20):
            server.record_activity_event("t", item("reasoning", f"r{index}", text=f"Step {index}"))
        payload = self.steps()
        self.assertEqual(payload["total"], server.ACTIVITY_MAX_STEPS + 1)
        self.assertEqual(payload["steps"][-1]["kind"], "notice")
        server.tasks["t"]["status"] = "failed"
        server.tasks["t"]["summary"] = "Codex timed out after 5 seconds."
        with patch.object(server, "atomic_json_write"):
            with patch.object(server, "task_root", return_value=Path(tempfile.gettempdir())):
                server.finish_activity("t")
        self.assertNotIn("t", server.task_activity)

    def test_events_without_a_running_exchange_are_ignored(self):
        server.record_activity_event("other", item("reasoning", "r", text="Lost"))
        server.record_activity_event("t", "not a dict")
        self.assertEqual(self.steps()["total"], 0)
        self.assertNotIn("other", server.task_activity)


class ActivityApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("tasks", {}), ("task_activity", {}), ("active_task_runners", set()), ("running_processes", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "start_background_task"))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}
        self.session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        session_dir = server.CODEX_HOME / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        (session_dir / f"rollout-2026-09-20T01-00-00-{self.session_id}.jsonl").write_text("{}\n")

    def get(self, task_id, after=None):
        query = "" if after is None else f"?after={after}"
        return self.client.get(f"/tasks/{task_id}/activity{query}", headers=self.headers)

    def create(self):
        response = self.client.post("/tasks", json={"prompt": "Review my automation"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.json["task_id"]

    def test_live_steps_then_stored_steps_then_new_turn(self):
        task_id = self.create()
        turn_id = server.tasks[task_id]["current_turn_id"]
        self.assertEqual(self.get(task_id).json, {"ok": True, "turn_id": turn_id, "seq": 0, "running": False, "total": 0, "steps": []})
        server.update_task(task_id, status="running")
        server.start_activity(task_id, turn_id)
        server.record_activity_event(task_id, item("reasoning", "r0", text="Looking"))
        server.record_activity_event(task_id, command("c1", "/bin/sh -lc 'ls'", status="in_progress", exit_code=None))
        live = self.get(task_id).json
        self.assertTrue(live["running"])
        self.assertEqual([step["text"] for step in live["steps"]], ["Looking", "ls"])
        self.assertEqual(self.get(task_id, after=live["seq"]).json["steps"], [])
        self.assertEqual(self.get(task_id, after="x").status_code, 400)
        self.assertEqual(self.get("missing").status_code, 404)

        server.update_task(task_id, status="cancelled", summary=server.CANCELLED_TASK_SUMMARY, session_id=self.session_id,
                           completed_at=server.utc_now())
        server.finish_activity(task_id)
        server.active_task_runners.discard(task_id)
        stored_file = self.root / "tasks" / task_id / "turns" / turn_id / server.ACTIVITY_FILE
        self.assertTrue(stored_file.is_file())
        self.assertNotIn(task_id, server.task_activity)
        stored = self.get(task_id).json
        self.assertFalse(stored["running"])
        self.assertEqual([(s["kind"], s["status"]) for s in stored["steps"]],
                         [("reasoning", "done"), ("command", "failed"), ("outcome", "failed")])
        self.assertEqual(stored["steps"][-1]["text"], "Stopped")
        self.assertEqual(len(self.get(task_id, after=stored["seq"] - 1).json["steps"]), 1)

        server.tasks[task_id]["cancellation_requested"] = True
        response = self.client.post(f"/tasks/{task_id}/continue", json={"message": "Go on"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        fresh = self.get(task_id).json
        self.assertNotEqual(fresh["turn_id"], turn_id)
        self.assertEqual(fresh["steps"], [])

    def test_failed_run_records_its_summary(self):
        task_id = self.create()
        server.update_task(task_id, status="running")
        server.start_activity(task_id, server.tasks[task_id]["current_turn_id"])
        server.update_task(task_id, status="failed", summary="Codex timed out after 5 seconds.", completed_at=server.utc_now())
        server.finish_activity(task_id)
        steps = self.get(task_id).json["steps"]
        self.assertEqual([step["text"] for step in steps], ["Failed: Codex timed out after 5 seconds."])

    def test_deleting_a_chat_forgets_its_steps(self):
        task_id = self.create()
        server.start_activity(task_id, server.tasks[task_id]["current_turn_id"])
        server.update_task(task_id, status="completed", session_id=self.session_id, completed_at=server.utc_now())
        server.active_task_runners.discard(task_id)
        response = self.client.delete(f"/tasks/{task_id}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(task_id, server.task_activity)


class ReasoningSummaryOptionTests(unittest.TestCase):
    def test_option_is_passed_to_codex_and_validated(self):
        for configured, expected in (("detailed", "detailed"), ("none", "none"), (None, "concise"), ("auto", "concise"), ("  Concise ", "concise")):
            with self.subTest(configured=configured):
                self.assertEqual(server.reasoning_summary({"reasoning_summary": configured}), expected)
        with (
            patch.object(server, "tasks", {"t": {"task_id": "t", "turns": [], "chat_settings": server.DEFAULT_CHAT_SETTINGS}}),
            patch.object(server, "read_options", return_value={"reasoning_summary": "detailed"}),
        ):
            args = server.build_codex_args("t", Path("p"), Path("f"), None)
        self.assertIn('model_reasoning_summary="detailed"', args)
        self.assertIn('model_reasoning_effort="medium"', args)


if __name__ == "__main__":
    unittest.main()
