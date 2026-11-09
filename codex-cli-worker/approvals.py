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
from time import monotonic
from typing import Any, Callable, TextIO


APPROVAL_GONE = "This permission request is no longer waiting for an answer."
DISPLAY_LIMIT = 32000
REQUEST_LIMIT = 50
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
MCP_ELICITATION = "mcpServer/elicitation/request"
USER_INPUT = {"tool/requestUserInput", "item/tool/requestUserInput"}
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
MCP_APPROVAL_QUESTION_PREFIX = "mcp_tool_call_approval_"
MCP_TIMEOUT_SECONDS = 300


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
        self._full_access = thread_params.get("sandbox") == "danger-full-access"
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
        self._pending_files: dict[str, str] = {}
        self._pending_mcp: dict[str, dict[str, Any]] = {}
        self._mcp_timers: dict[str, threading.Timer] = {}
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
            for approval_id, pending in list(self._pending_mcp.items()):
                request_id = self._forget(approval_id)
                try:
                    self._write({"id": request_id, "result": pending["fallback"]})
                except (OSError, ValueError):
                    pass
            self._pending.clear()
            self._pending_files.clear()
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
        raw = str(value or "")
        text = self.redact(raw)
        return text[:DISPLAY_LIMIT], text != raw or len(text) > DISPLAY_LIMIT

    def _publish_mcp(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        """Expose bounded confirmation forms; never accept hidden or secret input."""
        delay = params.get("autoResolutionMs")
        timeout = min(delay, MCP_TIMEOUT_SECONDS * 1000) / 1000 if type(delay) is int and delay >= 0 else MCP_TIMEOUT_SECONDS
        deadline = monotonic() + timeout
        is_form = method == MCP_ELICITATION
        fallback = {"action": "decline", "content": None} if is_form else {"answers": {}}
        if (not self.turn_id or params.get("threadId") != self.thread_id or
                params.get("turnId") not in (None, self.turn_id) or
                (not is_form and params.get("turnId") != self.turn_id)):
            self._write({"id": request_id, "result": fallback})
            return
        if request_id in self._pending.values():
            raise ValueError("duplicate live permission request")
        fields = []
        supported = True
        acceptance = None
        if is_form:
            reason = params.get("message", "")
            schema = params.get("requestedSchema") or {}
            supported = (isinstance(params.get("mode"), str) and params["mode"] in {"form", "openai/form", "openaiForm"} and
                         isinstance(schema, dict) and schema.get("type") == "object" and
                         set(schema) <= {"type", "properties", "required", "$schema"})
            properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
            required = schema.get("required", []) if isinstance(schema, dict) else []
            supported = (supported and isinstance(properties, dict) and len(properties) <= 10
                         and isinstance(required, list) and len(required) <= len(properties)
                         and all(isinstance(k, str) and k in properties for k in required))
            if supported:
                for key, spec in properties.items():
                    if not isinstance(spec, dict):
                        supported = False
                        break
                    allowed = {"type", "title", "description", "default", "enum", "enumNames", "oneOf"}
                    choices = None
                    if spec.get("type") == "boolean" and set(spec) <= {"type", "title", "description", "default"}:
                        choices = [{"value": True, "label": "Yes"}, {"value": False, "label": "No"}]
                    elif (spec.get("type") == "string" and set(spec) <= allowed and
                          not ("enum" in spec and "oneOf" in spec) and
                          ("enumNames" not in spec or "enum" in spec)):
                        if isinstance(spec.get("enum"), list) and 1 <= len(spec["enum"]) <= 20:
                            choices = [{"value": v, "label": v} for v in spec["enum"] if isinstance(v, str)]
                            if len(choices) != len(spec["enum"]): choices = None
                            names = spec.get("enumNames")
                            if names is not None:
                                if choices is None or not isinstance(names, list) or len(names) != len(choices) or not all(isinstance(n, str) for n in names):
                                    choices = None
                                else:
                                    for choice, label in zip(choices, names): choice["label"] = label
                        elif isinstance(spec.get("oneOf"), list) and 1 <= len(spec["oneOf"]) <= 20:
                            options = spec["oneOf"]
                            if all(isinstance(o, dict) and set(o) == {"const", "title"} and isinstance(o["const"], str) and isinstance(o["title"], str) for o in options):
                                choices = [{"value": o["const"], "label": o["title"]} for o in options]
                    if not isinstance(key, str) or not choices or len(choices) > 20:
                        supported = False
                        break
                    fields.append({"id": key, "label": str(spec.get("title") or key),
                                   "description": str(spec.get("description") or ""),
                                   "required": key in required, "choices": choices})
            meta = params.get("_meta")
            if supported and not fields and not (isinstance(meta, dict) and meta.get("codex_requires_user_input") is True):
                acceptance = {"action": "accept", "content": {}}
        else:
            questions = params.get("questions")
            supported = isinstance(questions, list) and 1 <= len(questions) <= 3
            reason = "The tool needs an answer to continue."
            if supported:
                for q in questions:
                    if (not isinstance(q, dict) or q.get("isSecret") or
                            not isinstance(q.get("id"), str) or not q["id"] or
                            not isinstance(q.get("options"), list) or not 1 <= len(q["options"]) <= 20 or
                            not all(isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"] for o in q["options"])):
                        supported = False
                        break
                    fields.append({"id": q["id"], "label": str(q.get("question") or q.get("header") or q["id"]),
                                   "description": "", "choices": [{"value": o["label"], "label": o["label"],
                                   "description": str(o.get("description") or "")} for o in q["options"]]})
                supported &= len({f["id"] for f in fields}) == len(fields)
                # The pinned CLI's legacy tool approval uses a reserved question ID
                # derived from the tool call ID and an explicit one-time choice.
                item_id = params.get("itemId")
                if (supported and len(fields) == 1 and isinstance(item_id, str) and item_id
                        and questions[0].get("isOther") is False
                        and fields[0]["id"] == MCP_APPROVAL_QUESTION_PREFIX + item_id
                        and any(choice["value"] == "Allow" for choice in fields[0]["choices"])):
                    reason = fields[0]["label"]
                    acceptance = {"answers": {fields[0]["id"]: {"answers": ["Allow"]}}}
                    fields = []
        if acceptance is not None and self._full_access:
            self._write({"id": request_id, "result": acceptance})
            return
        if len(self._records) >= REQUEST_LIMIT:
            self._write({"id": request_id, "result": fallback})
            return
        display = {"reason": reason, "server": params.get("serverName", "Codex"), "fields": fields}
        raw = json.dumps(display, ensure_ascii=False)
        safe = self.redact(raw)
        supported &= safe == raw and len(raw) <= DISPLAY_LIMIT and bool(reason)
        approval_id = uuid.uuid4().hex
        record = {"approval_id": approval_id, "status": "pending", "kind": "mcp" if acceptance is not None else "user_input",
                  "reason": self._display(reason)[0], "server": self._display(display["server"])[0],
                  "fields": fields if supported else [], "can_allow": supported,
                  "scope": "Your response applies only to this request.",
                  "note": "Expires within five minutes." if supported else
                          "Unsupported, secret, oversized or redacted request; rejected without executing."}
        self._records.append(record)
        if not supported:
            record["status"] = "denied"
            self.on_approvals(copy.deepcopy(self._records))
            self._write({"id": request_id, "result": fallback})
            return
        self._pending[approval_id] = request_id
        self._pending_mcp[approval_id] = {
            "form": is_form, "fields": fields, "fallback": fallback, "deadline": deadline, "acceptance": acceptance,
        }
        timer = threading.Timer(max(0, deadline - monotonic()), self._expire_mcp, args=(approval_id,))
        timer.daemon = True
        self._mcp_timers[approval_id] = timer
        timer.start()
        self.on_approvals(copy.deepcopy(self._records))

    def _expire_mcp(self, approval_id: str) -> None:
        with self.lock:
            pending = self._pending_mcp.get(approval_id)
            if self._closed or pending is None:
                return
            try:
                self._settle(approval_id, "expired", pending["fallback"])
            except OSError:
                pass  # _settle already closed the connection and recorded the failure.

    def _publish_approval(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        """Expose only a request for the active thread and turn, with an opaque UI ID."""
        if not self.turn_id or params.get("threadId") != self.thread_id or params.get("turnId") != self.turn_id:
            raise ValueError("permission request does not belong to the active exchange")
        if request_id in self._pending.values():
            raise ValueError("duplicate live permission request")
        if self._full_access:
            self._write({"id": request_id, "result": {"decision": "accept"}})
            return
        if len(self._records) >= REQUEST_LIMIT:
            self._write({"id": request_id, "result": {"decision": "decline"}})
            self.on_event({"type": "error", "message": "Too many permission requests in this exchange; request denied."})
            return
        reason, _ = self._display(params.get("reason"))
        clipped = False
        # Anything but a new command, such as input typed into a running one, is shown but never allowed.
        request_kind = params.get("kind", "command") if method == COMMAND_APPROVAL else ""
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
            if request_kind == "writeStdin":
                record["scope"] = "Send this input to a command that is already running outside the configured sandbox."
            record["can_allow"] = bool(record["command"] and record["cwd"]) and request_kind == "command"
            network = params.get("networkApprovalContext")
            if isinstance(network, dict):
                record["network"], _ = self._display(network.get("host"))
        else:
            record["changes"] = copy.deepcopy(self._file_changes.get(str(params.get("itemId")), []))
            record["scope"] = "Allow these file changes once, including any listed paths outside /config."
            record["can_allow"] = bool(record["changes"]) and all(
                change.get("path") and change.get("diff") and change.get("kind") in {"add", "update", "delete"}
                for change in record["changes"])
            clipped |= any(change.get("truncated") or change.get("redacted") for change in record["changes"])
        if clipped:
            record["can_allow"] = False
        if request_kind == "writeStdin":
            record["note"] = "Input to a running command cannot be allowed here. Deny it and ask for the whole action as one command."
        elif request_kind not in ("", "command"):
            record["note"] = "This kind of request cannot be allowed here. Deny it and ask for a single command or file change."
        elif not record["can_allow"]:
            record["note"] = "The complete action is missing, too large, or redacted. Deny it and request an action without hidden details."
        else:
            record["note"] = "Known credentials are hidden. This does not change your sandbox setting or approve future requests."
        self._records.append(record)
        self._pending[record["approval_id"]] = request_id
        if method == FILE_APPROVAL:
            self._pending_files[record["approval_id"]] = str(params.get("itemId"))
        # Persist the reviewable request before it can receive a decision.
        self.on_approvals(copy.deepcopy(self._records))

    def decide(self, approval_id: str, decision: str, content: Any = None) -> None:
        """Apply exactly one allow/deny response to a still-live request.

        Persistence precedes the write to Codex. If either fails, the connection
        is closed so a retry cannot accidentally authorize the action twice.
        """
        if decision not in {"allow", "deny"}:
            raise ValueError("decision must be allow or deny")
        with self.lock:
            if self._closed or not self.is_active() or approval_id not in self._pending:
                raise LookupError(APPROVAL_GONE)
            pending = self._pending_mcp.get(approval_id)
            if pending and monotonic() >= pending["deadline"]:
                self._settle(approval_id, "expired", pending["fallback"])
                raise LookupError(APPROVAL_GONE)
            record = next(item for item in self._records if item["approval_id"] == approval_id)
            if decision == "allow" and not record["can_allow"]:
                # The note already says why this request can only be denied.
                raise ValueError(record["note"])
            result = {"decision": "accept" if decision == "allow" else "decline"}
            if pending:
                result = pending["fallback"]
                if decision == "allow":
                    fields = {field["id"]: field for field in pending["fields"]}
                    required = {key for key, field in fields.items() if field.get("required", True)}
                    if not isinstance(content, dict) or not required <= content.keys() <= fields.keys():
                        raise ValueError("Choose an answer for every required field and use only listed fields.")
                    selected = {}
                    for key, value in content.items():
                        index = next((i for i, choice in enumerate(fields[key]["choices"])
                                      if type(value) is type(choice["value"]) and value == choice["value"]), None)
                        if index is None:
                            raise ValueError("Choose a listed answer. Arbitrary values are not accepted.")
                        selected[key] = index
                    if fields:
                        record["selected_choices"] = selected
                    if pending["acceptance"] is not None:
                        result = pending["acceptance"]
                    elif pending["form"]:
                        result = {"action": "accept", "content": content}
                    else:
                        result = {"answers": {key: {"answers": [value]} for key, value in content.items()}}
            elif content is not None:
                raise ValueError("This approval does not accept form content.")
            self._settle(approval_id, "allowed" if decision == "allow" else "denied", result)

    def _forget(self, approval_id: str) -> Any:
        """Remove the live request and cancel its timer while holding the session lock."""
        request_id = self._pending.pop(approval_id)
        self._pending_files.pop(approval_id, None)
        self._pending_mcp.pop(approval_id, None)
        timer = self._mcp_timers.pop(approval_id, None)
        if timer:
            timer.cancel()
        return request_id

    def _settle(self, approval_id: str, status: str, result: dict[str, Any]) -> None:
        request_id = self._forget(approval_id)
        record = next(item for item in self._records if item["approval_id"] == approval_id)
        record["status"] = status
        try:
            self.on_approvals(copy.deepcopy(self._records))
            self._write({"id": request_id, "result": result})
        except (OSError, ValueError):
            record["status"] = "expired"
            record.pop("selected_choices", None)
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
                elif method == PERMISSIONS_APPROVAL:
                    permissions = {}
                    if self._full_access:
                        if (not self.turn_id or params.get("threadId") != self.thread_id
                                or params.get("turnId") != self.turn_id
                                or not isinstance(params.get("permissions"), dict)):
                            raise ValueError("permission request does not belong to the active exchange")
                        permissions = params["permissions"]
                    else:
                        self.on_event({"type": "error", "message": "Broad permission grant denied. Request a single command or file change instead."})
                    self._write({"id": message["id"], "result": {"permissions": permissions, "scope": "turn"}})
                elif method == MCP_ELICITATION or method in USER_INPUT:
                    self._publish_mcp(message["id"], method, params)
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
                        self._forget(approval_id)
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
            elif method == "item/fileChange/patchUpdated" and params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id:
                self._cache_file_changes(str(params["itemId"]), params.get("changes") or [])
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
            if self._full_access and (result.get("sandbox") or {}).get("type") != "dangerFullAccess":
                raise ValueError("Codex did not enable Full access")
            self.thread_id = result["thread"]["id"]
            expected = self.thread_params.get("threadId")
            if expected and self.thread_id != expected:
                raise ValueError("Codex resumed a different conversation")
            self.on_event({"type": "thread.started", "thread_id": self.thread_id})
            self._request("turn/start", {**self.turn_params, "threadId": self.thread_id})
        elif method == "turn/start":
            self.turn_id = result["turn"]["id"]

    def _cache_file_changes(self, item_id: str, raw_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bound and redact complete file actions, refusing to replace a pending review."""
        changes = []
        remaining = DISPLAY_LIMIT
        for change in raw_changes[:REQUEST_LIMIT]:
            kind = change.get("kind") or {}
            entry: dict[str, Any] = {"kind": str(kind.get("type") or "unknown"), "truncated": False, "redacted": False}
            for key, raw in (("path", change.get("path")), ("move_path", kind.get("move_path")), ("diff", change.get("diff"))):
                raw = str(raw or "")
                text = self.redact(raw)
                entry["redacted"] |= text != raw
                entry["truncated"] |= len(text) > max(remaining, 0)
                entry[key] = text[:max(remaining, 0)]
                remaining -= len(text)
            changes.append(entry)
        if len(raw_changes) > REQUEST_LIMIT and changes:
            changes[-1]["truncated"] = True
        if item_id in self._pending_files.values() and self._file_changes.get(item_id) != changes:
            raise ValueError("File changes were updated while permission was pending. The exchange was stopped.")
        self._file_changes[item_id] = changes
        return changes

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
            changes = self._cache_file_changes(str(item["id"]), item.get("changes") or [])
            if method == "item/completed":
                self._file_changes.pop(str(item["id"]), None)
            event_item["changes"] = [{"path": c["path"], "kind": c["kind"], "move_path": c["move_path"]} for c in changes]
        elif kind == "reasoning":
            event_item = {"id": item["id"], "type": "reasoning", "text": "\n".join(item.get("summary") or [])}
        elif kind == "agentMessage" and method == "item/completed":
            if item.get("phase") != "commentary":
                self._final_text = str(item.get("text") or "")
        self.on_event({"type": method.replace("/", "."), "item": event_item})
