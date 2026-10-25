"""Private, bounded storage for user-approved dashboard browser exceptions."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import tempfile
from pathlib import Path
from urllib.parse import urlparse

MAX_RULES = 128
MAX_CANDIDATES = 16
MAX_FILE_BYTES = 256 * 1024
HEX = re.compile(r"[a-f0-9]{64}")
PATH = re.compile(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)?/?")
SENSITIVE = re.compile(r"token|secret|password|credential|authorization|signature|api_?key|authsig", re.I)
WRITE = re.compile(r"(?:^|[_/])(?:set|save|create|update|delete|remove|add|install|download|execute|exec|run|call|fire|start|stop|restart|reload|reboot|register|connect|disconnect|press|trigger|toggle|turn|send|offer|candidate|publish|write)(?:$|[_/])", re.I)


def valid_rule(value):
    """Validate fingerprints and eligibility again before storing an exception."""
    if (not isinstance(value, dict) or not isinstance(value.get("kind"), str)
        or value["kind"] not in {"websocket", "http", "map_tiles"}):
        return None
    kind, snapshot, identifier = value["kind"], value.get("request"), value.get("id")
    if not isinstance(snapshot, str) or len(snapshot.encode("utf-8")) > 1200 or not isinstance(identifier, str):
        return None
    if hashlib.sha256(f"{kind}\n{snapshot}".encode()).hexdigest() != identifier:
        return None
    try:
        request = json.loads(snapshot)
    except (ValueError, RecursionError):
        return None
    if not isinstance(request, dict):
        return None
    if kind == "map_tiles":
        if request != {"type": "map_tiles/raster"}:
            return None
    elif kind == "websocket":
        name = request.get("type")
        if (not isinstance(name, str) or len(name) > 160 or "id" in request
            or not re.fullmatch(r"[a-z][a-z0-9_]*(?:/[a-z][a-z0-9_]*){0,5}", name)
            or WRITE.search(name) or SENSITIVE.search(name)
            or re.match(r"^(auth|config|hassio|cloud|stream|backup|person|automation|script)(?:/|$)", name)
            or re.match(r"^camera/(?:webrtc|stream)(?:/|$)", name)
            or not re.search(r"(?:^|/)(?:get|list|subscribe)(?:_|$)|/(?:config|info|capabilities)$", name)):
            return None
    else:
        path, query = request.get("path"), request.get("query")
        if (set(request) != {"method", "path", "query"} or not isinstance(request.get("method"), str)
            or request["method"] not in {"GET", "HEAD"}
            or not isinstance(path, str) or len(path) > 512 or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", path)
            or "//" in path or re.search(r"(?:^|/)\.{1,2}(?:/|$)", path) or WRITE.search(path) or SENSITIVE.search(path)
            or re.match(r"^/(?:auth|api/(?:auth|services|events|config|hassio|webhook|camera|codex_cli|stream|backup|map_tiles))(?:/|$)", path)
            or not isinstance(query, list) or len(query) > 12
            or any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(item, str) for item in pair)
                   or SENSITIVE.search(pair[0]) for pair in query)):
            return None

    def inspectable(item, depth=0):
        """Reject credential-like fields and excessive nesting, without redacting them."""
        if depth > 12:
            return False
        if isinstance(item, str):
            return not re.search(r"bearer\s|(?:token|password|secret|authsig)\s*[=:]", item, re.I)
        if isinstance(item, dict):
            return all(not SENSITIVE.search(key) and inspectable(child, depth + 1) for key, child in item.items())
        if isinstance(item, list):
            return all(inspectable(child, depth + 1) for child in item)
        if type(item) is int:
            return abs(item) <= 2 ** 53 - 1
        if type(item) is float:
            return math.isfinite(item) and (not item.is_integer() or abs(item) <= 2 ** 53 - 1)
        return item is None or type(item) is bool

    if not inspectable(request):
        return None
    return {"id": identifier, "kind": kind, "request": snapshot}


def describe(rule):
    """Return a readable label and the complete, already-validated request."""
    request = json.loads(rule["request"])
    if rule["kind"] == "map_tiles":
        return {"label": "Load raster map tiles", "description": "Allows the map-only token and validated raster tile images. The token stays outside page scripts."}
    return {"label": f"WebSocket {request['type']}" if rule["kind"] == "websocket" else f"{request['method']} {request['path']}",
            "description": rule["request"]}


def scope(session, worker_token):
    """Bind exceptions to the paired worker, Core identity, and exact Core origin."""
    identity = session.get("installation_id")
    url = urlparse(session.get("url", ""))
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
        return None
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        return None
    return hashlib.sha256(f"{worker_token}\n{identity}\n{url.scheme}://{url.netloc}".encode()).hexdigest()


class BrowserPermissionStore:
    """Persist owner-granted rules outside /config and serialize their mutations."""

    def __init__(self, worker):
        """Resolve private storage through the worker at each access, including in tests."""
        self.worker = worker
        self.lock = threading.RLock()
        self.revision = 0

    def _read(self):
        """Fail closed on corrupt or oversized files rather than replacing permissions."""
        path = self.worker.DATA_ROOT / "browser-permissions.json"
        if not path.exists():
            return []
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("oversized")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, list) or len(value) > MAX_RULES:
                raise ValueError("invalid")
            for entry in value:
                if (not isinstance(entry, dict) or not isinstance(entry.get("scope"), str)
                    or not HEX.fullmatch(entry["scope"]) or not isinstance(entry.get("path"), str)
                    or not PATH.fullmatch(entry["path"]) or valid_rule(entry.get("rule")) is None
                    or entry.get("id") != hashlib.sha256(f"{entry['scope']}\n{entry['path']}\n{entry['rule']['id']}".encode()).hexdigest()):
                    raise ValueError("invalid")
            return value
        except (OSError, ValueError, RecursionError) as exc:
            raise ValueError("Saved browser permissions could not be read. Restore or remove the private permissions file before retrying.") from exc

    def rules(self, installation, path):
        """Return rules for this installation and exact dashboard view only."""
        with self.lock:
            return [entry["rule"] for entry in self._read() if entry["scope"] == installation and entry["path"] == path]

    def list(self):
        """Describe all saved exceptions without exposing the installation fingerprint."""
        with self.lock:
            return [{"id": entry["id"], "path": entry["path"], **describe(entry["rule"]),
                     "created_at": entry.get("created_at", "")} for entry in self._read()]

    def remember(self, installation, path, rules):
        """Atomically save a selected batch; malformed input never partially applies."""
        with self.lock:
            entries = self._read()
            for rule in rules:
                if valid_rule(rule) is None or not HEX.fullmatch(installation) or not PATH.fullmatch(path):
                    raise ValueError("Invalid browser permission")
                identifier = hashlib.sha256(f"{installation}\n{path}\n{rule['id']}".encode()).hexdigest()
                if not any(entry["id"] == identifier for entry in entries):
                    entries.append({"id": identifier, "scope": installation, "path": path,
                                    "rule": rule, "created_at": self.worker.utc_now()})
            if len(entries) > MAX_RULES:
                raise ValueError("Saved browser permission limit reached. Revoke unused permissions in Settings.")
            self._write(entries)

    def revoke(self, identifier):
        """Revoke one rule, invalidating any capture that already loaded exceptions."""
        with self.lock:
            entries = self._read()
            if not any(entry["id"] == identifier for entry in entries):
                raise ValueError("Saved browser permission no longer exists")
            self._write([entry for entry in entries if entry["id"] != identifier])

    def _write(self, entries):
        """Use atomic private storage and advance the running-capture generation."""
        path = self.worker.DATA_ROOT / "browser-permissions.json"
        content = json.dumps(entries)
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("Saved browser permission storage limit reached. Revoke unused permissions in Settings.")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            # NamedTemporaryFile creates mode 0600 before any contents exist.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".browser-permissions-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            self.revision += 1
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
