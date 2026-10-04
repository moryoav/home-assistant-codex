from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
if importlib.util.find_spec("websocket") is None:
    sys.modules["websocket"] = types.ModuleType("websocket")
SPEC = importlib.util.spec_from_file_location("codex_worker_server_local_docs", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def hours_ago(hours: float) -> str:
    """Return a download time the given number of hours before now."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


class LocalDocsTestCase(unittest.TestCase):
    """Shared setup: documentation folders in a temp directory and options the tests can change."""

    def setUp(self) -> None:
        """Isolate the documentation storage and options in a temp directory."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "data"
        self.data.mkdir()
        self.options = {"local_docs": True}
        self.stack.enter_context(patch.object(server, "HA_DOCS_ROOT", self.data / "ha-docs"))
        self.stack.enter_context(patch.object(server, "read_options", side_effect=lambda: dict(self.options)))
        self.stack.enter_context(
            patch.dict(server.ha_docs_state, {"error": "", "_failed_monotonic": 0.0, "_refreshing": False})
        )
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.active = server.HA_DOCS_ROOT
        self.staged = server.ha_docs_sibling("staged")
        self.download = server.ha_docs_sibling("download")

    def write_copy(self, root: Path, *, fetched_at: str | None = None, commit: str = "a" * 40) -> None:
        """Write one integration page, and the download record when a time is given."""
        page = root / "source" / "_integrations" / "light.markdown"
        page.parent.mkdir(parents=True)
        page.write_text(f"Light documentation from {commit}\n", encoding="utf-8")
        if fetched_at is not None:
            (root / server.HA_DOCS_INFO_FILE).write_text(
                json.dumps({"commit": commit, "fetched_at": fetched_at}), encoding="utf-8"
            )

    def page(self, root: Path) -> str:
        """Return the integration page of a copy."""
        return (root / "source" / "_integrations" / "light.markdown").read_text(encoding="utf-8")


class DownloadTests(LocalDocsTestCase):
    """The Git commands of a download and the environment they run in."""

    def fake_git(self, *, pages: bool = True):
        """Return recorded calls and a stand-in for Git that creates the files a clone would."""
        calls: list[tuple[str, ...]] = []

        def run(*args: str) -> str:
            """Act out one Git command on the filesystem."""
            calls.append(args)
            if args[0] == "clone":
                (Path(args[-1]) / ".git").mkdir(parents=True)
            elif "checkout" in args and pages:
                self.write_copy(Path(args[1]))
            elif "rev-parse" in args:
                return "b" * 40
            return ""

        return calls, run

    def test_download_fetches_only_the_text_folders_and_drops_git_metadata(self) -> None:
        """The clone is shallow, blobless and sparse, and leaves a download record without .git."""
        calls, run = self.fake_git()
        with patch.object(server, "_ha_docs_git", side_effect=run):
            info = server.download_ha_docs(self.download)

        clone, sparse, checkout, _ = calls
        self.assertEqual(clone[0], "clone")
        for flag in ("--depth", "--filter=blob:none", "--no-checkout", "--single-branch"):
            self.assertIn(flag, clone)
        self.assertEqual(clone[clone.index("--config") + 1], "core.symlinks=false")
        self.assertEqual(clone[-2:], (server.HA_DOCS_REPOSITORY, str(self.download)))
        self.assertEqual(sparse[2:5], ("sparse-checkout", "set", "--no-cone"))
        self.assertIn("/source/_integrations/", sparse)
        self.assertNotIn("/source/images/", sparse)
        self.assertEqual(checkout[2:], ("checkout", "--quiet", "current"))
        self.assertFalse((self.download / ".git").exists())
        self.assertEqual(info["commit"], "b" * 40)
        self.assertEqual(server.ha_docs_info(self.download), info)

    def test_download_replaces_an_interrupted_attempt(self) -> None:
        """Files left by an earlier attempt do not end up in the new download."""
        (self.download / "source").mkdir(parents=True)
        (self.download / "source" / "stale.markdown").write_text("stale", encoding="utf-8")
        _, run = self.fake_git()
        with patch.object(server, "_ha_docs_git", side_effect=run):
            server.download_ha_docs(self.download)

        self.assertFalse((self.download / "source" / "stale.markdown").exists())

    def test_download_without_integration_pages_is_not_a_complete_copy(self) -> None:
        """A checkout that produced no integration pages fails and gets no download record."""
        _, run = self.fake_git(pages=False)
        with patch.object(server, "_ha_docs_git", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "source/_integrations"):
                server.download_ha_docs(self.download)

        self.assertIsNone(server.ha_docs_info(self.download))

    def test_git_runs_without_home_assistant_credentials_or_prompts(self) -> None:
        """Git gets no Supervisor or Home Assistant token or address, cannot prompt, and has a time limit."""
        self.options["HA_TOKEN"] = "long-lived-token"
        self.assertIn("HA_URL", server.codex_env())
        completed = subprocess.CompletedProcess([], 0, stdout="abc\n", stderr="")
        with (
            patch.dict(server.os.environ, {"SUPERVISOR_TOKEN": "supervisor", "HASSIO_TOKEN": "hassio"}),
            patch.object(server.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(server._ha_docs_git("rev-parse", "HEAD"), "abc")

        self.assertEqual(run.call_args.args[0], ["git", "rev-parse", "HEAD"])
        env = run.call_args.kwargs["env"]
        for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HA_TOKEN", "HA_URL"):
            self.assertNotIn(key, env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(run.call_args.kwargs["timeout"], server.HA_DOCS_GIT_TIMEOUT_SECONDS)

    def test_git_gives_up_on_a_stalled_transfer_and_does_not_read_the_worker_input(self) -> None:
        """Git ends a transfer that stops moving, and its input is not the worker's Supervisor channel."""
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(server.subprocess, "run", return_value=completed) as run:
            server._ha_docs_git("clone")

        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_HTTP_LOW_SPEED_LIMIT"], "1000")
        self.assertEqual(env["GIT_HTTP_LOW_SPEED_TIME"], str(server.HA_DOCS_GIT_STALL_SECONDS))
        self.assertLess(server.HA_DOCS_GIT_STALL_SECONDS, server.HA_DOCS_GIT_TIMEOUT_SECONDS)
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_git_failure_reports_the_error_output(self) -> None:
        """A failing Git command raises with what Git wrote to standard error."""
        failed = subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: could not resolve host\n")
        with patch.object(server.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "could not resolve host"):
                server._ha_docs_git("clone")


# The worker runs on Linux. On Windows the fixture's symbolic link and Git's read-only pack files get in the way.
@unittest.skipUnless(os.name == "posix" and shutil.which("git"), "needs Git on a POSIX system")
class RealGitDownloadTests(LocalDocsTestCase):
    """A download from a local repository with the installed Git."""

    def git(self, *args: str) -> str:
        """Run Git in the fixture repository and return its output."""
        return subprocess.run(
            ["git", "-C", str(self.upstream), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_download_from_a_repository_keeps_only_the_selected_folders(self) -> None:
        """Real Git fetches the selected folders, and a symbolic link arrives as a plain file."""
        self.upstream = self.data.parent / "upstream"
        files = {
            "README.md": "website",
            "source/_integrations/light.markdown": "light",
            "source/_docs/automation/trigger.markdown": "trigger",
            "source/_posts/2026-10-01-release.markdown": "release notes",
            "source/images/screenshot.png": "image",
        }
        for name, content in files.items():
            path = self.upstream / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        outside = self.data.parent / "outside.txt"
        outside.write_text("not documentation", encoding="utf-8")
        (self.upstream / "source" / "_integrations" / "link.markdown").symlink_to(outside)
        self.git("init", "--quiet", "--initial-branch", server.HA_DOCS_BRANCH)
        # Let the fixture serve a blobless clone the way GitHub does.
        self.git("config", "uploadpack.allowFilter", "true")
        self.git("config", "uploadpack.allowAnySHA1InWant", "true")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Documentation")

        with patch.object(server, "HA_DOCS_REPOSITORY", self.upstream.as_uri()):
            info = server.download_ha_docs(self.download)

        self.assertEqual(info["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.page(self.download), "light")
        self.assertTrue((self.download / "source" / "_docs" / "automation" / "trigger.markdown").is_file())
        for excluded in ("README.md", ".git", "source/_posts", "source/images"):
            self.assertFalse((self.download / excluded).exists(), excluded)
        link = self.download / "source" / "_integrations" / "link.markdown"
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(encoding="utf-8"), str(outside))


class RefreshTests(LocalDocsTestCase):
    """When a new copy is downloaded, and what a finished or failed download leaves behind."""

    def test_refresh_is_due_without_a_copy_and_after_a_day(self) -> None:
        """A missing copy and a copy older than a day are due; a recent one is not."""
        self.assertTrue(server.ha_docs_refresh_due())

        self.write_copy(self.active, fetched_at=hours_ago(2))
        self.assertFalse(server.ha_docs_refresh_due())

        shutil.rmtree(self.active)
        self.write_copy(self.active, fetched_at=hours_ago(25))
        self.assertTrue(server.ha_docs_refresh_due())

    def test_a_waiting_download_counts_as_the_newest_copy(self) -> None:
        """A recent download that is not in use yet prevents another download."""
        self.write_copy(self.active, fetched_at=hours_ago(25))
        self.write_copy(self.staged, fetched_at=hours_ago(1))

        self.assertFalse(server.ha_docs_refresh_due())

    def test_unreadable_or_future_download_time_is_refreshed(self) -> None:
        """A download time that cannot be compared with now counts as due."""
        for fetched_at in ("not a time", "2026-10-01T10:00:00", hours_ago(-48)):
            with self.subTest(fetched_at=fetched_at):
                shutil.rmtree(self.active, ignore_errors=True)
                self.write_copy(self.active, fetched_at=fetched_at)
                self.assertTrue(server.ha_docs_refresh_due())

    def test_refresh_stages_the_download_and_leaves_the_current_copy(self) -> None:
        """A new download waits next to the copy in use instead of replacing it."""
        self.write_copy(self.active, fetched_at=hours_ago(30), commit="1" * 40)

        def download(target: Path) -> dict[str, str]:
            """Stand in for a successful download of a newer commit."""
            self.write_copy(target, fetched_at=hours_ago(0), commit="2" * 40)
            return {"commit": "2" * 40}

        with patch.object(server, "download_ha_docs", side_effect=download):
            server._refresh_ha_docs_worker()

        self.assertIn("1" * 40, self.page(self.active))
        self.assertIn("2" * 40, self.page(self.staged))
        self.assertFalse(self.download.exists())
        self.assertFalse(server.ha_docs_state["_refreshing"])
        self.assertEqual(server.ha_docs_state["error"], "")

    def test_first_download_is_usable_without_waiting_for_a_task(self) -> None:
        """With no copy in use, a finished download becomes the copy at once."""

        def download(target: Path) -> dict[str, str]:
            """Stand in for a successful first download."""
            self.write_copy(target, fetched_at=hours_ago(0))
            return {"commit": "a" * 40}

        with patch.object(server, "download_ha_docs", side_effect=download):
            server._refresh_ha_docs_worker()

        self.assertIsNotNone(server.ha_docs_info(self.active))
        self.assertFalse(self.staged.exists())

    def test_failed_refresh_keeps_the_copy_and_waits_before_retrying(self) -> None:
        """A failed download changes nothing, records the error, and is retried after an hour."""
        self.write_copy(self.active, fetched_at=hours_ago(30))

        def download(target: Path) -> dict[str, str]:
            """Stand in for a download that fails after creating its folder."""
            target.mkdir()
            raise RuntimeError("fatal: could not resolve host")

        with patch.object(server, "download_ha_docs", side_effect=download) as attempt:
            server._refresh_ha_docs_worker()
            server._refresh_ha_docs_worker()

        self.assertEqual(attempt.call_count, 1)
        self.assertTrue(self.active.is_dir())
        self.assertFalse(self.download.exists())
        self.assertFalse(self.staged.exists())
        self.assertIn("could not resolve host", server.ha_docs_state["error"])
        self.assertFalse(server.ha_docs_state["_refreshing"])

        retry_at = server.time.monotonic() + server.HA_DOCS_RETRY_INTERVAL_SECONDS + 1
        with patch.object(server.time, "monotonic", return_value=retry_at):
            self.assertTrue(server.ha_docs_refresh_due())

    def test_refresh_does_not_run_while_fresh_or_already_running(self) -> None:
        """Nothing is downloaded for a recent copy or while another download runs."""
        self.write_copy(self.active, fetched_at=hours_ago(1))
        with patch.object(server, "download_ha_docs") as download:
            server._refresh_ha_docs_worker()
            shutil.rmtree(self.active)
            server.ha_docs_state["_refreshing"] = True
            server._refresh_ha_docs_worker()

        download.assert_not_called()
        self.assertTrue(server.ha_docs_state["_refreshing"])


class ActivationTests(LocalDocsTestCase):
    """Switching to a finished download, and what a task start does with the copy."""

    def test_activation_replaces_the_current_copy_with_the_waiting_download(self) -> None:
        """The waiting download becomes the copy and no working folder is left."""
        self.write_copy(self.active, fetched_at=hours_ago(30), commit="1" * 40)
        self.write_copy(self.staged, fetched_at=hours_ago(1), commit="2" * 40)

        server.activate_ha_docs()

        self.assertIn("2" * 40, self.page(self.active))
        self.assertFalse(self.staged.exists())
        self.assertFalse(server.ha_docs_sibling("old").exists())

    def test_first_download_becomes_the_current_copy(self) -> None:
        """Activation works when there is no copy to replace."""
        self.write_copy(self.staged, fetched_at=hours_ago(0))

        server.activate_ha_docs()

        self.assertIsNotNone(server.ha_docs_info(self.active))

    def test_incomplete_download_is_not_activated(self) -> None:
        """A waiting folder without a download record does not replace the copy."""
        self.write_copy(self.active, fetched_at=hours_ago(30), commit="1" * 40)
        self.write_copy(self.staged, commit="2" * 40)

        server.activate_ha_docs()

        self.assertIn("1" * 40, self.page(self.active))

    def test_a_failed_switch_puts_back_the_copy_in_use(self) -> None:
        """When the download cannot be moved into place, the old copy is restored and the download waits."""
        self.write_copy(self.active, fetched_at=hours_ago(30), commit="1" * 40)
        self.write_copy(self.staged, fetched_at=hours_ago(1), commit="2" * 40)
        rename = Path.rename

        def failing_rename(path: Path, target: Path) -> Path:
            """Refuse to move the waiting download and move everything else."""
            if path == self.staged:
                raise OSError("input/output error")
            return rename(path, target)

        with patch.object(Path, "rename", autospec=True, side_effect=failing_rename):
            with self.assertRaises(OSError):
                server.activate_ha_docs()

        self.assertIn("1" * 40, self.page(self.active))
        self.assertIn("2" * 40, self.page(self.staged))
        self.assertFalse(server.ha_docs_sibling("old").exists())

        server.activate_ha_docs()

        self.assertIn("2" * 40, self.page(self.active))

    def started_threads(self) -> list[object]:
        """Replace thread creation and return the list of requested threads."""
        thread = self.stack.enter_context(patch.object(server.threading, "Thread"))
        return thread.call_args_list

    def test_prepare_activates_and_starts_a_background_refresh(self) -> None:
        """Preparing for a task switches to the waiting download and starts one refresh thread."""
        self.write_copy(self.staged, fetched_at=hours_ago(1))
        threads = self.started_threads()

        server.prepare_ha_docs()

        self.assertIsNotNone(server.ha_docs_info(self.active))
        self.assertEqual(len(threads), 1)
        self.assertIs(threads[0].kwargs["target"], server._refresh_ha_docs_worker)
        self.assertTrue(threads[0].kwargs["daemon"])

    def test_prepare_does_nothing_when_disabled_or_without_app_storage(self) -> None:
        """With the option off or no storage folder, nothing is switched or downloaded."""
        self.write_copy(self.staged, fetched_at=hours_ago(1))
        threads = self.started_threads()

        self.options["local_docs"] = False
        server.prepare_ha_docs()
        self.options["local_docs"] = True
        with patch.object(server, "HA_DOCS_ROOT", self.data / "missing" / "ha-docs"):
            server.prepare_ha_docs()

        self.assertFalse(self.active.exists())
        self.assertEqual(threads, [])

    def test_a_failed_switch_still_starts_the_task(self) -> None:
        """A filesystem error while switching copies does not stop task preparation."""
        threads = self.started_threads()
        with patch.object(server, "activate_ha_docs", side_effect=OSError("read-only file system")):
            server.prepare_ha_docs()

        self.assertEqual(len(threads), 1)


class PromptTests(LocalDocsTestCase):
    """The paragraph in the task prompt and the entry in the health response."""

    def test_prompt_points_to_the_local_copy_when_it_is_complete(self) -> None:
        """The prompt names the folder, the download date, and the fallback to web search."""
        self.write_copy(self.active, fetched_at="2026-10-01T08:30:00+00:00")

        prompt = server.build_prompt("Add a motion light", "task")

        self.assertIn(f"stored locally in {self.active / 'source'}", prompt)
        self.assertIn("downloaded 2026-10-01", prompt)
        self.assertIn("_integrations/<domain>.markdown", prompt)
        self.assertIn("Use web search for custom integrations", prompt)
        self.assertLess(prompt.index("stored locally"), prompt.index("At the end, return only an object"))

    def test_prompt_omits_the_note_without_a_complete_copy_or_when_disabled(self) -> None:
        """No copy, a copy without a download record, and the option off all leave the prompt unchanged."""
        self.assertNotIn("stored locally", server.build_prompt("x", "task"))

        self.write_copy(self.active)
        self.assertNotIn("stored locally", server.build_prompt("x", "task"))

        shutil.rmtree(self.active)
        self.write_copy(self.active, fetched_at=hours_ago(1))
        self.options["local_docs"] = False
        self.assertNotIn("stored locally", server.build_prompt("x", "task"))

    def test_status_reports_the_copy_and_the_last_error(self) -> None:
        """The health entry is empty without a copy and then carries its commit, time, and error."""
        self.assertEqual(
            server.ha_docs_status(),
            {"enabled": True, "available": False, "commit": "", "fetched_at": "", "error": ""},
        )

        self.write_copy(self.active, fetched_at="2026-10-01T08:30:00+00:00", commit="c" * 40)
        server.ha_docs_state["error"] = "fatal: could not resolve host"

        self.assertEqual(
            server.ha_docs_status(),
            {
                "enabled": True,
                "available": True,
                "commit": "c" * 40,
                "fetched_at": "2026-10-01T08:30:00+00:00",
                "error": "fatal: could not resolve host",
            },
        )


if __name__ == "__main__":
    unittest.main()
