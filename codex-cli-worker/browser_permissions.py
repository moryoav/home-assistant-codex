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

# The controller (browser_permissions.cjs) loads the same policy, so both sides
# agree on which requests can be approved and on every limit.
POLICY = json.loads(Path(__file__).with_name("browser_permission_policy.json").read_text(encoding="utf-8"))
LIMITS = POLICY["limits"]
MAX_RULES = LIMITS["saved_rules"]
MAX_CANDIDATES = LIMITS["candidates"]
MAX_RETRIES = LIMITS["retries"]
MAX_FILE_BYTES = 256 * 1024
# ASCII-only matching mirrors JavaScript: no Unicode case folding or spaces.
FLAGS = re.I | re.A
HEX = re.compile(r"[a-f0-9]{64}")
PATH = re.compile(POLICY["dashboard_path_pattern"])
TYPE = re.compile(POLICY["type_pattern"])
HTTP_PATH = re.compile(POLICY["http_path_pattern"])
STORE_ERROR = ("Saved browser permissions could not be read, so none are applied. "
               "Remove them in Settings > Browser permissions to start over.")


def _any(words):
    """Return a regular expression alternative matching any of the words literally."""
    return "|".join(re.escape(word) for word in words)


SENSITIVE = re.compile(_any(POLICY["sensitive_names"]), FLAGS)
CREDENTIAL = re.compile(
    f"bearer{POLICY['whitespace']}|(?:{_any(POLICY['credential_value_names'])}){POLICY['whitespace']}*[=:]", FLAGS)
READ = re.compile(f"(?:^|/)(?:{_any(POLICY['read_prefixes'])})(?:_|$)|/(?:{_any(POLICY['read_suffixes'])})$")
DENIED_TYPE = re.compile(f"^(?:{_any(POLICY['denied_websocket_prefixes'])})(?:/|$)")
WRITE_WORDS = frozenset(POLICY["write_words"])


def words(text):
    """Split a command or path into lowercase words at punctuation and camelCase."""
    return [word for word in re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).lower()) if word]


def writes(text):
    """Return whether any word of a command or path names a known write."""
    return any(word in WRITE_WORDS for word in words(text))


def dashboard_key(path):
    """Treat '/dash/view' and '/dash/view/' as the same dashboard view."""
    return path.rstrip("/") or "/"


def _entry_id(installation, path, rule_id):
    """Identify one saved exception by its scope, dashboard view and rule."""
    return hashlib.sha256(f"{installation}\n{path}\n{rule_id}".encode()).hexdigest()


def _inspectable(item, depth=0):
    """Reject credential-like fields and excessive nesting, without redacting them."""
    if depth > LIMITS["depth"]:
        return False
    if isinstance(item, str):
        return not CREDENTIAL.search(item)
    if isinstance(item, dict):
        return all(not SENSITIVE.search(key) and _inspectable(child, depth + 1) for key, child in item.items())
    if isinstance(item, list):
        return all(_inspectable(child, depth + 1) for child in item)
    if type(item) is int:
        return abs(item) <= 2 ** 53 - 1
    if type(item) is float:
        return math.isfinite(item) and (not item.is_integer() or abs(item) <= 2 ** 53 - 1)
    return item is None or type(item) is bool


def valid_rule(value):
    """Validate fingerprints and eligibility again before storing an exception."""
    if (not isinstance(value, dict) or not isinstance(value.get("kind"), str)
        or value["kind"] not in {"websocket", "http", "map_tiles"}):
        return None
    kind, snapshot, identifier = value["kind"], value.get("request"), value.get("id")
    if (not isinstance(snapshot, str) or len(snapshot.encode("utf-8")) > LIMITS["request_bytes"]
        or not isinstance(identifier, str)):
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
        if (not isinstance(name, str) or len(name) > LIMITS["type_length"] or "id" in request
            or not TYPE.fullmatch(name) or writes(name) or DENIED_TYPE.match(name)
            or name in POLICY["denied_websocket_types"] or SENSITIVE.search(name) or not READ.search(name)):
            return None
    else:
        path, query = request.get("path"), request.get("query")
        if (set(request) != {"method", "path", "query"} or not isinstance(request.get("method"), str)
            or request["method"] not in {"GET", "HEAD"}
            or not isinstance(path, str) or len(path) > LIMITS["path_length"] or not HTTP_PATH.fullmatch(path)
            or "//" in path or re.search(r"(?:^|/)\.{1,2}(?:/|$)", path) or writes(path) or SENSITIVE.search(path)
            or any(path == prefix or path.startswith(f"{prefix}/") for prefix in POLICY["denied_http_prefixes"])
            or not isinstance(query, list) or len(query) > LIMITS["query_pairs"]
            or any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(item, str) for item in pair)
                   or SENSITIVE.search(pair[0]) for pair in query)):
            return None
    if not _inspectable(request):
        return None
    return {"id": identifier, "kind": kind, "request": snapshot}


def describe(rule):
    """Return a readable label and the complete, already-validated request."""
    request = json.loads(rule["request"])
    if rule["kind"] == "map_tiles":
        return {"label": "Load raster map tiles", "description": "Allows the map-only token and validated raster tile images. The token stays outside page scripts."}
    return {"label": f"WebSocket {request['type']}" if rule["kind"] == "websocket" else f"{request['method']} {request['path']}",
            "description": rule["request"]}


def scope(session):
    """Bind exceptions to the Core installation identity and exact Core origin.

    The identity is the integration's browser user, which survives Home
    Assistant and worker restarts. The worker API token does not: the
    integration provisions a new one each time it starts.
    """
    identity = session.get("installation_id")
    url = urlparse(session.get("url", ""))
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
        return None
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        return None
    return hashlib.sha256(f"{identity}\n{url.scheme}://{url.netloc}".encode()).hexdigest()


class BrowserPermissionStore:
    """Persist owner-granted rules outside /config and serialize their mutations."""

    def __init__(self, worker):
        """Resolve private storage through the worker at each access, including in tests."""
        self.worker = worker
        self.lock = threading.RLock()
        self.revision = 0

    def _path(self):
        """Return the private permissions file, resolved at each access."""
        return self.worker.DATA_ROOT / "browser-permissions.json"

    def _read(self):
        """Return the stored entries; raise ValueError only when the file as a whole can't be trusted."""
        path = self._path()
        if not path.exists():
            return []
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("oversized")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise ValueError(STORE_ERROR) from exc
        if not isinstance(value, list):
            raise ValueError(STORE_ERROR)
        return value

    @staticmethod
    def _valid(entry):
        """Return whether one stored entry is still an eligible, untampered exception."""
        return bool(isinstance(entry, dict) and isinstance(entry.get("scope"), str) and HEX.fullmatch(entry["scope"])
                    and isinstance(entry.get("path"), str) and PATH.fullmatch(entry["path"])
                    and valid_rule(entry.get("rule")) is not None
                    and entry.get("id") == _entry_id(entry["scope"], entry["path"], entry["rule"]["id"]))

    def rules(self, installation, path):
        """Return valid rules for this installation and dashboard view; other entries are ignored, never fatal."""
        key = dashboard_key(path)
        with self.lock:
            return [entry["rule"] for entry in self._read() if self._valid(entry)
                    and entry["scope"] == installation and dashboard_key(entry["path"]) == key]

    def list(self):
        """Describe saved exceptions, including ignored ones the user can still revoke."""
        with self.lock:
            entries = self._read()
        listed = []
        for entry in entries:
            if self._valid(entry):
                listed.append({"id": entry["id"], "path": dashboard_key(entry["path"]), **describe(entry["rule"]),
                               "created_at": str(entry.get("created_at", ""))})
            elif isinstance(entry, dict) and isinstance(entry.get("id"), str) and len(entry["id"]) <= 128:
                path = entry.get("path")
                listed.append({"id": entry["id"], "path": path if isinstance(path, str) and PATH.fullmatch(path) else "",
                               "label": "Unsupported saved permission", "unsupported": True,
                               "description": "This saved permission is no longer supported, so it is ignored. Revoke it to free its slot.",
                               "created_at": str(entry.get("created_at", ""))})
        return listed

    def remember(self, installation, path, rules):
        """Atomically save a selected batch; malformed input never partially applies."""
        if not isinstance(installation, str) or not HEX.fullmatch(installation) or not isinstance(path, str) or not PATH.fullmatch(path):
            raise ValueError("Invalid browser permission")
        if any(valid_rule(rule) is None for rule in rules):
            raise ValueError("Invalid browser permission")
        key = dashboard_key(path)
        with self.lock:
            entries = self._read()
            known = {entry.get("id") for entry in entries if isinstance(entry, dict)}
            added = []
            for rule in rules:
                identifier = _entry_id(installation, key, rule["id"])
                if identifier not in known:
                    known.add(identifier)
                    added.append({"id": identifier, "scope": installation, "path": key,
                                  "rule": rule, "created_at": self.worker.utc_now()})
            if not added:
                return
            if len(entries) + len(added) > MAX_RULES:
                raise ValueError("Saved browser permission limit reached. Revoke unused permissions in Settings.")
            self._write([*entries, *added])

    def revoke(self, identifier):
        """Revoke one rule, valid or ignored; captures using a revoked rule stop."""
        with self.lock:
            entries = self._read()
            remaining = [entry for entry in entries if not (isinstance(entry, dict) and entry.get("id") == identifier)]
            if len(remaining) == len(entries):
                raise ValueError("Saved browser permission no longer exists")
            self._write(remaining)

    def clear(self):
        """Remove every saved exception, even when the file can't be read."""
        with self.lock:
            self._write([])

    def _write(self, entries):
        """Use atomic private storage and advance the running-capture generation."""
        path = self._path()
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
