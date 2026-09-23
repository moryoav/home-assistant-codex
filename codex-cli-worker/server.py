#!/usr/bin/env python3
"""HTTP worker that runs Codex CLI against /config for Home Assistant."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import hmac
import json
import os
import queue
import re
import select
import secrets
import shutil
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
from flask import Flask, Response, jsonify, request, send_file


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
    "codex_model": "default",
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
# Supported choices in the bundled CLI 0.154.0 model catalog. Availability still
# depends on the signed-in account; the CLI reports unavailable models normally.
CHAT_MODELS = (
    ("gpt-6-astra", "GPT-6 Astra", ("low", "medium", "high", "xhigh", "max", "ultra")),
    ("gpt-5.6-sol", "GPT-5.6 Sol", ("low", "medium", "high", "xhigh", "max", "ultra")),
    ("gpt-5.6-terra", "GPT-5.6 Terra", ("low", "medium", "high", "xhigh", "max", "ultra")),
    ("gpt-5.6-luna", "GPT-5.6 Luna", ("low", "medium", "high", "xhigh", "max")),
    ("gpt-5.5", "GPT-5.5", ("low", "medium", "high", "xhigh")),
)
CHAT_MODEL_EFFORTS = {model: efforts for model, _, efforts in CHAT_MODELS}
DEFAULT_CHAT_SETTINGS = {"model": None, "reasoning_effort": None}

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
RUNTIME_PROBE_TIMEOUT_SECONDS = 5
# The Codex execution probe starts Node, the native CLI, and several Bubblewrap
# processes. A timeout blocks every task, so allow for slow or busy hardware.
CODEX_SANDBOX_PROBE_TIMEOUT_SECONDS = 20
DIAGNOSTIC_ERROR_MAX_CHARS = 1000
CANCELLABLE_TASK_STATUSES = frozenset({"queued", "running"})
CANCELLED_TASK_SUMMARY = "Task cancelled"
# Built-in Codex image generation writes files under CODEX_HOME and records
# each result in the session rollout; `codex exec --json` does not report it.
IMAGE_GENERATION_KIND = "image_gen.generation"
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
ATTACHMENTS_PER_TURN_MAX = 12
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ATTACHMENT_ID_RE = re.compile(r"[0-9a-f]{32}")
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
URL_RE = re.compile(r"https?://[^\s<>)\"']+")
DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
SENSITIVE_USAGE_LABEL_RE = re.compile(
    r"(?i)\b(?:account|e-?mail|session(?:\s+id)?|organization|workspace(?:\s+id)?)\s*:"
)
USAGE_EXCERPT_LINE_RE = re.compile(
    r"(?i)(?:5\s*h|five\s*-?\s*hour|weekly|context(?:\s+(?:window|remaining))?|"
    r"model(?:\s+with\s+reasoning)?|limit|reset)"
)
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


WEB_ROOT = Path(__file__).resolve().parent / "web"
app = Flask(__name__, static_folder=str(WEB_ROOT / "assets"), static_url_path="/assets")
lock = threading.RLock()
auth_lock = threading.RLock()
tasks: dict[str, dict[str, Any]] = {}
running_processes: dict[str, subprocess.Popen] = {}
active_task_runners: set[str] = set()
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


def resolve_chat_settings(settings: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Resolve inheritance once per turn, without changing shared options."""
    model = settings.get("model") or str(options.get("codex_model") or "default")
    if model == "gpt-5.3-codex":
        model = "default"
    efforts = CHAT_MODEL_EFFORTS.get(model, ("low", "medium", "high", "xhigh"))
    effort = settings.get("reasoning_effort")
    if effort is not None and effort not in efforts:
        raise ValueError("The selected reasoning level is not supported by this model.")
    if effort is None:
        effort = model_reasoning_effort(options)
        # A legacy global setting may not suit an explicitly selected model.
        if model in CHAT_MODEL_EFFORTS and effort not in efforts:
            effort = "medium"
    return {"model": model, "reasoning_effort": effort}


def parse_chat_settings(payload: dict[str, Any], task: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = payload.get("chat_settings", (task or {}).get("chat_settings", DEFAULT_CHAT_SETTINGS))
    if not isinstance(settings, dict) or set(settings) - set(DEFAULT_CHAT_SETTINGS):
        raise ValueError("chat_settings must contain only model and reasoning_effort.")
    settings = {**DEFAULT_CHAT_SETTINGS, **settings}
    model, effort = settings["model"], settings["reasoning_effort"]
    if model is not None and (not isinstance(model, str) or model not in CHAT_MODEL_EFFORTS):
        raise ValueError("Select a supported model or use the add-on default.")
    if effort is not None and (not isinstance(effort, str) or effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}):
        raise ValueError("Select a supported reasoning level or use the add-on default.")
    resolve_chat_settings(settings, read_options())
    return settings


def codex_binary_path() -> str | None:
    """Return the fixed Codex executable path when it is usable."""
    path = Path(CODEX_BINARY)
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return CODEX_BINARY
    except OSError:
        pass
    return None


def _diagnostic_error(value: Any) -> str:
    """Return a short, redacted single-line diagnostic message."""
    text = " ".join(str(value or "").strip().split())
    return redact(text)[:DIAGNOSTIC_ERROR_MAX_CHARS]


def codex_version_status() -> dict[str, str]:
    """Return the pinned Codex version without using authentication or quota."""
    codex = codex_binary_path()
    if not codex:
        return {"version": "", "error": "Codex CLI executable is unavailable."}
    try:
        result = subprocess.run(
            [codex, "--version"],
            capture_output=True,
            text=True,
            timeout=RUNTIME_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"version": "", "error": "Codex version probe timed out."}
    except OSError as exc:
        return {"version": "", "error": _diagnostic_error(exc)}

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0 and output:
        return {"version": output.splitlines()[0].strip(), "error": ""}
    detail = output or f"Codex version probe exited with {result.returncode}."
    return {"version": "", "error": _diagnostic_error(detail)}


def _codex_sandbox_probe(mode: str) -> dict[str, Any]:
    """Execute a harmless command through the configured Codex sandbox."""
    if mode not in {"read-only", "workspace-write"}:
        return {"ok": False, "error": "Unsupported sandbox mode for the execution probe."}
    codex = codex_binary_path()
    if not codex:
        return {"ok": False, "error": "Codex CLI executable is unavailable."}
    try:
        # The pinned CLI (0.154.0) takes the command directly: `codex sandbox
        # [options] -- <command>`. It has no platform subcommand, so any word
        # before `--` that is not an option is executed as the program.
        result = subprocess.run(
            [
                codex,
                "sandbox",
                "--config",
                f"sandbox_mode={json.dumps(mode)}",
                "--config",
                "check_for_update_on_startup=false",
                "--",
                "/bin/true",
            ],
            cwd=str(CONFIG_ROOT),
            env=codex_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            # Undecodable CLI output must not raise out of a readiness check.
            errors="replace",
            timeout=CODEX_SANDBOX_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Codex sandbox execution probe timed out."}
    except OSError as exc:
        return {"ok": False, "error": _diagnostic_error(exc)}
    if result.returncode == 0:
        return {"ok": True, "error": ""}
    detail = result.stderr or result.stdout or f"Codex sandbox exited with {result.returncode}."
    return {"ok": False, "error": _diagnostic_error(detail)}


def _bubblewrap_probe(
    binary: str, *, mount_proc: bool, codex_mode: str | None = None,
) -> dict[str, Any]:
    """Probe namespaces and optionally verify the actual Codex execution path."""
    args = [
        binary,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
    ]
    if mount_proc:
        args.extend(["--proc", "/proc"])
    args.append("/bin/true")
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=RUNTIME_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Bubblewrap probe timed out."}
    except OSError as exc:
        return {"ok": False, "error": _diagnostic_error(exc)}

    if result.returncode == 0:
        if codex_mode is not None:
            codex_probe = _codex_sandbox_probe(codex_mode)
            return {
                "ok": codex_probe["ok"],
                "error": codex_probe["error"],
                "namespace_ok": True,
                "codex_probe": codex_probe,
            }
        return {"ok": True, "error": ""}
    detail = result.stderr or result.stdout or f"Bubblewrap exited with {result.returncode}."
    return {"ok": False, "error": _diagnostic_error(detail)}


def sandbox_readiness() -> dict[str, Any]:
    """Report whether the configured Codex sandbox can run on this host."""
    mode = str(read_options().get("codex_sandbox") or DEFAULT_OPTIONS["codex_sandbox"])
    required = mode != "danger-full-access"
    binary = shutil.which("bwrap") or ""
    if not required:
        namespace_probe = {
            "ok": False,
            "skipped": True,
            "error": "Namespace probe is not required in danger-full-access mode.",
        }
        proc_probe = {
            "ok": False,
            "skipped": True,
            "error": "Fresh /proc probe is not required in danger-full-access mode.",
        }
    elif not binary:
        namespace_probe = {"ok": False, "error": "Bubblewrap is not installed."}
        proc_probe = {"ok": False, "skipped": True, "error": "Namespace probe was not run."}
    else:
        namespace_probe = _bubblewrap_probe(binary, mount_proc=False, codex_mode=mode)
        if namespace_probe.get("namespace_ok", namespace_probe["ok"]):
            proc_probe = _bubblewrap_probe(binary, mount_proc=True)
        else:
            proc_probe = {"ok": False, "skipped": True, "error": "Namespace probe failed."}

    # A raw namespace success is not sufficient: the optional Codex probe above
    # must also pass. Keep the fresh /proc check as independent diagnostics.
    bubblewrap_ready = bool(required and namespace_probe["ok"])
    ready = bubblewrap_ready if required else True
    if ready and required and proc_probe["ok"]:
        message = "Codex sandbox execution probe passed."
    elif ready and required:
        message = (
            "Codex sandbox execution probe passed; the standalone fresh /proc "
            "probe failed. This host may require Codex's no-proc fallback."
        )
    elif not required:
        message = "The selected danger-full-access mode does not require Bubblewrap."
    else:
        message = f"Codex sandbox preflight failed: {namespace_probe.get('error')}"
    return {
        "mode": mode,
        "required": required,
        "ready": ready,
        "bubblewrap_ready": bubblewrap_ready,
        "proc_mount_supported": bool(proc_probe["ok"]),
        "bubblewrap_binary": binary,
        "namespace_probe": namespace_probe,
        "proc_probe": proc_probe,
        "message": message,
        "checked_at": utc_now(),
    }


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
        raise ValueError(f"{AGENTS_PATH} is larger than {AGENTS_MAX_BYTES} bytes")
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
                if key not in {"prompt", "reply_history", "turns"}
            }
            for task_id, task in tasks.items()
        }
        atomic_json_write(TASK_STATE_FILE, slim)


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
                    sync_current_turn(task)
                tasks[task_id] = task
            except Exception as exc:
                print(f"Could not load task metadata from {task_file}: {exc}", flush=True)
    if not TASK_STATE_FILE.exists():
        return
    try:
        loaded = json.loads(TASK_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for task_id, task in loaded.items():
                if task.get("status") in {"queued", "running"}:
                    task["status"] = "failed"
                    task["summary"] = "Worker restarted while this task was active."
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


def sanitize_usage_excerpt(text: str) -> str:
    """Keep only quota diagnostics and remove account/session identifiers."""
    sanitized_lines: list[str] = []
    for raw_line in clean_cli_text(text).splitlines():
        line = raw_line.strip()
        if not line or SENSITIVE_USAGE_LABEL_RE.search(line):
            continue
        if not USAGE_EXCERPT_LINE_RE.search(line):
            continue
        line = EMAIL_RE.sub("[redacted]", line)
        line = UUID_RE.sub("[redacted-session]", line)
        sanitized_lines.append(redact(line))
    return "\n".join(sanitized_lines[-25:])


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
        "raw_excerpt": sanitize_usage_excerpt("\n".join(lines[-25:])),
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
        if proc is not None:
            terminate_and_reap_process(proc, terminate_timeout=3, kill_timeout=2)


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


TURN_RESULT_FIELDS = (
    "status", "summary", "details", "question", "started_at", "completed_at",
    "returncode", "changes", "validation_errors", "lovelace_results", "error",
    "attachments",
)
CONTINUABLE_STATUSES = frozenset({"completed", "waiting_for_input", "failed", "cancelled"})
TASK_STATUSES = CONTINUABLE_STATUSES | {"queued", "running"}
TASK_ORDERS = frozenset({"created_asc", "updated_desc", "pinned_first"})
TITLE_MAX_LENGTH = 200


def atomic_json_write(path: Path, value: Any) -> None:
    """Replace metadata only after the complete new document has been written."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def task_turns(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt legacy records without inventing missing intermediate responses."""
    if "turns" in task:
        return copy.deepcopy(task["turns"])
    turns = [{
        "turn_id": "legacy", "message": task.get("prompt", ""),
        "created_at": task.get("created_at", ""), "legacy": True,
    }]
    for index, reply in enumerate(task.get("reply_history") or []):
        turns.append({
            "turn_id": f"legacy-{index + 1}", "message": reply.get("reply", ""),
            "created_at": reply.get("at", ""), "legacy": True,
        })
    turns[-1].update({key: copy.deepcopy(task[key]) for key in TURN_RESULT_FIELDS if key in task})
    return turns


def sync_current_turn(task: dict[str, Any]) -> None:
    """Keep this exchange's outcome alongside the compatible task-level result."""
    turns = task.get("turns") or []
    if turns and turns[-1].get("turn_id") == task.get("current_turn_id"):
        turns[-1].update({key: copy.deepcopy(task[key]) for key in TURN_RESULT_FIELDS if key in task})
        turns[-1]["updated_at"] = task.get("updated_at", utc_now())


def new_turn(message: str) -> dict[str, Any]:
    return {"turn_id": uuid.uuid4().hex, "message": message, "created_at": utc_now(), "status": "queued"}


def get_run_dir(task_id: str) -> Path:
    """Keep each exchange's prompts, output and snapshot in its own directory."""
    with lock:
        turn_id = tasks.get(task_id, {}).get("current_turn_id")
    root = get_task_dir(task_id)
    return root / "turns" / turn_id if turn_id else root


def session_rollout_path(session_id: str) -> Path | None:
    """Locate the rollout file in which Codex records a session's items."""
    sessions = CODEX_HOME / "sessions"
    if UUID_RE.fullmatch(session_id) is None or not sessions.is_dir():
        return None
    return next(iter(sessions.rglob(f"*-{session_id}.jsonl")), None)


def session_available(session_id: str) -> bool:
    """Avoid CLI fallback to a fresh conversation when a rollout is missing."""
    return session_rollout_path(session_id) is not None


def generated_images_dir(session_id: str) -> Path:
    """Codex saves built-in image generation output here by default."""
    return CODEX_HOME / "generated_images" / session_id


def image_generation_items(session_id: str) -> list[dict[str, Any]]:
    """Return the image generation items recorded in a session rollout.

    Each `item_completed` event carries the saved path, the revised prompt and
    the inline result. Later records for the same item id replace earlier ones.
    """
    rollout = session_rollout_path(session_id) if session_id else None
    if rollout is None:
        return []
    items: dict[str, dict[str, Any]] = {}
    try:
        with rollout.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if IMAGE_GENERATION_KIND not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                item = payload.get("item") if isinstance(payload, dict) else None
                if not isinstance(item, dict) or item.get("kind") != IMAGE_GENERATION_KIND:
                    continue
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    continue
                result = item.get("result")
                items[item_id] = {
                    "id": item_id,
                    "status": str(item.get("status") or ""),
                    "failure": item.get("failure"),
                    "revised_prompt": str(item.get("revisedPrompt") or item.get("revised_prompt") or ""),
                    "saved_path": str(item.get("savedPath") or item.get("saved_path") or ""),
                    "result": result if isinstance(result, str) else "",
                    "turn_id": str(payload.get("turn_id") or ""),
                }
    except OSError as exc:
        print(f"Could not read the Codex session rollout for {session_id}: {exc}", flush=True)
    return list(items.values())


def sniff_image(header: bytes) -> tuple[str, str] | None:
    """Identify supported image content from its leading bytes."""
    for signature, mime_type, suffix in IMAGE_SIGNATURES:
        if header.startswith(signature):
            return mime_type, suffix
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _image_generation_source(item: dict[str, Any], session_id: str) -> Path | bytes | None:
    """Prefer the file Codex saved; otherwise decode the inline result."""
    saved_path = item.get("saved_path") or ""
    if saved_path:
        try:
            path = Path(saved_path).resolve()
            if path.is_relative_to(generated_images_dir(session_id).resolve()) and path.is_file():
                return path
        except OSError:
            pass
    result = str(item.get("result") or "")
    if result.startswith("data:"):
        result = result.split(",", 1)[-1]
    if not result or len(result) > ATTACHMENT_MAX_BYTES * 4 // 3 + 4:
        return None
    try:
        return base64.b64decode(result, validate=True)
    except (ValueError, binascii.Error):
        return None


def store_attachment(
    task_id: str, source: Path | bytes | None, item: dict[str, Any],
) -> dict[str, Any] | None:
    """Copy verified image content into this exchange's attachment directory."""
    if source is None:
        return None
    if isinstance(source, Path):
        size = source.stat().st_size
        with source.open("rb") as handle:
            header = handle.read(16)
    else:
        size = len(source)
        header = source[:16]
    sniffed = sniff_image(header)
    if sniffed is None or size == 0 or size > ATTACHMENT_MAX_BYTES:
        return None
    mime_type, suffix = sniffed
    attachment_id = uuid.uuid4().hex
    target_dir = get_run_dir(task_id) / "attachments"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{attachment_id}{suffix}"
    if isinstance(source, Path):
        shutil.copyfile(source, target)
    else:
        target.write_bytes(source)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return {
        "attachment_id": attachment_id,
        "kind": "image",
        "name": f"codex-image-{attachment_id[:8]}{suffix}",
        "mime_type": mime_type,
        "size": target.stat().st_size,
        "sha256": file_hash(target),
        "created_at": utc_now(),
        "revised_prompt": str(item.get("revised_prompt") or "")[:2000],
        "generation_id": str(item.get("id") or ""),
        "path": target.relative_to(get_task_dir(task_id)).as_posix(),
        "url": f"/tasks/{task_id}/attachments/{attachment_id}",
    }


def collect_generated_images(task_id: str, session_id: str, known_ids: set[str]) -> list[dict[str, Any]]:
    """Attach the images this exchange generated, skipping earlier turns' items."""
    if not session_id:
        return []
    attachments: list[dict[str, Any]] = []
    for item in image_generation_items(session_id):
        if item["id"] in known_ids:
            continue
        if (item["status"] and item["status"] != "completed") or item.get("failure"):
            write_task_log(task_id, "worker", f"Skipped image generation {item['id']}: status={item['status'] or 'unknown'}")
            continue
        if len(attachments) >= ATTACHMENTS_PER_TURN_MAX:
            write_task_log(task_id, "worker", f"Skipped image generation {item['id']}: attachment limit reached")
            continue
        try:
            attachment = store_attachment(task_id, _image_generation_source(item, session_id), item)
        except Exception as exc:
            write_task_log(task_id, "worker", f"Could not attach image generation {item['id']}: {exc}")
            continue
        if attachment is None:
            write_task_log(task_id, "worker", f"Skipped image generation {item['id']}: no usable image content")
            continue
        attachments.append(attachment)
    return attachments


def find_attachment(task: dict[str, Any], attachment_id: str) -> dict[str, Any] | None:
    """Return the recorded metadata for an attachment id, newest exchange first."""
    for turn in reversed(task_turns(task)):
        for attachment in turn.get("attachments") or []:
            if isinstance(attachment, dict) and attachment.get("attachment_id") == attachment_id:
                return attachment
    return None


def task_payload(task: dict[str, Any], *, summary: bool = False) -> dict[str, Any]:
    if summary:
        fields = ("task_id", "title", "status", "created_at", "updated_at", "summary", "question")
        result = {key: task.get(key, "") for key in fields}
        result["summary"] = str(result["summary"])[:240]
        result["pinned"] = bool(task.get("pinned"))
    else:
        result = copy.deepcopy(task)
        result["chat_settings"] = copy.deepcopy(task.get("chat_settings", DEFAULT_CHAT_SETTINGS))
        result["turns"] = task_turns(task)
        result["history_incomplete"] = task.get("history_incomplete", "turns" not in task)
    result["can_continue"] = bool(task.get("session_id")) and task.get("status") in CONTINUABLE_STATUSES
    return result


def update_task(task_id: str, **updates: Any) -> bool:
    with lock:
        task = tasks.setdefault(task_id, {"task_id": task_id})
        if task.get("cancellation_requested") and updates.get("status") != "cancelled" and updates.get("cancellation_requested") is not False:
            return False
        task.update(updates)
        task["updated_at"] = utc_now()
        sync_current_turn(task)
        task_dir = get_task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(task_dir / "task.json", task)
    save_task_index()
    return True


def normalize_title(value: Any) -> str:
    """Keep a renamed chat on one line and within the sidebar's limits."""
    if not isinstance(value, str):
        raise ValueError("title must be text")
    title = " ".join(value.split())
    if not title:
        raise ValueError("title is required")
    if len(title) > TITLE_MAX_LENGTH:
        raise ValueError(f"title must be at most {TITLE_MAX_LENGTH} characters")
    return title


def save_task_fields(task_id: str, **fields: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Persist sidebar metadata in place without touching updated_at or the current turn.

    Pinning or renaming must neither move a chat in the recent list nor rewrite
    the outcome of an exchange, so this bypasses update_task on purpose. The
    task dict is mutated under the lock so a running task's later writes keep
    the new values.
    """
    missing = object()
    with lock:
        task = tasks.get(task_id)
        if task is None:
            return None, "task not found"
        previous = {key: task.get(key, missing) for key in fields}
        task.update(fields)
        try:
            task_dir = get_task_dir(task_id)
            task_dir.mkdir(parents=True, exist_ok=True)
            atomic_json_write(task_dir / "task.json", task)
        except OSError:
            for key, value in previous.items():
                if value is missing:
                    task.pop(key, None)
                else:
                    task[key] = value
            return None, "Could not save the conversation. Try again."
        save_task_index()
        return task, None


def remove_session_files(session_id: str) -> None:
    """Best-effort removal of the Codex session a deleted chat could resume."""
    if not session_id:
        return
    rollout = session_rollout_path(session_id)
    targets = [rollout] if rollout else []
    images = generated_images_dir(session_id)
    if UUID_RE.fullmatch(session_id) and images.is_dir():
        targets.append(images)
    for target in targets:
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            print(f"Could not remove session data {target}: {exc}", flush=True)


def task_cancellation_requested(task_id: str) -> bool:
    """Return whether cancellation won the task's terminal-state race."""
    with lock:
        task = tasks.get(task_id, {})
        return bool(task.get("cancellation_requested"))


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


def _active_task_ids_locked() -> set[str]:
    active = set(active_task_runners)
    active.update(running_processes)
    active.update(
        task_id
        for task_id, task in tasks.items()
        if task.get("status") in {"queued", "running"}
    )
    return active


def _active_task_id_locked() -> str | None:
    active = _active_task_ids_locked()
    for task_id in tasks:
        if task_id in active:
            return task_id
    return min(active) if active else None


def active_task_id() -> str | None:
    with lock:
        return _active_task_id_locked()


def active_task_count() -> int:
    with lock:
        return len(_active_task_ids_locked())


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
    task_dir = get_run_dir(task_id)
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
    """Return the authoritative Codex thread ID from one top-level JSON event."""
    if not isinstance(obj, dict) or obj.get("type") != "thread.started":
        return None
    thread_id = obj.get("thread_id")
    if not isinstance(thread_id, str) or UUID_RE.fullmatch(thread_id) is None:
        return None
    try:
        uuid.UUID(thread_id)
    except ValueError:
        return None
    return thread_id


def build_prompt(user_prompt: str, task_id: str, reply: str | None = None) -> str:
    current_request = reply if reply is not None else user_prompt
    return f"""You are Codex running as a Home Assistant add-on worker.

Workspace: /config
Task id: {task_id}

Follow /config/AGENTS.md. Treat this as a live Home Assistant config tree. Make focused edits, do not expose secrets, and validate changed YAML/JSON when practical.

This is a non-interactive run. Do not wait for terminal input. If you need the user's decision or verification before continuing, stop cleanly by returning status "needs_input" with one concise question.

If the user asks for an image, use the built-in image generation tool. Every image it generates is attached to this conversation and shown to the user automatically, so leave it at its default save location and describe it in the summary. Copy it into /config only when the user asks for a file at a specific path. The default save location is not a failure.

At the end, return only an object matching the provided JSON schema:
- status: "completed", "needs_input", or "failed"
- summary: concise result
- question: use an empty string unless status is "needs_input"
- details: use an empty string unless there are useful implementation/test notes
Current user message:
{current_request}
"""


def build_codex_args(task_id: str, prompt_file: Path, final_file: Path, session_id: str | None) -> list[str]:
    options = read_options()
    with lock:
        task = tasks.get(task_id, {})
        turns = task.get("turns") or []
        execution = copy.deepcopy(turns[-1].get("execution_settings")) if turns else None
    if execution is None:
        execution = resolve_chat_settings(task.get("chat_settings", DEFAULT_CHAT_SETTINGS), options)
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
    model = execution["model"]
    if model in {"default", "gpt-5.3-codex"}:
        model = ""
    if model:
        args.extend(["--model", model])
    args.extend(["--config", f'model_reasoning_effort="{execution["reasoning_effort"]}"'])
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
                existing = session_holder.get("session_id")
                if found and not existing:
                    session_holder["session_id"] = found
                    update_task(task_id, session_id=found)
            except (json.JSONDecodeError, TypeError):
                pass


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


def terminate_and_reap_process(
    proc: subprocess.Popen[Any],
    *,
    terminate_timeout: float = 20,
    kill_timeout: float = 5,
) -> bool:
    """Terminate and reap a child, returning whether exit was confirmed."""
    try:
        exited = proc.poll() is not None
    except OSError:
        exited = False
    if exited:
        try:
            proc.wait(timeout=0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return True
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=terminate_timeout)
        return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=kill_timeout)
        return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return proc.poll() is not None
    except OSError:
        return False


def request_task_cancellation(
    task_id: str,
) -> tuple[bool, subprocess.Popen[Any] | None, str]:
    """Atomically make cancellation the task's terminal outcome."""
    with lock:
        task = tasks.get(task_id)
        if task is None:
            return False, None, "task not found"

        status = str(task.get("status") or "")
        if task.get("cancellation_requested"):
            return True, running_processes.get(task_id), ""
        if status not in CANCELLABLE_TASK_STATUSES:
            return False, None, f"task is not active (status={status or 'unknown'})"

        completed_at = str(task.get("completed_at") or utc_now())
        task.update(
            {
                "status": "cancelled",
                "cancellation_requested": True,
                "completed_at": completed_at,
                "returncode": None,
                "summary": CANCELLED_TASK_SUMMARY,
                "question": "",
                "details": "",
                "error": "",
                "changes": {"added": [], "changed": [], "deleted": []},
                "validation_errors": [],
                "lovelace_results": [],
                "attachments": [],
                "updated_at": utc_now(),
            }
        )
        sync_current_turn(task)
        task_dir = get_task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(task_dir / "task.json", task)
        proc = running_processes.get(task_id)
    save_task_index()
    return True, proc, ""


def publish_cancelled_task_outcome(task_id: str, returncode: int | None = None) -> bool:
    """Publish one, and only one, final cancelled task event."""
    with lock:
        task = tasks.get(task_id)
        if not task or not task.get("cancellation_requested"):
            return False
        if task.get("cancellation_event_emitted"):
            return False

        task["cancellation_event_emitted"] = True
        if returncode is not None:
            task["returncode"] = returncode
        task["updated_at"] = utc_now()
        sync_current_turn(task)
        task_dir = get_task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(task_dir / "task.json", task)
        final_task = dict(task)
    save_task_index()

    changes = final_task.get("changes") or {"added": [], "changed": [], "deleted": []}
    validation_errors = final_task.get("validation_errors") or []
    lovelace_results = final_task.get("lovelace_results") or []
    event_data = {
        "task_id": task_id,
        "status": "cancelled",
        "codex_status": "cancelled",
        "summary": CANCELLED_TASK_SUMMARY,
        "question": "",
        "details": "",
        "returncode": final_task.get("returncode"),
        "session_id": str(final_task.get("session_id") or ""),
        "completed_at": str(final_task.get("completed_at") or ""),
        "changes": changes,
        "validation_errors": validation_errors,
        "lovelace_results": lovelace_results,
        "attachments": [],
        "response": {
            "status": "cancelled",
            "summary": CANCELLED_TASK_SUMMARY,
            "question": "",
            "details": "",
        },
    }
    ok, detail = fire_ha_event("codex_cli_task_result", event_data)
    if not ok:
        print(f"Codex task result event failed: {detail}", flush=True)
    notify("Codex task cancelled", f"{CANCELLED_TASK_SUMMARY}. Task: {task_id}")
    refresh_usage_status_async(force=True)
    return True


def fail_task_launch(
    task_id: str,
    exc: Exception,
    *,
    session_id: str | None = None,
    summary: str = "Could not start the Codex CLI executable.",
    detail_prefix: str | None = None,
) -> None:
    """Record and publish a clean task failure when Codex cannot start."""
    if task_cancellation_requested(task_id):
        return
    completed_at = utc_now()
    prefix = detail_prefix or f"Could not start {CODEX_BINARY}"
    details = redact(f"{prefix}: {exc}")
    with lock:
        existing_session_id = str(tasks.get(task_id, {}).get("session_id") or "")
    resolved_session_id = str(session_id or existing_session_id)
    write_task_log(task_id, "worker", details)
    task_updates: dict[str, Any] = {
        "status": "failed",
        "completed_at": completed_at,
        "returncode": None,
        "summary": summary,
        "question": "",
        "details": details,
        "error": details,
        "changes": {"added": [], "changed": [], "deleted": []},
        "validation_errors": [],
        "lovelace_results": [],
        "attachments": [],
    }
    if resolved_session_id:
        task_updates["session_id"] = resolved_session_id
    if update_task(task_id, **task_updates) is False:
        return
    event_data = {
        "task_id": task_id,
        "status": "failed",
        "codex_status": "failed",
        "summary": summary,
        "question": "",
        "details": details,
        "returncode": None,
        "session_id": resolved_session_id,
        "completed_at": completed_at,
        "changes": {"added": [], "changed": [], "deleted": []},
        "validation_errors": [],
        "lovelace_results": [],
        "attachments": [],
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


def record_background_start_failure(task_id: str, exc: Exception) -> None:
    """Release a runner reservation and record a terminal startup failure."""
    with lock:
        active_task_runners.discard(task_id)
    try:
        fail_task_launch(
            task_id,
            exc,
            summary="Could not start the Codex task worker.",
            detail_prefix="Could not start the background task runner",
        )
        return
    except Exception as persist_exc:
        details = redact(f"Could not start the background task runner: {exc}")
        completed_at = utc_now()
        with lock:
            task = tasks.setdefault(task_id, {"task_id": task_id})
            if not task.get("cancellation_requested"):
                task.update(
                    {
                        "status": "failed",
                        "completed_at": completed_at,
                        "returncode": None,
                        "summary": "Could not start the Codex task worker.",
                        "question": "",
                        "details": details,
                        "error": details,
                        "changes": {"added": [], "changed": [], "deleted": []},
                        "validation_errors": [],
                        "lovelace_results": [],
                        "attachments": [],
                        "updated_at": completed_at,
                    }
                )
        print(
            "Could not persist background task startup failure: "
            f"{redact(str(persist_exc))}",
            flush=True,
        )


def run_task(task_id: str, prompt: str, session_id: str | None = None, reply: str | None = None) -> None:
    if task_cancellation_requested(task_id):
        return
    task_dir = get_run_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    final_file = task_dir / ("final-resume.json" if reply else "final.json")
    prompt_file = task_dir / ("prompt-resume.txt" if reply else "prompt.txt")
    before_manifest_path = task_dir / "manifest-before.json"

    update_task(task_id, status="running", started_at=utc_now(), error="")
    if task_cancellation_requested(task_id):
        return
    if not before_manifest_path.exists():
        try:
            snapshot = create_snapshot(task_id)
            update_task(task_id, snapshot=snapshot)
        except Exception as exc:
            write_task_log(task_id, "worker", f"Snapshot failed: {exc}")
            update_task(task_id, snapshot_error=str(exc))
    if task_cancellation_requested(task_id):
        return

    try:
        final_file.unlink(missing_ok=True)
    except OSError as exc:
        fail_task_launch(
            task_id,
            exc,
            session_id=session_id,
            summary="Could not prepare the Codex task output.",
            detail_prefix="Could not reset the Codex final response",
        )
        return

    try:
        prompt_file.write_text(build_prompt(prompt, task_id, reply=reply), encoding="utf-8")
    except OSError as exc:
        fail_task_launch(
            task_id,
            exc,
            session_id=session_id,
            summary="Could not prepare the Codex task input.",
            detail_prefix="Could not write the Codex task prompt",
        )
        return
    if task_cancellation_requested(task_id):
        return
    args = build_codex_args(task_id, prompt_file, final_file, session_id)
    write_task_log(task_id, "worker", "Starting Codex: " + " ".join(args))

    readiness = sandbox_readiness()
    if task_cancellation_requested(task_id):
        return
    if readiness["required"] and not readiness["ready"]:
        fail_task_launch(
            task_id,
            RuntimeError(str(readiness["message"])),
            session_id=session_id,
            summary="The configured Codex sandbox is not available.",
            detail_prefix="Codex sandbox preflight failed",
        )
        return

    timeout = int(read_options().get("task_timeout_seconds") or 3600)
    session_holder: dict[str, str] = {}
    # Images recorded before this run belong to earlier exchanges of the session.
    known_image_ids = {item["id"] for item in image_generation_items(session_id or "")}
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
        fail_task_launch(task_id, exc, session_id=session_id)
        return
    cancel_after_launch = False
    with lock:
        if tasks.get(task_id, {}).get("cancellation_requested"):
            cancel_after_launch = True
        else:
            running_processes[task_id] = proc
    if cancel_after_launch:
        if not terminate_and_reap_process(proc):
            with lock:
                running_processes[task_id] = proc
        return
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
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
    lifecycle_error: Exception | None = None
    stdin_closed = False
    returncode = -1
    process_exit_confirmed = False
    try:
        try:
            proc.stdin.write(prompt_file.read_text(encoding="utf-8"))
            proc.stdin.close()
            stdin_closed = True
        except (OSError, ValueError) as exc:
            lifecycle_error = exc
            process_exit_confirmed = terminate_and_reap_process(proc)
        if lifecycle_error is None:
            try:
                returncode = proc.wait(timeout=timeout)
                process_exit_confirmed = True
            except subprocess.TimeoutExpired:
                timed_out = True
                process_exit_confirmed = terminate_and_reap_process(proc)
                returncode = proc.returncode if proc.returncode is not None else -1
            except OSError as exc:
                lifecycle_error = exc
                process_exit_confirmed = terminate_and_reap_process(proc)
    finally:
        if not stdin_closed:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        if process_exit_confirmed:
            with lock:
                if running_processes.get(task_id) is proc:
                    running_processes.pop(task_id, None)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    if task_cancellation_requested(task_id):
        return
    if lifecycle_error is not None:
        fail_task_launch(
            task_id,
            lifecycle_error,
            session_id=session_holder.get("session_id") or session_id,
            summary="Codex exited before accepting the task prompt.",
            detail_prefix="Could not send the task prompt to Codex",
        )
        return

    final = parse_final(final_file, returncode)
    after_manifest = build_manifest()
    (task_dir / "manifest-after.json").write_text(json.dumps(after_manifest, indent=2), encoding="utf-8")
    try:
        before_manifest = json.loads(before_manifest_path.read_text(encoding="utf-8"))
    except Exception:
        before_manifest = {}
    changes = diff_manifests(before_manifest, after_manifest)
    (task_dir / "changes.json").write_text(json.dumps(changes, indent=2), encoding="utf-8")
    if task_cancellation_requested(task_id):
        return
    validation_errors = validate_changed_files(changes)

    lovelace_results: list[dict[str, Any]] = []
    if read_options().get("auto_save_lovelace"):
        for ref in find_lovelace_dashboard_refs(changes):
            if task_cancellation_requested(task_id):
                return
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
    session_id = (
        session_holder.get("session_id")
        or session_id
        or tasks.get(task_id, {}).get("session_id", "")
    )
    try:
        attachments = collect_generated_images(task_id, session_id, known_image_ids)
    except Exception as exc:
        attachments = []
        write_task_log(task_id, "worker", f"Could not collect generated images: {exc}")
    if update_task(
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
        attachments=attachments,
    ) is False:
        return

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
        "attachments": attachments,
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


def _run_background_task(
    task_id: str,
    prompt: str,
    session_id: str | None,
    reply: str | None,
) -> None:
    try:
        run_task(task_id, prompt, session_id, reply)
    except Exception as exc:
        fail_task_launch(task_id, exc, session_id=session_id, summary="The Codex task could not finish.")
    finally:
        with lock:
            proc = running_processes.get(task_id)
        if proc is not None:
            process_exit_confirmed = terminate_and_reap_process(proc)
            if process_exit_confirmed:
                with lock:
                    if running_processes.get(task_id) is proc:
                        running_processes.pop(task_id, None)
        with lock:
            active_task_runners.discard(task_id)


def start_background_task(
    task_id: str,
    prompt: str,
    session_id: str | None = None,
    reply: str | None = None,
) -> threading.Thread:
    with lock:
        other_active = _active_task_ids_locked() - {task_id}
        if other_active:
            active = min(other_active)
            raise RuntimeError(f"another task is already running: {active}")
        active_task_runners.add(task_id)
    thread = threading.Thread(
        target=_run_background_task,
        args=(task_id, prompt, session_id, reply),
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        with lock:
            active_task_runners.discard(task_id)
        raise
    return thread


@app.get("/")
def index() -> Response:
    return Response((WEB_ROOT / "index.html").read_text(encoding="utf-8"), mimetype="text/html")


@app.get("/health")
@require_auth
def health() -> Response:
    version = codex_version_status()
    sandbox = sandbox_readiness()
    return jsonify(
        {
            "ok": True,
            "api_token_configured": bool(api_token()),
            "codex_binary": codex_binary_path() or "",
            "codex_version": version["version"],
            "codex_version_error": version["error"],
            "codex_login": codex_login_status(),
            "auth_flow": auth_status_payload(),
            "task_root": str(task_root()),
            "sandbox_readiness": sandbox,
        }
    )


@app.get("/status")
@require_auth
def status() -> Response:
    refresh_usage_status_async(force=False)
    with lock:
        task_values = sorted(tasks.values(), key=lambda task: task.get("updated_at") or task.get("created_at", ""))
        latest = {key: copy.deepcopy(value) for key, value in task_values[-1].items() if key != "turns"} if task_values else None
    return jsonify(
        {
            "ok": True,
            "active_task_id": active_task_id(),
            "active_task_count": active_task_count(),
            "task_count": active_task_count(),
            "total_task_count": len(task_values),
            "latest_task": latest,
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
    try:
        limit = int(request.args["limit"]) if "limit" in request.args else None
        offset = int(request.args.get("offset", "0"))
        if (limit is not None and not 1 <= limit <= 500) or offset < 0:
            raise ValueError
    except ValueError:
        return jsonify({"ok": False, "error": "limit must be 1-500 and offset must be non-negative"}), 400
    status = request.args.get("status")
    order = request.args.get("order", "created_asc")
    if status and status not in TASK_STATUSES:
        return jsonify({"ok": False, "error": "invalid task status"}), 400
    if order not in TASK_ORDERS:
        return jsonify({"ok": False, "error": "invalid task order"}), 400
    with lock:
        filtered = [task for task in tasks.values() if not status or task.get("status") == status]
        key = "created_at" if order == "created_asc" else "updated_at"
        ordered = sorted(filtered, key=lambda item: (item.get(key) or item.get("created_at", ""), item["task_id"]), reverse=order != "created_asc")
        if order == "pinned_first":
            # Stable sort keeps the recent-activity order inside each group.
            ordered.sort(key=lambda item: not item.get("pinned"))
        page = ordered[offset:offset + limit] if limit is not None else ordered[offset:]
        # Preserve the original unfiltered response shape for existing automations.
        summaries = request.args.get("summary") == "true"
        result = [task_payload(task, summary=summaries) for task in page]
        active = _active_task_id_locked()
    next_offset = offset + len(page)
    return jsonify({"ok": True, "tasks": result, "total": len(ordered),
                    "next_offset": next_offset if next_offset < len(ordered) else None,
                    "active_task_id": active})


@app.get("/chat-options")
@require_auth
def chat_options() -> Response:
    options = read_options()
    default = resolve_chat_settings(DEFAULT_CHAT_SETTINGS, options)
    return jsonify({
        "ok": True,
        "defaults": default,
        "models": [{"id": model, "label": label, "efforts": efforts} for model, label, efforts in CHAT_MODELS],
        "default_efforts": CHAT_MODEL_EFFORTS.get(default["model"], ("low", "medium", "high", "xhigh")),
    })


@app.post("/tasks/<task_id>/settings")
@require_auth
def save_chat_settings(task_id: str) -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "chat_settings" not in payload:
        return jsonify({"ok": False, "error": "chat_settings is required"}), 400
    with lock:
        task = tasks.get(task_id)
        if task is None:
            return jsonify({"ok": False, "error": "task not found"}), 404
        if task.get("status") in {"queued", "running"} or task_id in _active_task_ids_locked():
            return jsonify({"ok": False, "error": "Wait for this task to finish before changing its settings."}), 409
        try:
            settings = parse_chat_settings(payload, task)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        # Changing the next run's settings must not rewrite the previous turn or
        # move a conversation to the top of the recently active list.
        updated = {**task, "chat_settings": settings}
        try:
            task_dir = get_task_dir(task_id)
            task_dir.mkdir(parents=True, exist_ok=True)
            atomic_json_write(task_dir / "task.json", updated)
        except OSError:
            return jsonify({"ok": False, "error": "Could not save conversation settings. Try again."}), 500
        tasks[task_id] = updated
        save_task_index()
    return jsonify({"ok": True, "chat_settings": settings})


@app.post("/tasks/<task_id>/pin")
@require_auth
def pin_task(task_id: str) -> Response:
    """Pin or unpin a chat without moving it in the recent list."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("pinned"), bool):
        return jsonify({"ok": False, "error": "pinned must be true or false"}), 400
    task, error = save_task_fields(task_id, pinned=payload["pinned"])
    if task is None:
        return jsonify({"ok": False, "error": error}), 404 if error == "task not found" else 500
    return jsonify({"ok": True, "task_id": task_id, "pinned": bool(task.get("pinned"))})


@app.post("/tasks/<task_id>/title")
@require_auth
def rename_task(task_id: str) -> Response:
    """Rename a chat; the title is normalized and the recent order is kept."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "title is required"}), 400
    try:
        title = normalize_title(payload.get("title"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    task, error = save_task_fields(task_id, title=title)
    if task is None:
        return jsonify({"ok": False, "error": error}), 404 if error == "task not found" else 500
    return jsonify({"ok": True, "task_id": task_id, "title": task["title"]})


@app.delete("/tasks/<task_id>")
@require_auth
def delete_task(task_id: str) -> Response:
    """Delete a finished chat with its files, Codex session, and index entry."""
    with lock:
        task = tasks.get(task_id)
        if task is None:
            return jsonify({"ok": False, "error": "task not found"}), 404
        if task.get("status") in {"queued", "running"} or task_id in _active_task_ids_locked():
            return jsonify({"ok": False, "error": "Stop this task before deleting it."}), 409
        task_dir = get_task_dir(task_id)
        try:
            if task_dir.exists():
                shutil.rmtree(task_dir)
        except OSError:
            # The chat stays listed so the user can retry instead of losing track of it.
            return jsonify({"ok": False, "error": "Could not delete the conversation files. Try again."}), 500
        tasks.pop(task_id, None)
        save_task_index()
        session_id = str(task.get("session_id") or "")
    remove_session_files(session_id)
    return jsonify({"ok": True, "task_id": task_id, "deleted": True})


@app.post("/tasks")
@require_auth
def create_task() -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str):
        return jsonify({"ok": False, "error": "prompt must be text"}), 400
    prompt = payload["prompt"].strip()
    if not prompt:
        return jsonify({"ok": False, "error": "prompt is required"}), 400
    try:
        settings = parse_chat_settings(payload)
        execution = resolve_chat_settings(settings, read_options())
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    task_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    title = str(payload.get("title") or prompt[:80])
    turn = new_turn(prompt)
    turn["execution_settings"] = execution
    with lock:
        active = _active_task_id_locked()
        if active:
            return jsonify(
                {
                    "ok": False,
                    "error": "another task is already running",
                    "active_task_id": active,
                }
            ), 409
        active_task_runners.add(task_id)
    try:
        update_task(
            task_id,
            status="queued",
            cancellation_requested=False,
            cancellation_event_emitted=False,
            title=title,
            prompt=prompt,
            chat_settings=settings,
            turns=[turn],
            current_turn_id=turn["turn_id"],
            created_at=utc_now(),
            summary="",
            question="",
            details="",
            attachments=[],
        )
        (get_task_dir(task_id) / "user-prompt.txt").write_text(prompt, encoding="utf-8")
        start_background_task(task_id, prompt)
    except Exception as exc:
        record_background_start_failure(task_id, exc)
        return jsonify(
            {
                "ok": False,
                "error": "could not start the Codex task worker",
                "task_id": task_id,
            }
        ), 500
    return jsonify({"ok": True, "task_id": task_id, "status": "queued"})


@app.get("/tasks/<task_id>")
@require_auth
def get_task(task_id: str) -> Response:
    with lock:
        task = tasks.get(task_id)
        result = task_payload(task) if task else None
    if result is None:
        return jsonify({"ok": False, "error": "task not found"}), 404
    return jsonify({"ok": True, "task": result})


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


@app.get("/tasks/<task_id>/attachments/<attachment_id>")
@require_auth
def get_attachment(task_id: str, attachment_id: str) -> Response:
    """Serve a generated image recorded for this conversation."""
    if TASK_ID_RE.fullmatch(task_id) is None or ATTACHMENT_ID_RE.fullmatch(attachment_id) is None:
        return jsonify({"ok": False, "error": "attachment not found"}), 404
    with lock:
        task = tasks.get(task_id)
        attachment = find_attachment(task, attachment_id) if task else None
    if attachment is None:
        return jsonify({"ok": False, "error": "attachment not found"}), 404
    task_dir = get_task_dir(task_id).resolve()
    relative = str(attachment.get("path") or "")
    path = (task_dir / relative).resolve()
    try:
        inside = bool(relative) and not Path(relative).is_absolute() and path.is_relative_to(task_dir)
        if not inside or not path.is_file():
            return jsonify({"ok": False, "error": "attachment file is missing"}), 404
        size = path.stat().st_size
        with path.open("rb") as handle:
            sniffed = sniff_image(handle.read(16))
        # Serve only the bytes that were recorded when the image was captured.
        intact = (
            0 < size <= ATTACHMENT_MAX_BYTES
            and size == attachment.get("size")
            and file_hash(path) == str(attachment.get("sha256") or "")
        )
    except OSError:
        return jsonify({"ok": False, "error": "attachment file is missing"}), 404
    mime_type = str(attachment.get("mime_type") or "")
    if sniffed is None or sniffed[0] != mime_type or not intact:
        return jsonify({"ok": False, "error": "attachment content is not the recorded image"}), 404
    response = send_file(
        path,
        mimetype=mime_type,
        as_attachment=request.args.get("download") == "1",
        download_name=str(attachment.get("name") or f"{attachment_id}{sniffed[1]}"),
        conditional=True,
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@app.post("/tasks/<task_id>/cancel")
@require_auth
def cancel_task(task_id: str) -> Response:
    accepted, proc, error = request_task_cancellation(task_id)
    if not accepted:
        status_code = 404 if error == "task not found" else 409
        return jsonify({"ok": False, "error": error}), status_code
    if proc is not None:
        terminate_and_reap_process(proc)
    publish_cancelled_task_outcome(task_id, proc.returncode if proc is not None else None)
    return jsonify({"ok": True, "task_id": task_id, "status": "cancelled"})


@app.post("/tasks/<task_id>/reply")
@require_auth
def reply_task(task_id: str) -> Response:
    return continue_task_request(task_id, "reply", waiting_only=True)


@app.post("/tasks/<task_id>/continue")
@require_auth
def continue_task(task_id: str) -> Response:
    return continue_task_request(task_id, "message")


def continue_task_request(task_id: str, field: str, *, waiting_only: bool = False) -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get(field), str) or not payload[field].strip():
        return jsonify({"ok": False, "error": f"{field} must be non-empty text"}), 400
    message = payload[field].strip()
    with lock:
        task = tasks.get(task_id)
        if not task:
            return jsonify({"ok": False, "error": "task not found"}), 404
        if waiting_only and task.get("status") != "waiting_for_input":
            return jsonify({"ok": False, "error": "task is not waiting for input"}), 409
        if task.get("status") not in CONTINUABLE_STATUSES:
            return jsonify({"ok": False, "error": "task is still active"}), 409
        session_id = str(task.get("session_id") or "")
        if not session_id or not session_available(session_id):
            return jsonify({"ok": False, "error": "The saved Codex session is unavailable. Start a new chat and include the context you need."}), 409
        active = _active_task_id_locked()
        if active:
            return jsonify({"ok": False, "error": "another task is already running", "active_task_id": active}), 409
        try:
            settings = parse_chat_settings(payload, task)
            execution = resolve_chat_settings(settings, read_options())
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        turns = task_turns(task)
        turn = new_turn(message)
        turn["execution_settings"] = execution
        turns.append(turn)
        incomplete = task.get("history_incomplete", "turns" not in task)
        reply_history = list(task.get("reply_history") or [])
        reply_history.append({"at": turn["created_at"], "reply": message})
        active_task_runners.add(task_id)
        try:
            update_task(
                task_id, status="queued", cancellation_requested=False,
                chat_settings=settings,
                cancellation_event_emitted=False, turns=turns,
                current_turn_id=turn["turn_id"], history_incomplete=incomplete,
                reply_history=reply_history, summary="", question="", details="",
                error="", started_at="", completed_at="", returncode=None,
                changes={}, validation_errors=[], lovelace_results=[], attachments=[],
            )
        except Exception as exc:
            record_background_start_failure(task_id, exc)
            return jsonify({"ok": False, "task_id": task_id, "error": "Could not save the new message"}), 500
    try:
        start_background_task(task_id, str(task.get("prompt") or ""), session_id=session_id, reply=message)
    except Exception as exc:
        record_background_start_failure(task_id, exc)
        return jsonify({"ok": False, "task_id": task_id, "error": "Could not start the task worker"}), 500
    return jsonify({"ok": True, "task_id": task_id, "turn_id": turn["turn_id"], "status": "queued"})



def main() -> None:
    ensure_runtime_files()
    version = codex_version_status()
    sandbox = sandbox_readiness()
    if version["version"]:
        print(f"Codex runtime version: {version['version']}", flush=True)
    else:
        print(f"Codex version probe failed: {version['error']}", flush=True)
    print(f"Codex sandbox preflight: {sandbox['message']}", flush=True)
    load_task_index()
    auto_start_login_if_needed()
    threading.Thread(target=stdin_reader, daemon=True).start()
    app.run(host="0.0.0.0", port=9123)


if __name__ == "__main__":
    main()
