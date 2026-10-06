"""What the worker reports about the Codex integration: installed, recent enough, and in touch with the worker."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_server import server


LONG_AGO = 10 * server.INTEGRATION_CONTACT_SECONDS


class IntegrationStatusTests(unittest.TestCase):
    """The state of the Codex integration, from its files in /config and its calls to the worker."""

    def setUp(self):
        """Point the worker at an empty configuration folder, started long ago and never called by the integration."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.manifest = root / "custom_components" / "codex_cli" / "manifest.json"
        self.stack.enter_context(patch.object(server, "INTEGRATION_MANIFEST_PATH", self.manifest))
        self.stack.enter_context(
            patch.dict(server.integration_state, {"started": time.monotonic() - LONG_AGO, "seen": None})
        )
        self.stack.enter_context(patch.object(server, "api_token", return_value="test-token"))
        self.stack.enter_context(patch.object(server, "refresh_usage_status_async"))
        self.client = server.app.test_client()

    def install(self, version=server.MIN_INTEGRATION_VERSION, text=None):
        """Write the integration's manifest with the given version, or with the given text as it is."""
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(
            json.dumps({"domain": "codex_cli", "version": version}) if text is None else text, encoding="utf-8"
        )

    def called(self, seconds_ago=0.0):
        """Record a call from the integration the given number of seconds ago."""
        server.integration_state["seen"] = time.monotonic() - seconds_ago

    def state(self):
        """Return the state the worker reports for the integration."""
        return server.integration_status()["state"]

    def test_without_the_files_the_integration_is_not_installed(self):
        """No manifest in /config means not installed, right away, even while the worker is still starting."""
        server.integration_state["started"] = time.monotonic()
        self.assertEqual(
            server.integration_status(),
            {"state": "not_installed", "version": "", "minimum_version": server.MIN_INTEGRATION_VERSION},
        )
        # A folder without a manifest is not an installed integration either.
        self.manifest.parent.mkdir(parents=True)
        self.assertEqual(self.state(), "not_installed")

    def test_an_installed_integration_that_calls_is_ok(self):
        """With the files at the minimum version and a recent call, nothing is wrong."""
        self.install()
        self.called()
        self.assertEqual(
            server.integration_status(),
            {
                "state": "ok",
                "version": server.MIN_INTEGRATION_VERSION,
                "minimum_version": server.MIN_INTEGRATION_VERSION,
            },
        )

    def test_an_installed_integration_that_does_not_call_is_not_connected(self):
        """Files without a call, or with a call too long ago, mean installed but not connected."""
        self.install()
        self.assertEqual(self.state(), "not_connected")
        self.called(server.INTEGRATION_CONTACT_SECONDS + 5)
        self.assertEqual(self.state(), "not_connected")
        self.called(server.INTEGRATION_CONTACT_SECONDS - 5)
        self.assertEqual(self.state(), "ok")

    def test_a_worker_that_just_started_waits_for_the_integration(self):
        """Right after the worker starts, a missing call is not reported yet: the integration may still be finding it."""
        self.install()
        server.integration_state["started"] = time.monotonic() - (server.INTEGRATION_CONTACT_SECONDS - 5)
        self.assertEqual(self.state(), "starting")
        server.integration_state["started"] = time.monotonic() - (server.INTEGRATION_CONTACT_SECONDS + 5)
        self.assertEqual(self.state(), "not_connected")

    def test_an_integration_older_than_the_minimum_is_outdated(self):
        """A version below the minimum is outdated even while the integration calls; versions compare as numbers."""
        self.called()
        with patch.object(server, "MIN_INTEGRATION_VERSION", "0.1.69"):
            for version, expected in (
                ("0.1.68", "outdated"),
                ("0.0.99", "outdated"),
                ("0.1", "outdated"),
                ("0.1.69", "ok"),
                ("0.1.70", "ok"),
                # As text, "0.1.100" sorts before "0.1.69".
                ("0.1.100", "ok"),
                ("0.2.0", "ok"),
                ("1.0", "ok"),
            ):
                with self.subTest(version=version):
                    self.install(version)
                    status = server.integration_status()
                    self.assertEqual(status["state"], expected)
                    self.assertEqual(status["version"], version)
                    self.assertEqual(status["minimum_version"], "0.1.69")

    def test_an_outdated_integration_is_reported_before_a_missing_call(self):
        """An old integration that does not call is outdated: updating it comes first."""
        self.install("0.0.1")
        self.assertEqual(self.state(), "outdated")
        server.integration_state["started"] = time.monotonic()
        self.assertEqual(self.state(), "outdated")

    def test_a_version_that_cannot_be_read_is_not_outdated(self):
        """A manifest that is damaged, or whose version is not plain numbers, is installed and judged by its calls."""
        for text in (
            "{not json",
            "[]",
            json.dumps({"domain": "codex_cli"}),
            json.dumps({"version": 70}),
            json.dumps({"version": ""}),
            json.dumps({"version": "0.1.70b1"}),
            json.dumps({"version": "main"}),
        ):
            with self.subTest(manifest=text):
                self.install(text=text)
                server.integration_state["seen"] = None
                self.assertEqual(self.state(), "not_connected")
                self.called()
                self.assertEqual(self.state(), "ok")

    def test_version_tuple_reads_dotted_numbers_only(self):
        """Dotted numbers become a tuple; anything else has no version."""
        self.assertEqual(server.version_tuple("0.1.69"), (0, 1, 69))
        self.assertEqual(server.version_tuple(" 2026.10.1\n"), (2026, 10, 1))
        self.assertEqual(server.version_tuple("7"), (7,))
        # The comparison relies on the worker's own minimum being one.
        self.assertIsNotNone(server.version_tuple(server.MIN_INTEGRATION_VERSION))
        # The last text value is made of digits from another script.
        for value in ("", "0.1.", ".1", "0..1", "0.1.70b1", "v0.1.70", "0.1.70-dev", "٠.١", None, 70, ["0.1.70"]):
            with self.subTest(value=value):
                self.assertIsNone(server.version_tuple(value))

    def test_only_a_call_with_the_token_counts_as_the_integration(self):
        """The web UI reads the status through Ingress, which must not count as a call from the integration."""
        self.install()
        ingress = {
            "headers": {"X-Ingress-Path": "/api/hassio_ingress/test"},
            "environ_overrides": {"REMOTE_ADDR": server.INGRESS_PROXY_IP},
        }
        response = self.client.get("/status", **ingress)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["integration"]["state"], "not_connected")
        self.assertIsNone(server.integration_state["seen"])

        # A wrong token is refused and does not count either.
        self.assertEqual(self.client.get("/status", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertIsNone(server.integration_state["seen"])

        # The integration's own call counts, and the web UI sees it on its next read.
        response = self.client.get("/status", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(response.json["integration"]["state"], "ok")
        self.assertEqual(self.client.get("/status", **ingress).json["integration"]["state"], "ok")

    def test_any_authenticated_route_counts_as_a_call(self):
        """The integration is in touch whichever route it calls, such as listing tasks for an action."""
        self.install()
        with patch.object(server, "tasks", {}):
            self.assertEqual(self.client.get("/tasks", headers={"Authorization": "Bearer test-token"}).status_code, 200)
        self.assertEqual(self.state(), "ok")


if __name__ == "__main__":
    unittest.main()
