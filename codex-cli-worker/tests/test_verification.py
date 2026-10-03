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
from verification import DEFAULT_BROWSER_MEMORY_LIMIT_MIB, MAX_ACTIONS, MAX_BROWSERS, Verification
from verification import browser_memory_limit, browser_process_memory, kill_tracked_processes, track_browser_processes


class VerificationTests(unittest.TestCase):
    """Checks, browser captures, and credential handling for one running chat turn."""

    def setUp(self):
        """Create a temporary worker with one running chat turn and begin verification for it."""
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
        """Reject a wrong or ended capability and refuse operations outside the supported set."""
        with self.assertRaises(ValueError):
            self.engine.dispatch({"capability": "wrong", "operation": "entity"})
        result = self.engine.dispatch({"capability": self.capability, "operation": "restart_host"})
        self.assertEqual(result["status"], "unavailable")
        self.engine.end("chat")
        with self.assertRaises(ValueError):
            self.engine.dispatch({"capability": self.capability, "operation": "entity"})

    def test_fresh_state_assertion_persists_in_this_turn(self):
        """Read the entity state again for each check and record it in the turn, with only the requested attributes."""
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
        """Discard the result of a check whose turn ended, completed, or was replaced while it ran."""
        for outcome in ("ended", "completed", "replaced"):
            with self.subTest(outcome=outcome):
                server.tasks["chat"]["status"] = "running"
                self.engine.begin("chat")
                def read(*_args, **_kwargs):
                    """End, complete, or replace the turn while the entity state is being read."""
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
        """Reject a non-ASCII capability and refuse another app's logs without calling Home Assistant."""
        with self.assertRaises(ValueError):
            self.engine.dispatch({"capability": "שלום", "operation": "entity"})
        with patch.object(self.engine, "core") as core:
            result = self.engine.run("chat", {"operation": "logs", "target": "core_mosquitto"})
        self.assertEqual(result["status"], "unavailable")
        core.assert_not_called()

    def act(self, **request):
        """Run one act request for the chat's turn and return its result."""
        return self.engine.run("chat", request)

    def test_default_level_reloads_and_switches_automations_only(self):
        """At the default level reloads and automation.turn_on/turn_off reach Home Assistant; anything else is refused."""
        with patch.object(self.engine, "call_service", return_value="") as call:
            allowed = [
                self.act(operation="reload"),
                self.act(operation="reload", domain="automation"),
                self.act(operation="call_service", service="automation.turn_off", entity_id=["automation.locker"]),
                self.act(operation="call_service", service="automation.turn_on", entity_id="automation.school"),
                self.act(operation="call_service", service="script.reload"),
            ]
            refused = [
                self.act(operation="call_service", service="light.turn_on", entity_id=["light.kitchen"]),
                self.act(operation="call_service", service="automation.trigger", entity_id=["automation.locker"]),
                # Only automations, and nothing but their entity ids.
                self.act(operation="call_service", service="automation.turn_off", entity_id=["light.kitchen"]),
                self.act(operation="call_service", service="automation.turn_off"),
                self.act(operation="call_service", service="automation.turn_off", entity_id=["automation.locker"],
                         data='{"stop_actions": false}'),
                self.act(operation="call_service", service="automation.reload", data='{"entity_id": "all"}'),
            ]
        self.assertEqual([result["status"] for result in allowed], ["done"] * 5)
        self.assertEqual([result["status"] for result in refused], ["refused"] * 6)
        self.assertEqual([call_.args for call_ in call.call_args_list], [
            ("homeassistant.reload_all", {}),
            ("automation.reload", {}),
            ("automation.turn_off", {"entity_id": ["automation.locker"]}),
            ("automation.turn_on", {"entity_id": ["automation.school"]}),
            ("script.reload", {}),
        ])
        self.assertIn("all_services", refused[0]["message"])
        # Every action, also a refused one, is recorded on the turn with what it targeted.
        recorded = server.tasks["chat"]["turns"][0]["verification"]
        self.assertEqual(len(recorded), 11)
        self.assertEqual({key: recorded[2][key] for key in ("operation", "service", "entity_id", "status")},
                         {"operation": "call_service", "service": "automation.turn_off",
                          "entity_id": ["automation.locker"], "status": "done"})
        self.assertEqual(recorded[5]["status"], "refused")

    def test_all_services_level_calls_any_service_with_its_data(self):
        """At the all_services level any service is called, with its data and entity ids merged."""
        self.options["ha_actions"] = "all_services"
        with patch.object(self.engine, "call_service", return_value="") as call:
            light = self.act(operation="call_service", service="light.turn_on", entity_id=["light.kitchen"],
                             data='{"brightness_pct": 40}')
            restart = self.act(operation="call_service", service="homeassistant.restart")
        self.assertEqual([light["status"], restart["status"]], ["done", "done"])
        self.assertEqual([call_.args for call_ in call.call_args_list], [
            ("light.turn_on", {"brightness_pct": 40, "entity_id": ["light.kitchen"]}),
            ("homeassistant.restart", {}),
        ])
        # The service data itself is not kept with the result.
        self.assertNotIn("brightness_pct", json.dumps(server.tasks["chat"]["verification"]))

    def test_actions_are_refused_when_turned_off_or_read_only(self):
        """With the option off, and in read-only mode at any level, no action reaches Home Assistant."""
        cases = ({"ha_actions": "off"}, {"ha_actions": "all_services", "codex_sandbox": "read-only"},
                 {"codex_sandbox": "read-only"})
        for options in cases:
            with self.subTest(options=options), patch.dict(self.options, options), \
                 patch.object(self.engine, "call_service") as call:
                self.assertEqual(self.act(operation="reload", domain="automation")["status"], "refused")
                self.assertEqual(self.act(operation="call_service", service="automation.turn_off",
                                          entity_id=["automation.locker"])["status"], "refused")
                call.assert_not_called()
        # An unknown value in the option counts as the default level, not as everything.
        with patch.dict(self.options, {"ha_actions": "everything"}), patch.object(self.engine, "call_service", return_value=""):
            self.assertEqual(self.act(operation="reload")["status"], "done")
            self.assertEqual(self.act(operation="call_service", service="light.turn_on",
                                      entity_id=["light.kitchen"])["status"], "refused")

    def test_rejected_and_malformed_actions_are_reported_not_retried(self):
        """A call Home Assistant rejects is a failed action with its reason; a malformed request never reaches it."""
        self.options["ha_actions"] = "all_services"
        with patch.object(self.engine, "call_service", return_value="Home Assistant returned HTTP 400: Service not found") as call:
            result = self.act(operation="reload", domain="nonsense")
        self.assertEqual((result["status"], result["message"]), ("failed", "Home Assistant returned HTTP 400: Service not found"))
        call.assert_called_once_with("nonsense.reload", {})
        malformed = (
            {"operation": "reload", "domain": "automation.reload"},
            {"operation": "reload", "domain": "automation", "entity_id": ["automation.x"]},
            {"operation": "call_service"},
            {"operation": "call_service", "service": "light"},
            {"operation": "call_service", "service": "light.turn_on", "url": "http://example.test"},
            {"operation": "call_service", "service": "light.turn_on", "entity_id": ["Light.Kitchen"]},
            {"operation": "call_service", "service": "light.turn_on", "entity_id": [f"light.l{n}" for n in range(21)]},
            {"operation": "call_service", "service": "light.turn_on", "data": "[1, 2]"},
            {"operation": "call_service", "service": "light.turn_on", "data": "not json"},
            {"operation": "call_service", "service": "notify.notify", "data": json.dumps({"message": "x" * 9000})},
        )
        with patch.object(self.engine, "call_service") as call:
            for request in malformed:
                with self.subTest(request=request):
                    result = self.engine.run("chat", request)
                    self.assertEqual(result["status"], "unavailable")
                    self.assertTrue(result["message"].startswith("Action could not finish: "))
            call.assert_not_called()

    def test_actions_are_limited_per_turn(self):
        """A turn gets a bounded number of actions; the next one is not sent to Home Assistant."""
        with patch.object(self.engine, "call_service", return_value="") as call:
            for _ in range(MAX_ACTIONS):
                self.assertEqual(self.act(operation="reload", domain="automation")["status"], "done")
            extra = self.act(operation="reload", domain="automation")
        self.assertEqual(extra["status"], "unavailable")
        self.assertIn("Action limit", extra["message"])
        self.assertEqual(call.call_count, MAX_ACTIONS)

    def test_service_calls_go_through_the_supervisor_proxy_with_the_worker_token(self):
        """A service call is posted to the Supervisor's Core proxy with the worker's token, and Core's reason is kept."""
        class Response:
            """A canned HTTP answer that works as a context manager, like a streamed requests response."""

            def __init__(self, status_code, body=b""):
                """Hold the status code and body the answer carries."""
                self.status_code, self.body = status_code, body

            def __enter__(self):
                """Return the answer itself."""
                return self

            def __exit__(self, *_exc):
                """Close nothing; there is no connection."""
                return False

            def iter_content(self, _size):
                """Yield the body in one piece."""
                yield self.body

        answers = [Response(200, b"[]"), Response(400, b'{"message": "Service light.fly not found."}'), Response(502, b"<html>")]
        with patch.object(server, "ha_token", return_value="supervisor-token"), \
             patch("verification.requests.post", side_effect=answers) as post:
            self.assertEqual(self.engine.call_service("automation.reload", {}), "")
            self.assertEqual(self.engine.call_service("light.fly", {"entity_id": ["light.kitchen"]}),
                             "Home Assistant returned HTTP 400: Service light.fly not found.")
            self.assertEqual(self.engine.call_service("light.turn_on", {}), "Home Assistant returned HTTP 502")
        first = post.call_args_list[0]
        self.assertEqual(first.args, ("http://supervisor/core/api/services/automation/reload",))
        self.assertEqual(first.kwargs["headers"], {"Authorization": "Bearer supervisor-token"})
        self.assertIs(first.kwargs["allow_redirects"], False)
        self.assertEqual(post.call_args_list[1].kwargs["json"], {"entity_id": ["light.kitchen"]})
        # Without a token nothing is sent, and the action is reported as not done.
        with patch.object(server, "ha_token", return_value=""), patch("verification.requests.post") as post:
            result = self.act(operation="reload")
        self.assertEqual(result["status"], "unavailable")
        post.assert_not_called()

    def test_dashboard_readback_compares_configuration_not_save_ack(self):
        """Fail the readback when the dashboard Home Assistant returns differs from the expected configuration."""
        with patch.object(self.engine, "ws_read", return_value={"views": []}):
            result = self.engine.run("chat", {"operation": "dashboard_readback", "path": "/lovelace/0",
                                              "expected_config": {"views": [{"title": "Missing"}]}})
        self.assertEqual(result["status"], "failed")

    def test_automatic_dashboard_checks_respect_the_remaining_turn_budget(self):
        """Limit automatic dashboard captures to the browser budget left in the turn, recording no refused check."""
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
        """Authenticate the WebSocket read with the Home Assistant token and close a client that only offers close()."""
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
        """Reject unsafe paths and prevent disabled captures from issuing credentials or launching."""
        with patch.object(self.engine, "core") as core:
            for path in ("https://evil.test/", "//evil", "/lovelace/%2e%2e", "/lovelace/0?token=x"):
                self.assertEqual(self.engine.run("chat", {"operation": "dashboard", "path": path})["status"], "unavailable")
            self.options["browser_verification"] = False
            self.engine.begin("chat")
            with patch("verification.subprocess.Popen") as launch:
                self.assertEqual(self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/0"})["status"], "disabled")
                launch.assert_not_called()
            core.assert_not_called()

    def test_browser_session_revoked_when_launch_fails(self):
        """Revoke the browser session and keep its token out of the result when the browser cannot start."""
        with patch.object(self.engine, "core", return_value={"session_id": "lease", "url": "http://homeassistant:8123", "access_token": "private"}) as core:
            with patch("verification.subprocess.Popen", side_effect=OSError("unavailable")):
                result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/0"})
        self.assertEqual(result["status"], "unavailable")
        core.assert_called_with("DELETE", "codex_cli/browser_session", json={"session_id": "lease"})
        self.assertNotIn("private", json.dumps(result))

    def test_snapshot_excludes_credentials_but_manifest_detects_changes(self):
        """Credential files stay out of the archive but in change detection."""
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
        recovery = server.recovery_copies(server.get_run_dir("chat"),
            {"added": [], "changed": ["secrets.yaml"], "deleted": []}, ["secrets.yaml"], {})
        details = server.validation_details(["secrets.yaml: bad YAML"], {}, recovery)
        self.assertIn("excluded from recovery", details)
        self.assertNotIn("New files", details)

    def test_supervisor_token_is_not_in_subprocess_environment(self):
        """Keep the Supervisor tokens out of the environment Codex runs in."""
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "private", "HASSIO_TOKEN": "private"}):
            environment = server.codex_env()
        self.assertNotIn("SUPERVISOR_TOKEN", environment)
        self.assertNotIn("HASSIO_TOKEN", environment)

    def test_browser_memory_budget_stops_process_and_revokes_session(self):
        """Stop an over-budget browser and revoke its temporary session."""
        original_popen = subprocess.Popen
        processes = []
        def launch(_args, **kwargs):
            """Substitute a real waiting process for Chromium to observe cleanup."""
            process = original_popen([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"], **kwargs)
            processes.append(process)
            return process
        with patch.object(self.engine, "core", return_value={"session_id": "lease", "url": "http://homeassistant:8123", "access_token": "private"}) as core:
            with patch("verification.subprocess.Popen", side_effect=launch), patch("verification.track_browser_processes", return_value=(DEFAULT_BROWSER_MEMORY_LIMIT_MIB + 1) * 1024 * 1024):
                result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/0"})
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("memory budget", result["message"])
        self.assertIsNotNone(processes[0].poll())
        core.assert_called_with("DELETE", "codex_cli/browser_session", json={"session_id": "lease"})

    def test_browser_memory_option_defaults_and_rejects_unbounded_values(self):
        """Keep missing or malformed options bounded while accepting supported budgets."""
        self.assertEqual(browser_memory_limit({}), DEFAULT_BROWSER_MEMORY_LIMIT_MIB)
        for invalid in (None, True, False, 0, -1, 511, 8193, "2048", 2048.5):
            self.assertEqual(browser_memory_limit({"browser_memory_limit_mib": invalid}), DEFAULT_BROWSER_MEMORY_LIMIT_MIB)
        for limit in (512, 1536, 2048, 8192):
            self.assertEqual(browser_memory_limit({"browser_memory_limit_mib": limit}), limit)

    def test_browser_uses_configured_budget_for_each_capture(self):
        """Apply lowered and raised budgets on successive captures without caching options."""
        original_popen = subprocess.Popen
        def launch(_args, **kwargs):
            """Return a result after the memory guard has sampled the process."""
            return original_popen([sys.executable, "-c",
                "import sys,time; sys.stdin.read(); time.sleep(.3); print('{\"status\":\"issues\",\"screenshots\":[]}')"], **kwargs)
        for limit, measured, status in ((512, 600, "unavailable"), (2048, 1600, "issues")):
            with self.subTest(limit=limit):
                self.options["browser_memory_limit_mib"] = limit
                with patch.object(self.engine, "core", return_value={"session_id": "lease", "url": "http://homeassistant:8123", "access_token": "private"}) as core:
                    with patch("verification.subprocess.Popen", side_effect=launch), patch("verification.track_browser_processes", return_value=measured * 1024 * 1024):
                        result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/0"})
                self.assertEqual(result["status"], status)
                if status == "unavailable":
                    self.assertIn(f"{limit} MiB memory budget", result["message"])
                    self.assertIn("Browser memory limit", result["message"])
                else:
                    self.assertEqual(result["memory_limit_mib"], limit)
                    self.assertEqual(result["memory_peak_mib"], measured)
                core.assert_called_with("DELETE", "codex_cli/browser_session", json={"session_id": "lease"})

    @unittest.skipIf(os.name == "nt", "Linux container process accounting")
    def test_memory_accounting_includes_swap_and_falls_back_if_pss_unavailable(self):
        """Account for swapped pages and stay conservative without valid proportional data."""
        with patch("verification.Path.read_text", return_value="Rss: 2000 kB\nPss: 600 kB\nSwapPss: 100 kB\n"):
            self.assertEqual(browser_process_memory(123, 500), 700 * 1024)
        for content in ("Rss: 2000 kB\n", "Pss: invalid kB\n", "Pss: -1 kB\n"):
            with patch("verification.Path.read_text", return_value=content):
                self.assertEqual(browser_process_memory(123, 500), 500 * os.sysconf("SC_PAGE_SIZE"))
        with patch("verification.Path.read_text", side_effect=PermissionError):
            self.assertEqual(browser_process_memory(123, 500), 500 * os.sysconf("SC_PAGE_SIZE"))

    @unittest.skipIf(os.name == "nt", "Linux shared-memory accounting")
    def test_shared_browser_pages_are_not_charged_twice(self):
        """Use real shared pages to distinguish proportional accounting from summed RSS."""
        script = "import os,time; shared=bytearray(64*1024*1024); child=os.fork(); print('ready',flush=True); time.sleep(60)"
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True)
        tracked = {}
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assertEqual(process.stdout.readline().strip(), "ready")
            memory = track_browser_processes(process.pid, tracked)
            self.assertEqual(len(tracked), 2)
            rss = sum(int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[21]) for pid in tracked) * os.sysconf("SC_PAGE_SIZE")
            self.assertGreater(memory, 64 * 1024 * 1024)
            self.assertLess(memory, rss - 32 * 1024 * 1024)
        finally:
            kill_tracked_processes(tracked)
            process.kill() if process.poll() is None else None
            process.wait(timeout=5)
            process.stdout.close()

    @unittest.skipIf(os.name == "nt", "Linux container process accounting")
    def test_tracks_and_stops_detached_browser_descendants(self):
        """Track a browser descendant that started its own session, so it is measured and can be stopped."""
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
        """Refuse to save a pending dashboard in read-only mode, with auto-save off, or without a turn baseline."""
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
        """Use the Supervisor's WebSocket proxy when the token comes from the Supervisor."""
        with patch.object(server, "ha_token_source", return_value="supervisor"):
            self.assertEqual(server.ha_ws_url(), "ws://supervisor/core/websocket")

    def test_default_storage_dashboard_is_discovered(self):
        """Find the default dashboard in .storage/lovelace when that file changed."""
        path = self.config / ".storage" / "lovelace"
        path.parent.mkdir()
        path.write_text(json.dumps({"data": {"config": {"views": [{"title": "Home"}]}}}))
        refs = server.find_lovelace_dashboard_refs({"added": [], "changed": [".storage/lovelace"]})
        self.assertEqual(refs[0]["storage_file"], ".storage/lovelace")

    def test_expired_screenshot_is_deleted_but_other_images_remain(self):
        """Delete an expired verification screenshot and keep an image the user attached."""
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
