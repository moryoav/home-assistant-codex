"""Verification boundaries, evidence ownership, and credential protection."""
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_server import server
from verification import MAX_BROWSERS, Verification
from verification import kill_tracked_processes, track_browser_processes


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

    def test_expired_check_cannot_record_results_in_a_finished_or_replaced_turn(self):
        for outcome in ("ended", "completed", "replaced"):
            with self.subTest(outcome=outcome):
                server.tasks["chat"]["status"] = "running"
                self.engine.begin("chat")
                def read(*_args, **_kwargs):
                    if outcome == "ended":
                        self.engine.end("chat")
                    elif outcome == "completed":
                        server.tasks["chat"]["status"] = "completed"
                    else:
                        self.engine.begin("chat")
                    return {"state": "on", "attributes": {}}
                with patch.object(self.engine, "core", side_effect=read):
                    result = self.engine.run("chat", {"operation": "entity", "entity_id": "light.kitchen"})
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(server.tasks["chat"]["verification"], [])

    def test_non_ascii_capability_and_other_app_logs_fail_closed(self):
        with self.assertRaises(ValueError):
            self.engine.dispatch({"capability": "שלום", "operation": "entity"})
        with patch.object(self.engine, "core") as core:
            result = self.engine.run("chat", {"operation": "logs", "target": "core_mosquitto"})
        self.assertEqual(result["status"], "unavailable")
        core.assert_not_called()

    def test_dashboard_readback_compares_configuration_not_save_ack(self):
        with patch.object(self.engine, "ws_read", return_value={"views": []}):
            result = self.engine.run("chat", {"operation": "dashboard_readback", "path": "/lovelace/0",
                                              "expected_config": {"views": [{"title": "Missing"}]}})
        self.assertEqual(result["status"], "failed")

    def test_automatic_dashboard_checks_respect_the_remaining_turn_budget(self):
        config = {"views": [{"path": f"view-{index}"} for index in range(MAX_BROWSERS + 1)]}
        storage = self.config / ".storage"
        storage.mkdir()
        refs = []
        for name in ("first", "second"):
            (storage / name).write_text(json.dumps({"data": {"config": config}}))
            refs.append({"success": True, "storage_file": f".storage/{name}", "url_path": name})
        for previous in (0, 1, MAX_BROWSERS):
            with self.subTest(previous=previous):
                self.engine.begin("chat")
                with patch.object(self.engine, "browser", return_value={"status": "captured"}) as browser, \
                     patch.object(self.engine, "ws_read", return_value=config):
                    for index in range(previous):
                        self.engine.run("chat", {"operation": "dashboard", "path": f"/manual/{index}"})
                    self.engine.after_changes("chat", refs)
                self.assertEqual(browser.call_count, MAX_BROWSERS)
                paths = [call.args[1]["path"] for call in browser.call_args_list[previous:]]
                self.assertEqual(paths, [f"/first/view-{index}" for index in range(MAX_BROWSERS - previous)])
                checks = server.tasks["chat"]["verification"]
                self.assertEqual([check["status"] for check in checks].count("passed"), 2)
                self.assertNotIn("unavailable", [check["status"] for check in checks])

    def test_websocket_read_authenticates_and_closes_the_actual_client_interface(self):
        # websocket-client has close(), but no context manager methods.
        ws = Mock(spec=["recv", "send", "close"])
        ws.recv.side_effect = [
            '{"type":"auth_required"}', '{"type":"auth_ok"}',
            '{"id":1,"type":"result","success":true,"result":{"views":[]}}',
        ]
        with patch("verification.websocket.create_connection", return_value=ws), patch.object(server, "ha_token", return_value="supervisor-secret"):
            self.assertEqual(self.engine.ws_read({"type": "lovelace/config"}), {"views": []})
        self.assertEqual(json.loads(ws.send.call_args_list[0].args[0]), {"type": "auth", "access_token": "supervisor-secret"})
        ws.close.assert_called_once()

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
        self.assertEqual(snapshot["file_count"], 1)
        recovery = server.preserve_recovery_copies(server.get_run_dir("chat"),
            {"added": [], "changed": ["secrets.yaml"], "deleted": []}, ["secrets.yaml"])
        details = server.validation_details(["secrets.yaml: bad YAML"], {}, recovery)
        self.assertIn("excluded from recovery", details)
        self.assertNotIn("New files", details)

    def test_supervisor_token_is_not_in_subprocess_environment(self):
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "private", "HASSIO_TOKEN": "private"}):
            environment = server.codex_env()
        self.assertNotIn("SUPERVISOR_TOKEN", environment)
        self.assertNotIn("HASSIO_TOKEN", environment)

    def test_browser_memory_budget_stops_process_and_revokes_session(self):
        original_popen = subprocess.Popen
        processes = []
        def launch(_args, **kwargs):
            process = original_popen([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"], **kwargs)
            processes.append(process)
            return process
        with patch.object(self.engine, "core", return_value={"session_id": "lease", "url": "http://homeassistant:8123", "access_token": "private"}) as core:
            with patch("verification.subprocess.Popen", side_effect=launch), patch("verification.track_browser_processes", return_value=800 * 1024 * 1024):
                result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/0"})
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("memory budget", result["message"])
        self.assertIsNotNone(processes[0].poll())
        core.assert_called_with("DELETE", "codex_cli/browser_session", json={"session_id": "lease"})

    @unittest.skipIf(os.name == "nt", "Linux container process accounting")
    def test_tracks_and_stops_detached_browser_descendants(self):
        script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True); time.sleep(60)"
        process = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        tracked = {}
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                memory = track_browser_processes(process.pid, tracked)
                if len(tracked) >= 2:
                    break
                time.sleep(.05)
            self.assertGreaterEqual(len(tracked), 2)
            self.assertGreater(memory, 0)
        finally:
            kill_tracked_processes(tracked)
            process.kill() if process.poll() is None else None
            process.wait(timeout=5)

    def test_pending_dashboard_cannot_save_unrelated_or_unvalidated_files(self):
        self.options["codex_sandbox"] = "read-only"
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.engine.save_pending_dashboard("chat", "/lovelace/0")
        self.options["codex_sandbox"] = "workspace-write"
        self.options["auto_save_lovelace"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.engine.save_pending_dashboard("chat", "/lovelace/0")
        self.options["auto_save_lovelace"] = True
        with self.assertRaisesRegex(ValueError, "baseline"):
            self.engine.save_pending_dashboard("chat", "/lovelace/0")

    def test_supervisor_websocket_uses_its_websocket_proxy(self):
        with patch.object(server, "ha_token_source", return_value="supervisor"):
            self.assertEqual(server.ha_ws_url(), "ws://supervisor/core/websocket")

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
