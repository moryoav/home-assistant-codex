"""Bounded HA diagnostics and browser evidence, owned by the worker, not the model."""
from __future__ import annotations

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

MAX_CHECKS = 24
MAX_BROWSERS = 4
SCREENSHOT_MAX_BYTES = 5 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 3600
ENTITY_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
PATH_RE = re.compile(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)?/?")


class Verification:
    def __init__(self, worker):
        self.worker = worker
        self.capabilities = {}
        self.lock = threading.RLock()
        self.execution_lock = threading.Lock()

    def begin(self, task_id):
        self.cleanup()
        with self.lock:
            capability = secrets.token_urlsafe(32)
            self.capabilities[task_id] = capability
        self.worker.update_task(task_id, verification=[], verification_attachments=[])
        return capability

    def end(self, task_id):
        with self.lock:
            self.capabilities.pop(task_id, None)

    def dispatch(self, payload):
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
        with self.worker.lock:
            task = self.worker.tasks.get(task_id, {})
            return bool(capability and current == capability and task.get("status") == "running"
                        and task.get("current_turn_id") == turn_id
                        and not self.worker.task_cancellation_requested(task_id))

    def core(self, method, path, **kwargs):
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
            return entry
        finally:
            self.execution_lock.release()

    def entity(self, payload):
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

    def browser(self, task_id, payload, turn_id=None, capability=None):
        if turn_id is None:
            turn_id = self.worker.tasks.get(task_id, {}).get("current_turn_id")
        capability = capability or self.capabilities.get(task_id)
        if not self.worker.read_options().get("browser_verification", True):
            return {"status": "disabled", "message": "Dashboard browser verification is disabled"}
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
            with tempfile.TemporaryDirectory(prefix="ha-verification-") as directory, tempfile.TemporaryFile(mode="w+t", dir=directory) as output_file:
                environment = {key: os.environ[key] for key in ("PATH", "NODE_PATH", "HA_BROWSER_EXECUTABLE", "SystemRoot") if key in os.environ}
                process = subprocess.Popen(
                    ["node", str(Path(__file__).with_name("browser.cjs"))],
                    stdin=subprocess.PIPE, stdout=output_file, stderr=subprocess.DEVNULL,
                    text=True, env=environment, start_new_session=os.name != "nt",
                )
                try:
                    request = json.dumps({**session, "path": path, "directory": directory})
                    process.stdin.write(request)
                    process.stdin.close()
                    # A temporary output file avoids a pipe deadlock on verbose diagnostics.
                    deadline = time.monotonic() + 110
                    tracked = {}
                    while process.poll() is None:
                        if not self.active(task_id, turn_id, capability) or time.monotonic() >= deadline:
                            raise ValueError("Dashboard verification cancelled or timed out")
                        if track_browser_processes(process.pid, tracked) > 768 * 1024 * 1024:
                            raise ValueError("Dashboard browser exceeded its 768 MB memory budget")
                        time.sleep(0.1)
                    output_file.seek(0)
                    output = output_file.read(65537)
                    if len(output) > 65536:
                        raise ValueError("Browser output limit exceeded")
                    result = json.loads(output)
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
                    self.worker.update_task(task_id, verification_attachments=[*current, *images])
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
        engine = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
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


def track_browser_processes(root_pid, tracked):
    """Track descendants even when Chromium detaches from Node's process group."""
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
    return sum(processes[pid][2] for pid in known if pid in processes) * os.sysconf("SC_PAGE_SIZE")


def kill_tracked_processes(tracked):
    for pid, start in tracked.items():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if stat[19] == start:
                os.kill(pid, signal.SIGKILL)
        except (OSError, IndexError):
            pass
