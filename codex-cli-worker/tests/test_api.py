"""Home Assistant client contracts without a running Home Assistant instance."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

API_PATH = Path(__file__).resolve().parents[2] / "custom_components" / "codex_cli" / "api.py"
SPEC = importlib.util.spec_from_file_location("codex_api_test", API_PATH)
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.response = MagicMock()
        self.response.status = 200
        self.response.json = AsyncMock(return_value={"ok": True})
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=self.response)
        context.__aexit__ = AsyncMock(return_value=False)
        self.session = MagicMock()
        self.session.request.return_value = context
        self.client = api.CodexCliApiClient(self.session, "http://worker", "test-token")

    async def test_list_filters_are_encoded_and_legacy_url_stays_unchanged(self):
        await self.client.list_tasks()
        self.assertEqual(self.session.request.call_args.args, ("GET", "http://worker/tasks"))
        await self.client.list_tasks(limit=10, offset=20, order="updated_desc", summary=True)
        self.assertEqual(self.session.request.call_args.args, ("GET", "http://worker/tasks?limit=10&offset=20&order=updated_desc&summary=true"))

    async def test_continue_and_legacy_reply_use_correct_payloads(self):
        await self.client.continue_task("chat", "More detail")
        self.assertEqual(self.session.request.call_args.args, ("POST", "http://worker/tasks/chat/continue"))
        self.assertEqual(self.session.request.call_args.kwargs["json"], {"message": "More detail"})
        await self.client.reply_task("chat", "Yes")
        self.assertEqual(self.session.request.call_args.args, ("POST", "http://worker/tasks/chat/reply"))
        self.assertEqual(self.session.request.call_args.kwargs["json"], {"reply": "Yes"})

    async def test_continuation_error_is_actionable(self):
        self.response.status = 409
        self.response.json.return_value = {"ok": False, "error": "The saved Codex session is unavailable."}
        with self.assertRaisesRegex(api.CodexCliApiError, "saved Codex session") as raised:
            await self.client.continue_task("chat", "More")
        self.assertEqual(raised.exception.status, 409)

    async def test_non_json_http_error_keeps_status(self):
        self.response.status = 502
        self.response.json.side_effect = ValueError("not JSON")
        with self.assertRaisesRegex(api.CodexCliApiError, "HTTP 502"):
            await self.client.list_tasks()
