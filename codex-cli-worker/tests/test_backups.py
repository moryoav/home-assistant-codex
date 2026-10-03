"""Per-file backups, the optional full snapshot, change detection, and backup cleanup."""
from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_server import server


def sha(path: Path) -> str:
    """The hash the worker records for a file."""
    return server.file_hash(path)


class SavedCopyTests(unittest.TestCase):
    """Which copies count as the previous version of a changed file."""

    def setUp(self):
        """Use a temporary config tree and the folder of one exchange."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = root / "config"
        self.config.mkdir()
        self.run_dir = root / "run"
        self.backups = self.run_dir / server.BACKUP_DIR
        self.backups.mkdir(parents=True)
        self.stack.enter_context(patch.object(server, "CONFIG_ROOT", self.config))
        self.before = {}

    def original(self, rel, content):
        """Create a config file and record it as the worker saw it before the exchange."""
        path = self.config / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.before[rel] = {"sha256": sha(path)}

    def copy(self, rel, content):
        """Write a copy into the backup folder as Codex would."""
        path = self.backups / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def version(self, rel):
        """Where the worker finds the previous version of one changed file."""
        return server.previous_versions(self.run_dir, [rel], self.before)[0]

    def archive(self, files):
        """Create the full snapshot of the exchange with the given files."""
        with tarfile.open(self.run_dir / server.SNAPSHOT_FILE, "w:gz") as tar:
            for rel, content in files.items():
                data = content.encode("utf-8")
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    def test_identical_copy_is_saved_and_others_are_not(self):
        """Only a copy identical to the earlier file counts; a late or absent copy does not."""
        self.original("automations.yaml", "- alias: old\n")
        self.original("scripts.yaml", "a: 1\n")
        self.original("scenes.yaml", "[]\n")
        saved = self.copy("automations.yaml", "- alias: old\n")
        late = self.copy("scripts.yaml", "a: 2\n")
        self.assertEqual(self.version("automations.yaml"),
                         {"path": "automations.yaml", "status": "saved", "copy": str(saved.resolve())})
        self.assertEqual(self.version("scripts.yaml"),
                         {"path": "scripts.yaml", "status": "unverified", "copy": str(late.resolve())})
        self.assertEqual(self.version("scenes.yaml"),
                         {"path": "scenes.yaml", "status": "missing", "copy": ""})
        self.assertEqual(self.version("../outside.yaml")["status"], "missing")

    def test_credential_copies_are_removed_and_reported_as_excluded(self):
        """Copies of credential files are deleted and never listed as saved."""
        self.original("secrets.yaml", "token: old\n")
        self.original(".storage/auth", "{}")
        self.copy("secrets.yaml", "token: old\n")
        self.copy(".storage/auth", "{}")
        kept = self.copy("automations.yaml", "x")
        changes = {"added": [], "changed": ["secrets.yaml", ".storage/auth"], "deleted": []}
        result = server.review_backups(self.run_dir, self.before, changes, set())
        self.assertEqual(result, [{"path": "secrets.yaml", "status": "excluded", "copy": ""}])
        self.assertFalse((self.backups / "secrets.yaml").exists())
        self.assertFalse((self.backups / ".storage" / "auth").exists())
        self.assertTrue(kept.exists())

    def test_only_files_changed_by_codex_are_reviewed(self):
        """Home Assistant's own storage writes are not reported as files without a copy."""
        for rel in ("automations.yaml", "custom_components/demo/sensor.py", "notes.txt",
                    ".storage/lovelace.kitchen", ".storage/core.restore_state", ".storage/browser_mod.storage"):
            self.original(rel, f"old {rel}\n")
        self.copy("notes.txt", "old notes.txt\n")
        changes = {"added": ["packages/new.yaml"], "deleted": [".storage/lovelace.kitchen"],
                   "changed": ["automations.yaml", "custom_components/demo/sensor.py", "notes.txt",
                               ".storage/core.restore_state", ".storage/browser_mod.storage"]}
        result = server.review_backups(self.run_dir, self.before, changes, {"custom_components/demo/sensor.py", "unchanged.yaml"})
        self.assertEqual([(entry["path"], entry["status"]) for entry in result], [
            (".storage/lovelace.kitchen", "missing"),
            ("automations.yaml", "missing"),
            ("custom_components/demo/sensor.py", "missing"),
            ("notes.txt", "saved"),
        ])

    def test_full_snapshot_supplies_missing_and_wrong_copies(self):
        """With a full snapshot every reviewed file gets its previous version."""
        self.original("automations.yaml", "- alias: old\n")
        self.original("scripts.yaml", "a: 1\n")
        self.copy("scripts.yaml", "a: 2\n")
        self.archive({"automations.yaml": "- alias: old\n", "scripts.yaml": "a: 1\n"})
        changes = {"added": [], "changed": ["automations.yaml", "scripts.yaml"], "deleted": []}
        result = server.review_backups(self.run_dir, self.before, changes, set())
        self.assertEqual([entry["status"] for entry in result], ["saved", "saved"])
        self.assertEqual((self.backups / "automations.yaml").read_text(encoding="utf-8"), "- alias: old\n")
        self.assertEqual((self.backups / "scripts.yaml").read_text(encoding="utf-8"), "a: 1\n")

    def test_links_left_in_the_backup_folder_are_not_followed(self):
        """Links in the backup folder are neither read, written through, nor searched."""
        self.original("automations.yaml", "- alias: old\n")
        self.archive({"automations.yaml": "- alias: old\n"})
        outside = self.run_dir.parent / "outside"
        outside.mkdir()
        (self.backups / "automations.yaml").symlink_to(outside / "automations.yaml")
        self.assertEqual(self.version("automations.yaml")["status"], "missing")
        self.backups.joinpath("automations.yaml").unlink()
        # A linked folder is not searched for copies, so files under it are not taken for Codex's.
        self.original("notes.txt", "old\n")
        (self.backups / "linked").symlink_to(self.config, target_is_directory=True)
        self.assertEqual(server.copied_files(self.run_dir), {})
        self.assertEqual(server.review_backups(self.run_dir, self.before,
                                               {"added": [], "changed": ["notes.txt"], "deleted": []}, set()), [])
        (self.backups / "linked").unlink()
        self.backups.rmdir()
        self.backups.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.version("automations.yaml")["status"], "missing")
        self.assertEqual(server.review_backups(self.run_dir, self.before,
                                               {"added": [], "changed": ["automations.yaml"], "deleted": []}, set())[0]["status"], "missing")
        self.assertEqual(list(outside.iterdir()), [])

    def test_recovery_copies_explain_files_without_a_saved_version(self):
        """A failed validation names the saved copies and says why a file has none."""
        self.original("automations.yaml", "- alias: old\n")
        self.original("scripts.yaml", "a: 1\n")
        self.original("secrets.yaml", "token: old\n")
        saved = self.copy("automations.yaml", "- alias: old\n")
        changes = {"added": ["packages/new.yaml"], "changed": ["automations.yaml", "scripts.yaml", "secrets.yaml"], "deleted": []}
        result = server.recovery_copies(self.run_dir, changes,
                                        ["automations.yaml", "packages/new.yaml", "scripts.yaml", "secrets.yaml", "/etc/passwd"], self.before)
        self.assertEqual(result, [
            {"path": "automations.yaml", "copy": str(saved.resolve())},
            {"path": "packages/new.yaml", "copy": ""},
            {"path": "scripts.yaml", "copy": "", "reason": "no_backup"},
            {"path": "secrets.yaml", "copy": "", "reason": "excluded_credentials"},
        ])
        with patch.object(server, "read_options", return_value={"backup_retention_days": 3}):
            details = server.validation_details(["scripts.yaml: bad"], {}, result)
        # Each path is inline code, so the chat's Markdown leaves its underscores alone.
        self.assertIn("Validation errors: `scripts.yaml`: bad", details)
        self.assertIn(f"are kept for 3 days at: `{saved.resolve()}`", details)
        self.assertIn("New files that did not exist before: `packages/new.yaml`", details)
        self.assertIn("No copy of the previous version was saved for: `scripts.yaml`.", details)
        self.assertIn("excluded from recovery copies", details)


class BackupFolderTests(unittest.TestCase):
    """Where Codex is told to put its copies, and when it is not told at all."""

    def test_folder_depends_on_the_sandbox_and_the_task_folder(self):
        """Codex gets a backup folder only where its sandbox lets it write."""
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            inside = config / "codex_tasks" / "t" / "turns" / "a"
            outside = Path(temp) / "data" / "t"
            with patch.object(server, "CONFIG_ROOT", config):
                self.assertEqual(server.codex_backup_dir(inside, {"codex_sandbox": "workspace-write"}), inside / "backups")
                self.assertEqual(server.codex_backup_dir(inside, {}), inside / "backups")
                self.assertIsNone(server.codex_backup_dir(inside, {"codex_sandbox": "read-only"}))
                self.assertIsNone(server.codex_backup_dir(outside, {"codex_sandbox": "workspace-write"}))
                self.assertEqual(server.codex_backup_dir(outside, {"codex_sandbox": "danger-full-access"}), outside / "backups")

    def test_prompt_carries_the_backup_folder_only_when_there_is_one(self):
        """The backup instruction is in the prompt only when a folder is given."""
        with patch.object(server, "tasks", {}):
            folder = Path("/config/codex_tasks/t/turns/a/backups")
            prompt = server.build_prompt("Edit the automation", "t", backup_dir=folder)
            self.assertIn(f"copy it to {folder} under the same relative path", prompt)
            self.assertIn(f"{folder}/.storage/lovelace.kitchen", prompt)
            self.assertIn("Never copy secrets.yaml", prompt)
            self.assertNotIn("copy it to", server.build_prompt("Edit the automation", "t"))

    def test_retention_setting_is_clamped_and_published(self):
        """The retention setting stays within its range and reaches the web UI."""
        cases = ((None, 7), ("x", 7), (0, 1), (2, 2), ("30", 30), (9999, 365))
        for configured, expected in cases:
            with self.subTest(configured=configured):
                self.assertEqual(server.backup_retention_days({"backup_retention_days": configured}), expected)
        with (
            patch.object(server, "read_options", return_value={"backup_retention_days": 2}),
            patch.object(server, "api_token", return_value="test-token"),
        ):
            response = server.app.test_client().get("/chat-options", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(response.json["backup_retention_days"], 2)


class ManifestTests(unittest.TestCase):
    """The list of files with hashes that change detection compares."""

    def setUp(self):
        """Scan a temporary config tree with an empty cache."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "config"
        self.config.mkdir()
        self.stack.enter_context(patch.object(server, "CONFIG_ROOT", self.config))
        self.stack.enter_context(patch.object(server, "manifest_cache", {"root": "", "scanned_ns": 0, "files": {}}))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.config / "my_tasks"))
        self.hashed = []
        real = server.file_hash
        self.stack.enter_context(patch.object(
            server, "file_hash", side_effect=lambda path: (self.hashed.append(path.name), real(path))[1]))

    def write(self, rel, content, age=None):
        """Write a config file, optionally dated some seconds in the past."""
        path = self.config / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if age:
            os.utime(path, (time.time() - age, time.time() - age))
        return path

    @staticmethod
    def tick():
        """Let the file system clock move on, as it has long done between scans in real use."""
        time.sleep(0.05)

    def test_unchanged_files_are_read_once_and_changes_are_found(self):
        """Files at rest are not hashed again, and every kind of change is still found."""
        self.write("automations.yaml", "- alias: old\n")
        self.write("scripts.yaml", "a: 1\n")
        first = server.build_manifest()
        self.assertEqual(sorted(self.hashed), ["automations.yaml", "scripts.yaml"])
        # A file changed just before a scan may change again within the same timestamp, so it is read again.
        self.hashed.clear()
        self.assertEqual(server.build_manifest(), first)
        self.assertEqual(sorted(self.hashed), ["automations.yaml", "scripts.yaml"])
        # Once the files have been at rest for a while, a scan no longer reads them.
        server.manifest_cache["scanned_ns"] += 10 * server.MANIFEST_SETTLE_NS
        self.hashed.clear()
        self.assertEqual(server.build_manifest(), first)
        self.assertEqual(self.hashed, [])
        server.manifest_cache["scanned_ns"] += 10 * server.MANIFEST_SETTLE_NS
        self.hashed.clear()
        self.tick()
        self.write("automations.yaml", "- alias: new\n")
        self.write("packages/new.yaml", "sensor: []\n")
        (self.config / "scripts.yaml").unlink()
        second = server.build_manifest()
        self.assertEqual(sorted(self.hashed), ["automations.yaml", "new.yaml"])
        self.assertEqual(server.diff_manifests(first, second),
                         {"added": ["packages/new.yaml"], "changed": ["automations.yaml"], "deleted": ["scripts.yaml"]})

    def test_same_size_and_date_with_new_content_is_still_found(self):
        """New content behind an unchanged size and modification time is detected."""
        path = self.write("automations.yaml", "- alias: one\n", age=3600)
        first = server.build_manifest()
        server.manifest_cache["scanned_ns"] += 10 * server.MANIFEST_SETTLE_NS
        stat = path.stat()
        self.tick()
        path.write_text("- alias: two\n", encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = server.build_manifest()
        self.assertEqual(second["automations.yaml"]["mtime_ns"], first["automations.yaml"]["mtime_ns"])
        self.assertEqual(server.diff_manifests(first, second)["changed"], ["automations.yaml"])

    def test_cache_is_not_reused_for_another_config_folder(self):
        """Hashes remembered for one folder are not trusted for another."""
        self.write("automations.yaml", "- alias: old\n")
        server.build_manifest()
        server.manifest_cache.update(root="/elsewhere", scanned_ns=server.manifest_cache["scanned_ns"] + 10 * server.MANIFEST_SETTLE_NS)
        self.hashed.clear()
        server.build_manifest()
        self.assertEqual(self.hashed, ["automations.yaml"])

    def test_the_task_folder_is_left_out_under_any_name(self):
        """The worker's task files never count as configuration changes."""
        self.write("automations.yaml", "- alias: old\n")
        self.write("my_tasks/t/turns/a/backups/automations.yaml", "- alias: old\n")
        self.write("my_tasks/t/task.json", "{}")
        self.assertEqual(list(server.build_manifest()), ["automations.yaml"])


class RunTests(unittest.TestCase):
    """A whole exchange: the baseline, Codex's copies, the result, and the steps shown in the chat."""

    def setUp(self):
        """Run against a temporary config tree with Codex and Home Assistant faked."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = self.root / "config"
        for name, value in (("tasks", {}), ("task_activity", {}), ("active_task_runners", set()), ("running_processes", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "manifest_cache", {"root": "", "scanned_ns": 0, "files": {}}))
        self.stack.enter_context(patch.object(server, "CONFIG_ROOT", self.config))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.config / "codex_tasks"))
        self.stack.enter_context(patch.object(server, "TASK_STATE_FILE", self.root / "index.json"))
        self.stack.enter_context(patch.object(server, "CODEX_HOME", self.root / "codex"))
        self.options = {"task_timeout_seconds": 30, "auto_save_lovelace": False, "config_check": False}
        self.stack.enter_context(patch.object(server, "read_options", side_effect=lambda: dict(self.options)))
        self.stack.enter_context(patch.object(server, "sandbox_readiness", return_value={"required": True, "ready": True}))
        self.stack.enter_context(patch.object(server, "codex_env", return_value={}))
        self.stack.enter_context(patch.object(server, "notify"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.events = []
        self.stack.enter_context(patch.object(
            server, "fire_ha_event", side_effect=lambda _event, data: (self.events.append(data) or True, "")))
        for rel, content in (("automations.yaml", "- alias: old\n"), ("scripts.yaml", "a: 1\n"),
                             (".storage/core.restore_state", '{"data": 1}')):
            path = self.config / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.task_id = "20261002T000000Z-abcdef12"
        self.turn_id = "0123456789abcdef0123456789abcdef"
        server.tasks[self.task_id] = {
            "task_id": self.task_id, "status": "queued", "current_turn_id": self.turn_id,
            "turns": [{"turn_id": self.turn_id, "message": "Rename the automation", "status": "queued"}],
        }
        self.run_dir = self.config / "codex_tasks" / self.task_id / "turns" / self.turn_id

    def codex(self, *, keep_copy):
        """A fake Codex that edits two files and a Home Assistant that rewrites its own state meanwhile."""
        test = self

        class FakeProcess:
            """A stand-in for the Codex process that reports a file change and makes the edits when waited on."""

            def __init__(self, args, **kwargs):
                """Remember where Codex writes its answer and prepare its event stream."""
                del kwargs
                self.output = Path(args[args.index("--output-last-message") + 1])
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in (
                    {"type": "thread.started", "thread_id": "019fc242-910a-7c92-a17d-54c014e19fc4"},
                    {"type": "turn.started"},
                    {"type": "item.completed", "item": {"id": "item_0", "type": "file_change", "status": "completed", "changes": [
                        {"path": str(test.config / "scripts.yaml"), "kind": "update"},
                        {"path": str(test.config / "packages" / "new.yaml"), "kind": "add"}]}},
                )))
                self.stderr = io.StringIO("")
                self.returncode = 0

            def poll(self):
                """Report that the process has exited."""
                return 0

            def wait(self, timeout=None):
                """Make the edits, with or without a copy first, and write the final answer."""
                del timeout
                if keep_copy:
                    backups = self.output.parent / "backups"
                    (backups / "automations.yaml").write_text("- alias: old\n", encoding="utf-8")
                (test.config / "automations.yaml").write_text("- alias: new\n", encoding="utf-8")
                (test.config / "scripts.yaml").write_text("a: 2\n", encoding="utf-8")
                (test.config / "packages").mkdir(exist_ok=True)
                (test.config / "packages" / "new.yaml").write_text("sensor: []\n", encoding="utf-8")
                (test.config / ".storage" / "core.restore_state").write_text('{"data": 2}', encoding="utf-8")
                self.output.write_text(json.dumps({"status": "completed", "summary": "Renamed", "question": "", "details": "Notes"}))
                return 0

        return patch.object(server.subprocess, "Popen", side_effect=FakeProcess)

    def steps(self):
        """The kind, text, and status of the steps recorded for the exchange."""
        with server.lock:
            return [(step["kind"], step["text"], step["status"]) for step in server.task_activity[self.task_id]["steps"]]

    def test_per_file_copies_are_reviewed_without_a_snapshot(self):
        """By default no archive is made and Codex's copies are checked and reported."""
        with self.codex(keep_copy=True):
            server.run_task(self.task_id, "Rename the automation")
        task = server.tasks[self.task_id]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["changes"], {
            "added": ["packages/new.yaml"], "changed": [".storage/core.restore_state", "automations.yaml", "scripts.yaml"], "deleted": []})
        saved = self.run_dir / "backups" / "automations.yaml"
        expected = [
            {"path": "automations.yaml", "status": "saved", "copy": str(saved.resolve())},
            {"path": "scripts.yaml", "status": "missing", "copy": ""},
        ]
        self.assertEqual(task["backups"], expected)
        self.assertEqual(task["turns"][0]["backups"], expected)
        self.assertEqual(self.events[-1]["backups"], expected)
        self.assertEqual(task["details"], "Notes\n\nNo copy of the previous version was saved for: `scripts.yaml`. "
                                          "Use a Home Assistant backup to restore such a file.")
        self.assertFalse((self.run_dir / server.SNAPSHOT_FILE).exists())
        self.assertNotIn("snapshot", task)
        self.assertFalse((self.run_dir / "manifest-after.json").exists())
        self.assertIn(f"copy it to {self.run_dir / 'backups'} under", (self.run_dir / "prompt.txt").read_text(encoding="utf-8"))
        self.assertEqual(self.steps(), [
            ("phase", "Noting the current state of your configuration files", "done"),
            ("phase", "Starting Codex", "done"),
            ("phase", "Codex is thinking", "done"),
            ("file_change", "Edited scripts.yaml, Added packages/new.yaml", "done"),
            ("phase", "Checking the changes", "done"),
        ])

    def test_full_snapshot_fills_in_what_codex_did_not_copy(self):
        """With the option on, the archive is made and supplies the missing copies."""
        self.options["full_snapshot"] = True
        with self.codex(keep_copy=False):
            server.run_task(self.task_id, "Rename the automation")
        task = server.tasks[self.task_id]
        self.assertEqual([(entry["path"], entry["status"]) for entry in task["backups"]],
                         [("automations.yaml", "saved"), ("scripts.yaml", "saved")])
        self.assertEqual((self.run_dir / "backups" / "scripts.yaml").read_text(encoding="utf-8"), "a: 1\n")
        self.assertEqual(task["details"], "Notes")
        self.assertEqual(task["snapshot"]["file_count"], 3)
        with tarfile.open(self.run_dir / server.SNAPSHOT_FILE) as archive:
            self.assertEqual(sorted(archive.getnames()), [".storage/core.restore_state", "automations.yaml", "scripts.yaml"])
        self.assertIn(("phase", "Saving a full snapshot of your configuration", "done"), self.steps())

    def test_read_only_runs_get_no_backup_folder(self):
        """A read-only run is given no backup folder and no instruction."""
        self.options["codex_sandbox"] = "read-only"
        with self.codex(keep_copy=False):
            server.run_task(self.task_id, "Rename the automation")
        self.assertFalse((self.run_dir / "backups").exists())
        self.assertNotIn("copy it to", (self.run_dir / "prompt.txt").read_text(encoding="utf-8"))

    def test_a_snapshot_that_fails_is_not_shown_as_done(self):
        """A failed full snapshot stays marked as failed when the exchange itself ends well."""
        self.options["full_snapshot"] = True
        with self.codex(keep_copy=True), patch.object(server, "create_snapshot", side_effect=OSError("disk full")):
            server.run_task(self.task_id, "Rename the automation")
        task = server.tasks[self.task_id]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["snapshot_error"], "disk full")
        failed = ("phase", "Saving a full snapshot of your configuration", "failed")
        self.assertIn(failed, self.steps())
        self.assertIn(("phase", "Noting the current state of your configuration files", "done"), self.steps())
        server.finish_activity(self.task_id)
        stored = json.loads((self.run_dir / server.ACTIVITY_FILE).read_text(encoding="utf-8"))
        self.assertIn(failed, [(step["kind"], step["text"], step["status"]) for step in stored["steps"]])

    def test_working_files_are_removed_when_the_exchange_ends(self):
        """The file list is deleted after the run while the copies stay."""
        with self.codex(keep_copy=True), patch.object(server.verification, "end"):
            server._run_background_task(self.task_id, "Rename the automation", None, None)
        self.assertEqual(server.tasks[self.task_id]["status"], "completed")
        self.assertFalse((self.run_dir / server.BASELINE_FILE).exists())
        self.assertTrue((self.run_dir / "backups" / "automations.yaml").is_file())
        stored = json.loads((self.run_dir / server.ACTIVITY_FILE).read_text(encoding="utf-8"))
        self.assertTrue(all(step["status"] == "done" for step in stored["steps"]))
        self.assertNotIn("elapsed_ms", stored)


class CleanupTests(unittest.TestCase):
    """Backups are removed once their exchange is older than the retention setting."""

    def setUp(self):
        """Keep three exchanges of one chat, and one chat from before conversation history."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "tasks"
        for name, value in (("tasks", {}), ("active_task_runners", set()), ("running_processes", {})):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "task_root", return_value=self.root))
        self.options = {}
        self.stack.enter_context(patch.object(server, "read_options", side_effect=lambda: dict(self.options)))
        self.old, self.recent, self.current = "a" * 32, "b" * 32, "c" * 32
        self.task_id = "20261002T000000Z-abcdef12"
        server.tasks[self.task_id] = {
            "task_id": self.task_id, "status": "completed", "current_turn_id": self.current,
            "updated_at": "2026-10-02T00:00:00+00:00",
            "turns": [
                {"turn_id": self.old, "status": "completed", "completed_at": self.ago(days=8)},
                {"turn_id": self.recent, "status": "completed", "completed_at": self.ago(days=6)},
                {"turn_id": self.current, "status": "waiting_for_input", "completed_at": "", "updated_at": self.ago(days=30)},
            ],
        }
        server.tasks["legacy"] = {"task_id": "legacy", "status": "completed", "completed_at": self.ago(days=40), "prompt": "Old"}
        for turn_id in (self.old, self.recent, self.current):
            self.fill(self.root / self.task_id / "turns" / turn_id)
        self.fill(self.root / "legacy")

    @staticmethod
    def ago(**delta):
        """An ISO timestamp the given time before now."""
        return (datetime.now(timezone.utc) - timedelta(**delta)).replace(microsecond=0).isoformat()

    @staticmethod
    def fill(run_dir):
        """Give an exchange every kind of file the worker may have left in it."""
        (run_dir / "backups" / ".storage").mkdir(parents=True)
        (run_dir / "backups" / ".storage" / "lovelace.kitchen").write_text("{}")
        (run_dir / "recovery").mkdir()
        (run_dir / "recovery" / "automations.yaml").write_text("old")
        for name in ("snapshot-before.tar.gz", "manifest-before.json", "manifest-after.json", "prompt.txt", "changes.json"):
            (run_dir / name).write_text("x")

    def names(self, run_dir):
        """The files and folders left in an exchange's folder."""
        return sorted(path.name for path in run_dir.iterdir() if path.name not in {"turns", "task.json"})

    def test_expired_backups_go_and_the_rest_stays(self):
        """Old backups are deleted and marked without moving the chat or touching other files."""
        server.cleanup_backups()
        turns = self.root / self.task_id / "turns"
        self.assertEqual(self.names(turns / self.old), ["changes.json", "prompt.txt"])
        self.assertEqual(self.names(turns / self.recent), ["backups", "changes.json", "prompt.txt", "recovery", "snapshot-before.tar.gz"])
        self.assertEqual(self.names(turns / self.current), ["changes.json", "prompt.txt"])
        self.assertEqual(self.names(self.root / "legacy"), ["changes.json", "prompt.txt"])
        task = server.tasks[self.task_id]
        self.assertEqual([turn.get("backups_removed", False) for turn in task["turns"]], [True, False, True])
        self.assertTrue(server.tasks["legacy"]["backups_removed"])
        self.assertEqual(task["updated_at"], "2026-10-02T00:00:00+00:00")
        stored = json.loads((self.root / self.task_id / "task.json").read_text(encoding="utf-8"))
        self.assertTrue(stored["turns"][0]["backups_removed"])
        self.assertEqual(stored["updated_at"], "2026-10-02T00:00:00+00:00")
        server.cleanup_backups()
        self.assertEqual(self.names(turns / self.recent), ["backups", "changes.json", "prompt.txt", "recovery", "snapshot-before.tar.gz"])

    def test_the_running_exchange_is_left_alone(self):
        """Nothing of the exchange that is running is removed."""
        server.tasks[self.task_id]["status"] = "running"
        server.cleanup_backups()
        turns = self.root / self.task_id / "turns"
        self.assertEqual(len(self.names(turns / self.current)), 7)
        self.assertEqual(self.names(turns / self.old), ["changes.json", "prompt.txt"])
        self.assertNotIn("backups_removed", server.tasks[self.task_id]["turns"][2])

    def test_the_setting_decides_how_long_backups_are_kept(self):
        """A shorter retention setting removes more recent backups."""
        self.options["backup_retention_days"] = 5
        server.cleanup_backups()
        turns = self.root / self.task_id / "turns"
        self.assertEqual(self.names(turns / self.recent), ["changes.json", "prompt.txt"])

    def test_a_continued_chat_from_before_history_loses_its_old_archive(self):
        """The chat's own folder is cleaned for early exchanges, also after the chat was continued."""
        newer = "d" * 32
        server.tasks["continued"] = {
            "task_id": "continued", "status": "running", "current_turn_id": newer,
            "turns": [
                {"turn_id": "legacy", "created_at": self.ago(days=50), "legacy": True},
                {"turn_id": "legacy-1", "created_at": self.ago(days=45), "completed_at": self.ago(days=40), "legacy": True},
                {"turn_id": newer, "status": "running", "created_at": self.ago(minutes=1)},
            ],
        }
        self.fill(self.root / "continued")
        self.fill(self.root / "continued" / "turns" / newer)
        server.cleanup_backups()
        self.assertEqual(self.names(self.root / "continued"), ["changes.json", "prompt.txt"])
        self.assertEqual(len(self.names(self.root / "continued" / "turns" / newer)), 7)
        task = server.tasks["continued"]
        self.assertTrue(task["backups_removed"])
        self.assertFalse(any("backups_removed" in turn for turn in task["turns"]))
        # Early exchanges that are still recent keep their backups and lose only the working files.
        server.tasks["younger"] = {"task_id": "younger", "status": "completed", "current_turn_id": newer, "turns": [
            {"turn_id": "legacy", "completed_at": self.ago(days=2), "legacy": True},
            {"turn_id": newer, "status": "completed", "completed_at": self.ago(days=1)},
        ]}
        self.fill(self.root / "younger")
        server.cleanup_backups()
        self.assertEqual(self.names(self.root / "younger"), ["backups", "changes.json", "prompt.txt", "recovery", "snapshot-before.tar.gz"])
        self.assertNotIn("backups_removed", server.tasks["younger"])

    def test_a_linked_backup_folder_is_unlinked_not_emptied(self):
        """A link in place of the backup folder is removed without deleting its target."""
        target = self.root.parent / "elsewhere"
        target.mkdir()
        (target / "keep.yaml").write_text("keep")
        run_dir = self.root / self.task_id / "turns" / self.old
        server.remove_run_artifacts(run_dir, ("backups",))
        (run_dir / "backups").symlink_to(target, target_is_directory=True)
        server.cleanup_backups()
        self.assertFalse((run_dir / "backups").exists())
        self.assertTrue((target / "keep.yaml").is_file())

    def test_unusual_task_and_turn_names_are_never_used_as_paths(self):
        """Names that could lead outside the task folder are skipped."""
        server.tasks["../escape"] = {"task_id": "../escape", "status": "completed", "completed_at": self.ago(days=40)}
        server.tasks[self.task_id]["turns"].append({"turn_id": "../../x", "status": "completed", "completed_at": self.ago(days=40)})
        outside = self.root.parent / "escape"
        self.fill(outside)
        server.cleanup_backups()
        self.assertEqual(len(self.names(outside)), 7)


class ActivityPhaseTests(unittest.TestCase):
    """The worker's own steps and the timer the chat shows while an exchange runs."""

    def setUp(self):
        """Start collecting steps for one running exchange."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("tasks", {}), ("task_activity", {}), ("active_task_runners", set())):
            self.stack.enter_context(patch.object(server, name, value))
        server.tasks["t"] = {"task_id": "t", "status": "running", "current_turn_id": "turn-1"}
        server.start_activity("t", "turn-1")

    def payload(self):
        """What the activity endpoint would return now."""
        with server.lock:
            return server.activity_payload_locked(server.task_activity["t"])

    def test_phases_follow_the_start_of_codex(self):
        """The worker's steps close as Codex starts, thinks, and reports its first step."""
        server.start_phase("t", "launch", "Starting Codex")
        self.assertEqual([(s["kind"], s["text"], s["status"]) for s in self.payload()["steps"]],
                         [("phase", "Starting Codex", "running")])
        server.record_activity_event("t", {"type": "thread.started", "thread_id": "x"})
        server.record_activity_event("t", {"type": "turn.started"})
        self.assertEqual([(s["text"], s["status"]) for s in self.payload()["steps"]],
                         [("Starting Codex", "done"), ("Codex is thinking", "running")])
        server.record_activity_event("t", {"type": "item.completed", "item": {"id": "r0", "type": "reasoning", "text": "Looking"}})
        steps = self.payload()["steps"]
        self.assertEqual([(s["text"], s["status"]) for s in steps],
                         [("Starting Codex", "done"), ("Codex is thinking", "done"), ("Looking", "done")])
        self.assertIsInstance(steps[0]["duration_ms"], int)
        server.finish_phase("t", "launch", "never-started")
        self.assertEqual(self.payload()["total"], 3)

    def test_timer_is_reported_only_while_running(self):
        """The running time is sent while the exchange lasts and not afterwards."""
        with patch.object(server.time, "monotonic", return_value=server.task_activity["t"]["started"] + 12.5):
            self.assertEqual(self.payload()["elapsed_ms"], 12500)
        server.task_activity["t"]["running"] = False
        self.assertNotIn("elapsed_ms", self.payload())

    def test_edits_codex_reports_are_remembered(self):
        """Edited and deleted files under /config are noted for the backup review."""
        server.record_activity_event("t", {"type": "item.completed", "item": {"id": "f0", "type": "file_change", "changes": [
            {"path": "/config/automations.yaml", "kind": "update"}, {"path": "/config/old.yaml", "kind": "delete"},
            {"path": "/config/new.yaml", "kind": "add"}, {"path": "/tmp/scratch.txt", "kind": "update"}]}})
        self.assertEqual(server.edited_paths("t"), {"automations.yaml", "old.yaml"})
        self.assertEqual(server.edited_paths("missing"), set())

    def test_open_worker_steps_close_with_a_successful_exchange(self):
        """A worker step still open at a successful end is saved as done."""
        server.start_phase("t", "review", "Checking the changes")
        server.tasks["t"]["status"] = "completed"
        with patch.object(server, "atomic_json_write") as write, patch.object(server, "task_root", return_value=Path(tempfile.gettempdir())):
            server.finish_activity("t")
        self.assertEqual([step["status"] for step in write.call_args.args[1]["steps"]], ["done"])


if __name__ == "__main__":
    unittest.main()
