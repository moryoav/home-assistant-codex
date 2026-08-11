from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
if importlib.util.find_spec("websocket") is None:
    sys.modules["websocket"] = types.ModuleType("websocket")
SPEC = importlib.util.spec_from_file_location("codex_worker_server", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class CodexBinaryTests(unittest.TestCase):
    def test_codex_binary_path_requires_executable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "codex"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            with patch.object(server, "CODEX_BINARY", str(binary)):
                self.assertEqual(server.codex_binary_path(), str(binary))

            binary.unlink()
            with patch.object(server, "CODEX_BINARY", str(binary)):
                self.assertIsNone(server.codex_binary_path())

    def test_build_codex_args_uses_fixed_binary(self) -> None:
        with patch.object(
            server,
            "read_options",
            return_value={"codex_sandbox": "workspace-write"},
        ):
            args = server.build_codex_args("task", Path("prompt"), Path("final"), None)

        self.assertEqual(args[0], "/usr/local/bin/codex")


class RuntimeDiagnosticsTests(unittest.TestCase):
    def test_codex_version_probe_reports_version(self) -> None:
        completed = subprocess.CompletedProcess(
            [server.CODEX_BINARY, "--version"],
            0,
            stdout="codex-cli 0.146.0\n",
            stderr="",
        )
        with (
            patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
            patch.object(server.subprocess, "run", return_value=completed),
        ):
            result = server.codex_version_status()

        self.assertEqual(result, {"version": "codex-cli 0.146.0", "error": ""})

    def test_codex_version_probe_handles_timeout(self) -> None:
        with (
            patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
            patch.object(
                server.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired([server.CODEX_BINARY], 5),
            ),
        ):
            result = server.codex_version_status()

        self.assertEqual(result["version"], "")
        self.assertIn("timed out", result["error"])

    def test_workspace_sandbox_uses_no_proc_fallback(self) -> None:
        with (
            patch.object(server, "read_options", return_value={"codex_sandbox": "workspace-write"}),
            patch.object(server.shutil, "which", return_value="/usr/bin/bwrap"),
            patch.object(
                server,
                "_bubblewrap_probe",
                side_effect=[
                    {"ok": True, "error": ""},
                    {"ok": False, "error": "proc mount denied"},
                ],
            ) as probe,
        ):
            result = server.sandbox_readiness()

        self.assertTrue(result["required"])
        self.assertTrue(result["ready"])
        self.assertTrue(result["bubblewrap_ready"])
        self.assertFalse(result["proc_mount_supported"])
        self.assertIn("no-proc fallback", result["message"])
        self.assertEqual(probe.call_count, 2)

    def test_danger_full_access_does_not_require_bubblewrap(self) -> None:
        with (
            patch.object(server, "read_options", return_value={"codex_sandbox": "danger-full-access"}),
            patch.object(server.shutil, "which", return_value=None),
            patch.object(server, "_bubblewrap_probe") as probe,
        ):
            result = server.sandbox_readiness()

        self.assertFalse(result["required"])
        self.assertTrue(result["ready"])
        self.assertFalse(result["bubblewrap_ready"])
        self.assertFalse(result["proc_mount_supported"])
        probe.assert_not_called()


class UsageParsingTests(unittest.TestCase):
    def test_weekly_only_status_is_valid_and_redacts_identifiers(self) -> None:
        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        output = "\n".join(
            [
                "Account: John Doe (Plus)",
                "Email: person@example.com",
                f"Session ID: sess-secret-123-{session_id}",
                "Weekly limit: [####################] 87% left (resets 14:36 on 9 Aug)",
            ]
        )

        parsed = server._parse_usage_output(output)

        self.assertEqual(parsed["five_hour_percent"], "")
        self.assertEqual(parsed["weekly_percent"], "87")
        self.assertNotIn("John Doe", parsed["raw_excerpt"])
        self.assertNotIn("person@example.com", parsed["raw_excerpt"])
        self.assertNotIn(session_id, parsed["raw_excerpt"])
        self.assertNotIn("sess-secret", parsed["raw_excerpt"])
        self.assertIn("Weekly limit", parsed["raw_excerpt"])

    def test_five_hour_and_weekly_status_remain_supported(self) -> None:
        parsed = server._parse_usage_output(
            "5h limit 64% left (resets 19:20) weekly limit 91% left (resets 12:00 on 8 Aug)"
        )

        self.assertEqual(parsed["five_hour_percent"], "64")
        self.assertEqual(parsed["weekly_percent"], "91")


class UsageProcessCleanupTests(unittest.TestCase):
    def test_usage_pty_process_uses_bounded_reap_helper(self) -> None:
        fake_pty = types.ModuleType("pty")
        fake_pty.openpty = lambda: (10, 11)
        fake_fcntl = types.ModuleType("fcntl")
        fake_fcntl.ioctl = lambda *_args: None
        fake_termios = types.ModuleType("termios")
        fake_termios.TIOCSWINSZ = 0
        proc = object()

        with (
            patch.dict(
                sys.modules,
                {"pty": fake_pty, "fcntl": fake_fcntl, "termios": fake_termios},
            ),
            patch.object(server, "active_task_id", return_value=None),
            patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
            patch.object(server, "codex_login_status", return_value={"status_ok": True}),
            patch.object(server, "codex_env", return_value={}),
            patch.object(server.os, "close"),
            patch.object(server.subprocess, "Popen", return_value=proc),
            patch.object(server, "_capture_status_from_tui", side_effect=RuntimeError("stop")),
            patch.object(server, "terminate_and_reap_process") as reap,
        ):
            result = server.fetch_codex_usage_status()

        self.assertEqual(result["status"], "error")
        reap.assert_called_once_with(proc, terminate_timeout=3, kill_timeout=2)


class ModelSelectionTests(unittest.TestCase):
    def build_args_for_model(self, model: str) -> list[str]:
        with patch.object(
            server,
            "read_options",
            return_value={"codex_model": model, "codex_sandbox": "workspace-write"},
        ):
            return server.build_codex_args("task", Path("prompt"), Path("final"), None)

    def test_worker_defaults_to_cli_selected_model(self) -> None:
        config = server.yaml.safe_load(
            (SERVER_PATH.parent / "config.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(config["options"]["codex_model"], "default")
        self.assertEqual(server.DEFAULT_OPTIONS["codex_model"], "default")
        self.assertIn("gpt-5.3-codex", config["schema"]["codex_model"])

    def test_default_model_omits_model_argument(self) -> None:
        args = self.build_args_for_model("default")

        self.assertNotIn("--model", args)

    def test_legacy_default_model_omits_model_argument(self) -> None:
        args = self.build_args_for_model("gpt-5.3-codex")

        self.assertNotIn("--model", args)

    def test_explicit_model_is_passed_to_codex(self) -> None:
        args = self.build_args_for_model("gpt-5.5")

        model_index = args.index("--model")
        self.assertEqual(args[model_index + 1], "gpt-5.5")


class RuntimeConfigTests(unittest.TestCase):
    def test_runtime_config_disables_startup_update_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(server, "CODEX_HOME", root / "codex-home"),
                patch.object(server, "DATA_ROOT", root),
                patch.object(server, "AUTH_QR_DIR", root / "auth-qr"),
                patch.object(server, "SCHEMA_PATH", root / "schema.json"),
                patch.object(server, "CODEX_CONFIG_PATH", root / "codex-home" / "config.toml"),
                patch.object(server, "task_root", return_value=root / "tasks"),
                patch.object(server, "api_token", return_value="configured"),
            ):
                server.ensure_runtime_files()
                config = (root / "codex-home" / "config.toml").read_text(encoding="utf-8")

        self.assertIn("check_for_update_on_startup = false", config)


    def test_runtime_schema_describes_markdown_response_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schema_path = root / "schema.json"
            with (
                patch.object(server, "CODEX_HOME", root / "codex-home"),
                patch.object(server, "DATA_ROOT", root),
                patch.object(server, "AUTH_QR_DIR", root / "auth-qr"),
                patch.object(server, "SCHEMA_PATH", schema_path),
                patch.object(server, "CODEX_CONFIG_PATH", root / "codex-home" / "config.toml"),
                patch.object(server, "task_root", return_value=root / "tasks"),
                patch.object(server, "api_token", return_value="configured"),
            ):
                server.ensure_runtime_files()
                schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["status", "summary", "question", "details"])
        self.assertEqual(
            schema["properties"]["status"],
            {"type": "string", "enum": ["completed", "needs_input", "failed"]},
        )

        self.assertIn("Markdown", schema["description"])
        self.assertIn("raw HTML", schema["description"])
        for field in ("summary", "details", "question"):
            with self.subTest(field=field):
                description = schema["properties"][field]["description"]
                self.assertEqual(schema["properties"][field]["type"], "string")
                self.assertIn("Markdown", description)
                self.assertIn("raw HTML", description)


class HealthRouteTests(unittest.TestCase):
    def test_health_keeps_legacy_fields_and_adds_runtime_diagnostics(self) -> None:
        sandbox = {
            "mode": "workspace-write",
            "required": True,
            "ready": False,
            "message": "probe failed",
        }
        with (
            patch.object(server, "api_token", return_value="test-token"),
            patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
            patch.object(
                server,
                "codex_version_status",
                return_value={"version": "codex-cli 0.146.0", "error": ""},
            ),
            patch.object(server, "sandbox_readiness", return_value=sandbox),
            patch.object(
                server,
                "codex_login_status",
                return_value={"status_ok": True, "message": "logged in"},
            ),
            patch.object(server, "auth_status_payload", return_value={"status": "authenticated"}),
            patch.object(server, "task_root", return_value=Path("/config/codex_tasks")),
        ):
            response = server.app.test_client().get(
                "/health",
                headers={"Authorization": "Bearer test-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        for key in (
            "ok",
            "api_token_configured",
            "codex_binary",
            "codex_login",
            "auth_flow",
            "task_root",
        ):
            self.assertIn(key, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["codex_version"], "codex-cli 0.146.0")
        self.assertEqual(payload["sandbox_readiness"], sandbox)


class TaskLaunchFailureTests(unittest.TestCase):
    def test_popen_failure_marks_task_failed(self) -> None:
        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        updates: list[dict[str, object]] = []
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "task"
            with (
                patch.object(server, "get_task_dir", return_value=task_dir),
                patch.object(server, "update_task", side_effect=lambda _task_id, **values: updates.append(values)),
                patch.object(server, "create_snapshot", return_value={"path": "snapshot"}),
                patch.object(server, "read_options", return_value={"task_timeout_seconds": 30}),
                patch.object(
                    server,
                    "sandbox_readiness",
                    return_value={"required": True, "ready": True},
                ),
                patch.object(server, "write_task_log"),
                patch.object(
                    server.subprocess,
                    "Popen",
                    side_effect=FileNotFoundError(2, os.strerror(2)),
                ),
                patch.object(
                    server,
                    "fire_ha_event",
                    side_effect=lambda _event, data: (events.append(data) or True, ""),
                ),
                patch.object(server, "notify"),
                patch.object(server, "refresh_usage_status_async"),
            ):
                server.run_task("task", "inspect only", session_id=session_id)

        self.assertEqual(updates[-1]["status"], "failed")
        self.assertIn("Could not start", str(updates[-1]["details"]))
        self.assertEqual(updates[-1]["session_id"], session_id)
        self.assertEqual(events[-1]["status"], "failed")
        self.assertEqual(events[-1]["session_id"], session_id)

    def test_stdin_write_and_close_failures_are_cleaned_up(self) -> None:
        class FailingStdin:
            def __init__(self, fail_at: str) -> None:
                self.fail_at = fail_at

            def write(self, _value: str) -> None:
                if self.fail_at == "write":
                    raise BrokenPipeError(32, "broken pipe")

            def close(self) -> None:
                if self.fail_at == "close":
                    raise BrokenPipeError(32, "broken pipe")

        class FakeProcess:
            def __init__(self, fail_at: str) -> None:
                self.stdin = FailingStdin(fail_at)
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
                self.returncode: int | None = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                self.wait_calls += 1
                return self.returncode if self.returncode is not None else 0

        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        for fail_at in ("write", "close"):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temp_dir:
                task_id = f"task-{fail_at}"
                task_dir = Path(temp_dir) / task_id
                proc = FakeProcess(fail_at)
                updates: list[dict[str, object]] = []
                events: list[dict[str, object]] = []
                server.running_processes.pop(task_id, None)
                with (
                    patch.object(server, "get_task_dir", return_value=task_dir),
                    patch.object(
                        server,
                        "update_task",
                        side_effect=lambda _task_id, **values: updates.append(values),
                    ),
                    patch.object(server, "create_snapshot", return_value={"path": "snapshot"}),
                    patch.object(
                        server,
                        "read_options",
                        return_value={
                            "task_timeout_seconds": 30,
                            "codex_sandbox": "workspace-write",
                        },
                    ),
                    patch.object(
                        server,
                        "sandbox_readiness",
                        return_value={"required": True, "ready": True},
                    ),
                    patch.object(server, "write_task_log"),
                    patch.object(server.subprocess, "Popen", return_value=proc),
                    patch.object(
                        server,
                        "fire_ha_event",
                        side_effect=lambda _event, data: (events.append(data) or True, ""),
                    ),
                    patch.object(server, "notify"),
                    patch.object(server, "refresh_usage_status_async"),
                ):
                    server.run_task(task_id, "inspect only", session_id=session_id)

                self.assertTrue(proc.terminated)
                self.assertGreaterEqual(proc.wait_calls, 1)
                self.assertNotIn(task_id, server.running_processes)
                self.assertEqual(updates[-1]["status"], "failed")
                self.assertEqual(updates[-1]["session_id"], session_id)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["session_id"], session_id)


class BackgroundStartFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_tasks = dict(server.tasks)
        self.saved_processes = dict(server.running_processes)
        self.saved_runners = set(server.active_task_runners)
        server.tasks.clear()
        server.running_processes.clear()
        server.active_task_runners.clear()

    def tearDown(self) -> None:
        server.tasks.clear()
        server.tasks.update(self.saved_tasks)
        server.running_processes.clear()
        server.running_processes.update(self.saved_processes)
        server.active_task_runners.clear()
        server.active_task_runners.update(self.saved_runners)

    @staticmethod
    def capture_event(events: list[dict[str, object]]):
        def _capture(_event_type: str, data: dict[str, object]) -> tuple[bool, str]:
            events.append(data)
            return True, ""

        return _capture

    def test_create_thread_start_failure_is_terminal_and_releases_slot(self) -> None:
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(server, "get_task_dir", side_effect=lambda task_id: root / task_id),
                patch.object(server, "save_task_index"),
                patch.object(server, "api_token", return_value="test-token"),
                patch.object(server.threading.Thread, "start", side_effect=RuntimeError("thread unavailable")),
                patch.object(server, "fire_ha_event", side_effect=self.capture_event(events)),
                patch.object(server, "notify"),
                patch.object(server, "refresh_usage_status_async"),
            ):
                response = server.app.test_client().post(
                    "/tasks",
                    headers={"Authorization": "Bearer test-token"},
                    json={"prompt": "inspect only"},
                )

        payload = response.get_json()
        task_id = payload["task_id"]
        self.assertEqual(response.status_code, 500)
        self.assertFalse(payload["ok"])
        self.assertEqual(server.tasks[task_id]["status"], "failed")
        self.assertIn("thread unavailable", server.tasks[task_id]["details"])
        self.assertNotIn(task_id, server.active_task_runners)
        self.assertIsNone(server.active_task_id())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")

    def test_reply_thread_start_failure_is_terminal_and_releases_slot(self) -> None:
        task_id = "reply-start-failure"
        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        events: list[dict[str, object]] = []
        server.tasks[task_id] = {
            "task_id": task_id,
            "status": "waiting_for_input",
            "session_id": session_id,
            "prompt": "inspect only",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(server, "get_task_dir", side_effect=lambda value: root / value),
                patch.object(server, "save_task_index"),
                patch.object(server, "api_token", return_value="test-token"),
                patch.object(server.threading.Thread, "start", side_effect=RuntimeError("thread unavailable")),
                patch.object(server, "fire_ha_event", side_effect=self.capture_event(events)),
                patch.object(server, "notify"),
                patch.object(server, "refresh_usage_status_async"),
            ):
                response = server.app.test_client().post(
                    f"/tasks/{task_id}/reply",
                    headers={"Authorization": "Bearer test-token"},
                    json={"reply": "continue"},
                )

        payload = response.get_json()
        self.assertEqual(response.status_code, 500)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(server.tasks[task_id]["status"], "failed")
        self.assertEqual(server.tasks[task_id]["session_id"], session_id)
        self.assertNotIn(task_id, server.active_task_runners)
        self.assertIsNone(server.active_task_id())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["session_id"], session_id)


class TaskCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_tasks = dict(server.tasks)
        self.saved_processes = dict(server.running_processes)
        self.saved_runners = set(server.active_task_runners)
        server.tasks.clear()
        server.running_processes.clear()
        server.active_task_runners.clear()

    def tearDown(self) -> None:
        server.tasks.clear()
        server.tasks.update(self.saved_tasks)
        server.running_processes.clear()
        server.running_processes.update(self.saved_processes)
        server.active_task_runners.clear()
        server.active_task_runners.update(self.saved_runners)

    @staticmethod
    def capture_event(events: list[dict[str, object]]):
        def _capture(_event_type: str, data: dict[str, object]) -> tuple[bool, str]:
            events.append(data)
            return True, ""

        return _capture

    def test_queued_cancellation_is_terminal_and_event_is_emitted_once(self) -> None:
        task_id = "queued-task"
        events: list[dict[str, object]] = []
        server.tasks[task_id] = {"task_id": task_id, "status": "queued"}

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(server, "get_task_dir", return_value=Path(temp_dir) / task_id),
                patch.object(server, "save_task_index"),
                patch.object(server, "api_token", return_value="test-token"),
                patch.object(server, "fire_ha_event", side_effect=self.capture_event(events)),
                patch.object(server, "notify"),
                patch.object(server, "refresh_usage_status_async"),
                patch.object(server.subprocess, "Popen") as popen,
            ):
                client = server.app.test_client()
                headers = {"Authorization": "Bearer test-token"}
                first = client.post(f"/tasks/{task_id}/cancel", headers=headers)
                server.run_task(task_id, "must not start")
                second = client.post(f"/tasks/{task_id}/cancel", headers=headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(server.tasks[task_id]["status"], "cancelled")
        self.assertTrue(server.tasks[task_id]["cancellation_requested"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "cancelled")
        self.assertEqual(events[0]["response"]["status"], "cancelled")
        popen.assert_not_called()

    def test_preflight_cancellation_cannot_be_overwritten_by_failure(self) -> None:
        task_id = "preflight-task"
        events: list[dict[str, object]] = []
        cancel_statuses: list[int] = []
        overlap_statuses: list[int] = []
        server.tasks[task_id] = {"task_id": task_id, "status": "queued"}

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(server, "get_task_dir", return_value=Path(temp_dir) / task_id),
                patch.object(server, "save_task_index"),
                patch.object(server, "api_token", return_value="test-token"),
                patch.object(server, "create_snapshot", return_value={"path": "snapshot"}),
                patch.object(server, "build_prompt", return_value="prompt"),
                patch.object(
                    server,
                    "read_options",
                    return_value={"codex_sandbox": "workspace-write", "task_timeout_seconds": 30},
                ),
                patch.object(server, "write_task_log"),
                patch.object(server, "fire_ha_event", side_effect=self.capture_event(events)),
                patch.object(server, "notify"),
                patch.object(server, "refresh_usage_status_async"),
                patch.object(server.subprocess, "Popen") as popen,
            ):
                client = server.app.test_client()

                def cancel_during_preflight() -> dict[str, object]:
                    response = client.post(
                        f"/tasks/{task_id}/cancel",
                        headers={"Authorization": "Bearer test-token"},
                    )
                    cancel_statuses.append(response.status_code)
                    overlap = client.post(
                        "/tasks",
                        headers={"Authorization": "Bearer test-token"},
                        json={"prompt": "must remain blocked"},
                    )
                    overlap_statuses.append(overlap.status_code)
                    return {"required": True, "ready": False, "message": "preflight failed"}

                with patch.object(server, "sandbox_readiness", side_effect=cancel_during_preflight):
                    thread = server.start_background_task(task_id, "cancel during preflight")
                    thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(cancel_statuses, [200])
        self.assertEqual(overlap_statuses, [409])
        self.assertEqual(server.tasks[task_id]["status"], "cancelled")
        self.assertEqual(server.tasks[task_id]["summary"], server.CANCELLED_TASK_SUMMARY)
        self.assertIsNone(server.active_task_id())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "cancelled")
        popen.assert_not_called()

    def test_running_cancellation_reaps_process_and_wins_final_state(self) -> None:
        task_id = "running-task"
        events: list[dict[str, object]] = []
        server.tasks[task_id] = {"task_id": task_id, "status": "queued"}

        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
                self.returncode: int | None = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0
                self.client = None
                self.overlap_status: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                self.wait_calls += 1
                if self.wait_calls == 1:
                    assert self.client is not None
                    response = self.client.post(
                        f"/tasks/{task_id}/cancel",
                        headers={"Authorization": "Bearer test-token"},
                    )
                    if response.status_code != 200:
                        raise AssertionError(response.get_json())
                    overlap = self.client.post(
                        "/tasks",
                        headers={"Authorization": "Bearer test-token"},
                        json={"prompt": "must remain blocked"},
                    )
                    self.overlap_status = overlap.status_code
                return self.returncode if self.returncode is not None else 0

        proc = FakeProcess()
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(server, "get_task_dir", return_value=Path(temp_dir) / task_id),
                patch.object(server, "save_task_index"),
                patch.object(server, "api_token", return_value="test-token"),
                patch.object(server, "create_snapshot", return_value={"path": "snapshot"}),
                patch.object(server, "build_prompt", return_value="prompt"),
                patch.object(
                    server,
                    "read_options",
                    return_value={
                        "codex_sandbox": "workspace-write",
                        "task_timeout_seconds": 30,
                        "auto_save_lovelace": False,
                    },
                ),
                patch.object(
                    server,
                    "sandbox_readiness",
                    return_value={"required": True, "ready": True},
                ),
                patch.object(server, "write_task_log"),
                patch.object(server.subprocess, "Popen", return_value=proc),
                patch.object(server, "fire_ha_event", side_effect=self.capture_event(events)),
                patch.object(server, "notify"),
                patch.object(server, "refresh_usage_status_async"),
            ):
                proc.client = server.app.test_client()
                thread = server.start_background_task(task_id, "cancel while running")
                thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertTrue(proc.terminated)
        self.assertGreaterEqual(proc.wait_calls, 2)
        self.assertEqual(proc.overlap_status, 409)
        self.assertNotIn(task_id, server.running_processes)
        self.assertNotIn(task_id, server.active_task_runners)
        self.assertEqual(server.tasks[task_id]["status"], "cancelled")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "cancelled")

    def test_terminate_and_reap_escalates_after_timeout(self) -> None:
        class StubbornProcess:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("codex", timeout)
                return self.returncode if self.returncode is not None else 0

        proc = StubbornProcess()
        reaped = server.terminate_and_reap_process(
            proc,
            terminate_timeout=0.01,
            kill_timeout=0.01,
        )

        self.assertTrue(reaped)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertEqual(proc.wait_calls, 2)
        self.assertEqual(proc.returncode, -9)

    def test_unkillable_process_remains_registered_and_blocks_new_tasks(self) -> None:
        class UnkillableProcess:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls += 1
                raise subprocess.TimeoutExpired("codex", timeout)

        task_id = "unkillable-task"
        proc = UnkillableProcess()
        server.tasks[task_id] = {
            "task_id": task_id,
            "status": "cancelled",
            "cancellation_requested": True,
        }
        server.running_processes[task_id] = proc
        server.active_task_runners.add(task_id)

        with patch.object(server, "run_task"):
            server._run_background_task(task_id, "cancelled", None, None)

        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertEqual(proc.wait_calls, 2)
        self.assertNotIn(task_id, server.active_task_runners)
        self.assertIs(server.running_processes[task_id], proc)
        self.assertEqual(server.active_task_id(), task_id)
        self.assertEqual(server.active_task_count(), 1)


if __name__ == "__main__":
    unittest.main()
