"""Codex execution must succeed, not just the raw namespace preflight."""

from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from test_server import server


def completed(code: int = 0, error: str = "") -> subprocess.CompletedProcess:
    """Return a finished process with the given exit code and stderr."""
    return subprocess.CompletedProcess([], code, stdout="", stderr=error)


class CodexSandboxReadinessTests(unittest.TestCase):
    """The Codex sandbox execution probe and the readiness report that depends on it."""

    def test_probe_uses_selected_mode_task_environment_and_workspace(self) -> None:
        """The probe runs `codex sandbox` in the selected mode, with the task environment, from the config folder."""
        for mode in ("read-only", "workspace-write"):
            with (
                self.subTest(mode=mode),
                patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
                patch.object(server, "CONFIG_ROOT", Path("/config")),
                patch.object(server, "codex_env", return_value={"CODEX_HOME": "/data/codex-home"}),
                patch.object(server.subprocess, "run", return_value=completed()) as run,
            ):
                self.assertTrue(server._codex_sandbox_probe(mode)["ok"])
                args = run.call_args.args[0]
                self.assertEqual(args[:2], [server.CODEX_BINARY, "sandbox"])
                self.assertIn(f'sandbox_mode="{mode}"', args)
                self.assertEqual(args[-2:], ["--", "/bin/true"])
                # The pinned CLI has no platform subcommand: `codex sandbox linux ...`
                # runs a program named "linux". Only options may precede `--`.
                options = args[2:args.index("--")]
                self.assertEqual(len(options) % 2, 0)
                self.assertEqual(set(options[0::2]), {"--config"})
                self.assertNotIn("linux", args)
                self.assertNotIn("exec", args)
                self.assertEqual(run.call_args.kwargs["cwd"], "/config")
                self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], "/data/codex-home")
                self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(run.call_args.kwargs["timeout"], server.CODEX_SANDBOX_PROBE_TIMEOUT_SECONDS)
                self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_codex_probe_allows_more_time_than_the_raw_probes(self) -> None:
        """The Codex execution probe gets a longer timeout than the raw Bubblewrap probes."""
        # A timeout blocks every task; the Codex probe does far more work than bwrap alone.
        self.assertGreater(
            server.CODEX_SANDBOX_PROBE_TIMEOUT_SECONDS, server.RUNTIME_PROBE_TIMEOUT_SECONDS
        )

    def test_missing_cli_fails_closed(self) -> None:
        """Without the Codex executable the probe fails and runs nothing."""
        with (
            patch.object(server, "codex_binary_path", return_value=None),
            patch.object(server.subprocess, "run") as run,
        ):
            result = server._codex_sandbox_probe("read-only")
        self.assertFalse(result["ok"])
        self.assertIn("unavailable", result["error"])
        run.assert_not_called()

    def test_unsupported_mode_never_executes(self) -> None:
        """An unknown mode or danger-full-access fails the probe without running a command."""
        with patch.object(server.subprocess, "run") as run:
            for mode in ("unexpected", "danger-full-access"):
                self.assertFalse(server._codex_sandbox_probe(mode)["ok"])
        run.assert_not_called()

    def test_timeout_and_launch_failure_are_reported(self) -> None:
        """A probe that times out or cannot start fails with an error message."""
        for error in (subprocess.TimeoutExpired("codex", 5), OSError("exec failed")):
            with (
                self.subTest(error=error),
                patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
                patch.object(server, "codex_env", return_value={}),
                patch.object(server.subprocess, "run", side_effect=error),
            ):
                result = server._codex_sandbox_probe("read-only")
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])

    def test_failed_execution_is_redacted_and_not_ready(self) -> None:
        """A failed probe reports its error on one line with secrets redacted."""
        secret = "a" * 32
        with (
            patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
            patch.object(server, "codex_env", return_value={}),
            patch.object(server.subprocess, "run", return_value=completed(1, f"token={secret}\nfailed")),
        ):
            result = server._codex_sandbox_probe("workspace-write")
        self.assertFalse(result["ok"])
        self.assertNotIn(secret, result["error"])
        self.assertNotIn("\n", result["error"])

    def readiness(self, results: list[subprocess.CompletedProcess], mode: str = "workspace-write") -> dict:
        """Return the readiness report when the probe commands give these results in order; all must be used."""
        with (
            patch.object(server, "read_options", return_value={"codex_sandbox": mode}),
            patch.object(server.shutil, "which", return_value="/opt/codex-sandbox/bwrap"),
            patch.object(server, "codex_binary_path", return_value=server.CODEX_BINARY),
            patch.object(server, "codex_env", return_value={}),
            patch.object(server.subprocess, "run", side_effect=results) as run,
        ):
            result = server.sandbox_readiness()
        self.assertEqual(run.call_count, len(results))
        return result

    def test_proc_denial_requires_successful_codex_execution(self) -> None:
        """A host that denies a fresh /proc is ready when the Codex execution probe passes."""
        result = self.readiness([completed(), completed(), completed(1, "proc denied")])
        self.assertTrue(result["ready"])
        self.assertTrue(result["namespace_probe"]["codex_probe"]["ok"])
        self.assertFalse(result["proc_mount_supported"])
        self.assertIn("execution probe passed", result["message"])
        self.assertNotIn("will use", result["message"])

    def test_raw_success_cannot_hide_broken_codex_fallback(self) -> None:
        """A passing namespace probe does not make the sandbox ready when Codex execution fails."""
        for proc in (completed(), completed(1, "proc denied")):
            with self.subTest(proc=proc.returncode):
                result = self.readiness([completed(), completed(1, "Codex failed"), proc])
                self.assertFalse(result["ready"])
                self.assertFalse(result["bubblewrap_ready"])
                self.assertTrue(result["namespace_probe"]["namespace_ok"])
                self.assertIn("Codex failed", result["message"])

    def test_fresh_proc_success_still_checks_codex(self) -> None:
        """A host that can mount a fresh /proc still runs the Codex execution probe."""
        result = self.readiness([completed(), completed(), completed()], "read-only")
        self.assertTrue(result["ready"])
        self.assertTrue(result["proc_mount_supported"])

    def test_namespace_failure_skips_codex_and_proc_probes(self) -> None:
        """A failed namespace probe makes the sandbox not ready and skips the Codex and /proc probes."""
        result = self.readiness([completed(1, "namespace denied")])
        self.assertFalse(result["ready"])
        self.assertTrue(result["proc_probe"]["skipped"])
        self.assertIn("namespace denied", result["message"])

    def test_danger_mode_does_not_run_any_probe(self) -> None:
        """In danger-full-access mode the sandbox is not required and no probe runs."""
        result = self.readiness([], "danger-full-access")
        self.assertTrue(result["ready"])
        self.assertFalse(result["required"])


if __name__ == "__main__":
    unittest.main()
