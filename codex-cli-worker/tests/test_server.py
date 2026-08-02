from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
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


class TaskLaunchFailureTests(unittest.TestCase):
    def test_popen_failure_marks_task_failed(self) -> None:
        updates: list[dict[str, object]] = []
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "task"
            with (
                patch.object(server, "get_task_dir", return_value=task_dir),
                patch.object(server, "update_task", side_effect=lambda _task_id, **values: updates.append(values)),
                patch.object(server, "create_snapshot", return_value={"path": "snapshot"}),
                patch.object(server, "read_options", return_value={"task_timeout_seconds": 30}),
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
                server.run_task("task", "inspect only")

        self.assertEqual(updates[-1]["status"], "failed")
        self.assertIn("Could not start", str(updates[-1]["details"]))
        self.assertEqual(events[-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
