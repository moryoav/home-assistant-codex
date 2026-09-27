"""Verification boundaries, evidence ownership, and credential protection."""
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import server
from verification import Verification


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.options = {**server.DEFAULT_OPTIONS, "task_root": str(self.root / "tasks")}
        for key, value in {"CONFIG_ROOT": self.config, "DATA_ROOT": self.root,
                           "TASK_STATE_FILE": self.root / "index.json", "tasks": {}}.items():
            patcher = patch.object(server, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(server, "read_options", return_value=self.options)
        patcher.start()
        self.addCleanup(patcher.stop)
        server.get_task_dir("chat").mkdir(parents=True)
        turn = server.new_turn("Check my dashboard")
        server.tasks["chat"] = {"task_id": "chat", "status": "running",
                                "current_turn_id": turn["turn_id"], "turns": [turn]}
        self.engine = Verification(server)
        self.capability = self.engine.begin("chat")

    def test_capability_expires_and_cannot_run_arbitrary_operations(self):
        with self.assertRaises(ValueError):
            self.engine.dispatch({"capability": "wrong", "operation": "entity"})
        result = self.engine.dispatch({"capability": self.capability, "operation": "call_service"})
        self.assertEqual(result["status"], "unavailable")
        self.engine.end("chat")
        with self.assertRaises(ValueError):
            self.engine.dispatch({"capability": self.capability, "operation": "entity"})

    def test_fresh_state_assertion_persists_in_this_turn(self):
        with patch.object(self.engine, "core", side_effect=[
            {"state": "off", "attributes": {"brightness": 0, "token": "private"}},
            {"state": "on", "attributes": {"brightness": 100}},
        ]) as core:
            for expected in ("failed", "passed"):
                result = self.engine.run("chat", {"operation": "entity", "entity_id": "light.kitchen",
                                                  "expected_state": "on", "attributes": ["brightness"]})
                self.assertEqual(result["status"], expected)
                self.assertNotIn("token", result["attributes"])
            self.assertEqual(core.call_count, 2)
        self.assertEqual(len(server.tasks["chat"]["turns"][0]["verification"]), 2)
        self.assertNotIn(self.capability, json.dumps(server.task_payload(server.tasks["chat"])))
        self.engine.begin("chat")
        self.assertEqual(server.tasks["chat"]["verification"], [])

    def test_dashboard_readback_compares_configuration_not_save_ack(self):
        with patch.object(self.engine, "ws_read", return_value={"views": []}):
            result = self.engine.run("chat", {"operation": "dashboard_readback", "path": "/lovelace/0",
                                              "expected_config": {"views": [{"title": "Missing"}]}})
        self.assertEqual(result["status"], "failed")

    def test_paths_and_disabled_browser_fail_before_auth(self):
        with patch.object(self.engine, "core") as core:
            for path in ("https://evil.test/", "//evil", "/lovelace/%2e%2e", "/lovelace/0?token=x"):
                self.assertEqual(self.engine.run("chat", {"operation": "dashboard", "path": path})["status"], "unavailable")
            self.options["browser_verification"] = False
            self.assertEqual(self.engine.browser("chat", {"path": "/lovelace/0"})["status"], "disabled")
            core.assert_not_called()

    def test_browser_session_revoked_when_launch_fails(self):
        with patch.object(self.engine, "core", return_value={"session_id": "lease", "url": "http://homeassistant:8123", "access_token": "private"}) as core:
            with patch("verification.subprocess.Popen", side_effect=OSError("unavailable")):
                result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/0"})
        self.assertEqual(result["status"], "unavailable")
        core.assert_called_with("DELETE", "codex_cli/browser_session", json={"session_id": "lease"})
        self.assertNotIn("private", json.dumps(result))

    def test_snapshot_excludes_credentials_but_manifest_detects_changes(self):
        for name in ("secrets.yaml", ".storage/auth", ".storage/auth_provider.homeassistant", ".storage/core.config_entries", ".storage/lovelace"):
            path = self.config / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"private": "value"}')
        server.get_run_dir("chat").mkdir(parents=True)
        snapshot = server.create_snapshot("chat")
        with tarfile.open(snapshot["path"]) as archive:
            self.assertEqual(archive.getnames(), [".storage/lovelace"])
        self.assertIn("secrets.yaml", server.build_manifest())

    def test_supervisor_token_is_not_in_subprocess_environment(self):
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "private", "HASSIO_TOKEN": "private"}):
            environment = server.codex_env()
        self.assertNotIn("SUPERVISOR_TOKEN", environment)
        self.assertNotIn("HASSIO_TOKEN", environment)

    def test_default_storage_dashboard_is_discovered(self):
        path = self.config / ".storage" / "lovelace"
        path.parent.mkdir()
        path.write_text(json.dumps({"data": {"config": {"views": [{"title": "Home"}]}}}))
        refs = server.find_lovelace_dashboard_refs({"added": [], "changed": [".storage/lovelace"]})
        self.assertEqual(refs[0]["storage_file"], ".storage/lovelace")

    def test_expired_screenshot_is_deleted_but_other_images_remain(self):
        root = server.get_task_dir("chat")
        (root / "expired.png").write_bytes(b"expired")
        (root / "user.png").write_bytes(b"user")
        server.tasks["chat"]["turns"][0]["verification_attachments"] = [
            {"origin": "verification", "expires_at": 0, "path": "expired.png"},
            {"origin": "user", "expires_at": 0, "path": "user.png"},
        ]
        self.engine.cleanup()
        self.assertFalse((root / "expired.png").exists())
        self.assertTrue((root / "user.png").exists())
