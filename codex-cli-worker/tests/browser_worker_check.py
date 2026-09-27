"""Exercise the real worker capture, memory guard, and attachment storage in isolation."""
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server
from verification import Verification


def capture(payload):
    with tempfile.TemporaryDirectory(prefix="worker-browser-test-") as directory, ExitStack() as stack:
        root = Path(directory)
        config = root / "config"
        config.mkdir()
        options = {**server.DEFAULT_OPTIONS, "task_root": str(root / "tasks")}
        for key, value in {"CONFIG_ROOT": config, "DATA_ROOT": root,
                           "TASK_STATE_FILE": root / "index.json", "tasks": {}}.items():
            stack.enter_context(patch.object(server, key, value))
        stack.enter_context(patch.object(server, "read_options", return_value=options))
        turn = server.new_turn("Browser verification fixture")
        task_id = "fixture"
        server.tasks[task_id] = {"task_id": task_id, "status": "running",
                                 "current_turn_id": turn["turn_id"], "turns": [turn]}
        server.get_task_dir(task_id).mkdir(parents=True)
        engine = Verification(server)
        engine.begin(task_id)
        with patch.object(engine, "core", return_value=payload) as core:
            result = engine.run(task_id, {"operation": "dashboard", "path": payload["path"]})
        core.assert_called_with("DELETE", "codex_cli/browser_session", json={"session_id": payload["session_id"]})
        images = server.tasks[task_id]["verification_attachments"]
        result["stored_images"] = [{"viewport": item["viewport"],
                                    "size": (server.get_task_dir(task_id) / item["path"]).stat().st_size}
                                   for item in images]
        assert len(images) == len(result.get("attachments", []))
        return result


if __name__ == "__main__":
    print(json.dumps(capture(json.load(sys.stdin))))
