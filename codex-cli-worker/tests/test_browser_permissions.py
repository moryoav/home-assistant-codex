"""Human-only browser grants, retry ownership, persistence and revocation."""
import hashlib
import json
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import server
from browser_permissions import BrowserPermissionStore, describe, scope, valid_rule
from verification import Verification


def request_rule(kind="websocket", request=None):
    """Build a controller-format rule without needing Home Assistant or a browser."""
    request = request or {"type": "bubble_card_tools/list_modules"}
    snapshot = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"id": hashlib.sha256(f"{kind}\n{snapshot}".encode()).hexdigest(), "kind": kind, "request": snapshot}


def echo_controller(sent, candidates=(), delay=0):
    """Return a Popen stand-in whose controller reports the permissions it received."""
    original = subprocess.Popen
    program = ("import sys,json,time; p=json.load(sys.stdin); time.sleep(%r); "
               "print(json.dumps({'status':'issues','screenshots':[],'message':json.dumps(p['permissions']),"
               "'permission_candidates':%s}))" % (delay, json.dumps(list(candidates))))

    def launch(_args, **kwargs):
        """Run the stand-in controller with the worker's real stdin and output transport."""
        process = original([sys.executable, "-c", program], **kwargs)
        sent.append(process)
        return process

    return launch


class BrowserPermissionTests(unittest.TestCase):
    """Exercise real worker routes with a completed capture and private storage."""

    def setUp(self):
        """Create a paired installation and two eligible blocked requests."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.engine = Verification(server)
        self.options = {**server.DEFAULT_OPTIONS, "task_root": str(self.root / "tasks")}
        self.session = {"installation_id": "a" * 32, "url": "http://homeassistant:8123",
                        "session_id": "private-lease", "access_token": "private-access"}
        self.scope = scope(self.session)
        self.rules = [request_rule(), request_rule(request={"type": "subscribe_condition", "condition": {"condition": "state", "entity_id": "light.kitchen", "state": "on"}})]
        turn = server.new_turn("Inspect my dashboard")
        self.turn_id = turn["turn_id"]
        self.check = {"operation": "dashboard", "check_id": "capture-one", "status": "issues", "path": "/lovelace/home",
                      "permission_scope": self.scope, "permission_candidates": [{**rule, **describe(rule)} for rule in self.rules]}
        task = {"task_id": "chat", "status": "completed", "current_turn_id": self.turn_id, "turns": [turn],
                "verification": [self.check], "verification_attachments": [], "summary": "Original task answer",
                "updated_at": "2026-01-01T00:00:00Z"}
        server.sync_current_turn(task)
        for key, value in {"DATA_ROOT": self.root, "CONFIG_ROOT": self.root / "config", "tasks": {"chat": task},
                           "TASK_STATE_FILE": self.root / "index.json", "verification": self.engine,
                           "active_task_runners": set(), "running_processes": {}, "message_queue": []}.items():
            patcher = patch.object(server, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("read_options", self.options), ("api_token", "paired-worker")):
            patcher = patch.object(server, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = server.app.test_client()
        self.ingress = {"headers": {"X-Ingress-Path": "/preview"},
                        "environ_overrides": {"REMOTE_ADDR": server.INGRESS_PROXY_IP}}

    def payload(self, remember=False, **changes):
        """Select the complete batch from the recorded capture."""
        return {"turn_id": self.turn_id, "check_id": self.check["check_id"],
                "candidate_ids": [rule["id"] for rule in self.rules], "remember": remember, **changes}

    def active_ids(self):
        """Return the work the scheduler currently counts as active."""
        with server.lock:
            return server._active_task_ids_locked()

    def start_agent_check(self):
        """Make the chat a running task whose agent may request checks."""
        server.tasks["chat"]["status"] = "running"
        self.engine.begin("chat")

    def test_only_ingress_can_manage_permissions_even_with_worker_credentials(self):
        """A model/integration API credential cannot grant, list, clear or revoke UI permissions."""
        routes = (("get", "/browser-permissions", None), ("delete", "/browser-permissions", None),
                  ("delete", "/browser-permissions/missing", None),
                  ("post", "/tasks/chat/browser-permissions", self.payload(True)))
        for method, path, payload in routes:
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path, json=payload, headers={"Authorization": "Bearer paired-worker"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.engine.permissions.list(), [])
        self.assertEqual(self.client.get("/browser-permissions").status_code, 401)
        response = self.client.post("/tasks/chat/browser-permissions", json=self.payload(),
                                    headers={"X-Ingress-Path": "/preview"})
        self.assertEqual(response.status_code, 403, "a forged Ingress header is insufficient")

    def test_once_retries_only_capture_and_preserves_the_completed_exchange(self):
        """A one-off batch never reruns the model, changes its answer, moves the chat or persists grants."""
        with patch("verification.threading.Thread.start"), patch.object(self.engine, "browser", return_value={"status": "captured", "permission_candidates": []}) as browser:
            response = self.client.post("/tasks/chat/browser-permissions", json=self.payload(), **self.ingress)
            self.assertEqual(response.status_code, 202)
            capture = self.engine.ui_captures["chat"]
            self.assertEqual(server.task_payload(server.tasks["chat"])["browser_retry"]["turn_id"], self.turn_id)
            self.assertNotIn(capture["capability"], json.dumps(server.task_payload(server.tasks["chat"])))
            self.assertIn("chat", self.active_ids(), "a running retry is worker activity")
            self.assertEqual(self.client.post("/tasks/chat/browser-permissions", json=self.payload(), **self.ingress).status_code, 409)
            self.engine._retry_capture("chat", capture)
        self.assertEqual(browser.call_args.kwargs["retry"]["grants"], self.rules)
        self.assertEqual(browser.call_args.args[1], {"path": "/lovelace/home"})
        task = server.tasks["chat"]
        self.assertEqual((task["status"], task["summary"]), ("completed", "Original task answer"))
        self.assertEqual(task["updated_at"], "2026-01-01T00:00:00Z", "evidence must not move the chat")
        self.assertEqual(task["verification"][-1]["retry_of"], "capture-one")
        self.assertFalse(task["verification"][-1]["permission_decision"]["remember"])
        self.assertEqual(self.engine.permissions.list(), [])
        self.assertIsNone(self.engine.retry_status("chat"))
        self.assertEqual(self.active_ids(), set())
        self.assertEqual(self.client.post("/tasks/chat/browser-permissions", json=self.payload(), **self.ingress).status_code, 409)

    def test_retry_queues_other_work_and_keeps_the_latest_task(self):
        """Messages wait for a retry, and an idle chat's retry never becomes the latest task."""
        newer = server.new_turn("Which room?")
        server.tasks["newer"] = {"task_id": "newer", "status": "waiting_for_input", "question": "Which room?",
                                 "choices": ["Kitchen"], "current_turn_id": newer["turn_id"], "turns": [newer],
                                 "updated_at": "2026-02-01T00:00:00Z"}
        latest = lambda: max(server.tasks.values(), key=lambda task: task.get("updated_at") or "")["task_id"]
        started = []
        with patch("verification.threading.Thread.start"):
            self.assertEqual(self.client.post("/tasks/chat/browser-permissions", json=self.payload(True), **self.ingress).status_code, 202)
        self.assertEqual(latest(), "newer")
        with patch.object(server, "start_queued_message", side_effect=lambda entry: started.append(entry["task_id"])):
            server.message_queue.append({"task_id": "waiting"})
            server.start_next_queued()
            self.assertEqual(started, [], "a queued message waits while the retry runs")
            with patch.object(self.engine, "browser", return_value={"status": "captured"}):
                self.engine._retry_capture("chat", self.engine.ui_captures["chat"])
            self.assertEqual(started, ["waiting"], "the retry's end starts the queue")
        self.assertEqual(latest(), "newer")
        self.assertEqual(server.tasks["chat"]["updated_at"], "2026-01-01T00:00:00Z")

    def test_accepted_batch_cannot_be_replayed_after_worker_restart(self):
        """An accepted one-off stays consumed even if its capture never completed."""
        with patch("verification.threading.Thread.start"):
            self.engine.request_retry("chat", self.payload())
        capture = self.engine.ui_captures["chat"]
        persisted = json.loads((server.get_task_dir("chat") / "task.json").read_text())
        self.assertTrue(persisted["verification"][0]["permission_review"])
        server.tasks["chat"] = persisted
        replacement = Verification(server)
        with self.assertRaises(RuntimeError):
            replacement.request_retry("chat", self.payload())
        with patch.object(self.engine, "browser", return_value={"status": "captured"}):
            self.engine._retry_capture("chat", capture)
        self.assertEqual(replacement.permissions.list(), [])

    def test_remembered_batch_survives_restart_is_scoped_and_can_be_revoked(self):
        """Remembering covers this Core identity and view; revocation survives a fresh store."""
        with patch("verification.threading.Thread.start"):
            self.assertEqual(self.client.post("/tasks/chat/browser-permissions", json=self.payload(True), **self.ingress).status_code, 202)
        capture = self.engine.ui_captures["chat"]
        self.assertEqual(capture["grants"], [], "remembered rules come from the store only")
        with patch.object(self.engine, "browser", return_value={"status": "captured"}):
            self.engine._retry_capture("chat", capture)
        store = BrowserPermissionStore(server)
        self.assertEqual(store.rules(self.scope, "/lovelace/home"), self.rules)
        self.assertEqual(store.rules(self.scope, "/lovelace/home/"), self.rules, "a trailing slash names the same view")
        self.assertEqual(store.rules(self.scope, "/lovelace/other"), [])
        self.assertEqual(store.rules("b" * 64, "/lovelace/home"), [])
        listing = self.client.get("/browser-permissions", **self.ingress).get_json()["permissions"]
        self.assertEqual(len(listing), 2)
        self.assertNotIn(self.scope, json.dumps(listing))
        revision = self.engine.permissions.revision
        self.assertEqual(self.client.delete("/browser-permissions/" + listing[0]["id"], **self.ingress).status_code, 200)
        self.assertGreater(self.engine.permissions.revision, revision)
        self.assertEqual(len(store.rules(self.scope, "/lovelace/home")), 1)
        self.assertEqual(self.client.delete("/browser-permissions/" + listing[0]["id"], **self.ingress).status_code, 400)

    def test_remembered_permissions_survive_a_rotated_worker_token(self):
        """The integration provisions a new worker token at every start; saved rules must still apply."""
        self.engine.permissions.remember(self.scope, "/lovelace/home", self.rules[:1])
        self.start_agent_check()
        sent = []
        with patch.object(server, "api_token", return_value="provisioned-after-restart"), \
             patch.object(self.engine, "core", return_value=self.session), \
             patch("verification.subprocess.Popen", side_effect=echo_controller(sent)):
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/home/"})
        self.assertEqual(json.loads(result["message"]), self.rules[:1])

    def test_partial_selection_and_invalid_batches_never_grant_unselected_requests(self):
        """Select one request atomically, refusing fabricated IDs, duplicates and hidden bodies."""
        for changes in ({"candidate_ids": ["f" * 64]}, {"candidate_ids": []},
                        {"candidate_ids": [self.rules[0]["id"]] * 2}, {"remember": "yes"},
                        {"extra_rule": self.rules[0]}, {"turn_id": "older-turn"}, {"turn_id": 5},
                        {"check_id": ["capture-one"]}):
            with self.subTest(changes=changes):
                response = self.client.post("/tasks/chat/browser-permissions", json={**self.payload(True), **changes}, **self.ingress)
                self.assertIn(response.status_code, {400, 409})
                self.assertEqual(self.engine.permissions.list(), [])
        with patch("verification.threading.Thread.start"):
            response = self.client.post("/tasks/chat/browser-permissions", json={**self.payload(True), "candidate_ids": [self.rules[0]["id"]]}, **self.ingress)
        self.assertEqual(response.status_code, 202)
        capture = self.engine.ui_captures["chat"]
        with patch.object(self.engine, "browser", return_value={"status": "captured"}):
            self.engine._retry_capture("chat", capture)
        self.assertEqual(self.engine.permissions.rules(self.scope, "/lovelace/home"), self.rules[:1])

    def test_busy_turns_stale_checks_and_deleted_chats_fail_before_start(self):
        """Never apply old approvals to a working, replaced, cancelled or removed exchange."""
        task = server.tasks["chat"]
        for field, value in (("status", "running"), ("current_turn_id", "new-turn"), ("cancellation_requested", True)):
            with self.subTest(field=field), patch.dict(task, {field: value}):
                response = self.client.post("/tasks/chat/browser-permissions", json=self.payload(True), **self.ingress)
                self.assertEqual(response.status_code, 409)
                if field == "cancellation_requested":
                    self.assertIn("stopped", response.get_json()["error"])
        with patch.dict(server.tasks, {}, clear=True):
            self.assertEqual(self.client.post("/tasks/chat/browser-permissions", json=self.payload(True), **self.ingress).status_code, 409)
        self.assertEqual(self.engine.permissions.list(), [])
        self.assertTrue(self.engine.execution_lock.acquire(blocking=False))
        self.engine.execution_lock.release()

    def test_unreadable_storage_never_grants_access_or_blocks_captures(self):
        """Keep corrupt state intact, run captures without it, and let Settings remove it."""
        path = self.root / "browser-permissions.json"
        path.write_text('{"broken": true}')
        response = self.client.get("/browser-permissions", **self.ingress)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.get_json()["unreadable"])
        self.assertEqual(self.client.post("/tasks/chat/browser-permissions", json=self.payload(True), **self.ingress).status_code, 400)
        self.assertEqual(path.read_text(), '{"broken": true}')
        self.assertNotIn("permission_review", server.tasks["chat"]["verification"][0])
        self.start_agent_check()
        sent = []
        with patch.object(self.engine, "core", return_value=self.session), \
             patch("verification.subprocess.Popen", side_effect=echo_controller(sent)):
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/home"})
        self.assertEqual(json.loads(result["message"]), [], "the capture still runs, without saved permissions")
        self.assertIn("could not be read", result["permission_store_error"])
        self.assertEqual(self.client.delete("/browser-permissions", **self.ingress).status_code, 200)
        self.assertEqual(self.client.get("/browser-permissions", **self.ingress).get_json()["permissions"], [])

    def test_failed_writes_roll_back_the_receipt(self):
        """Make failed persistence actionable without consuming the batch or leaking paths."""
        with patch("browser_permissions.os.replace", side_effect=OSError("private path")):
            response = self.client.post("/tasks/chat/browser-permissions", json=self.payload(True), **self.ingress)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private path", response.get_data(as_text=True))
        # The receipt saves, then only the permission store fails.
        with patch.object(self.engine.permissions, "_write", side_effect=OSError("private path")):
            response = self.client.post("/tasks/chat/browser-permissions", json=self.payload(True), **self.ingress)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("permission_review", server.tasks["chat"]["verification"][0])
        persisted = json.loads((server.get_task_dir("chat") / "task.json").read_text())
        self.assertNotIn("permission_review", persisted["verification"][0])
        self.assertEqual(self.engine.permissions.list(), [])
        self.assertIsNone(self.engine.retry_status("chat"))
        self.assertEqual(self.active_ids(), set())
        self.assertTrue(self.engine.execution_lock.acquire(blocking=False))
        self.engine.execution_lock.release()

    def test_one_unsupported_entry_is_ignored_listed_and_revocable(self):
        """An entry a later version rejects never disables the others."""
        self.engine.permissions.remember(self.scope, "/lovelace/home", self.rules)
        path = self.root / "browser-permissions.json"
        entries = json.loads(path.read_text())
        entries[0]["rule"]["request"] = entries[0]["rule"]["request"].replace("list_modules", "delete_modules")
        path.write_text(json.dumps(entries))
        self.assertEqual(self.engine.permissions.rules(self.scope, "/lovelace/home"), self.rules[1:])
        listing = self.client.get("/browser-permissions", **self.ingress).get_json()["permissions"]
        unsupported = [item for item in listing if item.get("unsupported")]
        self.assertEqual((len(listing), len(unsupported)), (2, 1))
        self.assertEqual(self.client.delete("/browser-permissions/" + unsupported[0]["id"], **self.ingress).status_code, 200)
        self.assertEqual([item["label"] for item in self.engine.permissions.list()], ["WebSocket subscribe_condition"])

    def test_failed_thread_start_records_failure_and_releases_the_capture_slot(self):
        """An accepted batch gets an explicit failure if its capture cannot start."""
        with patch("verification.threading.Thread.start", side_effect=RuntimeError("No threads")), \
             patch.object(self.engine, "browser") as browser:
            response = self.client.post("/tasks/chat/browser-permissions", json=self.payload(), **self.ingress)
        self.assertEqual(response.status_code, 202)
        browser.assert_not_called()
        self.assertEqual(server.tasks["chat"]["verification"][-1]["status"], "unavailable")
        self.assertIsNone(self.engine.retry_status("chat"))
        self.assertEqual(self.active_ids(), set())
        self.assertTrue(self.engine.execution_lock.acquire(blocking=False))
        self.engine.execution_lock.release()

    def test_sensitive_write_restricted_and_malformed_rules_are_never_eligible(self):
        """Validation refuses dangerous or restricted commands even with a correctly computed fingerprint."""
        bad = [request_rule(request={"type": name}) for name in (
            "call_service", "auth/sign_path", "map_tiles/access_token", "camera/webrtc/get_client_config", "camera/webrtc/offer", "camera/webrtc/candidate",
            "custom_tools/get_and_delete", "custom_tools/register", "config/list", "execute_script",
            "knob_swipe_navigation/subscribe_rotations", "subscribe_events", "browser_mod/settings/get")]
        bad += [request_rule(request={"type": "custom/get", "password": "hidden"}),
                request_rule(request={"type": "custom/get", "query": "Bearer secret"}),
                request_rule(request={"type": "custom_icons/list", "set": "../../../config"}),
                request_rule(request={"type": "subscribe_events", "event_type": "call_service"}),
                request_rule("http", {"method": "POST", "path": "/api/custom/read", "query": []}),
                request_rule("http", {"method": "GET", "path": "/api/custom/get_token", "query": []}),
                request_rule("http", {"method": "GET", "path": "/api/custom/delete-all", "query": []}),
                request_rule("http", {"method": "GET", "path": "/api/x/setValue", "query": []}),
                request_rule("http", {"method": "GET", "path": "/api/x/reboot.cgi", "query": []}),
                request_rule(request={"type": "custom/get", "value": 10 ** 1000}),
                request_rule("http", {"method": "GET", "path": "/api/custom/read", "query": [["token", "hidden"]]})]
        for rule in bad:
            with self.subTest(rule=rule):
                self.assertIsNone(valid_rule(rule))
        self.assertIsNone(valid_rule({**self.rules[0], "id": "0" * 64}))
        self.assertIsNotNone(valid_rule(request_rule("map_tiles", {"type": "map_tiles/raster"})))
        self.assertIsNotNone(valid_rule(request_rule("http", {"method": "GET", "path": "/api/x/getSettings", "query": []})))
        self.assertIsNone(valid_rule({"kind": [], "request": "{}", "id": "f" * 64}))

    def test_identity_and_origin_isolate_rules_but_the_worker_token_does_not(self):
        """Another Core identity or origin gets no grants; the rotating pairing token is not part of the scope."""
        self.assertIsNone(scope({"url": self.session["url"]}))
        for changes in ({"installation_id": "b" * 32}, {"url": "http://other:8123"}):
            self.assertNotEqual(scope({**self.session, **changes}), self.scope)
        self.assertEqual(scope({**self.session, "access_token": "another-lease"}), self.scope)

    def test_changed_installation_is_rejected_and_its_session_is_revoked(self):
        """A new Core identity cannot consume an approval collected on the previous one."""
        with patch("verification.threading.Thread.start"):
            self.engine.request_retry("chat", self.payload())
        with patch.object(self.engine, "core", return_value={**self.session, "installation_id": "b" * 32}) as core, \
             patch("verification.subprocess.Popen") as launch:
            self.engine._retry_capture("chat", self.engine.ui_captures["chat"])
        launch.assert_not_called()
        core.assert_called_with("DELETE", "codex_cli/browser_session", json={"session_id": "private-lease"})
        result = server.tasks["chat"]["verification"][-1]
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("installation changed", result["message"])

    def test_new_turn_or_deletion_during_retry_drops_its_result(self):
        """Late capture results cannot attach to a new message or recreate a deleted task."""
        with patch("verification.threading.Thread.start"):
            self.engine.request_retry("chat", self.payload())
        capture = self.engine.ui_captures["chat"]
        with patch.object(self.engine, "browser", side_effect=lambda *args, **kwargs: server.tasks.clear() or {"status": "captured"}):
            self.engine._retry_capture("chat", capture)
        self.assertNotIn("chat", server.tasks)
        self.assertIsNone(self.engine.retry_status("chat"))
        self.assertEqual(self.active_ids(), set())

    def test_saved_rules_reach_controller_and_model_payload_cannot_add_grants(self):
        """The worker supplies grants from private storage, ignoring model-supplied extras."""
        self.engine.permissions.remember(self.scope, "/lovelace/home", self.rules[:1])
        self.start_agent_check()
        sent = []
        with patch.object(self.engine, "core", return_value=self.session), patch("verification.subprocess.Popen", side_effect=echo_controller(sent)):
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/home",
                                               "permissions": self.rules, "grants": self.rules})
        self.assertEqual(json.loads(result["message"]), self.rules[:1])
        self.assertIsNotNone(sent[0].poll())

    def test_agent_events_and_token_api_see_labels_only(self):
        """Exact blocked requests stay with the Ingress review."""
        full = self.client.get("/tasks/chat", **self.ingress).get_json()["task"]
        self.assertIn("request", full["verification"][0]["permission_candidates"][0])
        for response in (self.client.get("/tasks/chat", headers={"Authorization": "Bearer paired-worker"}),
                         self.client.get("/tasks", headers={"Authorization": "Bearer paired-worker"})):
            body = response.get_json()
            task = body["task"] if "task" in body else next(item for item in body["tasks"] if item["task_id"] == "chat")
            for record in (task, task["turns"][-1]):
                check = record["verification"][0]
                self.assertNotIn("permission_scope", check)
                self.assertEqual(check["permission_candidates"][0],
                                 {"id": self.rules[0]["id"], "kind": "websocket", "label": "WebSocket bubble_card_tools/list_modules"})
        self.start_agent_check()
        sent = []
        with patch.object(self.engine, "core", return_value=self.session), \
             patch("verification.subprocess.Popen", side_effect=echo_controller(sent, candidates=self.rules)):
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/home"})
        self.assertEqual([item["label"] for item in result["permission_candidates"]],
                         ["WebSocket bubble_card_tools/list_modules", "WebSocket subscribe_condition"])
        self.assertNotIn("light.kitchen", json.dumps(result), "the agent sees no request parameters")
        self.assertIn("light.kitchen", json.dumps(server.tasks["chat"]["verification"][-1]), "the Ingress review keeps them")

    def test_status_only_exposes_exact_requests_to_ingress(self):
        """The integration's status poll must sanitize every private verification field too."""
        self.check.update(permission_grants=self.rules,
                          permission_review={"approved": [describe(rule) for rule in self.rules]},
                          permission_decision={"approved": [describe(self.rules[0])], "carried": [describe(self.rules[1])]})
        with patch.object(server, "refresh_usage_status_async"), \
             patch.object(server, "codex_login_status", return_value={}), \
             patch.object(server, "auth_status_payload", return_value={}), \
             patch.object(server, "usage_status_payload", return_value={}), \
             patch.object(server, "integration_status", return_value={}):
            private = self.client.get("/status", **self.ingress).get_json()["latest_task"]["verification"][0]
            response = self.client.get("/status", headers={"Authorization": "Bearer paired-worker"})
        self.assertEqual(response.status_code, 200)
        public = response.get_json()["latest_task"]["verification"][0]
        self.assertNotIn("permission_scope", public)
        self.assertNotIn("permission_grants", public)
        self.assertEqual(public["permission_candidates"][0],
                         {"id": self.rules[0]["id"], "kind": "websocket", "label": "WebSocket bubble_card_tools/list_modules"})
        self.assertEqual(public["permission_review"]["approved"], [{"label": describe(rule)["label"]} for rule in self.rules])
        self.assertEqual(public["permission_decision"]["carried"], [{"label": describe(self.rules[1])["label"]}])
        self.assertIn("request", private["permission_candidates"][0])
        self.assertEqual(private["permission_grants"], self.rules)
        self.assertIn("permission_scope", self.check, "a public projection must not change the stored evidence")

    def test_older_integration_and_hidden_credentials_are_explained(self):
        """Requests that can't be offered are never silently dropped."""
        hidden = request_rule(request={"type": "custom/get", "note": "apikey=AAAAAAAAAAAAAAAAAAAA"})
        self.assertIsNotNone(valid_rule(hidden))
        self.start_agent_check()
        older = {key: value for key, value in self.session.items() if key != "installation_id"}
        sent = []
        with patch.object(self.engine, "core", return_value=older), \
             patch("verification.subprocess.Popen", side_effect=echo_controller(sent, candidates=self.rules)):
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/home"})
        self.assertEqual(result["permission_candidates"], [])
        self.assertIn("Update the Codex integration", result["permission_review_unavailable"])
        with patch.object(self.engine, "core", return_value=self.session), \
             patch("verification.subprocess.Popen", side_effect=echo_controller(sent, candidates=[*self.rules, hidden])):
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/home"})
        self.assertEqual(len(result["permission_candidates"]), 2)
        self.assertEqual(result["permission_candidates_omitted"], 1)

    def test_one_off_grants_carry_into_later_retries(self):
        """Approving a request revealed by a retry keeps the requests approved before it."""
        later = request_rule(request={"type": "custom/get", "after": "list_modules"})
        with patch("verification.threading.Thread.start"):
            self.engine.request_retry("chat", self.payload(candidate_ids=[self.rules[0]["id"]]))
        revealed = {"status": "issues", "path": "/lovelace/home", "permission_scope": self.scope,
                    "permission_candidates": [{**later, **describe(later)}]}
        with patch.object(self.engine, "browser", return_value=revealed):
            self.engine._retry_capture("chat", self.engine.ui_captures["chat"])
        first = server.tasks["chat"]["verification"][-1]
        persisted = json.loads((server.get_task_dir("chat") / "task.json").read_text())
        self.assertNotIn("permission_grants", persisted["verification"][-1], "active grants stay out of editable chat files")
        # Even a forged legacy field must not replace the worker's private approvals.
        first["permission_grants"] = self.rules[1:]
        with patch("verification.threading.Thread.start"):
            self.engine.request_retry("chat", self.payload(check_id=first["check_id"], candidate_ids=[later["id"]]))
        with patch.object(self.engine, "browser", return_value={"status": "captured"}) as browser:
            self.engine._retry_capture("chat", self.engine.ui_captures["chat"])
        self.assertEqual(browser.call_args.kwargs["retry"]["grants"], [self.rules[0], later])
        decision = server.tasks["chat"]["verification"][-1]["permission_decision"]
        self.assertEqual([item["label"] for item in decision["carried"]], ["WebSocket bubble_card_tools/list_modules"])
        self.assertEqual(self.engine.permissions.list(), [], "one-off approvals are never saved")

    def test_restored_task_metadata_cannot_grant_unselected_requests(self):
        """A model can edit task.json, but its restored grants have no authority after a restart."""
        self.options["task_root"] = str(server.CONFIG_ROOT / "codex_tasks")
        self.check["permission_grants"] = self.rules[1:]
        server.update_task_evidence("chat", verification=[self.check])
        server.tasks.clear()
        server.load_task_index()
        replacement = Verification(server)
        with patch("verification.threading.Thread.start"):
            replacement.request_retry("chat", self.payload(candidate_ids=[self.rules[0]["id"]]))
        capture = replacement.ui_captures["chat"]
        self.assertEqual(capture["carried"], [])
        self.assertEqual(capture["grants"], self.rules[:1])
        with patch.object(replacement, "browser", return_value={"status": "captured"}):
            replacement._retry_capture("chat", capture)

    def test_one_off_grants_expire_on_worker_restart(self):
        """A completed retry's audit record cannot revive its one-off access in a new worker."""
        later = request_rule(request={"type": "custom/get", "after": "list_modules"})
        with patch("verification.threading.Thread.start"):
            self.engine.request_retry("chat", self.payload(candidate_ids=[self.rules[0]["id"]]))
        revealed = {"status": "issues", "path": "/lovelace/home", "permission_scope": self.scope,
                    "permission_candidates": [{**later, **describe(later)}]}
        with patch.object(self.engine, "browser", return_value=revealed):
            self.engine._retry_capture("chat", self.engine.ui_captures["chat"])
        persisted = json.loads((server.get_task_dir("chat") / "task.json").read_text())
        server.tasks["chat"] = persisted
        replacement = Verification(server)
        with patch("verification.threading.Thread.start"):
            replacement.request_retry("chat", self.payload(check_id=persisted["verification"][-1]["check_id"], candidate_ids=[later["id"]]))
        capture = replacement.ui_captures["chat"]
        self.assertEqual(capture["carried"], [])
        self.assertEqual(capture["grants"], [later])
        with patch.object(replacement, "browser", return_value={"status": "captured"}):
            replacement._retry_capture("chat", capture)

    def test_revocation_stops_a_running_capture_and_revokes_its_core_session(self):
        """A capture cannot keep using the grants it loaded before a revocation."""
        self.engine.permissions.remember(self.scope, "/lovelace/home", self.rules[:1])
        saved = self.engine.permissions.list()[0]
        self.start_agent_check()
        original = subprocess.Popen
        processes = []

        def launch(_args, **kwargs):
            """Keep a real child alive until the worker's revocation guard stops it."""
            process = original([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"], **kwargs)
            processes.append(process)
            return process

        def revoke(_pid, _tracked):
            """Revoke after the controller starts and before its next lifecycle check."""
            self.engine.permissions.revoke(saved["id"])
            return 0

        with patch.object(self.engine, "core", return_value=self.session) as core, \
             patch("verification.subprocess.Popen", side_effect=launch), patch("verification.track_browser_processes", side_effect=revoke):
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/home"})
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("permissions changed", result["message"])
        self.assertIsNotNone(processes[0].poll())
        core.assert_called_with("DELETE", "codex_cli/browser_session", json={"session_id": "private-lease"})

    def test_unrelated_revocation_does_not_stop_a_capture(self):
        """Only revoking a saved permission the capture loaded stops it."""
        other = request_rule(request={"type": "custom/get", "dashboard": "other"})
        self.engine.permissions.remember(self.scope, "/lovelace/home", self.rules[:1])
        self.engine.permissions.remember(self.scope, "/lovelace/other", [other])
        other_id = next(item["id"] for item in self.engine.permissions.list() if item["path"] == "/lovelace/other")
        self.start_agent_check()
        revoked = []

        def revoke_other(_pid, _tracked):
            """Revoke another dashboard's permission while this capture runs."""
            if not revoked:
                revoked.append(self.engine.permissions.revoke(other_id))
            return 0

        sent = []
        with patch.object(self.engine, "core", return_value=self.session), \
             patch("verification.subprocess.Popen", side_effect=echo_controller(sent, delay=0.5)), \
             patch("verification.track_browser_processes", side_effect=revoke_other):
            result = self.engine.run("chat", {"operation": "dashboard", "path": "/lovelace/home"})
        self.assertTrue(revoked)
        self.assertEqual(result["status"], "issues")
        self.assertEqual(json.loads(result["message"]), self.rules[:1])

    def test_revocation_before_a_pending_capture_does_not_resurrect_saved_access(self):
        """A retry cannot reintroduce a remembered grant that Settings revoked before it ran."""
        with patch("verification.threading.Thread.start"):
            self.engine.request_retry("chat", self.payload(True))
        capture = self.engine.ui_captures["chat"]
        self.engine.permissions.revoke(self.engine.permissions.list()[0]["id"])
        sent = []
        with patch.object(self.engine, "core", return_value=self.session), \
             patch("verification.subprocess.Popen", side_effect=echo_controller(sent)):
            self.engine._retry_capture("chat", capture)
        result = server.tasks["chat"]["verification"][-1]
        self.assertEqual(json.loads(result["message"]), self.rules[1:])
        self.assertEqual(len(self.engine.permissions.rules(self.scope, "/lovelace/home")), 1)


if __name__ == "__main__":
    unittest.main()
