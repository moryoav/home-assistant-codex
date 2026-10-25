"""Bounded HA diagnostics and browser evidence, owned by the worker, not the model."""
from __future__ import annotations

import copy
import hmac
import json
import os
import re
import secrets
import signal
import socketserver
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import requests
import websocket

from browser_permissions import (BrowserPermissionStore, HEX, LIMITS, MAX_CANDIDATES, MAX_RETRIES, PATH,
                                 describe, scope, valid_rule)

MAX_CHECKS = 24
MAX_BROWSERS = 4
DEFAULT_BROWSER_MEMORY_LIMIT_MIB = 1536
SCREENSHOT_MAX_BYTES = 5 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 3600
ENTITY_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
# One dashboard path rule, shared with the permission store.
PATH_RE = PATH
INTEGRATION_UPDATE_NOTE = "Update the Codex integration in Home Assistant to approve blocked browser requests."


def public_check(entry):
    """Return a check without exact blocked-request details, saved scopes or grants.

    Request parameters can carry values the denylists don't recognize, so only
    the human Ingress review sees them. The agent, Home Assistant events and
    token-authenticated API callers get each request's label.
    """
    if not isinstance(entry, dict):
        return entry
    result = {key: value for key, value in entry.items() if key not in {"permission_scope", "permission_grants"}}

    def labels(items):
        """Keep only the readable label of each described request."""
        return [{"label": item.get("label", "")} for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    if "permission_candidates" in entry:
        result["permission_candidates"] = [{"id": item.get("id"), "kind": item.get("kind"), "label": item.get("label", "")}
                                           for item in entry["permission_candidates"] or [] if isinstance(item, dict)]
    for key in ("permission_review", "permission_decision"):
        if isinstance(entry.get(key), dict):
            result[key] = {**entry[key], **{name: labels(entry[key][name]) for name in ("approved", "carried") if name in entry[key]}}
    return result


class Verification:
    """Run the bounded checks a running task may request with its capability, and record them on its current turn."""

    def __init__(self, worker):
        """Keep the worker that provides tasks, options and Home Assistant access; no capability is issued yet."""
        self.worker = worker
        self.capabilities = {}
        self.lock = threading.RLock()
        self.execution_lock = threading.Lock()
        self.permissions = BrowserPermissionStore(worker)
        self.ui_captures = {}
        # Authority stays in worker memory, never in agent-writable task.json.
        # Restarting the worker expires one-off grants; audit records remain.
        self.retry_grants = {}

    def begin(self, task_id):
        """Issue a new capability for the task's turn, clear its recorded checks, and delete expired screenshots."""
        self.cleanup()
        with self.lock:
            capability = secrets.token_urlsafe(32)
            self.capabilities[task_id] = capability
        self.worker.update_task(task_id, verification=[], verification_attachments=[])
        return capability

    def end(self, task_id):
        """Withdraw the task's capability so later requests that present it are refused."""
        with self.lock:
            self.capabilities.pop(task_id, None)

    def dispatch(self, payload):
        """Run a request's check for the running task that owns its capability; raise ValueError if there is none."""
        if not isinstance(payload, dict):
            raise ValueError("Expected a verification object")
        supplied = payload.get("capability", "")
        if not isinstance(supplied, str) or not supplied or not supplied.isascii():
            raise ValueError("No active verification capability")
        with self.lock:
            task_id = next((key for key, value in self.capabilities.items()
                            if hmac.compare_digest(supplied, value)), None)
        if not task_id or self.worker.task_cancellation_requested(task_id):
            raise ValueError("Verification capability expired")
        with self.worker.lock:
            if self.worker.tasks.get(task_id, {}).get("status") != "running":
                raise ValueError("Verification capability expired")
        return self.run(task_id, {k: v for k, v in payload.items() if k != "capability"}, supplied)

    def active(self, task_id, turn_id, capability):
        """Reject checks that outlive cancellation, completion, or a replaced turn."""
        with self.lock:
            current = self.capabilities.get(task_id)
            manual = self.ui_captures.get(task_id, {})
        with self.worker.lock:
            task = self.worker.tasks.get(task_id, {})
            authorized = (current == capability and task.get("status") == "running") or (
                manual.get("capability") == capability and manual.get("turn_id") == turn_id
                and task.get("status") not in {"running", "queued", "in_queue"})
            return bool(capability and task and authorized and task.get("current_turn_id") == turn_id
                        and not self.worker.task_cancellation_requested(task_id))

    def retry_status(self, task_id):
        """Expose progress to the UI without exposing its private capture capability."""
        with self.lock:
            item = self.ui_captures.get(task_id)
            return {"source_check_id": item["source_check_id"], "turn_id": item["turn_id"]} if item else None

    def request_retry(self, task_id, payload):
        """Accept an Ingress user's selected batch, then run a fresh observational capture.

        This entry point is never exposed by the model's MCP tool. Only candidate
        IDs from an unreviewed check in the current completed turn are accepted.
        """
        if (not isinstance(payload, dict) or set(payload) != {"turn_id", "check_id", "candidate_ids", "remember"}
            or not isinstance(payload.get("turn_id"), str) or not isinstance(payload.get("check_id"), str)
            or type(payload.get("remember")) is not bool or not isinstance(payload.get("candidate_ids"), list)
            or not 1 <= len(payload["candidate_ids"]) <= MAX_CANDIDATES
            or any(not isinstance(item, str) or not HEX.fullmatch(item) for item in payload["candidate_ids"])
            or len(set(payload["candidate_ids"])) != len(payload["candidate_ids"])):
            raise ValueError("Select one or more complete browser requests and a permission duration")
        if not self.execution_lock.acquire(blocking=False):
            raise RuntimeError("Another verification is running. Wait for it to finish, then retry.")
        handed_off = False
        try:
            with self.worker.lock:
                task = self.worker.tasks.get(task_id)
                if not task or task.get("current_turn_id") != payload["turn_id"]:
                    raise RuntimeError("The conversation changed. Refresh and try again.")
                if task.get("cancellation_requested"):
                    raise RuntimeError("This exchange was stopped. Start a new inspection to approve these requests.")
                if self.worker._active_task_ids_locked():
                    raise RuntimeError("A task is still working. Approve these requests after it finishes.")
                entries = list(task.get("verification") or [])
                source = next((item for item in entries if item.get("check_id") == payload["check_id"]
                               and item.get("operation") == "dashboard"), None)
                if (not source or not HEX.fullmatch(str(source.get("permission_scope", "")))
                    or not PATH_RE.fullmatch(str(source.get("path", "")))
                    or source.get("permission_review")
                    or any(item.get("retry_of") == payload["check_id"] for item in entries)):
                    raise RuntimeError("This inspection is no longer available for approval. Refresh the conversation.")
                if len(entries) >= MAX_CHECKS or sum(bool(item.get("retry_of")) for item in entries) >= MAX_RETRIES:
                    raise RuntimeError("Inspection retry limit reached for this exchange. Start a new inspection.")
                candidates = {rule["id"]: rule for rule in map(valid_rule, source.get("permission_candidates") or []) if rule}
                if any(item not in candidates for item in payload["candidate_ids"]):
                    raise RuntimeError("The selected requests no longer match this inspection. Refresh and try again.")
                selected = [candidates[item] for item in payload["candidate_ids"]]
                # One-off approvals made earlier in this exchange keep applying to
                # its later retries. Task metadata, including legacy grant fields,
                # is editable by the agent and cannot authorize carried access.
                grant_key = (task_id, payload["turn_id"], payload["check_id"])
                with self.lock:
                    self.retry_grants = {key: rules for key, rules in self.retry_grants.items()
                                         if self.worker.tasks.get(key[0], {}).get("current_turn_id") == key[1]}
                    carried = [rule for rule in map(valid_rule, self.retry_grants.get(grant_key, [])) if rule]
                if any(self.worker.redact(rule["request"]) != rule["request"] for rule in [*selected, *carried]):
                    raise ValueError("Requests with hidden credentials cannot be approved")
                grants = list({rule["id"]: rule for rule in [*carried, *([] if payload["remember"] else selected)]}.values())
                previous = copy.deepcopy(task)
                reviewed = {**source, "permission_review": {"remember": payload["remember"],
                            "reviewed_at": self.worker.utc_now(), "approved": [describe(rule) for rule in selected]}}
                try:
                    # Consume the approval durably before starting any capture.
                    # A restart must not turn an accepted one-off into a replay.
                    # Evidence never moves the chat or the latest task.
                    with self.permissions.lock:
                        self.worker.update_task_evidence(task_id, verification=[reviewed if item is source else item for item in entries])
                        if payload["remember"]:
                            self.permissions.remember(source["permission_scope"], source["path"], selected)
                except (OSError, ValueError):
                    self.worker.tasks[task_id] = previous
                    try:
                        self.worker.atomic_json_write(self.worker.get_task_dir(task_id) / "task.json", previous)
                        self.worker.save_task_index()
                    except OSError:
                        pass  # A persisted receipt is conservative if rollback cannot be saved.
                    raise
                capture = {"capability": secrets.token_urlsafe(32), "turn_id": payload["turn_id"],
                           "source_check_id": payload["check_id"], "scope": source["permission_scope"],
                           "path": source["path"], "selected": selected, "carried": carried, "grants": grants,
                           "remember": payload["remember"]}
                with self.lock:
                    self.retry_grants.pop(grant_key, None)
                    self.ui_captures[task_id] = capture
                # The retry is worker activity: new messages wait in the queue,
                # and the chat can't be deleted or continued, until it ends.
                self.worker.active_task_runners.add(task_id)
            thread = threading.Thread(target=self._retry_capture, args=(task_id, capture), daemon=True)
            handed_off = True
            try:
                thread.start()
            except Exception:
                # The permission was accepted; retain an explicit failed retry
                # rather than pretending that saved grants were rolled back.
                self._retry_capture(task_id, capture, launch_failed=True)
            return {"accepted": True}
        finally:
            if not handed_off:
                self.execution_lock.release()

    def _retry_capture(self, task_id, capture, launch_failed=False):
        """Capture without rerunning the agent or changing its completed task status."""
        entry = {"operation": "dashboard", "check_id": secrets.token_hex(16),
                 "checked_at": self.worker.utc_now(), "path": capture["path"],
                 "retry_of": capture["source_check_id"],
                 "permission_decision": {"remember": capture["remember"],
                                         "approved": [describe(rule) for rule in capture["selected"]],
                                         "carried": [describe(rule) for rule in capture["carried"]]}}
        try:
            try:
                if launch_failed:
                    raise ValueError("The capture could not start. Start a new inspection to retry.")
                entry.update(self.browser(task_id, {"path": capture["path"]}, capture["turn_id"],
                                          capture["capability"], retry=capture))
            except Exception as exc:
                # Do not persist network exception URLs, tokens, or response bodies.
                detail = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
                entry.update(status="unavailable", message=f"Inspection retry could not finish: {detail[:200]}")
            entry = json.loads(self.worker.redact(json.dumps(entry)))
            with self.worker.lock:
                if self.active(task_id, capture["turn_id"], capture["capability"]):
                    entries = list(self.worker.tasks[task_id].get("verification") or [])
                    if self.worker.update_task_evidence(task_id, verification=[*entries, entry]) and capture["grants"]:
                        with self.lock:
                            self.retry_grants[(task_id, capture["turn_id"], entry["check_id"])] = capture["grants"]
        finally:
            with self.lock:
                self.ui_captures.pop(task_id, None)
            self.execution_lock.release()
            try:
                # Release the retry's activity and start messages that waited for it.
                self.worker.start_next_queued(release=task_id)
            except Exception as exc:
                print(f"Could not start queued messages after a browser retry: {type(exc).__name__}", flush=True)

    def core(self, method, path, **kwargs):
        """Call a Core API path through the Supervisor proxy and return the JSON of its answer.

        Requests to the integration's own codex_cli/ paths also carry the worker token in their JSON body.
        Raises ValueError when no token is available, the answer is not HTTP 200, or its body exceeds 256 KiB.
        """
        token = self.worker.ha_token()
        if not token:
            raise ValueError("Home Assistant authentication is unavailable")
        headers = {"Authorization": f"Bearer {token}"}
        if path.startswith("codex_cli/"):
            kwargs["json"] = {**kwargs.get("json", {}), "worker_token": self.worker.api_token()}
        # The automatic token belongs to the Supervisor proxy, never a supplied URL.
        url = "http://supervisor/core/api/" + path
        with requests.request(method, url, headers=headers, timeout=20,
                              allow_redirects=False, stream=True, **kwargs) as response:
            if response.status_code != 200:
                raise ValueError(f"Home Assistant returned HTTP {response.status_code}")
            content = bytearray()
            for chunk in response.iter_content(8192):
                content.extend(chunk)
                if len(content) > 256 * 1024:
                    raise ValueError("Home Assistant response exceeds the diagnostic limit")
        return json.loads(content)

    def ws_read(self, message):
        """Send one command over the Core WebSocket and return its result.

        Raises ValueError when authentication fails, a reply exceeds 1 MiB, or no successful result arrives.
        """
        token = self.worker.ha_token()
        with closing(websocket.create_connection("ws://supervisor/core/websocket", timeout=15)) as ws:
            first = json.loads(ws.recv())
            if first.get("type") == "auth_required":
                ws.send(json.dumps({"type": "auth", "access_token": token}))
                first = json.loads(ws.recv())
            if first.get("type") != "auth_ok":
                raise ValueError("Home Assistant WebSocket authentication failed")
            ws.send(json.dumps({"id": 1, **message}))
            for _ in range(20):
                raw = ws.recv()
                if len(raw) > 1024 * 1024:
                    raise ValueError("Dashboard response exceeds the verification limit")
                result = json.loads(raw)
                if result.get("id") == 1:
                    if not result.get("success"):
                        raise ValueError("Home Assistant could not read this dashboard")
                    return result["result"]
        raise ValueError("Home Assistant did not return a result")

    def run(self, task_id, payload, capability=None):
        """Run one check for the task's current turn and add its redacted result to the turn's verification list.

        Returns an "unavailable" result instead of raising when another check is running, the capability
        has expired, the turn's check limit is reached, or the operation fails.
        """
        # One inspection at a time, even if the agent launches parallel commands.
        if not self.execution_lock.acquire(blocking=False):
            return {"status": "unavailable", "message": "Another verification is running"}
        try:
            with self.lock:
                capability = capability or self.capabilities.get(task_id)
            with self.worker.lock:
                task = self.worker.tasks.get(task_id, {})
                turn_id = task.get("current_turn_id")
                entries = list(task.get("verification") or [])
            if not self.active(task_id, turn_id, capability):
                return {"status": "unavailable", "message": "Verification capability expired"}
            if len(entries) >= MAX_CHECKS:
                return {"status": "unavailable", "message": "Verification limit reached for this turn"}
            operation = payload.get("operation")
            entry = {"operation": str(operation)[:40], "checked_at": self.worker.utc_now()}
            if operation == "dashboard":
                entry["check_id"] = secrets.token_hex(16)
            try:
                if operation == "entity":
                    entry.update(self.entity(payload))
                elif operation == "config_check":
                    entry.update(self.worker.check_home_assistant_config())
                    entry["status"] = {"valid": "passed", "invalid": "failed"}.get(entry["result"], "unavailable")
                elif operation == "logs":
                    target = payload.get("target", "core")
                    if target != "core":
                        raise ValueError("Only Core logs are available to verification")
                    result = self.core("POST", "codex_cli/diagnostic_logs", json={"target": target})
                    entry.update(status="observed", target=target, message=result["text"], truncated=result.get("truncated", False))
                elif operation == "dashboard":
                    if sum(item.get("operation") == "dashboard" for item in entries) >= MAX_BROWSERS:
                        raise ValueError("Dashboard limit reached for this turn")
                    entry.update(self.browser(task_id, payload, turn_id, capability))
                elif operation == "dashboard_readback":
                    entry.update(self.dashboard_readback(payload))
                else:
                    raise ValueError("Supported operations: entity, config_check, logs, dashboard, dashboard_readback")
            except Exception as exc:
                # Do not persist network exception URLs, tokens, or response bodies.
                detail = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
                entry.update(status="unavailable", message=f"Verification could not finish: {detail[:200]}")
            entry = json.loads(self.worker.redact(json.dumps(entry)))
            with self.worker.lock:
                if self.active(task_id, turn_id, capability):
                    self.worker.update_task(task_id, verification=[*entries, entry])
                else:
                    return {"status": "unavailable", "message": "Verification capability expired"}
            # The agent gets request labels; exact details stay with the Ingress review.
            return public_check(entry)
        finally:
            self.execution_lock.release()

    def entity(self, payload):
        """Read an entity's state from Core, compare it with any expected_state, and keep only the named attributes."""
        entity_id = payload.get("entity_id", "")
        if not isinstance(entity_id, str) or not ENTITY_RE.fullmatch(entity_id):
            raise ValueError("Invalid entity_id")
        expected = payload.get("expected_state")
        if expected is not None and (not isinstance(expected, str) or len(expected) > 255):
            raise ValueError("expected_state must be a short string")
        # Only explicitly requested attributes are retained. No blanket state dump.
        keys = payload.get("attributes", [])
        if not isinstance(keys, list) or len(keys) > 10 or any(not isinstance(k, str) or len(k) > 80 for k in keys):
            raise ValueError("Request at most ten named attributes")
        state = self.core("GET", f"states/{entity_id}")
        attributes = {key: state.get("attributes", {}).get(key) for key in keys}
        if len(json.dumps(attributes)) > 8000:
            raise ValueError("Requested attributes exceed the diagnostic limit")
        return {"status": "observed" if expected is None else "passed" if state.get("state") == expected else "failed",
                "entity_id": entity_id, "state": state.get("state"), "expected_state": expected,
                "attributes": attributes, "last_updated": state.get("last_updated"),
                "message": "Fresh entity state readback. This does not verify automation triggers, conditions, or actions."}

    def dashboard_readback(self, payload):
        """Read a dashboard's configuration from Core and compare it with expected_config when one is given."""
        path = payload.get("path", "/lovelace/0")
        if not isinstance(path, str) or not PATH_RE.fullmatch(path):
            raise ValueError("Use a local dashboard path such as /lovelace/0")
        dashboard = path.strip("/").split("/")[0]
        message = {"type": "lovelace/config"}
        if dashboard != "lovelace":
            message["url_path"] = dashboard
        actual = self.ws_read(message)
        expected = payload.get("expected_config")
        return {"status": "observed" if expected is None else "passed" if actual == expected else "failed",
                "path": path, "message": "Fresh dashboard configuration readback" +
                (" matches the saved configuration." if expected is not None and actual == expected else
                 " differs from the saved configuration." if expected is not None else "."),
                "view_count": len(actual.get("views", []))}

    def _confirm_saved(self, installation, path, saved_ids):
        """Stop a capture once a saved permission it loaded is revoked; other changes don't matter."""
        try:
            with self.permissions.lock:
                current = {rule["id"] for rule in self.permissions.rules(installation, path)}
                revision = self.permissions.revision
        except ValueError:
            current, revision = set(), None
        if not saved_ids <= current:
            raise ValueError("Browser permissions changed: a saved permission this inspection used was revoked. Run a new inspection.")
        return revision

    def browser(self, task_id, payload, turn_id=None, capability=None, *, retry=None):
        """Capture a dashboard within the configured budget and retain turn evidence.

        `retry` is the capture an Ingress user approved: its one-off grants
        apply, and its evidence is recorded without moving the chat.
        """
        grants = retry["grants"] if retry else []
        save_evidence = self.worker.update_task_evidence if retry else self.worker.update_task
        if turn_id is None:
            turn_id = self.worker.tasks.get(task_id, {}).get("current_turn_id")
        capability = capability or self.capabilities.get(task_id)
        options = self.worker.read_options()
        if not options.get("browser_verification", True):
            return {"status": "disabled", "message": "Dashboard browser verification is disabled"}
        memory_limit_mib = browser_memory_limit(options)
        path = payload.get("path", "")
        if not isinstance(path, str) or not PATH_RE.fullmatch(path):
            raise ValueError("Use a local dashboard path such as /lovelace/0")
        if payload.get("save_pending") is True:
            self.save_pending_dashboard(task_id, path)
        session = self.core("POST", "codex_cli/browser_session")
        try:
            url = urlparse(session["url"])
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("Home Assistant supplied an invalid browser address")
            installation = scope(session) if session.get("installation_id") else None
            if retry and installation != retry["scope"]:
                raise ValueError("The paired Home Assistant installation changed. Run a new inspection before approving requests.")
            # Saved permissions only widen access: if they can't be read, the
            # capture runs without them and says so.
            saved, store_error = [], None
            permission_revision = self.permissions.revision
            if installation:
                try:
                    with self.permissions.lock:
                        saved = self.permissions.rules(installation, path)
                        permission_revision = self.permissions.revision
                except ValueError as exc:
                    store_error = str(exc)
            saved_ids = {rule["id"] for rule in saved}
            rules = list({rule["id"]: rule for rule in [*saved, *grants]}.values())
            with tempfile.TemporaryDirectory(prefix="ha-verification-") as directory, tempfile.TemporaryFile(mode="w+t", dir=directory) as output_file:
                request = json.dumps({**session, "path": path, "directory": directory, "permissions": rules})
                if len(request.encode("utf-8")) > LIMITS["controller_input_bytes"]:
                    raise ValueError("Too many browser permissions apply to this dashboard. Revoke some in Settings > Browser permissions.")
                environment = {key: os.environ[key] for key in ("PATH", "NODE_PATH", "HA_BROWSER_EXECUTABLE", "SystemRoot") if key in os.environ}
                process = subprocess.Popen(
                    ["node", str(Path(__file__).with_name("browser.cjs"))],
                    stdin=subprocess.PIPE, stdout=output_file, stderr=subprocess.DEVNULL,
                    text=True, env=environment, start_new_session=os.name != "nt",
                )
                try:
                    process.stdin.write(request)
                    process.stdin.close()
                    # A temporary output file avoids a pipe deadlock on verbose diagnostics.
                    deadline = time.monotonic() + 110
                    tracked = {}
                    peak_memory = 0
                    while process.poll() is None:
                        if not self.active(task_id, turn_id, capability) or time.monotonic() >= deadline:
                            raise ValueError("Dashboard verification cancelled or timed out")
                        # Only revoking a saved permission this capture loaded stops it.
                        if saved_ids and self.permissions.revision != permission_revision:
                            permission_revision = self._confirm_saved(installation, path, saved_ids)
                        memory = track_browser_processes(process.pid, tracked)
                        peak_memory = max(peak_memory, memory)
                        if memory > memory_limit_mib * 1024 * 1024:
                            raise ValueError(f"Dashboard browser exceeded its {memory_limit_mib} MiB memory budget ({memory / 1024 ** 2:.0f} MiB measured). Adjust Browser memory limit in app configuration.")
                        time.sleep(0.1)
                    output_file.seek(0)
                    output = output_file.read(65537)
                    if len(output) > 65536:
                        raise ValueError("Browser output limit exceeded")
                    result = json.loads(output)
                    # Only complete, inspectable rules from the controller are
                    # offered. Redacted/truncated requests remain blocked, and
                    # are counted so the review says some were left out.
                    raw = result.pop("permission_candidates", [])
                    raw = raw if isinstance(raw, list) else []
                    omitted = result.get("permission_candidates_omitted")
                    omitted = (omitted if type(omitted) is int and omitted >= 0 else 0) + max(0, len(raw) - MAX_CANDIDATES)
                    candidates = []
                    if installation:
                        for candidate in raw[:MAX_CANDIDATES]:
                            rule = valid_rule(candidate)
                            if rule and self.worker.redact(rule["request"]) == rule["request"]:
                                candidates.append({**rule, **describe(rule)})
                            else:
                                omitted += 1
                    elif raw:
                        # An older integration can't identify the installation approvals bind to.
                        result["permission_review_unavailable"] = INTEGRATION_UPDATE_NOTE
                    result.update(permission_candidates=candidates, permission_scope=installation,
                                  permission_candidates_omitted=omitted)
                    if store_error:
                        result["permission_store_error"] = store_error
                    result.update(memory_peak_mib=round(peak_memory / 1024 ** 2, 1),
                                  memory_limit_mib=memory_limit_mib)
                finally:
                    # Playwright detaches Chromium into a separate process group.
                    # Let its signal handler close first, then reap tracked children
                    # even if Node crashed. Check start times to avoid PID reuse.
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    kill_tracked_processes(locals().get("tracked", {}))
                    process.wait(timeout=10)
                if not self.active(task_id, turn_id, capability):
                    raise ValueError("Dashboard verification cancelled")
                if saved_ids and self.permissions.revision != permission_revision:
                    self._confirm_saved(installation, path, saved_ids)
                images = []
                for shot in result.pop("screenshots", [])[:2]:
                    name = shot.get("name")
                    if name not in {"desktop", "mobile"}:
                        continue
                    source = Path(directory) / f"{name}.png"
                    if not source.is_file() or source.is_symlink() or source.stat().st_size > SCREENSHOT_MAX_BYTES:
                        continue
                    stored = self.worker._write_attachment_file(task_id, source.read_bytes(), turn_id)
                    if stored:
                        attachment_id, target, mime = stored
                        record = self.worker.attachment_record(task_id, attachment_id, target, mime,
                            name=f"dashboard-{name}.png", origin="verification", viewport=name,
                            expires_at=time.time() + RETENTION_SECONDS)
                        images.append(record)
                with self.worker.lock:
                    current = list(self.worker.tasks.get(task_id, {}).get("verification_attachments") or [])
                    if not self.active(task_id, turn_id, capability):
                        for item in images:
                            (self.worker.get_task_dir(task_id) / item["path"]).unlink(missing_ok=True)
                        raise ValueError("Dashboard verification cancelled")
                    save_evidence(task_id, verification_attachments=[*current, *images])
                # Local paths let the agent inspect screenshots with its image viewer.
                result.update(path=path, attachments=[item["attachment_id"] for item in images],
                              image_paths=[str(self.worker.get_task_dir(task_id) / item["path"]) for item in images])
                if result.get("status") == "captured" and len(images) != 2:
                    result.update(status="unavailable", message="Both viewport screenshots could not be retained")
                return result
        finally:
            try:
                self.core("DELETE", "codex_cli/browser_session", json={"session_id": session["session_id"]})
            except Exception:
                pass  # Core independently revokes the session after 180 seconds.

    def after_changes(self, task_id, lovelace_results):
        """Check each dashboard the worker saved for this turn: read its configuration back, then capture its views.

        Captures stop at the turn's dashboard limit. If the saved dashboard file cannot be read, a dashboard
        check with an empty path is run instead; it captures nothing and leaves an unavailable (or disabled)
        result on the turn.
        """
        for ref in lovelace_results:
            if not ref.get("success") or self.worker.task_cancellation_requested(task_id):
                continue
            try:
                storage = json.loads((self.worker.CONFIG_ROOT / ref["storage_file"]).read_text())
                config = storage["data"]["config"]
                base = "/" + (ref.get("url_path") or "lovelace")
                self.run(task_id, {"operation": "dashboard_readback", "path": base, "expected_config": config})
                with self.worker.lock:
                    checks = self.worker.tasks.get(task_id, {}).get("verification") or []
                    remaining = max(0, MAX_BROWSERS - sum(check.get("operation") == "dashboard" for check in checks))
                for index, view in enumerate(config.get("views", [])[:remaining]):
                    path = base + "/" + str(view.get("path", index))
                    self.run(task_id, {"operation": "dashboard", "path": path})
            except (OSError, ValueError, KeyError):
                self.run(task_id, {"operation": "dashboard", "path": ""})

    def save_pending_dashboard(self, task_id, path):
        """Save only a dashboard edited in this turn, before the AI views it."""
        if self.worker.read_options().get("codex_sandbox") == "read-only":
            raise ValueError("Dashboard saves are disabled in read-only mode")
        if not self.worker.read_options().get("auto_save_lovelace", True):
            raise ValueError("Dashboard auto-save is disabled; inspect the loaded dashboard without save_pending")
        manifest = self.worker.get_run_dir(task_id) / "manifest-before.json"
        if not manifest.is_file():
            raise ValueError("The turn has no baseline for a dashboard save")
        changes = self.worker.diff_manifests(json.loads(manifest.read_text()), self.worker.build_manifest())
        dashboard = path.strip("/").split("/")[0]
        refs = [ref for ref in self.worker.find_lovelace_dashboard_refs(changes)
                if (ref.get("url_path") or "lovelace") == dashboard]
        if not refs:
            return  # No pending edit for this dashboard; inspect current Core state.
        for ref in refs:
            errors = self.worker.validate_changed_files({"added": [], "changed": [ref["storage_file"]], "deleted": []})
            if errors:
                raise ValueError("The edited dashboard failed JSON validation")
            ok, _ = self.worker.save_lovelace_dashboard(ref)
            if not ok:
                raise ValueError("Home Assistant did not accept the pending dashboard")
            config = json.loads((self.worker.CONFIG_ROOT / ref["storage_file"]).read_text())["data"]["config"]
            check = self.dashboard_readback({"path": path, "expected_config": config})
            if check["status"] != "passed":
                raise ValueError("The pending dashboard was accepted but API readback differs")

    def cleanup(self):
        """Delete only expired verification images from known conversation metadata."""
        with self.worker.lock:
            tasks = list(self.worker.tasks.values())
        for task in tasks:
            root = self.worker.get_task_dir(task["task_id"]).resolve()
            for turn in self.worker.task_turns(task):
                for item in (turn.get("verification_attachments") or []):
                    if item.get("origin") != "verification" or item.get("expires_at", 0) > time.time():
                        continue
                    path = (root / item.get("path", "")).resolve()
                    if path.is_relative_to(root) and path.is_file():
                        path.unlink(missing_ok=True)

    def serve(self, path):
        """Start a Unix socket server at path that answers verification requests in background threads, and return it.

        Any existing socket file is replaced, and the new one is accessible to its owner only.
        """
        engine = self

        class Handler(socketserver.StreamRequestHandler):
            """Answer one newline-terminated JSON request per connection."""

            def handle(self):
                """Read one request of at most 16384 bytes, dispatch it and write the result as a JSON line.

                A request that is invalid, too large or refused gets a generic "unavailable" result.
                """
                self.request.settimeout(240)
                try:
                    raw = self.rfile.readline(16385)
                    if len(raw) > 16384:
                        raise ValueError("Request too large")
                    result = engine.dispatch(json.loads(raw))
                except (OSError, ValueError):
                    result = {"status": "unavailable", "message": "Invalid or expired verification request"}
                try:
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass  # A cancelled caller may have closed its pipe/socket.

        Path(path).unlink(missing_ok=True)
        server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        server.daemon_threads = True
        os.chmod(path, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def browser_memory_limit(options):
    """Keep old or malformed options bounded even outside Supervisor validation."""
    value = options.get("browser_memory_limit_mib", DEFAULT_BROWSER_MEMORY_LIMIT_MIB)
    return value if type(value) is int and 512 <= value <= 8192 else DEFAULT_BROWSER_MEMORY_LIMIT_MIB


def track_browser_processes(root_pid, tracked):
    """Track descendants and charge shared pages proportionally, not per process."""
    if os.name == "nt":
        return 0
    processes = {}
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            stat = (item / "stat").read_text().rsplit(")", 1)[1].split()
            processes[int(item.name)] = (int(stat[1]), stat[19], int(stat[21]))  # ppid, starttime, RSS pages
        except (OSError, ValueError, IndexError):
            continue
    known = {root_pid} | {pid for pid, start in tracked.items() if processes.get(pid, (0, None, 0))[1] == start}
    while True:
        children = {pid for pid, (parent, _, _) in processes.items() if parent in known}
        if children.issubset(known):
            break
        known.update(children)
    for pid in known:
        if pid in processes:
            tracked[pid] = processes[pid][1]
    return sum(browser_process_memory(pid, processes[pid][2]) for pid in known if pid in processes)


def browser_process_memory(pid, rss_pages):
    """Use Linux PSS plus proportional swap; retain RSS as a conservative fallback."""
    try:
        fields = {}
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"Pss", "SwapPss"}:
                amount, unit = value.split()
                if unit != "kB" or int(amount) < 0:
                    raise ValueError("Invalid memory accounting")
                fields[key] = int(amount) * 1024
        if "Pss" in fields:
            return fields["Pss"] + fields.get("SwapPss", 0)
    except (OSError, ValueError):
        pass
    return rss_pages * os.sysconf("SC_PAGE_SIZE")


def kill_tracked_processes(tracked):
    """Kill each tracked process whose start time still matches, so a reused PID is never signalled."""
    for pid, start in tracked.items():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if stat[19] == start:
                os.kill(pid, signal.SIGKILL)
        except (OSError, IndexError):
            pass
