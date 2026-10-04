"""Answers Codex offers with a question: storing them, replying with one, and the notification buttons."""
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


class ChoiceTests(unittest.TestCase):
    """A question with choices, from Codex's answer to the reply that picks one."""

    def setUp(self):
        """Give each test an empty worker with temporary storage, a saved session, and no real runs."""
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
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.runner = self.stack.enter_context(patch.object(server, "start_background_task"))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}
        self.session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        session_dir = server.CODEX_HOME / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        (session_dir / f"rollout-2026-09-20T01-00-00-{self.session_id}.jsonl").write_text("{}\n")

    def post(self, path, body):
        """POST a JSON body to the worker API with the test token."""
        return self.client.post(path, json=body, headers=self.headers)

    def create(self, message="Tidy up my automations"):
        """Start a chat through the API and return its id; it stays active until it is finished."""
        response = self.post("/tasks", {"prompt": message})
        self.assertEqual(response.status_code, 200)
        return response.json["task_id"]

    def ask(self, task_id, choices=("Go ahead", "Don't change anything")):
        """End the current exchange with a question and its choices, and return the id of the turn that asked."""
        server.update_task(
            task_id, status="waiting_for_input", session_id=self.session_id, summary="Two automations are duplicates.",
            question="Remove the duplicate?", choices=list(choices),
        )
        server.active_task_runners.discard(task_id)
        return server.tasks[task_id]["current_turn_id"]

    def run_codex(self, task_id, final, *, reply=None):
        """Run one exchange with a stand-in Codex that returns the given final answer; return the events and notify calls."""
        session = self.session_id
        events = []

        class FakeProcess:
            """A Codex process that reports the session and writes the final answer when waited for."""

            def __init__(self, args, **kwargs):
                """Serve the session id on stdout and note where the final answer goes."""
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(json.dumps({"type": "thread.started", "thread_id": session}) + "\n")
                self.stderr = io.StringIO("")
                self.returncode = 0
                self.output = Path(args[args.index("--output-last-message") + 1])

            def poll(self):
                """Report that the process has exited."""
                return 0

            def wait(self, timeout=None):
                """Write the final answer and report a clean exit."""
                self.output.write_text(json.dumps(final))
                return 0

        with (
            patch.object(server, "CONFIG_ROOT", self.root),
            patch.object(server, "build_manifest", return_value={}),
            patch.object(server, "sandbox_readiness", return_value={"required": True, "ready": True}),
            patch.object(server, "read_options", return_value={"task_timeout_seconds": 30, "auto_save_lovelace": False}),
            patch.object(server, "validate_changed_files", return_value=[]),
            patch.object(server.subprocess, "Popen", side_effect=FakeProcess),
            patch.object(server, "fire_ha_event", side_effect=lambda _type, data: (events.append(data), (True, ""))[1]),
            patch.object(server, "notify"), patch.object(server, "notify_question") as question,
        ):
            server.run_task(task_id, "Tidy up my automations", self.session_id if reply else None, reply)
        server.active_task_runners.discard(task_id)
        return events, question

    def test_choices_keep_only_short_distinct_text(self):
        """Choices are trimmed text: blanks, repeats, non-text and overly long entries are dropped, and three are kept."""
        self.assertEqual(server.clean_choices(["  Go   ahead ", "go ahead", "", 3, None, "Skip", "x" * 201, "Ask later", "Fourth"]),
                         ["Go ahead", "Skip", "Ask later"])
        for value in (None, "Go ahead", {"a": 1}, []):
            self.assertEqual(server.clean_choices(value), [])

    def test_every_answer_field_is_required_by_the_schema(self):
        """Codex's strict output needs every property required, and choices is a list of text."""
        schema = server.FINAL_RESPONSE_SCHEMA
        self.assertEqual(sorted(schema["properties"]), sorted(schema["required"]))
        self.assertEqual(schema["properties"]["choices"], {"type": "array", "items": {"type": "string"}})
        self.assertIn("choices", server.build_prompt("Tidy up", "task"))

    def test_question_with_choices_is_stored_and_published_with_its_turn(self):
        """A question's choices are saved on the task and its turn, and go out in the event and the notification."""
        task_id = self.create()
        final = {"status": "needs_input", "summary": "Two automations are duplicates.", "question": "Remove the duplicate?",
                 "details": "", "choices": ["Go ahead", " Don't change anything ", "Go ahead"]}
        events, question = self.run_codex(task_id, final)
        task = server.tasks[task_id]
        turn_id = task["current_turn_id"]
        choices = ["Go ahead", "Don't change anything"]
        self.assertEqual(task["status"], "waiting_for_input")
        self.assertEqual(task["choices"], choices)
        self.assertEqual(task["turns"][0]["choices"], choices)
        self.assertEqual(events[-1]["choices"], choices)
        self.assertEqual(events[-1]["response"]["choices"], choices)
        self.assertEqual(events[-1]["turn_id"], turn_id)
        question.assert_called_once_with(task_id, turn_id, "Remove the duplicate?", choices)
        latest = self.client.get("/status", headers=self.headers).json["latest_task"]
        self.assertEqual((latest["choices"], latest["current_turn_id"]), (choices, turn_id))

    def test_choices_are_kept_only_for_a_question(self):
        """An answer that asks nothing has no choices, whatever Codex put in the list."""
        task_id = self.create()
        final = {"status": "completed", "summary": "Done.", "question": "", "details": "", "choices": ["Go ahead"]}
        events, question = self.run_codex(task_id, final)
        self.assertEqual(server.tasks[task_id]["choices"], [])
        self.assertEqual(events[-1]["choices"], [])
        question.assert_not_called()
        # An answer from before choices existed has no list at all.
        other = self.create("Another request")
        events, _ = self.run_codex(other, {"status": "needs_input", "summary": "", "question": "Which one?", "details": ""})
        self.assertEqual(server.tasks[other]["choices"], [])
        self.assertEqual(events[-1]["choices"], [])

    def test_reply_by_choice_sends_its_text_and_clears_the_choices(self):
        """Picking a choice by position sends its text as the next message, and the new exchange starts without choices."""
        task_id = self.create()
        turn_id = self.ask(task_id)
        response = self.post(f"/tasks/{task_id}/reply", {"choice": 1, "turn_id": turn_id})
        self.assertEqual(response.status_code, 200)
        self.runner.assert_called_with(task_id, "Tidy up my automations", session_id=self.session_id, reply="Don't change anything")
        task = server.tasks[task_id]
        self.assertEqual(task["turns"][1]["message"], "Don't change anything")
        self.assertEqual(task["choices"], [])
        # The question keeps its choices in the history.
        self.assertEqual(task["turns"][0]["choices"], ["Go ahead", "Don't change anything"])
        self.assertEqual(task["turns"][1]["choices"], [])

    def test_continue_takes_a_choice_too(self):
        """The chat's own endpoint accepts a choice in place of a message, as the buttons under the question send it."""
        task_id = self.create()
        turn_id = self.ask(task_id)
        response = self.post(f"/tasks/{task_id}/continue", {"choice": 0, "turn_id": turn_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.tasks[task_id]["turns"][1]["message"], "Go ahead")

    def test_typed_reply_still_works_with_and_without_a_turn(self):
        """A typed reply is accepted as before, and with the waiting turn's id as well."""
        task_id = self.create()
        turn_id = self.ask(task_id)
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"reply": "Keep the newer one", "turn_id": turn_id}).status_code, 200)
        self.assertEqual(server.tasks[task_id]["turns"][1]["message"], "Keep the newer one")
        # An empty turn id counts as none, as in a reply without one.
        self.ask(task_id)
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"reply": "Yes", "turn_id": ""}).status_code, 200)
        self.ask(task_id)
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"reply": "Yes"}).status_code, 200)

    def test_answer_to_an_earlier_question_is_refused(self):
        """A reply that names a turn is refused once that turn's question is answered, so it cannot land on a newer one."""
        task_id = self.create()
        first = self.ask(task_id)
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"choice": 0, "turn_id": first}).status_code, 200)
        second = self.ask(task_id, ["Keep the older one", "Keep the newer one"])
        self.assertNotEqual(first, second)
        before = copy.deepcopy(server.tasks[task_id])
        self.runner.reset_mock()
        for path, body in (("reply", {"choice": 0, "turn_id": first}), ("reply", {"reply": "Yes", "turn_id": first}),
                           ("continue", {"message": "Yes", "turn_id": first})):
            response = self.post(f"/tasks/{task_id}/{path}", body)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json["error"], server.QUESTION_NOT_WAITING)
        self.assertEqual(server.tasks[task_id], before)
        self.runner.assert_not_called()
        # The same holds once the chat is no longer waiting at all.
        server.update_task(task_id, status="completed")
        response = self.post(f"/tasks/{task_id}/continue", {"message": "Yes", "turn_id": second})
        self.assertEqual((response.status_code, response.json["error"]), (409, server.QUESTION_NOT_WAITING))

    def test_invalid_choice_and_turn_values_are_rejected(self):
        """A choice must be the position of an answer the named question offers, and a turn id must be text."""
        task_id = self.create()
        turn_id = self.ask(task_id)
        for choice in (2, -1, "0", True, 1.0):
            self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"choice": choice, "turn_id": turn_id}).status_code, 400, choice)
        for body in ({"reply": "Yes", "turn_id": 7}, {"choice": 0, "turn_id": ["x"]}):
            self.assertEqual(self.post(f"/tasks/{task_id}/reply", body).status_code, 400, body)
        # A position says nothing without the question it belongs to.
        for path, body in (("reply", {"choice": 0}), ("continue", {"choice": 0}), ("reply", {"choice": 0, "turn_id": ""})):
            response = self.post(f"/tasks/{task_id}/{path}", body)
            self.assertEqual((response.status_code, response.json["error"]),
                             (400, "choice needs the turn_id of the question it answers"))
        self.assertEqual(len(server.tasks[task_id]["turns"]), 1)
        self.runner.assert_called_once()  # Only the chat's first message ever started.
        server.update_task(task_id, status="completed")
        for path in ("reply", "continue"):
            response = self.post(f"/tasks/{task_id}/{path}", {"choice": 0, "turn_id": turn_id})
            self.assertEqual((response.status_code, response.json["error"]), (409, server.QUESTION_NOT_WAITING))
        self.runner.assert_called_once()

    def test_choice_waits_in_the_queue_behind_a_working_chat(self):
        """A choice picked while another chat works joins the queue with its text, when the request allows queueing."""
        task_id = self.create()
        turn_id = self.ask(task_id)
        working = self.create("Another request")
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"choice": 0, "turn_id": turn_id}).status_code, 409)
        response = self.post(f"/tasks/{task_id}/reply", {"choice": 0, "turn_id": turn_id, "queue": True})
        self.assertEqual((response.status_code, response.json["status"]), (202, "in_queue"))
        self.assertEqual([entry["message"] for entry in server.message_queue], ["Go ahead"])
        self.assertEqual(response.json["active_task_id"], working)
        # The waiting answer remembers which question it is for.
        self.assertEqual(server.message_queue[0]["answers_turn_id"], turn_id)

    def release(self, task_id):
        """End a working chat's run and free the worker without letting the queue move on yet."""
        server.update_task(task_id, status="completed", session_id=self.session_id, summary="Done", completed_at=server.utc_now())
        server.active_task_runners.discard(task_id)

    def test_direct_message_cannot_overtake_a_queued_answer(self):
        """A chat whose answer waits in the queue takes no other message, so the answer still meets its own question."""
        task_id = self.create()
        turn_id = self.ask(task_id)
        working = self.create("Another request")
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"choice": 0, "turn_id": turn_id, "queue": True}).status_code, 202)
        # The working chat has ended and the queue has not moved on yet: the moment a direct request could slip in.
        self.release(working)
        self.runner.reset_mock()
        for path, body in (("reply", {"reply": "No, wait"}), ("continue", {"message": "Something else"}),
                           ("reply", {"choice": 1, "turn_id": turn_id})):
            response = self.post(f"/tasks/{task_id}/{path}", body)
            self.assertEqual(response.status_code, 409, body)
            self.assertIn("already has a message in the queue", response.json["error"])
        self.assertEqual(len(server.tasks[task_id]["turns"]), 1)
        self.runner.assert_not_called()
        server.start_next_queued()
        self.runner.assert_called_once_with(task_id, "Tidy up my automations", session_id=self.session_id, reply="Go ahead")
        self.assertEqual([turn["message"] for turn in server.tasks[task_id]["turns"]], ["Tidy up my automations", "Go ahead"])

    def test_queued_answer_is_dropped_when_its_question_is_gone(self):
        """A queued answer is not sent to a chat that moved on from its question, and the queue carries on."""
        task_id = self.create()
        turn_id = self.ask(task_id)
        working = self.create("Another request")
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"choice": 0, "turn_id": turn_id, "queue": True}).status_code, 202)
        later = self.post("/tasks", {"prompt": "A later chat", "queue": True})
        self.assertEqual(later.status_code, 202)
        before = len(server.tasks[task_id]["turns"])
        # However the chat got there, its question is no longer the one waiting.
        server.update_task(task_id, status="completed", question="", choices=[])
        self.runner.reset_mock()
        server.update_task(working, status="completed", session_id=self.session_id, summary="Done", completed_at=server.utc_now())
        with patch("builtins.print") as log:
            server.start_next_queued(release=working)
        self.assertIn("its question is no longer waiting", log.call_args_list[0].args[0])
        self.assertEqual(len(server.tasks[task_id]["turns"]), before)
        self.assertEqual(server.message_queue, [])
        self.assertEqual(json.loads(server.MESSAGE_QUEUE_FILE.read_text()), [])
        # The message behind it started, on the worker the ended run freed in the same step.
        self.runner.assert_called_once_with(later.json["task_id"], "A later chat")
        self.assertNotIn(working, server.active_task_runners)

    def test_queued_answer_survives_a_restart_with_its_question(self):
        """After a restart a queued answer still names its question, and is sent while that question waits."""
        task_id = self.create()
        turn_id = self.ask(task_id)
        working = self.create("Another request")
        self.assertEqual(self.post(f"/tasks/{task_id}/reply", {"choice": 1, "turn_id": turn_id, "queue": True}).status_code, 202)
        self.release(working)
        self.runner.reset_mock()
        server.tasks.clear()
        server.active_task_runners.clear()
        server.message_queue.clear()
        server.load_task_index()
        server.load_message_queue()
        self.assertEqual([entry.get("answers_turn_id") for entry in server.message_queue], [turn_id])
        server.start_next_queued()
        self.runner.assert_called_once_with(task_id, "Tidy up my automations", session_id=self.session_id, reply="Don't change anything")

    def test_mobile_app_notification_has_a_button_per_choice(self):
        """A mobile app notify service gets each choice as a button whose action names the position, turn, and task."""
        turn_id = "0123456789abcdef0123456789abcdef"
        with (
            patch.object(server, "read_options", return_value={"notify_service": "notify.mobile_app_pixel"}),
            patch.object(server, "call_ha_service", return_value=(True, "ok")) as call,
        ):
            server.notify_question("20261004T101500Z-1a2b3c4d", turn_id, "Remove the duplicate?", ["Go ahead", "Don't change anything"])
        call.assert_called_once_with("notify.mobile_app_pixel", {
            "title": "Codex needs input",
            "message": "Remove the duplicate? Task: 20261004T101500Z-1a2b3c4d",
            "data": {"actions": [
                {"action": f"CODEX_CLI_CHOICE_0_{turn_id}_20261004T101500Z-1a2b3c4d", "title": "Go ahead"},
                {"action": f"CODEX_CLI_CHOICE_1_{turn_id}_20261004T101500Z-1a2b3c4d", "title": "Don't change anything"},
            ]},
        })

    def test_other_notifications_list_the_choices_in_the_text(self):
        """Without a mobile app service the choices are listed in the message, and an open question is sent as before."""
        for options in ({"notify_service": "notify.telegram"}, {}):
            with (
                patch.object(server, "read_options", return_value=options),
                patch.object(server, "call_ha_service", return_value=(True, "ok")) as call,
            ):
                server.notify_question("task-1", "0" * 32, "Remove the duplicate?", ["Go ahead", "Skip"])
                server.notify_question("task-1", "0" * 32, "Which automation?", [])
            service = options.get("notify_service") or "persistent_notification.create"
            self.assertEqual([entry.args[0] for entry in call.call_args_list], [service, service])
            first, second = (entry.args[1] for entry in call.call_args_list)
            self.assertEqual(first["message"], "Remove the duplicate? Options: Go ahead; Skip. Task: task-1")
            self.assertEqual(second["message"], "Which automation? Task: task-1")
            self.assertNotIn("data", first)

    def test_buttons_fall_back_to_text_when_the_service_refuses_them(self):
        """If the notification with buttons fails, the question is still sent, with its choices in the text."""
        with (
            patch.object(server, "read_options", return_value={"notify_service": "notify.mobile_app_pixel"}),
            patch.object(server, "call_ha_service", side_effect=[(False, "HTTP 400"), (True, "ok")]) as call,
            patch("builtins.print"),
        ):
            server.notify_question("task-1", "0" * 32, "Remove the duplicate?", ["Go ahead", "Skip"])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.args, ("notify.mobile_app_pixel", {
            "title": "Codex needs input", "message": "Remove the duplicate? Options: Go ahead; Skip. Task: task-1"}))


if __name__ == "__main__":
    unittest.main()
