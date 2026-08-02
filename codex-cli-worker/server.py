#!/usr/bin/env python3
"""HTTP worker that runs Codex CLI against /config for Home Assistant."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import select
import secrets
import signal
import sys
import subprocess
import tarfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
import websocket
import yaml
from flask import Flask, Response, jsonify, request


CONFIG_ROOT = Path("/config")
DATA_ROOT = Path("/data")
CODEX_HOME = DATA_ROOT / "codex-home"
OPTIONS_PATH = DATA_ROOT / "options.json"
WORKER_TOKEN_PATH = DATA_ROOT / "worker_api_token"
SCHEMA_PATH = DATA_ROOT / "codex-output-schema.json"
CODEX_CONFIG_PATH = CODEX_HOME / "config.toml"
CODEX_BINARY = "/usr/local/bin/codex"
AGENTS_PATH = CONFIG_ROOT / "AGENTS.md"
TASK_STATE_FILE = DATA_ROOT / "task_index.json"

DEFAULT_OPTIONS = {
    "codex_model": "gpt-5.3-codex",
    "model_reasoning_effort": "medium",
    "codex_sandbox": "workspace-write",
    "task_root": "/config/codex_tasks",
    "notify_service": "",
    "task_timeout_seconds": 3600,
    "auto_save_lovelace": True,
    "ha_url": "http://supervisor/core",
    "HA_TOKEN": "",
}
REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}

AGENTS_MAX_BYTES = 256 * 1024
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024
LOG_TAIL_BYTES = 300_000
AUTH_NOTIFY_ID = "codex_cli_login"
AUTH_QR_DIR = CONFIG_ROOT / "www" / "codex_cli_auth"
INGRESS_PROXY_IP = "172.30.32.2"
USAGE_REFRESH_INTERVAL_SECONDS = 300
USAGE_READY_TIMEOUT_SECONDS = 8
USAGE_STATUS_TIMEOUT_SECONDS = 25
USAGE_POST_TRUST_READY_SECONDS = 10
USAGE_COMMAND_SUBMIT_DELAY_SECONDS = 0.7
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
URL_RE = re.compile(r"https?://[^\s<>)\"']+")
DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[PX^_].*?\x1b\\|[@-Z\\-_])")
FIVE_HOUR_RE = re.compile(
    r"(?i)(?<!\w)(?:5\s*h(?:\s+limit)?|5\s*-\s*hour|five\s*-\s*hour|five\s+hour)"
    r"(?::)?(?:\s+\[[^\]]+\])?\s+(?P<percent>\d{1,3})%(?:\s+left)?"
    r"(?:\s+\(resets\s+(?P<reset>[^)]+)\))?"
)
WEEKLY_RE = re.compile(
    r"(?i)(?<!\w)weekly(?:\s+limit)?(?::)?(?:\s+\[[^\]]+\])?\s+"
    r"(?P<percent>\d{1,3})%(?:\s+left)?(?:\s+\(resets\s+(?P<reset>[^)]+)\))?"
)
CONTEXT_RE = re.compile(r"(?i)(?<!\w)context\s+(?P<percent>\d{1,3})%\s+left\b")
RESET_TIME_RE = re.compile(
    r"(?i)^\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?:\s+on\s+(?P<day>\d{1,2})\s+(?P<month>[a-z]{3,9})(?:\s+(?P<year>\d{4}))?)?\s*$"
)
MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

EXCLUDED_PARTS = {
    ".cache",
    "__pycache__",
    "ai_history",
    "audio",
    "codex_tasks",
    "deps",
    "downloads",
    "image",
    "log",
    "media",
    "model_cache",
    "tmp",
    "tts",
    "www",
}
EXCLUDED_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".fault",
    ".log",
    ".old",
    ".png",
    ".webp",
    ".jpg",
    ".jpeg",
    ".mp3",
    ".mp4",
    ".pickle",
    ".pkl",
    ".ttf",
}
SENSITIVE_REPLACEMENTS = [
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(api[_-]?key['\"\s:=]+)[A-Za-z0-9._~+/=-]{16,}"), r"\1[redacted]"),
    (re.compile(r"(?i)(token['\"\s:=]+)[A-Za-z0-9._~+/=-]{16,}"), r"\1[redacted]"),
]


app = Flask(__name__)
lock = threading.RLock()
auth_lock = threading.RLock()
tasks: dict[str, dict[str, Any]] = {}
running_processes: dict[str, subprocess.Popen] = {}
auth_state: dict[str, Any] = {}
auth_process: subprocess.Popen | None = None
event_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
usage_lock = threading.RLock()
usage_state: dict[str, Any] = {
    "status": "unavailable",
    "updated_at": "",
    "five_hour_limit": "",
    "five_hour_percent": "",
    "five_hour_reset": "",
    "five_hour_reset_at": "",
    "weekly_limit": "",
    "weekly_percent": "",
    "weekly_reset": "",
    "weekly_reset_at": "",
    "context_remaining": "",
    "context_percent": "",
    "raw_excerpt": "",
    "error": "",
    "_updated_monotonic": 0.0,
    "_refreshing": False,
}


@app.before_request
def enforce_ingress_boundary() -> Response | None:
    """Reject spoofed ingress requests and direct access to the web UI."""
    if request.headers.get("X-Ingress-Path"):
        remote_addr = request.remote_addr or ""
        if remote_addr != INGRESS_PROXY_IP:
            print(f"Rejected ingress request from unexpected source {remote_addr}", flush=True)
            return jsonify({"ok": False, "error": "forbidden"}), 403
        return None

    if request.endpoint == "index":
        return Response(
            "Open the Codex CLI Worker through Home Assistant Ingress.",
            status=403,
            mimetype="text/plain",
        )

    return None


class HassYamlLoader(yaml.SafeLoader):
    """YAML loader that accepts Home Assistant tags like !include."""


def _unknown_yaml(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


HassYamlLoader.add_multi_constructor("!", _unknown_yaml)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_options() -> dict[str, Any]:
    options = dict(DEFAULT_OPTIONS)
    if OPTIONS_PATH.exists():
        try:
            options.update(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"Could not read add-on options: {exc}", flush=True)
    return options


def task_root() -> Path:
    return Path(str(read_options().get("task_root") or DEFAULT_OPTIONS["task_root"]))


def model_reasoning_effort(options: dict[str, Any]) -> str:
    effort = str(options.get("model_reasoning_effort") or DEFAULT_OPTIONS["model_reasoning_effort"]).strip()
    if effort not in REASONING_EFFORTS:
        return DEFAULT_OPTIONS["model_reasoning_effort"]
    return effort


def codex_binary_path() -> str | None:
    """Return the fixed Codex executable path when it is usable."""
    path = Path(CODEX_BINARY)
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return CODEX_BINARY
    except OSError:
        pass
    return None


def api_token() -> str:
    if WORKER_TOKEN_PATH.exists():
        try:
            return WORKER_TOKEN_PATH.read_text(encoding="utf-8").strip()
        except Exception as exc:
            print(f"Could not read worker API token: {exc}", flush=True)
    return str(os.environ.get("CODEX_WORKER_TOKEN") or "")


def set_api_token(token: str) -> bool:
    """Store the worker API token in private app storage."""
    if len(token) < 32:
        return False
    WORKER_TOKEN_PATH.write_text(token, encoding="utf-8")
    WORKER_TOKEN_PATH.chmod(0o600)
    return True


def read_agents_file() -> str:
    if not AGENTS_PATH.exists():
        return ""
    if AGENTS_PATH.stat().st_size > AGENTS_MAX_BYTES:
        raise ValueError(f"{AGENTS_PATH} is larger than {AGENTS_MAX_BYTES} bytes")
    return AGENTS_PATH.read_text(encoding="utf-8")


def write_agents_file(content: str) -> None:
    encoded = content.encode("utf-8")
    if len(encoded) > AGENTS_MAX_BYTES:
        raise ValueError(f"AGENTS.md is larger than {AGENTS_MAX_BYTES} bytes")
    AGENTS_PATH.write_text(content.rstrip() + "\n", encoding="utf-8")


def ensure_runtime_files() -> None:
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    task_root().mkdir(parents=True, exist_ok=True)
    AUTH_QR_DIR.mkdir(parents=True, exist_ok=True)
    if not api_token():
        set_api_token(secrets.token_urlsafe(32))
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["completed", "needs_input", "failed"]},
            "summary": {"type": "string"},
            "question": {"type": "string"},
            "details": {"type": "string"},
        },
        "required": ["status", "summary", "question", "details"],
    }
    SCHEMA_PATH.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    CODEX_CONFIG_PATH.write_text(
        "\n".join(
            [
                "check_for_update_on_startup = false",
                'approval_policy = "never"',
                'sandbox_mode = "workspace-write"',
                'web_search = "cached"',
                "",
                "[profiles.ha_auto_review]",
                'approval_policy = "never"',
                'sandbox_mode = "workspace-write"',
                'web_search = "cached"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def save_task_index() -> None:
    with lock:
        slim = {
            task_id: {
                key: value
                for key, value in task.items()
                if key not in {"prompt", "reply_history"}
            }
            for task_id, task in tasks.items()
        }
    TASK_STATE_FILE.write_text(json.dumps(slim, indent=2), encoding="utf-8")


def load_task_index() -> None:
    root = task_root()
    if root.exists():
        for task_file in root.glob("*/task.json"):
            try:
                task = json.loads(task_file.read_text(encoding="utf-8"))
                task_id = str(task.get("task_id") or task_file.parent.name)
                task["task_id"] = task_id
                if task.get("status") in {"queued", "running"}:
                    task["status"] = "failed"
                    task["summary"] = "Worker restarted while this task was active."
                tasks[task_id] = task
            except Exception as exc:
                print(f"Could not load task metadata from {task_file}: {exc}", flush=True)
    if not TASK_STATE_FILE.exists():
        return
    try:
        loaded = json.loads(TASK_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for task_id, task in loaded.items():
                tasks.setdefault(task_id, task)
    except Exception as exc:
        print(f"Could not load task index: {exc}", flush=True)


def redact(text: str) -> str:
    redacted = text
    for pattern, replacement in SENSITIVE_REPLACEMENTS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def clean_cli_text(text: str) -> str:
    """Remove ANSI escapes and normalize Codex CLI output."""
    return ANSI_RE.sub("", text).replace("\r", "\n")


def compact_cli_text(text: str) -> str:
    """Return CLI output normalized for prompt detection across TUI layout noise."""
    return re.sub(r"[^a-z0-9]+", "", clean_cli_text(text).casefold())


def parse_codex_reset_at(reset_text: str, now: datetime | None = None) -> str:
    """Normalize Codex reset text into a local ISO timestamp when the format is known."""
    if not reset_text:
        return ""
    match = RESET_TIME_RE.match(reset_text)
    if not match:
        return ""
    current = now or datetime.now().astimezone()
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    day_text = match.group("day")
    month_text = (match.group("month") or "").casefold()
    year_text = match.group("year")
    try:
        if day_text and month_text:
            month = MONTHS.get(month_text)
            if not month:
                return ""
            year = int(year_text) if year_text else current.year
            parsed = datetime(
                year,
                month,
                int(day_text),
                hour,
                minute,
                tzinfo=current.tzinfo,
            )
            if not year_text and parsed < current - timedelta(minutes=1):
                parsed = parsed.replace(year=year + 1)
        else:
            parsed = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if parsed < current - timedelta(minutes=1):
                parsed += timedelta(days=1)
    except ValueError:
        return ""
    return parsed.isoformat()


def usage_status_payload() -> dict[str, Any]:
    with usage_lock:
        return {k: v for k, v in usage_state.items() if not k.startswith("_")}


def _update_usage_state(**updates: Any) -> None:
    with usage_lock:
        usage_state.update(updates)
        usage_state["updated_at"] = utc_now()
        usage_state["_updated_monotonic"] = time.monotonic()


def _read_pty(master_fd: int, timeout_seconds: float) -> str:
    chunks: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        wait_for = max(0.0, min(0.25, deadline - time.monotonic()))
        readable, _, _ = select.select([master_fd], [], [], wait_for)
        if not readable:
            continue
        try:
            data = os.read(master_fd, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data.decode("utf-8", errors="replace"))
    return "".join(chunks)


def _parse_usage_output(text: str) -> dict[str, str]:
    cleaned = clean_cli_text(text)
    lines = [" ".join(line.strip().split()) for line in cleaned.splitlines() if line.strip()]
    five_hour = ""
    five_hour_percent = ""
    five_hour_reset = ""
    weekly = ""
    weekly_percent = ""
    weekly_reset = ""
    context = ""
    context_percent = ""
    now = datetime.now().astimezone()
    for line in lines:
        if matches := list(FIVE_HOUR_RE.finditer(line)):
            match = matches[-1]
            five_hour_percent = match.group("percent")
            if reset := match.group("reset"):
                five_hour_reset = reset
            five_hour = f"5h {five_hour_percent}%"
        if matches := list(WEEKLY_RE.finditer(line)):
            match = matches[-1]
            weekly_percent = match.group("percent")
            if reset := match.group("reset"):
                weekly_reset = reset
            weekly = f"weekly {weekly_percent}%"
        if matches := list(CONTEXT_RE.finditer(line)):
            match = matches[-1]
            context_percent = match.group("percent")
            context = f"Context {context_percent}% left"
    return {
        "five_hour_limit": five_hour,
        "five_hour_percent": five_hour_percent,
        "five_hour_reset": five_hour_reset,
        "five_hour_reset_at": parse_codex_reset_at(five_hour_reset, now),
        "weekly_limit": weekly,
        "weekly_percent": weekly_percent,
        "weekly_reset": weekly_reset,
        "weekly_reset_at": parse_codex_reset_at(weekly_reset, now),
        "context_remaining": context,
        "context_percent": context_percent,
        "raw_excerpt": "\n".join(lines[-25:]),
    }


def _has_rich_status_panel(text: str) -> bool:
    compacted = compact_cli_text(text)
    return (
        "chatgptcomcodexsettingsusage" in compacted
        or ("5hlimit" in compacted and "weeklylimit" in compacted)
        or ("account" in compacted and "session" in compacted and "model" in compacted)
    )


def _capture_status_from_tui(master_fd: int) -> str:
    captured = _read_pty(master_fd, USAGE_READY_TIMEOUT_SECONDS)

    # First-run Codex can pause on the trust-directory screen before accepting slash commands.
    if "doyoutrustthecontentsofthisdirectory" in compact_cli_text(captured):
        os.write(master_fd, b"1\r")
        captured += _read_pty(master_fd, USAGE_POST_TRUST_READY_SECONDS)

    os.write(master_fd, b"/status")
    time.sleep(USAGE_COMMAND_SUBMIT_DELAY_SECONDS)
    os.write(master_fd, b"\r")
    deadline = time.monotonic() + USAGE_STATUS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        captured += _read_pty(master_fd, 1.0)
        if _has_rich_status_panel(captured):
            break
    return captured


def fetch_codex_usage_status() -> dict[str, Any]:
    if active_task_id():
        return {
            "status": "deferred",
            "error": "A Codex task is running; usage refresh is deferred until it finishes.",
            "five_hour_limit": usage_status_payload().get("five_hour_limit", ""),
            "five_hour_percent": usage_status_payload().get("five_hour_percent", ""),
            "five_hour_reset": usage_status_payload().get("five_hour_reset", ""),
            "five_hour_reset_at": usage_status_payload().get("five_hour_reset_at", ""),
            "weekly_limit": usage_status_payload().get("weekly_limit", ""),
            "weekly_percent": usage_status_payload().get("weekly_percent", ""),
            "weekly_reset": usage_status_payload().get("weekly_reset", ""),
            "weekly_reset_at": usage_status_payload().get("weekly_reset_at", ""),
            "context_remaining": usage_status_payload().get("context_remaining", ""),
            "context_percent": usage_status_payload().get("context_percent", ""),
            "raw_excerpt": usage_status_payload().get("raw_excerpt", ""),
        }
    codex = codex_binary_path()
    if not codex:
        return {
            "status": "unavailable",
            "error": "codex binary not found",
            "five_hour_limit": "",
            "five_hour_percent": "",
            "five_hour_reset": "",
            "five_hour_reset_at": "",
            "weekly_limit": "",
            "weekly_percent": "",
            "weekly_reset": "",
            "weekly_reset_at": "",
            "context_remaining": "",
            "context_percent": "",
            "raw_excerpt": "",
        }
    login = codex_login_status()
    if not login.get("status_ok"):
        return {
            "status": "unavailable",
            "error": "Not logged in",
            "five_hour_limit": "",
            "five_hour_percent": "",
            "five_hour_reset": "",
            "five_hour_reset_at": "",
            "weekly_limit": "",
            "weekly_percent": "",
            "weekly_reset": "",
            "weekly_reset_at": "",
            "context_remaining": "",
            "context_percent": "",
            "raw_excerpt": "",
        }
    try:
        import pty
        import fcntl
        import struct
        import termios
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": f"pty support unavailable: {exc}",
            "five_hour_limit": "",
            "five_hour_percent": "",
            "five_hour_reset": "",
            "five_hour_reset_at": "",
            "weekly_limit": "",
            "weekly_percent": "",
            "weekly_reset": "",
            "weekly_reset_at": "",
            "context_remaining": "",
            "context_percent": "",
            "raw_excerpt": "",
        }

    master_fd, slave_fd = pty.openpty()
    proc: subprocess.Popen[bytes] | None = None
    try:
        env = codex_env()
        env.setdefault("TERM", "xterm-256color")
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        status_line_config = (
            'tui.status_line=["model-with-reasoning","context-remaining","five-hour-limit","weekly-limit"]'
        )
        proc = subprocess.Popen(
            [
                codex,
                "--no-alt-screen",
                "--config",
                "check_for_update_on_startup=false",
                "--config",
                status_line_config,
            ],
            cwd="/config",
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        status_text = _capture_status_from_tui(master_fd)
        try:
            os.write(master_fd, b"/quit\r")
        except OSError:
            pass
        parsed = _parse_usage_output(status_text)
        if not parsed["five_hour_limit"] and not parsed["weekly_limit"]:
            return {
                "status": "error",
                "error": "Codex usage limits were not visible in /status output yet",
                **parsed,
            }
        return {"status": "ok", "error": "", **parsed}
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "five_hour_limit": "",
            "five_hour_percent": "",
            "five_hour_reset": "",
            "five_hour_reset_at": "",
            "weekly_limit": "",
            "weekly_percent": "",
            "weekly_reset": "",
            "weekly_reset_at": "",
            "context_remaining": "",
            "context_percent": "",
            "raw_excerpt": "",
        }
    finally:
        try:
            os.close(master_fd)
        except Exception:
            pass
        if slave_fd >= 0:
            try:
                os.close(slave_fd)
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()


def _refresh_usage_worker(force: bool = False) -> None:
    with usage_lock:
        if usage_state.get("_refreshing"):
            return
        if not force:
            last = float(usage_state.get("_updated_monotonic") or 0.0)
            if last and (time.monotonic() - last) < USAGE_REFRESH_INTERVAL_SECONDS:
                return
        usage_state["_refreshing"] = True

    try:
        result = fetch_codex_usage_status()
        _update_usage_state(**result)
    finally:
        with usage_lock:
            usage_state["_refreshing"] = False


def refresh_usage_status_async(force: bool = False) -> None:
    thread = threading.Thread(target=_refresh_usage_worker, args=(force,), daemon=True)
    thread.start()


def write_task_log(task_id: str, stream: str, text: str) -> None:
    task_dir = get_task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "codex.log"
    line = f"[{utc_now()}] {stream}: {redact(text.rstrip())}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def get_task_dir(task_id: str) -> Path:
    return task_root() / task_id


def update_task(task_id: str, **updates: Any) -> None:
    with lock:
        task = tasks.setdefault(task_id, {"task_id": task_id})
        task.update(updates)
        task["updated_at"] = utc_now()
        task_dir = get_task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")
    save_task_index()


def require_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if is_ingress_request():
            return func(*args, **kwargs)
        expected = api_token()
        if not expected:
            return jsonify({"ok": False, "error": "api_token is not configured in add-on options"}), 503
        supplied = request.headers.get("X-Codex-Worker-Token", "")
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not hmac.compare_digest(supplied, expected):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return func(*args, **kwargs)

    return wrapper


def is_ingress_request() -> bool:
    """Return true for requests proxied through Home Assistant Ingress."""
    if not request.headers.get("X-Ingress-Path"):
        return False
    remote_addr = request.remote_addr or ""
    return remote_addr == INGRESS_PROXY_IP


def handle_stdin_message(message: dict[str, Any]) -> None:
    """Handle Supervisor-managed control messages from Home Assistant."""
    command = str(message.get("command") or "")
    if command == "set_api_token":
        token = str(message.get("token") or "")
        if set_api_token(token):
            print("Updated worker API token from Home Assistant.", flush=True)
        else:
            print("Rejected invalid worker API token from Home Assistant.", flush=True)


def stdin_reader() -> None:
    """Read Supervisor app_stdin messages."""
    for line in sys.stdin:
        try:
            payload = json.loads(line)
        except ValueError:
            print("Ignored non-JSON stdin message.", flush=True)
            continue
        if isinstance(payload, dict):
            handle_stdin_message(payload)


def active_task_id() -> str | None:
    with lock:
        for task_id, task in tasks.items():
            if task.get("status") in {"queued", "running"}:
                return task_id
    return None


def active_task_count() -> int:
    with lock:
        return sum(1 for task in tasks.values() if task.get("status") in {"queued", "running"})


def should_include_file(path: Path) -> bool:
    try:
        rel = path.relative_to(CONFIG_ROOT)
    except ValueError:
        return False
    if not path.is_file():
        return False
    parts = set(rel.parts)
    if parts & EXCLUDED_PARTS:
        return False
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in EXCLUDED_SUFFIXES:
        return False
    if name.startswith("home-assistant_v2.db"):
        return False
    try:
        if path.stat().st_size > SNAPSHOT_MAX_BYTES:
            return False
    except OSError:
        return False
    return True


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_manifest() -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for path in CONFIG_ROOT.rglob("*"):
        if not should_include_file(path):
            continue
        rel = path.relative_to(CONFIG_ROOT).as_posix()
        try:
            stat = path.stat()
            manifest[rel] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": file_hash(path),
            }
        except OSError:
            continue
    return manifest


def diff_manifests(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    before_keys = set(before)
    after_keys = set(after)
    changed = [
        rel
        for rel in sorted(before_keys & after_keys)
        if before[rel].get("sha256") != after[rel].get("sha256")
    ]
    return {
        "added": sorted(after_keys - before_keys),
        "changed": changed,
        "deleted": sorted(before_keys - after_keys),
    }


def create_snapshot(task_id: str) -> dict[str, Any]:
    task_dir = get_task_dir(task_id)
    snapshot_path = task_dir / "snapshot-before.tar.gz"
    manifest = build_manifest()
    with tarfile.open(snapshot_path, "w:gz") as tar:
        for rel in sorted(manifest):
            path = CONFIG_ROOT / rel
            if path.exists():
                tar.add(path, arcname=rel, recursive=False)
    (task_dir / "manifest-before.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"path": str(snapshot_path), "file_count": len(manifest), "created_at": utc_now()}


def validate_changed_files(changes: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    for rel in sorted(set(changes.get("added", []) + changes.get("changed", []))):
        path = CONFIG_ROOT / rel
        if not path.exists() or not path.is_file():
            continue
        try:
            if rel.startswith(".storage/") or path.suffix.lower() == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            elif path.suffix.lower() in {".yaml", ".yml"}:
                yaml.load(path.read_text(encoding="utf-8"), Loader=HassYamlLoader)
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            errors.append(f"{rel}: {exc}")
    return errors


def find_lovelace_dashboard_refs(changes: dict[str, list[str]]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    changed_paths = sorted(set(changes.get("added", []) + changes.get("changed", [])))
    registry = read_lovelace_registry()
    for rel in changed_paths:
        if not rel.startswith(".storage/lovelace."):
            continue
        if rel in {".storage/lovelace_resources", ".storage/lovelace_dashboards"}:
            continue
        path = CONFIG_ROOT / rel
        try:
            storage = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(storage.get("data", {}).get("config", {}).get("views"), list):
                continue
        except Exception:
            continue
        dashboard_id = Path(rel).name.removeprefix("lovelace.")
        url_path = registry.get(dashboard_id, {}).get("url_path", "")
        refs.append({"dashboard_id": dashboard_id, "url_path": url_path, "storage_file": rel})
    return refs


def read_lovelace_registry() -> dict[str, dict[str, Any]]:
    registry_path = CONFIG_ROOT / ".storage" / "lovelace_dashboards"
    if not registry_path.exists():
        return {}
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        items = registry.get("data", {}).get("items", [])
        return {item.get("id"): item for item in items if item.get("id")}
    except Exception:
        return {}


def ha_token() -> str:
    return str(os.environ.get("SUPERVISOR_TOKEN") or "")


def ha_token_source() -> str:
    if os.environ.get("SUPERVISOR_TOKEN"):
        return "supervisor"
    return "none"


def create_persistent_notification(title: str, message: str, notification_id: str) -> None:
    ok, detail = call_ha_service(
        "persistent_notification.create",
        {"title": title, "message": message, "notification_id": notification_id},
    )
    if not ok:
        print(f"Persistent notification failed: {detail}", flush=True)


def dismiss_persistent_notification(notification_id: str) -> None:
    call_ha_service("persistent_notification.dismiss", {"notification_id": notification_id})


def ha_base_url() -> str:
    if ha_token_source() == "supervisor":
        return "http://supervisor/core"
    return str(read_options().get("ha_url") or DEFAULT_OPTIONS["ha_url"]).rstrip("/")


def ha_api_url(path: str) -> str:
    base = ha_base_url().rstrip("/")
    if path.startswith("/"):
        path = path[1:]
    if base.endswith("/api"):
        return f"{base}/{path.removeprefix('api/')}"
    return f"{base}/api/{path.removeprefix('api/')}"


def ha_ws_url() -> str:
    if ha_token_source() == "supervisor":
        return "ws://supervisor/core/api/websocket"
    parsed = urlparse(ha_api_url("websocket"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse(parsed._replace(scheme=scheme))


def call_ha_service(service: str, data: dict[str, Any]) -> tuple[bool, str]:
    token = ha_token()
    if not token:
        return False, "No Home Assistant token available"
    if "." not in service:
        return False, f"Invalid service name: {service}"
    domain, name = service.split(".", 1)
    url = ha_api_url(f"services/{domain}/{name}")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    attempts = [url]
    if ha_token_source() == "supervisor" and not url.startswith("http://supervisor/core/api/"):
        attempts.insert(0, f"http://supervisor/core/api/services/{domain}/{name}")

    errors: list[str] = []
    for attempt_url in attempts:
        try:
            response = requests.post(
                attempt_url,
                headers=headers,
                json=data,
                timeout=15,
            )
            if response.status_code < 400:
                return True, "ok"
            errors.append(f"{attempt_url}: HTTP {response.status_code}: {response.text[:300]}")
        except Exception as exc:
            errors.append(f"{attempt_url}: {exc}")
    return False, "; ".join(errors)


def fire_ha_event(event_type: str, data: dict[str, Any]) -> tuple[bool, str]:
    token = ha_token()
    if not token:
        return False, "No Home Assistant token available"
    url = ha_api_url(f"events/{event_type}")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    attempts = [url]
    if ha_token_source() == "supervisor" and not url.startswith("http://supervisor/core/api/"):
        attempts.insert(0, f"http://supervisor/core/api/events/{event_type}")

    errors: list[str] = []
    for attempt_url in attempts:
        try:
            response = requests.post(attempt_url, headers=headers, json=data, timeout=15)
            if response.status_code < 400:
                return True, "ok"
            errors.append(f"{attempt_url}: HTTP {response.status_code}: {response.text[:300]}")
        except Exception as exc:
            errors.append(f"{attempt_url}: {exc}")
    return False, "; ".join(errors)


def notify(title: str, message: str) -> None:
    service = str(read_options().get("notify_service") or "")
    if service:
        ok, detail = call_ha_service(service, {"title": title, "message": message})
        if ok:
            return
        print(f"Notification through {service} failed: {detail}", flush=True)
    call_ha_service(
        "persistent_notification.create",
        {"title": title, "message": message, "notification_id": "codex_cli_worker"},
    )


def save_lovelace_dashboard(ref: dict[str, str]) -> tuple[bool, str]:
    token = ha_token()
    if not token:
        return False, "No Home Assistant token available"
    storage_path = CONFIG_ROOT / ref["storage_file"]
    storage = json.loads(storage_path.read_text(encoding="utf-8"))
    config = storage["data"]["config"]
    payload: dict[str, Any] = {"config": config}
    if ref.get("url_path"):
        payload["url_path"] = ref["url_path"]

    ws = None
    try:
        ws = websocket.create_connection(
            ha_ws_url(),
            timeout=20,
            header=[f"Authorization: Bearer {token}"],
        )
        first = json.loads(ws.recv())
        if first.get("type") == "auth_required":
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(ws.recv())
            if auth.get("type") != "auth_ok":
                return False, f"WebSocket auth failed: {auth}"
        msg_id = 1
        ws.send(json.dumps({"id": msg_id, "type": "lovelace/config/save", **payload}))
        while True:
            response = json.loads(ws.recv())
            if response.get("id") != msg_id:
                continue
            if response.get("success"):
                return True, "saved"
            return False, json.dumps(response.get("error", response))[:500]
    except Exception as exc:
        return False, str(exc)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def codex_login_status() -> dict[str, Any]:
    auth_file = CODEX_HOME / "auth.json"
    result = {"has_auth_file": auth_file.exists(), "status_ok": False, "message": ""}
    codex = codex_binary_path()
    if not codex:
        result["message"] = "codex binary not found"
        return result
    try:
        proc = subprocess.run(
            [codex, "login", "status"],
            cwd="/config",
            env=codex_env(),
            text=True,
            capture_output=True,
            timeout=20,
        )
        result["status_ok"] = proc.returncode == 0
        result["message"] = redact((proc.stdout or proc.stderr or "").strip())
    except Exception as exc:
        result["message"] = str(exc)
    return result


def auth_status_payload() -> dict[str, Any]:
    with auth_lock:
        state = dict(auth_state)
        proc = auth_process
        if proc is not None and proc.poll() is None:
            state["process_running"] = True
        else:
            state["process_running"] = False
    if state.get("output"):
        parsed = parse_login_output(str(state["output"]))
        if parsed["url"] and not state.get("verification_url"):
            state["verification_url"] = parsed["url"]
        if parsed["code"] and not state.get("user_code"):
            state["user_code"] = parsed["code"]
    state["codex_login"] = codex_login_status()
    return state


def parse_login_output(text: str) -> dict[str, str]:
    cleaned = clean_cli_text(text)
    urls = [url.rstrip(".,;") for url in URL_RE.findall(cleaned)]
    codes = DEVICE_CODE_RE.findall(cleaned)
    return {
        "url": urls[0] if urls else "",
        "code": codes[0] if codes else "",
    }


def write_login_qr(login_id: str, url: str) -> str:
    from qrcode import QRCode
    from qrcode.image.svg import SvgImage

    AUTH_QR_DIR.mkdir(parents=True, exist_ok=True)
    qr = QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgImage)
    filename = f"codex_login_{login_id}.svg"
    path = AUTH_QR_DIR / filename
    image.save(str(path))
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        namespace = root.tag.removesuffix("svg")
        rect = ET.Element(f"{namespace}rect", {"width": "100%", "height": "100%", "fill": "#fff"})
        root.insert(0, rect)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    except Exception as exc:
        print(f"Could not add QR background: {exc}", flush=True)
    return f"/local/codex_cli_auth/{filename}"


def update_auth_state(**updates: Any) -> None:
    with auth_lock:
        auth_state.update(updates)
        auth_state["updated_at"] = utc_now()


def notify_codex_login_ready(login_id: str, url: str, code: str) -> str:
    qr_url = write_login_qr(login_id, url)
    code_line = (
        f"\n\nOpenAI will ask for this code:\n\n**`{code}`**"
        if code
        else "\n\nWaiting for Codex CLI to print the one-time code. This notification will update automatically."
    )
    message = (
        "Scan the QR code below, or open the sign-in link, to authorize Codex CLI "
        "for the Home Assistant worker. The QR code opens the device page; type the code from this notification into that page.\n\n"
        f"![Codex login QR]({qr_url})\n\n"
        f"[Open sign-in page]({url})"
        f"{code_line}\n\n"
        "After you approve it, the worker will detect completion automatically."
    )
    title = "Codex CLI sign-in code" if code else "Codex CLI sign-in"
    create_persistent_notification(title, message, AUTH_NOTIFY_ID)
    return qr_url


def auth_reader_thread(handle) -> None:
    buffer = ""
    notified = False
    notified_code = ""
    notified_url = ""
    for line in iter(handle.readline, ""):
        if not line:
            break
        cleaned = clean_cli_text(line)
        buffer += cleaned
        write_task_log("auth", "codex-login", cleaned)
        parsed = parse_login_output(buffer)
        if parsed["url"] and (not notified or (parsed["code"] and parsed["code"] != notified_code)):
            with auth_lock:
                login_id = str(auth_state.get("login_id") or uuid.uuid4().hex[:8])
            qr_url = notify_codex_login_ready(login_id, parsed["url"], parsed["code"])
            update_auth_state(
                status="waiting_for_user",
                login_id=login_id,
                verification_url=parsed["url"],
                user_code=parsed["code"],
                qr_url=qr_url,
                output=redact(buffer)[-4000:],
            )
            notified = True
            notified_url = parsed["url"]
            notified_code = parsed["code"]
        else:
            updates: dict[str, Any] = {"output": redact(buffer)[-4000:]}
            if parsed["url"] and parsed["url"] != notified_url:
                updates["verification_url"] = parsed["url"]
            if parsed["code"] and parsed["code"] != notified_code:
                updates["user_code"] = parsed["code"]
            update_auth_state(**updates)


def run_codex_device_login(login_id: str) -> None:
    global auth_process
    codex = CODEX_BINARY
    update_auth_state(
        status="starting",
        login_id=login_id,
        started_at=utc_now(),
        verification_url="",
        user_code="",
        qr_url="",
        output="",
        error="",
    )
    try:
        proc = subprocess.Popen(
            [codex, "login", "--device-auth"],
            cwd="/config",
            env=codex_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        update_auth_state(status="failed", error=str(exc), completed_at=utc_now())
        notify("Codex sign-in failed", str(exc))
        return

    with auth_lock:
        auth_process = proc

    assert proc.stdout is not None
    reader = threading.Thread(target=auth_reader_thread, args=(proc.stdout,), daemon=True)
    reader.start()
    returncode = proc.wait()
    reader.join(timeout=5)
    with auth_lock:
        auth_process = None

    if returncode == 0 and codex_login_status().get("status_ok"):
        update_auth_state(status="completed", completed_at=utc_now(), returncode=returncode)
        dismiss_persistent_notification(AUTH_NOTIFY_ID)
        notify("Codex sign-in complete", "Codex CLI is now authenticated for the Home Assistant worker.")
        refresh_usage_status_async(force=True)
    else:
        update_auth_state(status="failed", completed_at=utc_now(), returncode=returncode)
        notify("Codex sign-in failed", f"Device login exited with code {returncode}.")


def start_codex_login_flow(force: bool = False) -> dict[str, Any]:
    global auth_process
    current = codex_login_status()
    if current.get("status_ok") and not force:
        update_auth_state(status="already_logged_in")
        return auth_status_payload()
    with auth_lock:
        if auth_process is not None and auth_process.poll() is None:
            return auth_status_payload()
        login_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        auth_state.update(
            {
                "status": "queued",
                "login_id": login_id,
                "updated_at": utc_now(),
                "verification_url": "",
                "user_code": "",
                "qr_url": "",
                "output": "",
                "error": "",
            }
        )
    thread = threading.Thread(target=run_codex_device_login, args=(login_id,), daemon=True)
    thread.start()
    return auth_status_payload()


def logout_codex() -> dict[str, Any]:
    global auth_process
    if active_task_id():
        return {"ok": False, "error": "cannot log out while a Codex task is running", "status": auth_status_payload()}
    proc_to_stop: subprocess.Popen[str] | None = None
    with auth_lock:
        if auth_process is not None and auth_process.poll() is None:
            proc_to_stop = auth_process
            auth_process = None
    if proc_to_stop is not None:
        proc_to_stop.terminate()
        try:
            proc_to_stop.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc_to_stop.kill()
    codex = CODEX_BINARY
    try:
        proc = subprocess.run(
            [codex, "logout"],
            cwd="/config",
            env=codex_env(),
            text=True,
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:
        update_auth_state(status="logout_failed", error=str(exc), completed_at=utc_now())
        return {"ok": False, "error": str(exc), "status": auth_status_payload()}
    message = redact((proc.stdout or proc.stderr or "").strip())
    login = codex_login_status()
    if proc.returncode == 0 or not login.get("status_ok"):
        dismiss_persistent_notification(AUTH_NOTIFY_ID)
        update_auth_state(
            status="logged_out",
            completed_at=utc_now(),
            returncode=proc.returncode,
            message=message,
            verification_url="",
            user_code="",
            qr_url="",
            output="",
            error="",
        )
        notify("Codex signed out", "Codex CLI credentials were removed from the Home Assistant worker.")
        refresh_usage_status_async(force=True)
        return {"ok": True, "message": message, "status": auth_status_payload()}
    update_auth_state(status="logout_failed", completed_at=utc_now(), returncode=proc.returncode, error=message)
    return {"ok": False, "error": message or f"codex logout exited with code {proc.returncode}", "status": auth_status_payload()}


def auto_start_login_if_needed() -> None:
    status = codex_login_status()
    if status.get("status_ok"):
        update_auth_state(status="authenticated", message=status.get("message", ""))
        refresh_usage_status_async(force=False)
        return
    print("Codex is not authenticated; starting device-code login flow.", flush=True)
    start_codex_login_flow(False)


def codex_env() -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(CODEX_HOME)
    env["HOME"] = str(DATA_ROOT)
    ha_token = str(read_options().get("HA_TOKEN") or "").strip()
    if ha_token:
        env["HA_TOKEN"] = ha_token
    return env


def extract_session_id(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key in ("session_id", "thread_id", "conversation_id", "rollout_id"):
            value = obj.get(key)
            if isinstance(value, str) and UUID_RE.fullmatch(value):
                return value
        for value in obj.values():
            found = extract_session_id(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = extract_session_id(value)
            if found:
                return found
    elif isinstance(obj, str):
        match = UUID_RE.search(obj)
        if match:
            return match.group(0)
    return None


def build_prompt(user_prompt: str, task_id: str, reply: str | None = None) -> str:
    reply_text = ""
    if reply:
        reply_text = f"\nUser reply for this resumed task:\n{reply}\n"
    return f"""You are Codex running as a Home Assistant add-on worker.

Workspace: /config
Task id: {task_id}

Follow /config/AGENTS.md. Treat this as a live Home Assistant config tree. Make focused edits, do not expose secrets, and validate changed YAML/JSON when practical.

This is a non-interactive run. Do not wait for terminal input. If you need the user's decision or verification before continuing, stop cleanly by returning status "needs_input" with one concise question.

At the end, return only an object matching the provided JSON schema:
- status: "completed", "needs_input", or "failed"
- summary: concise result
- question: use an empty string unless status is "needs_input"
- details: use an empty string unless there are useful implementation/test notes
{reply_text}
User request:
{user_prompt}
"""


def build_codex_args(task_id: str, prompt_file: Path, final_file: Path, session_id: str | None) -> list[str]:
    options = read_options()
    args = [
        CODEX_BINARY,
        "exec",
        "--cd",
        "/config",
        "--skip-git-repo-check",
        "--sandbox",
        str(options.get("codex_sandbox") or "workspace-write"),
        "--json",
        "--output-schema",
        str(SCHEMA_PATH),
        "--output-last-message",
        str(final_file),
    ]
    model = str(options.get("codex_model") or "").strip()
    if model == "default":
        model = ""
    if model:
        args.extend(["--model", model])
    args.extend(["--config", f'model_reasoning_effort="{model_reasoning_effort(options)}"'])
    if session_id:
        args.extend(["resume", session_id, "-"])
    else:
        args.append("-")
    return args


def reader_thread(task_id: str, stream_name: str, handle, session_holder: dict[str, str]) -> None:
    for line in iter(handle.readline, ""):
        if not line:
            break
        write_task_log(task_id, stream_name, line)
        if stream_name == "stdout":
            try:
                event = json.loads(line)
                found = extract_session_id(event)
                if found:
                    session_holder["session_id"] = found
                    update_task(task_id, session_id=found)
            except Exception:
                match = UUID_RE.search(line)
                if match:
                    session_holder["session_id"] = match.group(0)
                    update_task(task_id, session_id=match.group(0))


def parse_final(final_file: Path, returncode: int) -> dict[str, Any]:
    if not final_file.exists():
        log_tail = ""
        log_file = final_file.parent / "codex.log"
        if log_file.exists():
            log_tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:].strip()
        return {
            "status": "failed" if returncode else "completed",
            "summary": f"Codex exited before writing the final response file (returncode={returncode}).",
            "details": log_tail or f"returncode={returncode}",
        }
    raw = final_file.read_text(encoding="utf-8", errors="replace").strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {
        "status": "failed" if returncode else "completed",
        "summary": raw[:1000] or "Codex completed without a structured final response.",
        "details": f"returncode={returncode}",
    }


def fail_task_launch(task_id: str, exc: OSError) -> None:
    """Record and publish a clean task failure when Codex cannot start."""
    completed_at = utc_now()
    details = redact(f"Could not start {CODEX_BINARY}: {exc}")
    summary = "Could not start the Codex CLI executable."
    write_task_log(task_id, "worker", details)
    update_task(
        task_id,
        status="failed",
        completed_at=completed_at,
        returncode=None,
        summary=summary,
        question="",
        details=details,
        error=details,
        changes={"added": [], "changed": [], "deleted": []},
        validation_errors=[],
        lovelace_results=[],
    )
    event_data = {
        "task_id": task_id,
        "status": "failed",
        "codex_status": "failed",
        "summary": summary,
        "question": "",
        "details": details,
        "returncode": None,
        "session_id": "",
        "completed_at": completed_at,
        "changes": {"added": [], "changed": [], "deleted": []},
        "validation_errors": [],
        "lovelace_results": [],
        "response": {
            "status": "failed",
            "summary": summary,
            "question": "",
            "details": details,
        },
    }
    ok, detail = fire_ha_event("codex_cli_task_result", event_data)
    if not ok:
        print(f"Codex task result event failed: {detail}", flush=True)
    notify("Codex task failed", f"{summary} Task: {task_id}")
    refresh_usage_status_async(force=True)


def run_task(task_id: str, prompt: str, session_id: str | None = None, reply: str | None = None) -> None:
    task_dir = get_task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    final_file = task_dir / ("final-resume.json" if reply else "final.json")
    prompt_file = task_dir / ("prompt-resume.txt" if reply else "prompt.txt")
    before_manifest_path = task_dir / "manifest-before.json"

    update_task(task_id, status="running", started_at=utc_now(), error="")
    if not before_manifest_path.exists():
        try:
            snapshot = create_snapshot(task_id)
            update_task(task_id, snapshot=snapshot)
        except Exception as exc:
            write_task_log(task_id, "worker", f"Snapshot failed: {exc}")
            update_task(task_id, snapshot_error=str(exc))

    prompt_file.write_text(build_prompt(prompt, task_id, reply=reply), encoding="utf-8")
    args = build_codex_args(task_id, prompt_file, final_file, session_id)
    write_task_log(task_id, "worker", "Starting Codex: " + " ".join(args))

    timeout = int(read_options().get("task_timeout_seconds") or 3600)
    session_holder: dict[str, str] = {}
    try:
        proc = subprocess.Popen(
            args,
            cwd="/config",
            env=codex_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        fail_task_launch(task_id, exc)
        return
    with lock:
        running_processes[task_id] = proc
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    proc.stdin.write(prompt_file.read_text(encoding="utf-8"))
    proc.stdin.close()

    stdout_thread = threading.Thread(
        target=reader_thread,
        args=(task_id, "stdout", proc.stdout, session_holder),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=reader_thread,
        args=(task_id, "stderr", proc.stderr, session_holder),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        returncode = proc.returncode if proc.returncode is not None else -1
    finally:
        with lock:
            running_processes.pop(task_id, None)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    final = parse_final(final_file, returncode)
    after_manifest = build_manifest()
    (task_dir / "manifest-after.json").write_text(json.dumps(after_manifest, indent=2), encoding="utf-8")
    try:
        before_manifest = json.loads(before_manifest_path.read_text(encoding="utf-8"))
    except Exception:
        before_manifest = {}
    changes = diff_manifests(before_manifest, after_manifest)
    (task_dir / "changes.json").write_text(json.dumps(changes, indent=2), encoding="utf-8")
    validation_errors = validate_changed_files(changes)

    lovelace_results: list[dict[str, Any]] = []
    if read_options().get("auto_save_lovelace"):
        for ref in find_lovelace_dashboard_refs(changes):
            ok, detail = save_lovelace_dashboard(ref)
            lovelace_results.append({**ref, "success": ok, "message": detail})

    if timed_out:
        final = {
            "status": "failed",
            "summary": f"Codex timed out after {timeout} seconds.",
            "details": final.get("summary", ""),
        }
    elif returncode != 0 and final.get("status") == "completed":
        final["status"] = "failed"
        final["details"] = f"Codex exited with {returncode}. {final.get('details', '')}".strip()

    status = str(final.get("status") or "failed")
    if validation_errors and status == "completed":
        status = "failed"
        final["status"] = "failed"
        final["details"] = "Validation errors: " + "; ".join(validation_errors[:5])

    task_status = "waiting_for_input" if status == "needs_input" else status
    completed_at = utc_now() if status != "needs_input" else ""
    session_id = session_holder.get("session_id") or tasks.get(task_id, {}).get("session_id", "")
    update_task(
        task_id,
        status=task_status,
        completed_at=completed_at,
        returncode=returncode,
        session_id=session_id,
        summary=final.get("summary", ""),
        question=final.get("question", ""),
        details=final.get("details", ""),
        changes=changes,
        validation_errors=validation_errors,
        lovelace_results=lovelace_results,
    )

    event_data = {
        "task_id": task_id,
        "status": task_status,
        "codex_status": status,
        "summary": final.get("summary", ""),
        "question": final.get("question", ""),
        "details": final.get("details", ""),
        "returncode": returncode,
        "session_id": session_id,
        "completed_at": completed_at,
        "changes": changes,
        "validation_errors": validation_errors,
        "lovelace_results": lovelace_results,
        "response": {
            "status": status,
            "summary": final.get("summary", ""),
            "question": final.get("question", ""),
            "details": final.get("details", ""),
        },
    }
    ok, detail = fire_ha_event("codex_cli_task_result", event_data)
    if not ok:
        print(f"Codex task result event failed: {detail}", flush=True)

    if status == "needs_input":
        notify("Codex needs input", f"{final.get('question', 'Codex needs your input.')} Task: {task_id}")
    elif status == "completed":
        notify("Codex task completed", f"{final.get('summary', 'Done')} Task: {task_id}")
    else:
        notify("Codex task failed", f"{final.get('summary', 'Failed')} Task: {task_id}")
    refresh_usage_status_async(force=True)


def start_background_task(task_id: str, prompt: str, session_id: str | None = None, reply: str | None = None) -> None:
    thread = threading.Thread(target=run_task, args=(task_id, prompt, session_id, reply), daemon=True)
    thread.start()


@app.get("/")
def index() -> Response:
    token_configured = bool(api_token())
    status = codex_login_status()
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex CLI Worker</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #18212f; background: #f6f8fb; }}
    main {{ max-width: 860px; margin: auto; }}
    section {{ background: white; border: 1px solid #d6dde8; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1rem; }}
    code, pre {{ background: #eef2f7; border-radius: 4px; padding: .15rem .3rem; }}
    input, textarea {{ width: 100%; box-sizing: border-box; margin: .35rem 0 .75rem; padding: .55rem; }}
    button {{ padding: .55rem .9rem; border: 0; border-radius: 6px; background: #0b63ce; color: white; cursor: pointer; }}
  </style>
</head>
<body>
<main>
  <h1>Codex CLI Worker</h1>
  <section>
    <p>Worker API authentication: <strong>{"enabled" if token_configured else "not ready"}</strong></p>
    <p>Codex auth file: <strong>{str(status.get("has_auth_file")).lower()}</strong></p>
    <p>Codex login status: <strong>{str(status.get("status_ok")).lower()}</strong></p>
    <p><code>{status.get("message", "")}</code></p>
  </section>
  <section>
    <h2>ChatGPT login</h2>
    <p>Start the device-code login from here or with the <code>codex_cli.start_login</code> Home Assistant service. The worker will send a persistent notification with a QR code and sign-in link.</p>
    <button onclick="startLogin()">Start login</button>
    <button onclick="logoutCodex()" style="background:#5c6675">Log out</button>
    <pre id="login-result"></pre>
    <div id="login-card"></div>
  </section>
  <section>
    <h2>AGENTS.md</h2>
    <textarea id="agents-md" rows="14" spellcheck="false"></textarea>
    <button onclick="loadAgents()">Load</button>
    <button onclick="saveAgents()" style="background:#188038">Save</button>
    <pre id="agents-result"></pre>
  </section>
  <section>
    <h2>Start a task</h2>
    <label>Prompt</label>
    <textarea id="prompt" rows="8"></textarea>
    <button onclick="startTask()">Start</button>
    <pre id="result"></pre>
  </section>
</main>
<script>
function workerUrl(path) {{
  return path.replace(/^\\//, '');
}}
function workerHeaders(includeJson = true) {{
  const headers = {{}};
  if (includeJson) headers['Content-Type'] = 'application/json';
  return headers;
}}
async function renderJsonResponse(result, res) {{
  const text = await res.text();
  try {{
    result.textContent = JSON.stringify(JSON.parse(text), null, 2);
  }} catch (err) {{
    result.textContent = text || ('HTTP ' + res.status);
  }}
}}
function renderLoginCard(auth) {{
  const card = document.getElementById('login-card');
  if (!card || !auth) return;
  const qr = auth.qr_url ? '<p><img src="' + auth.qr_url + '" alt="Codex login QR" style="max-width:260px;background:white;padding:12px;border:1px solid #d6dde8;border-radius:8px"></p>' : '';
  const link = auth.verification_url ? '<p><a href="' + auth.verification_url + '" target="_blank" rel="noreferrer">Open sign-in page</a></p>' : '';
  const code = auth.user_code ? '<p>Device code: <code>' + auth.user_code + '</code></p>' : '';
  card.innerHTML = '<p>Status: <strong>' + (auth.status || 'unknown') + '</strong></p>' + qr + link + code;
}}
async function refreshLoginStatus() {{
  const result = document.getElementById('login-result');
  try {{
    const res = await fetch(workerUrl('/auth/status'), {{
      method: 'GET',
      headers: workerHeaders(false)
    }});
    const data = await res.json();
    renderLoginCard(data.auth);
    if (data.auth && ['waiting_for_user', 'starting', 'queued'].includes(data.auth.status)) {{
      window.setTimeout(refreshLoginStatus, 3000);
    }}
  }} catch (err) {{
    if (result) result.textContent = String(err);
  }}
}}
async function startTask() {{
  const prompt = document.getElementById('prompt').value;
  const result = document.getElementById('result');
  const res = await fetch(workerUrl('/tasks'), {{
    method: 'POST',
    headers: workerHeaders(),
    body: JSON.stringify({{ prompt }})
  }});
  await renderJsonResponse(result, res);
}}
async function loadAgents() {{
  const result = document.getElementById('agents-result');
  result.textContent = 'Loading...';
  const res = await fetch(workerUrl('/agents'), {{
    method: 'GET',
    headers: workerHeaders(false)
  }});
  const text = await res.text();
  try {{
    const data = JSON.parse(text);
    if (!res.ok || !data.ok) {{
      result.textContent = JSON.stringify(data, null, 2);
      return;
    }}
    document.getElementById('agents-md').value = data.content || '';
    result.textContent = data.exists ? 'Loaded /config/AGENTS.md' : 'AGENTS.md does not exist yet.';
  }} catch (err) {{
    result.textContent = text || ('HTTP ' + res.status);
  }}
}}
async function saveAgents() {{
  const result = document.getElementById('agents-result');
  const content = document.getElementById('agents-md').value;
  result.textContent = 'Saving...';
  const res = await fetch(workerUrl('/agents'), {{
    method: 'POST',
    headers: workerHeaders(),
    body: JSON.stringify({{ content }})
  }});
  await renderJsonResponse(result, res);
}}
async function startLogin() {{
  const result = document.getElementById('login-result');
  result.textContent = 'Starting login...';
  const res = await fetch(workerUrl('/auth/start'), {{
    method: 'POST',
    headers: workerHeaders(),
    body: JSON.stringify({{}})
  }});
  await renderJsonResponse(result, res);
  refreshLoginStatus();
}}
async function logoutCodex() {{
  const result = document.getElementById('login-result');
  result.textContent = 'Logging out...';
  const res = await fetch(workerUrl('/auth/logout'), {{
    method: 'POST',
    headers: workerHeaders(),
    body: JSON.stringify({{}})
  }});
  await renderJsonResponse(result, res);
  refreshLoginStatus();
}}
refreshLoginStatus();
loadAgents();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/health")
@require_auth
def health() -> Response:
    return jsonify(
        {
            "ok": True,
            "api_token_configured": bool(api_token()),
            "codex_binary": codex_binary_path() or "",
            "codex_login": codex_login_status(),
            "auth_flow": auth_status_payload(),
            "task_root": str(task_root()),
        }
    )


@app.get("/status")
@require_auth
def status() -> Response:
    refresh_usage_status_async(force=False)
    with lock:
        task_values = list(tasks.values())
    return jsonify(
        {
            "ok": True,
            "active_task_id": active_task_id(),
            "active_task_count": active_task_count(),
            "task_count": active_task_count(),
            "total_task_count": len(task_values),
            "latest_task": task_values[-1] if task_values else None,
            "codex_login": codex_login_status(),
            "auth_flow": auth_status_payload(),
            "codex_usage": usage_status_payload(),
        }
    )


@app.get("/agents")
@require_auth
def get_agents() -> Response:
    try:
        content = read_agents_file()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "path": str(AGENTS_PATH)}), 500
    return jsonify(
        {
            "ok": True,
            "path": str(AGENTS_PATH),
            "exists": AGENTS_PATH.exists(),
            "content": content,
        }
    )


@app.post("/agents")
@require_auth
def save_agents() -> Response:
    payload = request.get_json(silent=True) or {}
    if "content" not in payload:
        return jsonify({"ok": False, "error": "content is required"}), 400
    try:
        write_agents_file(str(payload.get("content") or ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "path": str(AGENTS_PATH)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "path": str(AGENTS_PATH)}), 500
    return jsonify({"ok": True, "path": str(AGENTS_PATH), "bytes": AGENTS_PATH.stat().st_size})


@app.post("/auth/start")
@require_auth
def start_auth() -> Response:
    payload = request.get_json(silent=True) or {}
    return jsonify({"ok": True, "auth": start_codex_login_flow(bool(payload.get("force")))})


@app.get("/auth/status")
@require_auth
def get_auth_status() -> Response:
    return jsonify({"ok": True, "auth": auth_status_payload()})


@app.post("/auth/logout")
@require_auth
def logout_auth() -> Response:
    result = logout_codex()
    status_code = 200 if result.get("ok") else 409 if active_task_id() else 500
    return jsonify(result), status_code


@app.get("/tasks")
@require_auth
def list_tasks() -> Response:
    with lock:
        ordered = sorted(tasks.values(), key=lambda item: item.get("created_at", ""))
    return jsonify({"ok": True, "tasks": ordered})


@app.post("/tasks")
@require_auth
def create_task() -> Response:
    active = active_task_id()
    if active:
        return jsonify({"ok": False, "error": "another task is already running", "active_task_id": active}), 409
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400
    task_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    title = str(payload.get("title") or prompt[:80])
    update_task(
        task_id,
        status="queued",
        title=title,
        prompt=prompt,
        created_at=utc_now(),
        summary="",
        question="",
        details="",
    )
    (get_task_dir(task_id) / "user-prompt.txt").write_text(prompt, encoding="utf-8")
    start_background_task(task_id, prompt)
    return jsonify({"ok": True, "task_id": task_id, "status": "queued"})


@app.get("/tasks/<task_id>")
@require_auth
def get_task(task_id: str) -> Response:
    with lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "task not found"}), 404
    return jsonify({"ok": True, "task": task})


@app.get("/tasks/<task_id>/log")
@require_auth
def get_log(task_id: str) -> Response:
    path = get_task_dir(task_id) / "codex.log"
    if not path.exists():
        return Response("", mimetype="text/plain")
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - LOG_TAIL_BYTES), os.SEEK_SET)
        data = handle.read().decode("utf-8", errors="replace")
    return Response(data, mimetype="text/plain")


@app.post("/tasks/<task_id>/cancel")
@require_auth
def cancel_task(task_id: str) -> Response:
    with lock:
        proc = running_processes.get(task_id)
        task = tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "task not found"}), 404
    if proc and proc.poll() is None:
        proc.terminate()
        update_task(task_id, status="cancelled", completed_at=utc_now(), summary="Task cancelled")
        return jsonify({"ok": True, "task_id": task_id, "status": "cancelled"})
    update_task(task_id, status="cancelled", completed_at=utc_now(), summary="Task cancelled")
    return jsonify({"ok": True, "task_id": task_id, "status": "cancelled"})


@app.post("/tasks/<task_id>/reply")
@require_auth
def reply_task(task_id: str) -> Response:
    active = active_task_id()
    if active:
        return jsonify({"ok": False, "error": "another task is already running", "active_task_id": active}), 409
    payload = request.get_json(silent=True) or {}
    reply = str(payload.get("reply") or "").strip()
    if not reply:
        return jsonify({"ok": False, "error": "reply is required"}), 400
    with lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "task not found"}), 404
    session_id = str(task.get("session_id") or "")
    if not session_id:
        return jsonify({"ok": False, "error": "task has no Codex session id to resume"}), 409
    if task.get("status") != "waiting_for_input":
        return jsonify({"ok": False, "error": "task is not waiting for input"}), 409
    reply_history = list(task.get("reply_history") or [])
    reply_history.append({"at": utc_now(), "reply": reply})
    update_task(task_id, status="queued", reply_history=reply_history)
    start_background_task(task_id, str(task.get("prompt") or ""), session_id=session_id, reply=reply)
    return jsonify({"ok": True, "task_id": task_id, "status": "queued"})


def main() -> None:
    ensure_runtime_files()
    load_task_index()
    auto_start_login_if_needed()
    threading.Thread(target=stdin_reader, daemon=True).start()
    app.run(host="0.0.0.0", port=9123)


if __name__ == "__main__":
    main()
