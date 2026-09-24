"""Home Assistant configuration check, recovery copies, and gated dashboard saves."""
from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server


class FakeResponse:
    """A minimal stand-in for a `requests` response."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        """Return the canned payload, or fail like a non-JSON body would."""
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class CheckConfigApiTests(unittest.TestCase):
    """The call to Home Assistant's configuration check endpoint."""

    def setUp(self):
        """Pretend to run inside the add-on with a Supervisor token."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(server, "ha_token", return_value="supervisor-token"))
        self.stack.enter_context(patch.object(server, "ha_token_source", return_value="supervisor"))

    def test_valid_invalid_and_unavailable_results(self):
        cases = [
            (FakeResponse(200, {"result": "valid", "errors": None, "warnings": None}), ("valid", "", "")),
            (FakeResponse(200, {"result": "invalid", "errors": "Invalid config for 'automation'", "warnings": "deprecated"}),
             ("invalid", "Invalid config for 'automation'", "deprecated")),
            (FakeResponse(200, {"result": "invalid", "errors": None}), ("invalid", "Home Assistant reported an invalid configuration.", "")),
            (FakeResponse(500, text="boom"), ("unavailable", None, "")),
            (FakeResponse(200, None), ("unavailable", None, "")),
        ]
        for response, (result, errors, warnings) in cases:
            with self.subTest(result=result, errors=errors):
                with patch.object(server.requests, "post", return_value=response) as post:
                    outcome = server.check_home_assistant_config()
                self.assertEqual(outcome["result"], result)
                self.assertEqual(outcome["warnings"], warnings)
                if errors is not None:
                    self.assertEqual(outcome["errors"], errors)
                else:
                    self.assertTrue(outcome["errors"])
                self.assertEqual(post.call_args.args[0], "http://supervisor/core/api/config/core/check_config")
                self.assertEqual(post.call_args.kwargs["timeout"], server.CONFIG_CHECK_TIMEOUT)

    def test_connection_failure_and_missing_token(self):
        with patch.object(server.requests, "post", side_effect=OSError("unreachable")):
            outcome = server.check_home_assistant_config()
        self.assertEqual(outcome["result"], "unavailable")
        self.assertIn("unreachable", outcome["errors"])
        with patch.object(server, "ha_token", return_value=""):
            self.assertEqual(server.check_home_assistant_config()["result"], "unavailable")


class AssessChangesTests(unittest.TestCase):
    """Validation, the configuration check, recovery copies, and dashboard saves after a run."""

    def setUp(self):
        """Use a temporary config tree and run directory with the Home Assistant calls faked."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = root / "config"
        self.config.mkdir()
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        self.stack.enter_context(patch.object(server, "CONFIG_ROOT", self.config))
        self.stack.enter_context(patch.object(server, "write_task_log"))
        self.stack.enter_context(patch.object(server, "task_cancellation_requested", return_value=False))
        self.options = {"auto_save_lovelace": True, "config_check": True}
        self.stack.enter_context(patch.object(server, "read_options", side_effect=lambda: dict(self.options)))
        self.check = self.stack.enter_context(patch.object(
            server, "check_home_assistant_config", return_value={"result": "valid", "errors": "", "warnings": ""}))
        self.save = self.stack.enter_context(patch.object(server, "save_lovelace_dashboard", return_value=(True, "ok")))
        self.stack.enter_context(patch.object(server, "read_lovelace_registry", return_value={}))

    def snapshot(self, files):
        """Write files into the config tree and record them in the run's pre-change snapshot."""
        with tarfile.open(self.run_dir / "snapshot-before.tar.gz", "w:gz") as tar:
            for rel, content in files.items():
                path = self.config / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                tar.add(path, arcname=rel)

    def write(self, rel, content):
        """Write a file into the config tree as the task would."""
        path = self.config / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_yaml_change_runs_the_check_and_passes(self):
        self.snapshot({"automations.yaml": "- alias: old\n"})
        self.write("automations.yaml", "- alias: new\n")
        result = server.assess_changes("t", self.run_dir, {"added": [], "changed": ["automations.yaml"], "deleted": []})
        self.assertEqual(result["validation_errors"], [])
        self.assertEqual(result["config_check"]["result"], "valid")
        self.assertEqual(result["recovery_files"], [])
        self.check.assert_called_once()
        self.assertFalse((self.run_dir / "recovery").exists())

    def test_failed_check_fails_validation_and_keeps_pre_change_copies(self):
        self.snapshot({"automations.yaml": "- alias: old\n"})
        self.write("automations.yaml", "- alias: new\n")
        self.write("packages/new.yaml", "sensor: []\n")
        self.check.return_value = {"result": "invalid", "errors": "required key 'trigger' not provided", "warnings": "w"}
        result = server.assess_changes("t", self.run_dir, {"added": ["packages/new.yaml"], "changed": ["automations.yaml"], "deleted": []})
        self.assertEqual(result["validation_errors"], ["Home Assistant configuration check failed: required key 'trigger' not provided"])
        self.assertEqual(result["config_check"]["result"], "invalid")
        copy_path = self.run_dir / "recovery" / "automations.yaml"
        self.assertEqual(result["recovery_files"], [
            {"path": "automations.yaml", "copy": str(copy_path.resolve())},
            {"path": "packages/new.yaml", "copy": ""},
        ])
        self.assertEqual(copy_path.read_text(encoding="utf-8"), "- alias: old\n")
        details = server.validation_details(result["validation_errors"], result["config_check"], result["recovery_files"])
        self.assertIn("Pre-change copies of the affected files are kept at: " + str(copy_path.resolve()), details)
        self.assertIn("New files that did not exist before: packages/new.yaml", details)
        self.assertIn("Home Assistant warnings: w", details)

    def test_syntax_error_skips_the_check_and_dashboard_save(self):
        self.snapshot({".storage/lovelace.home": json.dumps({"data": {"config": {"views": []}}}), "scripts.yaml": "a: 1\n"})
        self.write(".storage/lovelace.home", "{not json")
        self.write("scripts.yaml", "a: [\n")
        result = server.assess_changes("t", self.run_dir, {"added": [], "changed": [".storage/lovelace.home", "scripts.yaml"], "deleted": []})
        self.assertEqual(len(result["validation_errors"]), 2)
        self.assertEqual(result["config_check"]["result"], "skipped")
        self.check.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(result["lovelace_results"], [{
            "dashboard_id": "home", "url_path": "", "storage_file": ".storage/lovelace.home",
            "success": False, "message": "Not saved: the file failed validation.",
        }])
        self.assertEqual({entry["path"] for entry in result["recovery_files"]}, {".storage/lovelace.home", "scripts.yaml"})
        self.assertTrue((self.run_dir / "recovery" / ".storage" / "lovelace.home").is_file())

    def test_storage_only_changes_skip_the_check_but_save_dashboards(self):
        self.write(".storage/lovelace.home", json.dumps({"data": {"config": {"views": []}}}))
        result = server.assess_changes("t", self.run_dir, {"added": [], "changed": [".storage/lovelace.home"], "deleted": []})
        self.assertEqual(result["config_check"]["result"], "skipped")
        self.check.assert_not_called()
        self.save.assert_called_once()
        self.assertEqual(result["lovelace_results"][0]["success"], True)

    def test_option_disables_the_check(self):
        self.options["config_check"] = False
        self.write("automations.yaml", "- alias: new\n")
        result = server.assess_changes("t", self.run_dir, {"added": [], "changed": ["automations.yaml"], "deleted": []})
        self.assertEqual(result["config_check"]["result"], "disabled")
        self.check.assert_not_called()

    def test_deleted_yaml_triggers_the_check(self):
        result = server.assess_changes("t", self.run_dir, {"added": [], "changed": [], "deleted": ["packages/old.yaml"]})
        self.assertEqual(result["config_check"]["result"], "valid")
        self.check.assert_called_once()


class RunTaskWiringTests(unittest.TestCase):
    """The assessment reaches the task record, the details text, and the result event."""

    def run_with(self, assessment):
        """Run a resumed task through the test harness with a canned assessment."""
        from test_server import SessionIdParsingTests
        harness = SessionIdParsingTests("read_stdout")
        with patch.object(server, "assess_changes", return_value=assessment):
            return harness.run_resumed_task(
                json.dumps({"type": "thread.started", "thread_id": harness.THREAD_ID}) + "\n",
                final_payload={"status": "completed", "summary": "Edited the automation", "question": "", "details": "Notes"},
            )

    def test_failed_check_marks_the_task_failed_with_recovery_details(self):
        task, events = self.run_with({
            "validation_errors": ["Home Assistant configuration check failed: bad"],
            "config_check": {"result": "invalid", "errors": "bad", "warnings": ""},
            "recovery_files": [{"path": "automations.yaml", "copy": "/tasks/x/recovery/automations.yaml"}],
            "lovelace_results": [],
        })
        self.assertEqual(task["status"], "failed")
        self.assertIn("Validation errors: Home Assistant configuration check failed: bad", task["details"])
        self.assertIn("/tasks/x/recovery/automations.yaml", task["details"])
        self.assertEqual(task["config_check"]["result"], "invalid")
        self.assertEqual(events[-1]["config_check"]["result"], "invalid")
        self.assertEqual(events[-1]["recovery_files"][0]["path"], "automations.yaml")

    def test_unavailable_check_keeps_success_but_notes_it(self):
        task, events = self.run_with({
            "validation_errors": [],
            "config_check": {"result": "unavailable", "errors": "HTTP 502", "warnings": ""},
            "recovery_files": [],
            "lovelace_results": [],
        })
        self.assertEqual(task["status"], "completed")
        self.assertTrue(task["details"].startswith("Notes\n\nHome Assistant could not check the configuration"))
        self.assertIn("HTTP 502", task["details"])
        self.assertEqual(events[-1]["config_check"]["result"], "unavailable")


class TerminalOutcomeResetTests(unittest.TestCase):
    """Launch failures, cancellations, and new turns never carry the previous turn's check."""

    STALE = {
        "config_check": {"result": "invalid", "errors": "old", "warnings": ""},
        "recovery_files": [{"path": "automations.yaml", "copy": "/old/copy"}],
    }

    def setUp(self):
        """Give the worker one task whose previous turn failed the check."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("tasks", {}), ("active_task_runners", set()), ("running_processes", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=root / "tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", root / "index.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", root / "codex"))
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "write_task_log"))
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.events = []
        self.stack.enter_context(patch.object(
            server, "fire_ha_event", side_effect=lambda _event, data: (self.events.append(data) or True, "")))
        self.stack.enter_context(patch.object(server, "start_background_task"))
        self.client = server.app.test_client()
        self.headers = {"Authorization": "Bearer test-token"}
        self.session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        session_dir = server.CODEX_HOME / "sessions" / "2026" / "09" / "20"
        session_dir.mkdir(parents=True)
        (session_dir / f"rollout-2026-09-20T01-00-00-{self.session_id}.jsonl").write_text("{}\n")
        response = self.client.post("/tasks", json={"prompt": "Edit the automation"}, headers=self.headers)
        self.task_id = response.json["task_id"]
        server.update_task(self.task_id, status="failed", session_id=self.session_id, summary="Bad edit",
                           completed_at=server.utc_now(), **self.STALE)
        server.active_task_runners.discard(self.task_id)

    def assert_reset(self, record):
        """The record carries an empty check and no recovery files."""
        self.assertEqual(record["config_check"], server.EMPTY_CONFIG_CHECK)
        self.assertEqual(record["recovery_files"], [])

    def test_new_turn_starts_without_the_previous_check(self):
        response = self.client.post(f"/tasks/{self.task_id}/continue", json={"message": "Try again"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        task = server.tasks[self.task_id]
        self.assert_reset(task)
        self.assert_reset(task["turns"][-1])
        self.assertEqual(task["turns"][0]["config_check"]["result"], "invalid")

    def test_failed_launch_resets_and_reports_the_fields(self):
        self.client.post(f"/tasks/{self.task_id}/continue", json={"message": "Try again"}, headers=self.headers)
        server.tasks[self.task_id].update(self.STALE)
        server.fail_task_launch(self.task_id, RuntimeError("no binary"))
        self.assert_reset(server.tasks[self.task_id])
        self.assert_reset(server.tasks[self.task_id]["turns"][-1])
        self.assert_reset(self.events[-1])

    def test_cancellation_resets_and_reports_the_fields(self):
        self.client.post(f"/tasks/{self.task_id}/continue", json={"message": "Try again"}, headers=self.headers)
        server.tasks[self.task_id].update(self.STALE)
        ok, _proc, error = server.request_task_cancellation(self.task_id)
        self.assertTrue(ok, error)
        self.assert_reset(server.tasks[self.task_id])
        self.assert_reset(server.tasks[self.task_id]["turns"][-1])
        self.assertTrue(server.publish_cancelled_task_outcome(self.task_id))
        self.assert_reset(self.events[-1])


if __name__ == "__main__":
    unittest.main()
