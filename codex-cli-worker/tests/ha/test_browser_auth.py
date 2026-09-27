"""Run against real Home Assistant auth, not a mock token implementation."""
import asyncio
import json
import os
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch
import io

import pytest
from aiohttp import FormData, web
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.setup import async_setup_component

ROOT = Path(__file__).resolve().parents[3] / "custom_components" / "codex_cli"
package = types.ModuleType("verification_ha_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("verification_ha_test.browser_auth", ROOT / "browser_auth.py")
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


async def test_real_read_only_identity_lease_and_revocation(hass):
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    assert broker.user.system_generated
    assert broker.user.local_only
    assert not broker.user.is_admin
    assert [group.id for group in broker.user.groups] == [GROUP_ID_READ_ONLY]
    with patch.object(auth, "get_supervisor_network_url", return_value="http://homeassistant:8123"):
        session = await broker.issue()
        refresh = hass.auth.async_validate_access_token(session["access_token"])
        assert refresh.user.id == broker.user.id
        with pytest.raises(web.HTTPTooManyRequests):
            await broker.issue()
    broker.revoke(session["session_id"])
    assert hass.auth.async_validate_access_token(session["access_token"]) is None
    assert not broker.user.refresh_tokens
    broker.close()


async def test_reload_revokes_orphaned_credentials_and_reuses_identity(hass):
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    with patch.object(auth, "get_supervisor_network_url", return_value="http://homeassistant:8123"):
        session = await broker.issue()
    replacement = auth.BrowserSessions(hass, "paired-worker")
    await replacement.setup()
    assert replacement.user.id == broker.user.id
    assert hass.auth.async_validate_access_token(session["access_token"]) is None
    broker.close()
    replacement.close()


async def test_auto_expiry_and_unload(hass):
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    with patch.object(auth, "get_supervisor_network_url", return_value="http://homeassistant:8123"), patch.object(auth, "SESSION_SECONDS", 0.01):
        session = await broker.issue()
        await asyncio.sleep(0.03)
    assert not broker.sessions
    assert not broker.user.refresh_tokens
    broker.close()
    with pytest.raises(web.HTTPServiceUnavailable):
        await broker.issue()


async def test_unload_during_credential_creation_revokes_the_new_credential(hass):
    """Unloading while HA awaits credential persistence must not leave a lease."""
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    create = hass.auth.async_create_refresh_token
    async def interrupted(*args, **kwargs):
        token = await create(*args, **kwargs)
        broker.close()
        return token
    with patch.object(auth, "get_supervisor_network_url", return_value="http://homeassistant:8123"), \
         patch.object(hass.auth, "async_create_refresh_token", side_effect=interrupted):
        with pytest.raises(web.HTTPServiceUnavailable):
            await broker.issue()
    assert not broker.sessions
    assert not broker.user.refresh_tokens


async def test_broker_rejects_admin_without_pairing_and_read_only_user(hass):
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    hass.data.setdefault(auth.DOMAIN, {})["browser_sessions"] = broker
    view = auth.BrowserSessionView(hass)
    class Request(dict):
        headers = {"X-Verification-Worker": "wrong"}
    request = Request({KEY_HASS_USER: types.SimpleNamespace(is_admin=True)})
    with pytest.raises(web.HTTPForbidden):
        view.broker(request, {"worker_token": "wrong"})
    with pytest.raises(web.HTTPForbidden):
        view.broker(request, {"worker_token": "שלום"})
    request.headers["X-Verification-Worker"] = "paired-worker"
    assert view.broker(request, {"worker_token": "paired-worker"}) is broker
    request[KEY_HASS_USER] = broker.user
    with pytest.raises(web.HTTPForbidden):
        view.broker(request, {"worker_token": "paired-worker"})
    broker.close()


async def test_session_http_requires_pairing_and_accepts_chunked_proxy_body(hass, hass_client):
    assert await async_setup_component(hass, "http", {})
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    hass.data.setdefault(auth.DOMAIN, {})["browser_sessions"] = broker
    hass.http.register_view(auth.BrowserSessionView(hass))
    client = await hass_client()
    try:
        response = await client.post(auth.BrowserSessionView.url, json={})
        assert response.status == 403
        async def chunks():
            yield b'{"worker_token":'
            yield b'"paired-worker"}'
        with patch.object(auth, "get_supervisor_network_url", return_value="http://homeassistant:8123"):
            response = await client.post(auth.BrowserSessionView.url, data=chunks(), headers={"Content-Type": "application/json"})
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        session = await response.json()
        response = await client.delete(auth.BrowserSessionView.url, json={"worker_token": "paired-worker", "session_id": session["session_id"]})
        assert response.status == 200
        assert not broker.user.refresh_tokens
        response = await client.post(auth.BrowserSessionView.url, data=b"x" * 5000)
        assert response.status == 400
    finally:
        broker.close()


async def test_read_only_session_cannot_save_dashboard(hass, hass_ws_client):
    assert await async_setup_component(hass, "lovelace", {})
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    try:
        with patch.object(auth, "get_supervisor_network_url", return_value="http://homeassistant:8123"):
            session = await broker.issue()
        client = await hass_ws_client(hass, access_token=session["access_token"])
        await client.send_json({"id": 1, "type": "lovelace/config/save", "config": {"views": []}})
        response = await client.receive_json()
        assert response["success"] is False
        assert response["error"]["code"] == "unauthorized"
        broker.revoke(session["session_id"])
        from aiohttp import WSMsgType
        closed = await asyncio.wait_for(client.receive(), timeout=2)
        assert closed.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}
    finally:
        broker.close()


async def test_browser_url_uses_internal_tls_configuration(hass):
    """Do not substitute the plain Supervisor address when Core requires TLS."""
    hass.config.api = types.SimpleNamespace(use_ssl=True, port=8123, local_ip="192.0.2.1")
    hass.config.internal_url = "https://ha.example.test:8123"
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    try:
        with patch("homeassistant.helpers.network.is_hassio", return_value=True):
            session = await broker.issue()
        assert session["url"] == "https://ha.example.test:8123"
    finally:
        broker.close()


async def test_diagnostic_endpoint_rejects_every_non_core_target(hass, hass_client):
    """Pairing must not grant access to another installed app's logs."""
    assert await async_setup_component(hass, "http", {})
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    hass.data.setdefault(auth.DOMAIN, {})["browser_sessions"] = broker
    hass.http.register_view(auth.DiagnosticView(hass))
    client = await hass_client()
    try:
        with patch.object(auth, "async_get_clientsession") as session:
            for target in ("core_mosquitto", "local_codex_cli_worker", "../core/logs", None):
                response = await client.post(auth.DiagnosticView.url, json={"worker_token": "paired-worker", "target": target})
                assert response.status == 400
            session.assert_not_called()
    finally:
        broker.close()


@pytest.mark.skipif(not os.environ.get("HA_BROWSER_NODE"), reason="Optional real frontend browser check needs Node and Chromium")
async def test_real_frontend_with_temporary_identity(async_setup_recorder_instance, hass, hass_client, tmp_path):
    """Exercise HA's shipped frontend with the same browser bridge as production."""
    hass.config.config_dir = str(tmp_path)
    from homeassistant.components.onboarding.const import STEPS
    from homeassistant.helpers.storage import Store
    await Store(hass, 4, "onboarding").async_save({"done": list(STEPS)})
    (tmp_path / "ui-lovelace.yaml").write_text("""title: Verification fixture
views:
  - title: Home
    path: home
    cards:
      - type: entities
        entities:
          - sensor.verification_fixture
          - light.kitchen
          - binary_sensor.door
          - switch.fan
          - person.fixture
      - type: tile
        entity: light.kitchen
      - type: markdown
        content: "Fixture value: {{ states('sensor.verification_fixture') }}"
      - type: glance
        entities: [binary_sensor.door, switch.fan]
""")
    configuration = {"lovelace": {"mode": "yaml"}}
    # The frontend reads recorder/info during startup. Missing it produces an
    # unhandled rejection and a delayed system_log.write service call.
    await async_setup_recorder_instance(hass)
    assert await async_setup_component(hass, "frontend", configuration)
    assert await async_setup_component(hass, "lovelace", configuration)
    for component in ("labs", "persistent_notification", "brands", "sensor", "light", "binary_sensor", "switch", "person", "image_upload"):
        assert await async_setup_component(hass, component, configuration)
    client = await hass_client()
    from PIL import Image
    picture = io.BytesIO()
    Image.new("RGB", (64, 64), "green").save(picture, format="PNG")
    picture.seek(0)
    data = FormData()
    data.add_field("file", picture, filename="fixture.png", content_type="image/png")
    upload = await client.post("/api/image/upload", data=data)
    assert upload.status == 200
    image_id = (await upload.json())["id"]
    hass.states.async_set("sensor.verification_fixture", "42", {"friendly_name": "Fixture sensor", "unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})
    hass.states.async_set("binary_sensor.door", "off", {"device_class": "door"})
    hass.states.async_set("switch.fan", "off", {})
    hass.states.async_set("person.fixture", "home", {"entity_picture": f"/api/image/serve/{image_id}/512x512"})
    broker = auth.BrowserSessions(hass, "paired-worker")
    await broker.setup()
    with patch.object(auth, "get_supervisor_network_url", return_value=str(client.make_url("/")).rstrip("/")):
        session = await broker.issue()
    try:
        # Exercise the Python memory guard and persisted attachments too, not
        # just browser.cjs. The token is the real broker's temporary identity.
        script = str(ROOT.parents[1] / "codex-cli-worker" / "tests" / "browser_worker_check.py")
        proc = await asyncio.create_subprocess_exec(sys.executable, script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps({
                **session, "path": "/lovelace/home",
            }).encode()), timeout=130)
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        assert stdout.strip(), stderr.decode()
        result = json.loads(stdout)
        print(json.dumps({"frontend_browser_result": result}))
        assert result["status"] == "captured", (result, stderr.decode())
        assert result["errors"] == [], result
        assert result["blocked"] == [], result
        assert len(result["attachments"]) == 2, result
        assert [image["viewport"] for image in result["stored_images"]] == ["desktop", "mobile"]
        assert all(image["size"] > 100 for image in result["stored_images"])
        assert 0 < result["memory_peak_mib"] <= result["memory_limit_mib"]
        assert not any("lovelace" in error or "redirected" in error for error in result["errors"]), result
    finally:
        broker.close()
