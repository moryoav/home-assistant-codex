"""Session ids, Codex arguments and model choice, diagnostics, usage parsing, and task failure and cancellation."""
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
from threading import Event, Thread
from unittest.mock import patch

from app_server_fixture import StubAppServer


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
if importlib.util.find_spec("websocket") is None:
    sys.modules["websocket"] = types.ModuleType("websocket")
SPEC = importlib.util.spec_from_file_location("codex_worker_server", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class SessionIdParsingTests(unittest.TestCase):
    """The session id the worker takes from Codex's JSON events, and how a resumed task ends."""

    THREAD_ID = "019fc242-910a-7c92-a17d-54c014e19fc4"
    OTHER_ID = "123e4567-e89b-12d3-a456-426614174000"

    def test_accepts_valid_top_level_thread_started_id(self) -> None:
        """A top-level thread.started event with a valid thread id gives the session id."""
        event = {"type": "thread.started", "thread_id": self.THREAD_ID}

        self.assertEqual(server.extract_session_id(event), self.THREAD_ID)

    def test_nested_ids_and_ids_in_arbitrary_strings_are_ignored(self) -> None:
        """A thread id nested in an item, wrapped in a list, or embedded in text is not taken as the session id."""
        values = [
            {"type": "item.completed", "item": {"thread_id": self.THREAD_ID}},
            {"type": "item.completed", "message": f"exec-{self.THREAD_ID}"},
            [{"type": "thread.started", "thread_id": self.THREAD_ID}],
            f"tool output {self.THREAD_ID}",
        ]

        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(server.extract_session_id(value))

    def run_resumed_task(
        self,
        *,
        final_payload: dict[str, str] | None,
        returned_thread: str | None = None,
        turn_error: str | None = None,
        stale_payload: dict[str, str] | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Resume a waiting task with a fake Codex process; return the final task record and the result events."""
        task_id = "resumed-task"
        events: list[dict[str, object]] = []

        proc = StubAppServer(returned_thread or self.THREAD_ID, final_payload, turn_error=turn_error)

        saved_task = server.tasks.get(task_id)
        server.tasks[task_id] = {
            "task_id": task_id,
            "status": "waiting_for_input",
            "session_id": self.THREAD_ID,
        }
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                task_dir = Path(temp_dir) / task_id
                final_file = task_dir / "final-resume.json"
                if stale_payload is not None:
                    task_dir.mkdir(parents=True)
                    final_file.write_text(json.dumps(stale_payload), encoding="utf-8")

                with (
                    patch.object(server, "get_task_dir", return_value=task_dir),
                    patch.object(server, "save_task_index"),
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
                    patch.object(server, "codex_env", return_value={}),
                    patch.object(server, "build_manifest", return_value={}),
                    patch.object(server, "validate_changed_files", return_value=[]),
                    patch.object(server, "write_task_log"),
                    patch.object(
                        server.subprocess,
                        "Popen",
                        return_value=proc,
                    ),
                    patch.object(
                        server,
                        "fire_ha_event",
                        side_effect=lambda _event, data: (events.append(data) or True, ""),
                    ),
                    patch.object(server, "notify"),
                    patch.object(server, "refresh_usage_status_async"),
                ):
                    server.run_task(
                        task_id,
                        "continue",
                        session_id=self.THREAD_ID,
                        reply="more detail",
                    )

            result = dict(server.tasks[task_id])
        finally:
            server.running_processes.pop(task_id, None)
            if saved_task is None:
                server.tasks.pop(task_id, None)
            else:
                server.tasks[task_id] = saved_task

        return result, events

    def test_only_actionable_verification_results_add_a_review_warning(self) -> None:
        """Only verification checks that failed, found issues or could not run add a review note to a completed task."""
        for check_status in ("disabled", "captured", "passed", "failed", "issues", "unavailable"):
            with self.subTest(check_status=check_status):
                def record_check(task_id, _results):
                    """Record one verification check with the status under test."""
                    server.update_task(task_id, verification=[{"status": check_status}])

                with patch.object(server.verification, "after_changes", side_effect=record_check):
                    task, _ = self.run_resumed_task(
                        final_payload={"status": "completed", "summary": "Done", "details": "Original details"},
                    )
                self.assertEqual(task["status"], "completed")
                self.assertIn("Original details", task["details"])
                self.assertEqual("Verification needs review" in task["details"],
                                 check_status in {"failed", "issues", "unavailable"})

    def test_resume_rejects_a_different_thread(self) -> None:
        task, events = self.run_resumed_task(returned_thread=self.OTHER_ID, final_payload=None)
        self.assertEqual(task["status"], "failed")
        self.assertIn("Codex resumed a different conversation", task["details"])
        self.assertEqual(task["session_id"], self.THREAD_ID)
        self.assertEqual(events[-1]["session_id"], self.THREAD_ID)

    def test_resume_preserves_the_requested_thread(self) -> None:
        task, events = self.run_resumed_task(
            final_payload={"status": "completed", "summary": "resumed", "question": "", "details": ""},
        )
        self.assertEqual(task["session_id"], self.THREAD_ID)
        self.assertEqual(events[-1]["session_id"], self.THREAD_ID)

    def test_resume_does_not_reuse_stale_final_response(self) -> None:
        """A final response left by an earlier turn is not reused when the resumed run exits without writing one."""
        old_response = {
            "status": "needs_input",
            "summary": "old summary",
            "question": "old question",
            "details": "old details",
        }
        task, events = self.run_resumed_task(
            final_payload=None,
            stale_payload=old_response,
        )

        self.assertEqual(task["status"], "failed")
        self.assertIn("without a final answer", str(task["error"]))
        self.assertNotEqual(task["summary"], old_response["summary"])
        self.assertNotEqual(task["question"], old_response["question"])
        self.assertEqual(events[-1]["status"], "failed")

    def test_failed_exchange_names_the_reason_in_the_task_error(self) -> None:
        """A task that ends as failed names the reason in its error; any other outcome leaves the error empty."""
        answer = {"status": "completed", "summary": "Edited the automation", "question": "", "details": ""}
        cases = [
            ({**answer, "status": "failed", "summary": "The entity does not exist"}, None, "failed", "The entity does not exist"),
            (answer, "Model connection lost", "failed", "Model connection lost"),
            (None, None, "failed", "Codex finished without a final answer."),
            (answer, None, "completed", ""),
            ({**answer, "status": "needs_input", "question": "Which light?"}, None, "waiting_for_input", ""),
        ]
        for final_payload, turn_error, status, error in cases:
            with self.subTest(status=status, error=error):
                task, _events = self.run_resumed_task(final_payload=final_payload, turn_error=turn_error)
                self.assertEqual(task["status"], status)
                self.assertEqual(task["error"], error)

class CodexBinaryTests(unittest.TestCase):
    """The fixed path of the Codex executable, and its place on the command line."""

    def test_codex_binary_path_requires_executable_file(self) -> None:
        """The Codex path is returned while an executable file is there, and None once it is gone."""
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "codex"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            with patch.object(server, "CODEX_BINARY", str(binary)):
                self.assertEqual(server.codex_binary_path(), str(binary))

            binary.unlink()
            with patch.object(server, "CODEX_BINARY", str(binary)):
                self.assertIsNone(server.codex_binary_path())

    def test_app_server_uses_fixed_binary(self) -> None:
        self.assertEqual(server.build_app_server_args()[:2], ["/usr/local/bin/codex", "app-server"])


class RuntimeDiagnosticsTests(unittest.TestCase):
    """The Codex version probe and the sandbox readiness report."""

    def test_codex_version_probe_reports_version(self) -> None:
        """A successful codex --version run is reported as the version, with no error."""
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
        """A version probe that times out reports no version and a timeout error."""
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
        """A workspace sandbox is ready if only the fresh /proc probe fails; the message names the no-proc fallback."""
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
        """In danger-full-access mode the sandbox is ready without Bubblewrap, and no probe is run."""
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


# What the usage check captures from the bundled CLI: the startup banner, then the
# /status panel, which names the model by its display name.
STATUS_PANEL = """\
╭────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.160.0)                 │
│ model:     loading   /model to change      │
│ directory: /config                         │
╰────────────────────────────────────────────╯
/status

  >_ OpenAI Codex (v0.160.0)

  Visit https://chatgpt.com/codex/settings/usage for up-to-date
  information on rate limits and credits

  Model:           GPT-6.1-Sol (reasoning low, summaries auto)
  Model provider:  openai
  Directory:       /config
  Permissions:     Custom (workspace with network access, Ask for approval)
  Agents.md:       <none>

  Token usage:     2K total  (1.4K input + 600 output)
  Context window:  100% left (2.2K used / 272K)
  5h limit:        [███████████░░░░░░░░░] 55% left (resets 09:25)
  Weekly limit:    [██████████████░░░░░░] 70% left (resets 09:55)
"""


class UsageParsingTests(unittest.TestCase):
    """Reading the quota limits and the model from the CLI's /status output."""

    def test_weekly_only_status_is_valid_and_redacts_identifiers(self) -> None:
        """A status with only a weekly limit is valid, and account identifiers are kept out of the excerpt."""
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
        """A status with both a 5h limit and a weekly limit gives both percentages."""
        parsed = server._parse_usage_output(
            "5h limit 64% left (resets 19:20) weekly limit 91% left (resets 12:00 on 8 Aug)"
        )

        self.assertEqual(parsed["five_hour_percent"], "64")
        self.assertEqual(parsed["weekly_percent"], "91")

    def test_reset_time_survives_a_status_line_on_the_same_line(self) -> None:
        """A limit's reset time is kept when the CLI's status line repeats the limit later on the same line."""
        # As captured from CLI 0.160.0: the redrawn status line follows the panel's last line.
        output = "\n".join(
            [
                "5h limit: [███████████████████░] 95% left (resets 17:22)",
                "Weekly limit: [████████████████████] 98% left (resets 07:01 on 10 Oct) › Ask Codex to do anything "
                "GPT-6.1-Sol default · Context 100% left · 5h 94% left · weekly 97% left",
            ]
        )

        parsed = server._parse_usage_output(output)

        self.assertEqual(parsed["weekly_reset"], "07:01 on 10 Oct")
        self.assertTrue(parsed["weekly_reset_at"])
        self.assertEqual(parsed["five_hour_reset"], "17:22")
        # The percentage is still the last one on the line, the freshest the CLI printed.
        self.assertEqual((parsed["five_hour_percent"], parsed["weekly_percent"]), ("94", "97"))
        # The same for the 5-hour limit when it is the panel's last line.
        parsed = server._parse_usage_output("5h limit: [###] 95% left (resets 17:22) › Ask Codex · 5h 95% left")
        self.assertEqual(parsed["five_hour_reset"], "17:22")

    def test_status_panel_names_the_model_the_cli_picks(self) -> None:
        """The /status panel names the model the CLI picked; a model the worker offers is returned as its id.

        The startup banner, a panel that is still loading and cut-off output give no model.
        """
        # The panel shows the display name; a model the worker offers is returned as its id.
        self.assertEqual(server._parse_status_model(STATUS_PANEL), "gpt-6.1-sol")
        # The limits are still read from the same panel.
        self.assertEqual(server._parse_usage_output(STATUS_PANEL)["weekly_percent"], "70")
        lines = {
            "Model:           gpt-6-luna\n": "gpt-6-luna",
            # A model the worker does not offer keeps the name the CLI gives it.
            "Model:           Luna Reserve (reasoning low, summaries auto)\n": "Luna Reserve",
            # The startup banner, which can still say "loading".
            "model:     GPT-6-Sol medium   /model to change\n": "",
            # /status before the session is ready.
            "Model:           loading (reasoning none, summaries auto)\n": "",
            "Model provider:  openai\n": "",
            # Output cut off in the middle of the name.
            "Model:           GPT-6.1-S": "",
        }
        for text, model in lines.items():
            with self.subTest(text=text):
                self.assertEqual(server._parse_status_model(text), model)


class UsageProcessCleanupTests(unittest.TestCase):
    """The throwaway CLI process behind the usage check: its start, its prompts, its cleanup, and the model it names."""

    def fetch_usage(self, **capture):
        """Run the usage check against a fake pty and CLI; `capture` sets what reading /status does."""
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
            patch.object(server.os, "write"),
            patch.object(server.subprocess, "Popen", return_value=proc) as popen,
            patch.object(server, "_capture_status_from_tui", **capture),
            patch.object(server, "terminate_and_reap_process") as reap,
        ):
            result = server.fetch_codex_usage_status()
        return result, proc, popen, reap

    def test_usage_pty_process_uses_bounded_reap_helper(self) -> None:
        """The usage probe runs with --no-daemon and is reaped with bounded timeouts, even if reading /status fails."""
        result, proc, popen, reap = self.fetch_usage(side_effect=RuntimeError("stop"))

        self.assertEqual(result["status"], "error")
        reap.assert_called_once_with(proc, terminate_timeout=3, kill_timeout=2)
        # The throwaway probe must not start or attach to Codex's shared background server.
        self.assertIn("--no-daemon", popen.call_args.args[0])

    def test_usage_check_remembers_the_model_the_cli_picks(self) -> None:
        """The usage check remembers the model the CLI picks as the default model, outside the reported quota."""
        options = {"codex_model": "default"}
        with patch.dict(server.usage_state):
            self.assertEqual(server.default_model(options), server.CLI_DEFAULT_MODEL)
            # The probe starts the CLI without a model, so /status names the CLI's own pick.
            result, _proc, popen, _reap = self.fetch_usage(
                return_value=STATUS_PANEL.replace("GPT-6.1-Sol", "GPT-6-Luna")
            )
            self.assertEqual(result["status"], "ok")
            self.assertNotIn("--model", popen.call_args.args[0])
            self.assertEqual(server.default_model(options), "gpt-6-luna")
            # It is not part of the quota the worker reports.
            self.assertNotIn("_default_model", server.usage_status_payload())
            # A later check that sees no model keeps the last one.
            self.fetch_usage(return_value="5h limit: 10% left  weekly limit: 20% left")
            self.assertEqual(server.default_model(options), "gpt-6-luna")
            # A model named in the add-on options is not left to the CLI.
            self.assertEqual(server.default_model({"codex_model": "gpt-6-sol"}), "gpt-6-sol")
            self.assertEqual(server.default_model({"codex_model": "gpt-5.5"}), "gpt-5.6-sol")

    def test_a_change_of_account_forgets_the_model_the_cli_picked(self) -> None:
        """Signing out forgets the remembered model, and a usage check that was running meanwhile does not restore it."""
        options = {"codex_model": "default"}
        luna_panel = STATUS_PANEL.replace("GPT-6.1-Sol", "GPT-6-Luna")
        with patch.dict(server.usage_state):
            self.fetch_usage(return_value=luna_panel)
            self.assertEqual(server.default_model(options), "gpt-6-luna")
            with (
                patch.object(server, "active_task_id", return_value=None),
                patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")),
                patch.object(server, "codex_env", return_value={}),
                patch.object(server, "codex_login_status", return_value={"status_ok": False}),
                patch.object(server, "dismiss_persistent_notification"),
                patch.object(server, "notify"),
                patch.object(server, "refresh_usage_status_async"),
            ):
                self.assertTrue(server.logout_codex()["ok"])
            self.assertEqual(server.default_model(options), server.CLI_DEFAULT_MODEL)

            def sign_out_during_the_check(_fd: int) -> str:
                """Forget the model while /status is being read, then return the previous account's panel."""
                server.forget_default_model()
                return luna_panel

            self.fetch_usage(side_effect=sign_out_during_the_check)
            self.assertEqual(server.default_model(options), server.CLI_DEFAULT_MODEL)
            # The next check, with the new account, is remembered again.
            self.fetch_usage(return_value=STATUS_PANEL.replace("GPT-6.1-Sol", "GPT-6-Astra"))
            self.assertEqual(server.default_model(options), "gpt-6-astra")

    def test_status_probe_answers_both_folder_trust_prompts(self) -> None:
        """The status probe accepts the folder trust prompt of CLI 0.154 and of CLI 0.157 before it sends /status."""
        prompts = {
            "0.154": "Do you trust the contents of this directory?\n1. Yes, continue\n2. No, quit",
            "0.157": "Folder access\n/config\nTrust this folder?\n\u203a 1. Trust and continue\n2. Quit",
        }
        for version, prompt in prompts.items():
            with self.subTest(version=version):
                reads = iter([prompt, "", "5h limit: 10% used  weekly limit: 20% used"])
                with (
                    patch.object(server, "_read_pty", side_effect=lambda *_args: next(reads, "")),
                    patch.object(server.os, "write") as write,
                    patch.object(server.time, "sleep"),
                ):
                    server._capture_status_from_tui(10)

                sent = [call.args[1] for call in write.call_args_list]
                self.assertEqual(sent[:3], [b"1\r", b"/status", b"\r"])


class ModelSelectionTests(unittest.TestCase):
    """Which model reaches app-server for default, legacy, retired and named add-on options."""

    def session_for_model(self, model, session_id=None):
        return server.create_approval_session(
            "task", StubAppServer(session_id or "thread"), "Prompt", Path("final"), session_id, {},
            {"codex_model": model, "codex_sandbox": "workspace-write"},
        )

    def test_worker_model_schema_matches_supported_choices(self) -> None:
        """config.yaml and the worker default the model to "default", and the schema lists the accepted models."""
        config = server.yaml.safe_load(
            (SERVER_PATH.parent / "config.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(config["options"]["codex_model"], "default")
        self.assertEqual(server.DEFAULT_OPTIONS["codex_model"], "default")
        self.assertEqual(
            config["schema"]["codex_model"],
            "list(default|gpt-6-astra|gpt-6.1-sol|gpt-6.1-terra|gpt-6-sol|gpt-6-luna|gpt-5.6-sol|gpt-5.6-terra|gpt-5.6-luna|gpt-5.5)",
        )

    def test_home_assistant_api_url_defaults_to_core(self) -> None:
        """config.yaml and the worker default the API URL to Home Assistant's own address, where HA_TOKEN works."""
        config = server.yaml.safe_load(
            (SERVER_PATH.parent / "config.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(config["options"]["ha_url"], "http://homeassistant:8123")
        self.assertEqual(server.DEFAULT_OPTIONS["ha_url"], "http://homeassistant:8123")
        self.assertEqual(server.codex_ha_url(config["options"]), "http://homeassistant:8123")
        translations = server.yaml.safe_load(
            (SERVER_PATH.parent / "translations" / "en.yaml").read_text(encoding="utf-8")
        )
        self.assertIn("HA_URL", translations["configuration"]["ha_url"]["description"])

    def test_default_model_omits_model_argument(self) -> None:
        """The default option lets Codex pick the model, and the model the chat names meanwhile is one on offer."""
        session = self.session_for_model("default")

        self.assertNotIn("model", session.thread_params)
        # The chat names the bundled CLI's pick until the CLI reports its own; it must be a model on offer.
        self.assertIn(server.CLI_DEFAULT_MODEL, server.CHAT_MODEL_EFFORTS)

    def test_legacy_default_model_omits_model_argument(self) -> None:
        """The old default gpt-5.3-codex is treated like the default model option."""
        session = self.session_for_model("gpt-5.3-codex")

        self.assertNotIn("model", session.thread_params)

    def test_retired_option_model_runs_on_the_next_model_up(self) -> None:
        """A retired model saved in the add-on options runs on the model that replaces it."""
        session = self.session_for_model("gpt-5.5")

        self.assertEqual(session.thread_params["model"], "gpt-5.6-sol")

    def test_retired_models_stay_accepted_and_lead_to_an_offered_model(self) -> None:
        """Every retired model stays in the option schema, is not offered in chats, and leads to a model that is."""
        config = server.yaml.safe_load(
            (SERVER_PATH.parent / "config.yaml").read_text(encoding="utf-8")
        )
        accepted = config["schema"]["codex_model"].removeprefix("list(").removesuffix(")").split("|")

        for model in server.RETIRED_MODELS:
            with self.subTest(model=model):
                # Home Assistant does not start an app whose saved option is missing from the list.
                self.assertIn(model, accepted)
                self.assertNotIn(model, server.CHAT_MODEL_EFFORTS)
                self.assertIn(server.current_model(model), server.CHAT_MODEL_EFFORTS)

    def test_explicit_model_is_passed_to_codex(self) -> None:
        """A model named in the options is passed to app-server on new and resumed sessions."""
        for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-terra"):
            for session_id in (None, "019fc242-910a-7c92-a17d-54c014e19fc4"):
                with self.subTest(model=model, session_id=session_id):
                    session = self.session_for_model(model, session_id)

                    self.assertEqual(session.thread_params["model"], model)
                    self.assertEqual(session.turn_params["effort"], "medium")
                    self.assertEqual(session.thread_params.get("threadId"), session_id)


class RuntimeConfigTests(unittest.TestCase):
    """The Codex configuration the worker writes when it starts."""

    def test_runtime_config_disables_startup_update_check(self) -> None:
        """The config.toml the worker writes turns off the CLI's update check on startup."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(server, "CODEX_HOME", root / "codex-home"),
                patch.object(server, "DATA_ROOT", root),
                patch.object(server, "AUTH_QR_DIR", root / "auth-qr"),
                patch.object(server, "CODEX_CONFIG_PATH", root / "codex-home" / "config.toml"),
                patch.object(server, "task_root", return_value=root / "tasks"),
                patch.object(server, "api_token", return_value="configured"),
            ):
                server.ensure_runtime_files()
                config = (root / "codex-home" / "config.toml").read_text(encoding="utf-8")

        self.assertIn("check_for_update_on_startup = false", config)


class HealthRouteTests(unittest.TestCase):
    """The worker's /health route."""

    def test_health_keeps_legacy_fields_and_adds_runtime_diagnostics(self) -> None:
        """/health keeps its original fields and adds the Codex version and the sandbox readiness."""
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
    """A task whose Codex process cannot start, or cannot take its prompt, ends as failed."""

    def test_popen_failure_marks_task_failed(self) -> None:
        """When Codex cannot be started, the task and its result event are failed and keep the session id."""
        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        updates: list[dict[str, object]] = []
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "task"
            with (
                patch.object(server, "get_task_dir", return_value=task_dir),
                patch.object(server, "update_task", side_effect=lambda _task_id, **values: updates.append(values)),
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

    def test_stdin_write_and_flush_failures_are_cleaned_up(self) -> None:
        """A broken pipe on the first request fails the task and reaps the process."""
        class FakeProcess(StubAppServer):
            def __init__(self, fail_at):
                super().__init__(session_id)
                self.fail_at = fail_at
                self.terminated = False
                self.wait_calls = 0

            def write(self, _value):
                if self.fail_at == "write":
                    raise BrokenPipeError(32, "broken pipe")

            def flush(self):
                if self.fail_at == "flush":
                    raise BrokenPipeError(32, "broken pipe")

            def terminate(self):
                self.terminated = True
                super().terminate()

            def wait(self, timeout=None):
                self.wait_calls += 1
                return super().wait(timeout)

        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        for fail_at in ("write", "flush"):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temp_dir:
                task_id = f"task-{fail_at}"
                task_dir = Path(temp_dir) / task_id
                proc = FakeProcess(fail_at)
                updates: list[dict[str, object]] = []
                events: list[dict[str, object]] = []
                server.running_processes.pop(task_id, None)
                task = {"task_id": task_id, "status": "queued"}

                def record_update(_task_id, **values):
                    task.update(values)
                    updates.append(values)
                    return True

                with (
                    patch.object(server, "tasks", {task_id: task}),
                    patch.object(server, "get_task_dir", return_value=task_dir),
                    patch.object(
                        server,
                        "update_task",
                        side_effect=record_update,
                    ),
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
    """A task whose background thread cannot start ends as failed and frees the worker."""

    def setUp(self) -> None:
        """Start with no tasks, running processes or task runners, and keep the existing ones to restore."""
        self.saved_tasks = dict(server.tasks)
        self.saved_processes = dict(server.running_processes)
        self.saved_runners = set(server.active_task_runners)
        server.tasks.clear()
        server.running_processes.clear()
        server.active_task_runners.clear()

    def tearDown(self) -> None:
        """Put back the tasks, running processes and task runners from before the test."""
        server.tasks.clear()
        server.tasks.update(self.saved_tasks)
        server.running_processes.clear()
        server.running_processes.update(self.saved_processes)
        server.active_task_runners.clear()
        server.active_task_runners.update(self.saved_runners)

    @staticmethod
    def capture_event(events: list[dict[str, object]]):
        """Return a stand-in for fire_ha_event that adds each event's data to the given list."""
        def _capture(_event_type: str, data: dict[str, object]) -> tuple[bool, str]:
            """Keep the event data and report the event as sent."""
            events.append(data)
            return True, ""

        return _capture

    def test_create_thread_start_failure_is_terminal_and_releases_slot(self) -> None:
        """A new task whose thread cannot start gets a 500, is failed with one event, and no longer counts as active."""
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
        """A reply whose thread cannot start gets a 500, fails the task, keeps its session id, and frees the worker."""
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
                patch.object(server, "session_available", return_value=True),
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
    """Cancelling a task while queued, during preflight or while Codex runs, and stopping a process that resists."""

    def setUp(self) -> None:
        """Start with no tasks, running processes or task runners, and keep the existing ones to restore."""
        self.saved_tasks = dict(server.tasks)
        self.saved_processes = dict(server.running_processes)
        self.saved_runners = set(server.active_task_runners)
        server.tasks.clear()
        server.running_processes.clear()
        server.active_task_runners.clear()

    def tearDown(self) -> None:
        """Put back the tasks, running processes and task runners from before the test."""
        server.tasks.clear()
        server.tasks.update(self.saved_tasks)
        server.running_processes.clear()
        server.running_processes.update(self.saved_processes)
        server.active_task_runners.clear()
        server.active_task_runners.update(self.saved_runners)

    @staticmethod
    def capture_event(events: list[dict[str, object]]):
        """Return a stand-in for fire_ha_event that adds each event's data to the given list."""
        def _capture(_event_type: str, data: dict[str, object]) -> tuple[bool, str]:
            """Keep the event data and report the event as sent."""
            events.append(data)
            return True, ""

        return _capture

    def test_queued_cancellation_is_terminal_and_event_is_emitted_once(self) -> None:
        """A cancelled queued task never starts Codex and fires one cancelled event, also when cancelled twice."""
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
        """A task cancelled during the sandbox check stays cancelled when the check then fails; Codex never starts."""
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
                    """Cancel the task and try to start another one mid-check, then report the sandbox as not ready."""
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
        """Cancelling a running task terminates and reaps Codex, blocks new tasks meanwhile, and ends as cancelled."""
        task_id = "running-task"
        events: list[dict[str, object]] = []
        server.tasks[task_id] = {"task_id": task_id, "status": "queued"}

        class FakeProcess(StubAppServer):
            def __init__(self):
                super().__init__("019fc242-910a-7c92-a17d-54c014e19fc4", hold=True)
                self.reaping = Event()
                self.release_exit = Event()

            def wait(self, timeout=None):
                self.reaping.set()
                if not self.release_exit.wait(5):
                    raise subprocess.TimeoutExpired("codex", timeout)
                return super().wait(timeout)

        proc = FakeProcess()
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(server, "get_task_dir", return_value=Path(temp_dir) / task_id),
                patch.object(server, "save_task_index"),
                patch.object(server, "api_token", return_value="test-token"),
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
                headers = {"Authorization": "Bearer test-token"}
                thread = server.start_background_task(task_id, "cancel while running")
                self.assertTrue(proc.started.wait(5))
                cancelled = []
                cancel_thread = Thread(target=lambda: cancelled.append(
                    server.app.test_client().post(f"/tasks/{task_id}/cancel", headers=headers).status_code))
                cancel_thread.start()
                try:
                    self.assertTrue(proc.reaping.wait(5))
                    overlap = server.app.test_client().post("/tasks", headers=headers, json={"prompt": "must remain blocked"})
                    self.assertEqual(overlap.status_code, 409)
                finally:
                    proc.release_exit.set()
                    thread.join(timeout=5)
                    cancel_thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled, [200])
        self.assertIsNotNone(proc.poll())
        self.assertNotIn(task_id, server.running_processes)
        self.assertNotIn(task_id, server.active_task_runners)
        self.assertEqual(server.tasks[task_id]["status"], "cancelled")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "cancelled")

    def test_terminate_and_reap_escalates_after_timeout(self) -> None:
        """A process that ignores terminate is killed and reaped, and its exit is confirmed."""
        class StubbornProcess:
            """A process that ignores terminate and exits only when killed."""

            def __init__(self) -> None:
                """Start as a running process that has received no signal."""
                self.returncode: int | None = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                """Return the exit code, or None while the process runs."""
                return self.returncode

            def terminate(self) -> None:
                """Record the request and keep running."""
                self.terminated = True

            def kill(self) -> None:
                """Record the request and exit as killed."""
                self.killed = True
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                """Time out on the first call, then return the exit code."""
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
        """A process that survives terminate and kill stays registered, so its task stays active and blocks new ones."""
        class UnkillableProcess:
            """A process that keeps running through terminate and kill."""

            def __init__(self) -> None:
                """Start as a running process that has received no signal."""
                self.returncode: int | None = None
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                """Report the process as still running."""
                return None

            def terminate(self) -> None:
                """Record the request and keep running."""
                self.terminated = True

            def kill(self) -> None:
                """Record the request and keep running."""
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                """Count the call and time out."""
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


class WorkerNoteTests(unittest.TestCase):
    """The notes the worker adds to a task's details."""

    def test_notes_keep_file_names_as_inline_code(self) -> None:
        """Paths in the worker's notes are Markdown inline code, so the chat shows their underscores and asterisks."""
        self.assertEqual(server.code_span("custom_components/foo/__init__.py"), "`custom_components/foo/__init__.py`")
        # A backtick in a name gets a longer fence, and padding when it is at an end.
        self.assertEqual(server.code_span("odd`name.yaml"), "``odd`name.yaml``")
        self.assertEqual(server.code_span("`quoted`"), "`` `quoted` ``")
        self.assertEqual(
            server.unsaved_note(["__pycache__/a.yaml", "b.yaml"]),
            "No copy of the previous version was saved for: `__pycache__/a.yaml`, `b.yaml`. "
            "Use a Home Assistant backup to restore such a file.",
        )
        self.assertIn(", and 2 more", server.unsaved_note([f"file_{n}.yaml" for n in range(12)]))
        # A message from the configuration check names no file and is left as it is.
        self.assertEqual(
            server.validation_details(["packages/__init__.yaml: bad", "Home Assistant configuration check failed: x"], {}, []),
            "Validation errors: `packages/__init__.yaml`: bad; Home Assistant configuration check failed: x",
        )


if __name__ == "__main__":
    unittest.main()
