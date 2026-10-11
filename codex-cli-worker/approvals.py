"""Bridge Codex app-server turns to explicit, single-action user approvals.

The worker owns the process and authentication. This adapter owns JSON-RPC
correlation and live requests; only redacted display records leave it. Approval
responses never install command rules or grant access for a session.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from typing import Any, Callable, TextIO


APPROVAL_GONE = "This permission request is no longer waiting for an answer."
DISPLAY_LIMIT = 32000
REQUEST_LIMIT = 50
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"


class AppServerSession:
    """Run one app-server turn and correlate each user decision with its request.

    All state and writes use the worker's reentrant lock, including cancellation
    and the HTTP decision route. A browser cannot supply protocol request IDs,
    commands, permission scopes, or policy amendments.
    """

    def __init__(
        self, writer: TextIO, *, lock: Any, thread_params: dict[str, Any],
        turn_params: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
        on_approvals: Callable[[list[dict[str, Any]]], None],
        on_final: Callable[[str], None], is_active: Callable[[], bool],
        redact: Callable[[str], str],
    ) -> None:
        """Bind a live transport to one worker exchange and its persistence callbacks."""
        self.writer = writer
        self.lock = lock
        self.thread_params = copy.deepcopy(thread_params)
        self.turn_params = copy.deepcopy(turn_params)
        self.on_event = on_event
        self.on_approvals = on_approvals
        self.on_final = on_final
        self.is_active = is_active
        self.redact = redact
        self.finished = threading.Event()
        self.error = ""
        self.returncode = 1
        self.thread_id = ""
        self.turn_id = ""
        self._closed = False
        self._turn_ended = False
        self._next_id = 0
        self._requests: dict[str, str] = {}
        self._pending: dict[str, Any] = {}
        self._records: list[dict[str, Any]] = []
        self._file_changes: dict[str, list[dict[str, Any]]] = {}
        self._final_text = ""

    def _write(self, message: dict[str, Any]) -> None:
        """Write one complete JSON-RPC message while the shared lock is held."""
        self.writer.write(json.dumps(message) + "\n")
        self.writer.flush()

    def _request(self, method: str, params: dict[str, Any]) -> None:
        """Send a client request with a namespace distinct from server request IDs."""
        self._next_id += 1
        request_id = f"worker-{self._next_id}"
        self._requests[request_id] = method
        self._write({"id": request_id, "method": method, "params": params})

    def start(self) -> None:
        """Initialize the protocol; thread and turn creation follow their replies."""
        with self.lock:
            if self._closed or not self.is_active():
                self.close()
                return
            self._request("initialize", {"clientInfo": {
                "name": "home_assistant_worker", "title": "Home Assistant", "version": "1",
            }})

    def read(self, handle: TextIO) -> None:
        """Read the JSONL transport until completion or EOF, failing closed on errors."""
        try:
            for line in iter(handle.readline, ""):
                self.receive(json.loads(line))
                if self.finished.is_set():
                    break
            if not self.finished.is_set():
                self.fail("Codex closed the permission connection before the exchange finished.")
        except Exception as exc:
            self.fail(f"Codex permission connection failed: {exc}")

    def fail(self, message: str) -> None:
        """Record a redacted transport error and invalidate every outstanding request."""
        with self.lock:
            self.error = self.redact(message)
            try:
                self.close()
            except OSError:
                # A failed audit write must not keep the transport alive.
                pass

    def close(self) -> None:
        """Expire all live requests and close stdin; saved records cannot grant access."""
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.thread_id and self.turn_id and not self._turn_ended:
                try:
                    self._write({"id": "worker-stop", "method": "turn/interrupt", "params": {
                        "threadId": self.thread_id, "turnId": self.turn_id,
                    }})
                except (OSError, ValueError):
                    pass
            self._pending.clear()
            for record in self._records:
                if record["status"] == "pending":
                    record["status"] = "expired"
            try:
                if self._records:
                    self.on_approvals(copy.deepcopy(self._records))
            finally:
                self.finished.set()
                try:
                    self.writer.close()
                except (OSError, ValueError):
                    pass

    def _display(self, value: Any) -> tuple[str, bool]:
        """Redact display text and signal when review would hide part of an action."""
        text = self.redact(str(value or ""))
        return text[:DISPLAY_LIMIT], len(text) > DISPLAY_LIMIT

    def _publish_approval(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        """Expose only a request for the active thread and turn, with an opaque UI ID."""
        if params.get("threadId") != self.thread_id or params.get("turnId") != self.turn_id:
            raise ValueError("permission request does not belong to the active exchange")
        if len(self._records) >= REQUEST_LIMIT:
            self._write({"id": request_id, "result": {"decision": "decline"}})
            self.on_event({"type": "error", "message": "Too many permission requests in this exchange; request denied."})
            return
        if request_id in self._pending.values():
            raise ValueError("duplicate live permission request")
        reason, clipped = self._display(params.get("reason"))
        record: dict[str, Any] = {
            "approval_id": uuid.uuid4().hex, "status": "pending", "reason": reason,
            "kind": "command" if method == COMMAND_APPROVAL else "file_change",
            "can_allow": True, "command": "", "cwd": "", "changes": [],
        }
        if method == COMMAND_APPROVAL:
            record["command"], truncated = self._display(params.get("command"))
            record["cwd"], folder_truncated = self._display(params.get("cwd"))
            clipped |= truncated or folder_truncated
            record["scope"] = (
                "Allow this command once outside the configured sandbox. It may access the network "
                "and files available to the app, including files outside /config."
            )
            record["can_allow"] = bool(record["command"] and record["cwd"]) and params.get("kind", "command") == "command"
            network = params.get("networkApprovalContext")
            if isinstance(network, dict):
                record["network"], _ = self._display(network.get("host"))
        else:
            record["changes"] = copy.deepcopy(self._file_changes.get(str(params.get("itemId")), []))
            record["scope"] = "Allow these file changes once, including any listed paths outside /config."
            record["can_allow"] = bool(record["changes"]) and all(
                change.get("path") and change.get("diff") for change in record["changes"])
            clipped |= any(change.get("truncated") for change in record["changes"])
        if clipped:
            record["can_allow"] = False
        if not record["can_allow"]:
            record["note"] = "The complete action is unavailable or too large to review. Deny it and ask for a smaller action."
        else:
            record["note"] = "Known credentials are hidden. This does not change your sandbox setting or approve future requests."
        self._records.append(record)
        self._pending[record["approval_id"]] = request_id
        # Persist the reviewable request before it can receive a decision.
        self.on_approvals(copy.deepcopy(self._records))

    def decide(self, approval_id: str, decision: str) -> None:
        """Apply exactly one allow/deny response to a still-live request.

        Persistence precedes the write to Codex. If either fails, the connection
        is closed so a retry cannot accidentally authorize the action twice.
        """
        if decision not in {"allow", "deny"}:
            raise ValueError("decision must be allow or deny")
        with self.lock:
            if self._closed or not self.is_active() or approval_id not in self._pending:
                raise LookupError(APPROVAL_GONE)
            record = next(item for item in self._records if item["approval_id"] == approval_id)
            if decision == "allow" and not record["can_allow"]:
                raise ValueError("The complete action must be available before it can be allowed.")
            request_id = self._pending.pop(approval_id)
            record["status"] = "allowed" if decision == "allow" else "denied"
            try:
                self.on_approvals(copy.deepcopy(self._records))
                self._write({"id": request_id, "result": {"decision": "accept" if decision == "allow" else "decline"}})
            except (OSError, ValueError):
                record["status"] = "expired"
                self.fail("Could not deliver the permission decision. The exchange was stopped.")
                raise OSError(self.error) from None

    def receive(self, message: dict[str, Any]) -> None:
        """Dispatch a server message without treating model prose as permission."""
        with self.lock:
            if self._closed:
                return
            if not isinstance(message, dict):
                raise ValueError("invalid app-server message")
            method = message.get("method")
            params = message.get("params") or {}
            if "id" in message and method:
                if not self.is_active():
                    self.close()
                elif method in {COMMAND_APPROVAL, FILE_APPROVAL}:
                    self._publish_approval(message["id"], method, params)
                elif method == "item/permissions/requestApproval":
                    # Broad turn/session grants are deliberately unsupported.
                    self._write({"id": message["id"], "result": {"permissions": {}, "scope": "turn"}})
                    self.on_event({"type": "error", "message": "Broad permission grant denied. Request a single command or file change instead."})
                elif method == "mcpServer/elicitation/request":
                    self._write({"id": message["id"], "result": {"action": "decline"}})
                else:
                    self._write({"id": message["id"], "error": {"code": -32601, "message": "Unsupported worker request"}})
                return
            if "id" in message:
                self._reply(message)
                return
            if method == "serverRequest/resolved":
                changed = False
                for approval_id, request_id in list(self._pending.items()):
                    if request_id == params.get("requestId") and params.get("threadId") == self.thread_id:
                        self._pending.pop(approval_id)
                        next(item for item in self._records if item["approval_id"] == approval_id)["status"] = "expired"
                        changed = True
                if changed:
                    self.on_approvals(copy.deepcopy(self._records))
            elif method == "turn/started":
                if params.get("threadId") == self.thread_id:
                    self.turn_id = params["turn"]["id"]
                    self.on_event({"type": "turn.started"})
            elif params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id and method in {"item/started", "item/completed"}:
                self._item(method, params["item"])
            elif method == "turn/completed" and params.get("threadId") == self.thread_id:
                turn = params["turn"]
                if turn.get("id") != self.turn_id:
                    raise ValueError("completion does not belong to the active exchange")
                self._turn_ended = True
                if turn.get("status") == "completed" and self._final_text:
                    self.on_final(self._final_text)
                    self.returncode = 0
                else:
                    self.error = self.redact(str((turn.get("error") or {}).get("message") or "Codex finished without a final answer."))
                self.close()

    def _reply(self, message: dict[str, Any]) -> None:
        """Advance initialization only after the preceding request succeeded."""
        method = self._requests.pop(message["id"], None)
        if method is None:
            return
        if message.get("error"):
            raise ValueError(f"{method}: {message['error'].get('message', 'request failed')}")
        result = message.get("result") or {}
        if method == "initialize":
            self._write({"method": "initialized"})
            self._request("thread/resume" if self.thread_params.get("threadId") else "thread/start", self.thread_params)
        elif method in {"thread/start", "thread/resume"}:
            if result.get("approvalPolicy") != "on-request" or result.get("approvalsReviewer") != "user":
                raise ValueError("Codex did not enable user approval requests")
            self.thread_id = result["thread"]["id"]
            expected = self.thread_params.get("threadId")
            if expected and self.thread_id != expected:
                raise ValueError("Codex resumed a different conversation")
            self.on_event({"type": "thread.started", "thread_id": self.thread_id})
            self._request("turn/start", {**self.turn_params, "threadId": self.thread_id})
        elif method == "turn/start":
            self.turn_id = result["turn"]["id"]

    def _item(self, method: str, item: dict[str, Any]) -> None:
        """Translate app-server items into the worker's existing activity format."""
        kind = item.get("type")
        mapped = {"agentMessage": "agent_message", "commandExecution": "command_execution",
                  "fileChange": "file_change", "mcpToolCall": "mcp_tool_call", "webSearch": "web_search"}
        if kind not in mapped and kind != "reasoning":
            # Images are collected from the rollout after completion. Do not
            # duplicate image bytes or user input in the activity log.
            return
        event_item = {**item, "type": mapped.get(kind, kind)}
        if item.get("status") == "inProgress":
            event_item["status"] = "in_progress"
        if kind == "commandExecution":
            event_item.update(aggregated_output=item.get("aggregatedOutput"), exit_code=item.get("exitCode"))
        elif kind == "fileChange":
            changes = []
            remaining = DISPLAY_LIMIT
            raw_changes = item.get("changes") or []
            for change in raw_changes[:REQUEST_LIMIT]:
                path = self.redact(str(change.get("path") or ""))
                path_clipped = len(path) > max(remaining, 0)
                path = path[:max(remaining, 0)]
                remaining -= len(path)
                diff = self.redact(str(change.get("diff") or ""))
                changes.append({"path": path, "diff": diff[:max(remaining, 0)],
                                "kind": (change.get("kind") or {}).get("type", "update"),
                                "truncated": path_clipped or len(diff) > remaining})
                remaining -= len(diff)
            if len(raw_changes) > REQUEST_LIMIT and changes:
                changes[-1]["truncated"] = True
            if method == "item/started":
                self._file_changes[str(item["id"])] = changes
            else:
                self._file_changes.pop(str(item["id"]), None)
            event_item["changes"] = [{"path": c["path"], "kind": c["kind"]} for c in changes]
        elif kind == "reasoning":
            event_item = {"id": item["id"], "type": "reasoning", "text": "\n".join(item.get("summary") or [])}
        elif kind == "agentMessage" and method == "item/completed":
            if item.get("phase") != "commentary":
                self._final_text = str(item.get("text") or "")
        self.on_event({"type": method.replace("/", "."), "item": event_item})
