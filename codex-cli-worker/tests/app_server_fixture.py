"""A deterministic subprocess fixture for the worker's JSON-RPC transport."""

import io
import json
from queue import Queue
from threading import Event


class StubAppServer:
    """Answer worker requests and emit the items of one test exchange."""

    turn_id = "fixture-turn"

    def __init__(self, session_id, final=None, *, items=(), on_turn=None, hold=False, turn_error=None):
        self.session_id = session_id
        self.final = final
        self.items = items
        self.on_turn = on_turn
        self.hold = hold
        self.turn_error = turn_error
        self.messages = Queue()
        self.started = Event()
        self.stdin = self.stdout = self
        self.stderr = io.StringIO()
        self.returncode = None
        self.closed = False

    def emit(self, **message):
        self.messages.put(json.dumps(message) + "\n")

    def write(self, line):
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            self.emit(id=message["id"], result={})
        elif method in {"thread/start", "thread/resume"}:
            sandbox = {"read-only": "readOnly", "workspace-write": "workspaceWrite", "danger-full-access": "dangerFullAccess"}
            self.emit(id=message["id"], result={
                "thread": {"id": self.session_id}, "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "sandbox": {"type": sandbox[message["params"]["sandbox"]]},
            })
        elif method == "turn/start":
            turn = {"id": self.turn_id}
            self.emit(id=message["id"], result={"turn": turn})
            self.emit(method="turn/started", params={"threadId": self.session_id, "turn": turn})
            if self.on_turn:
                self.on_turn()
            self.started.set()
            if self.hold:
                return
            context = {"threadId": self.session_id, "turnId": self.turn_id}
            for item in self.items:
                self.emit(method="item/completed", params={**context, "item": item})
            if self.final is not None:
                self.emit(method="item/completed", params={**context, "item": {
                    "id": "final", "type": "agentMessage", "phase": "final_answer", "text": json.dumps(self.final),
                }})
            outcome = {**turn, "status": "completed"}
            if self.turn_error:
                outcome.update(status="failed", error={"message": self.turn_error})
            self.emit(method="turn/completed", params={"threadId": self.session_id, "turn": outcome})

    def readline(self):
        return self.messages.get(timeout=10)

    def flush(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self.messages.put("")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.close()

    kill = terminate
