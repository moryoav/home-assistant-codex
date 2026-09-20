"""Local browser fixture. No Codex process or Home Assistant request is made."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple
from werkzeug.wrappers import Response


def main():
    spec = importlib.util.spec_from_file_location("web_fixture_server", Path(__file__).resolve().parents[1] / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    with tempfile.TemporaryDirectory(prefix="codex-chat-preview-") as temp:
        root = Path(temp)
        server.task_root = lambda: root / "tasks"
        server.TASK_STATE_FILE = root / "index.json"
        server.AGENTS_PATH = root / "AGENTS.md"
        server.CODEX_HOME = root / "codex-home"
        server.api_token = lambda: "preview-only"
        server.auth_status_payload = lambda: {"status": "authenticated", "message": "Signed in (local preview)"}
        server.fire_ha_event = lambda *args: (True, "")
        server.notify = lambda *args: None
        server.refresh_usage_status_async = lambda **kwargs: None
        session_id = "019fc242-910a-7c92-a17d-54c014e19fc4"
        sessions = server.CODEX_HOME / "sessions"
        sessions.mkdir(parents=True)
        (sessions / f"rollout-preview-{session_id}.jsonl").write_text("{}\n")

        def finish(task_id, prompt, session_id=session_id, reply=None):
            server.update_task(task_id, status="completed", session_id=session_id,
                               summary="Preview response: " + (reply or prompt), details="", question="")
            server.active_task_runners.discard(task_id)

        server.start_background_task = finish
        examples = [
            ("A quieter evening routine", "Review my evening lighting automation. Suggest improvements before making changes.",
             "Your evening routine looks good. There are two small changes that would make it easier to maintain.",
             "1. Use a single scene for the living room lights so brightness and color stay together.\n\n2. Add a condition that checks whether anyone is home before the routine runs.\n\nThe automation currently triggers at sunset. A 20-minute offset would keep the lights from coming on too early."),
            ("Check the energy dashboard", "Check my energy dashboard configuration.", "The dashboard configuration is valid.", "The electricity sensors are reporting the expected units."),
            ("Kitchen motion lights", "Review the kitchen motion sensor.", "The motion sensor is available.", ""),
        ]
        for index in range(25):
            title, message, summary, details = examples[index % 3]
            turn = server.new_turn(message)
            server.update_task(f"preview-{index:02}", title=title if index < 3 else f"Earlier chat {index}",
                               prompt=message, created_at=f"2026-09-{20 - index % 19:02}T10:00:00+00:00",
                               turns=[turn], current_turn_id=turn["turn_id"], status="completed",
                               session_id=session_id, summary=summary, details=details, question="")
            server.tasks[f"preview-{index:02}"]["updated_at"] = f"2026-09-{20 - index % 19:02}T10:00:00+00:00"
        # A prefix checks that every browser URL works under Home Assistant Ingress.
        mounted = DispatcherMiddleware(Response("Open /preview/", status=404), {"/preview": server.app})

        def ingress(environ, start_response):
            environ["HTTP_X_INGRESS_PATH"] = "/preview"
            environ["REMOTE_ADDR"] = server.INGRESS_PROXY_IP
            return mounted(environ, start_response)

        run_simple("127.0.0.1", 9137, ingress, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
