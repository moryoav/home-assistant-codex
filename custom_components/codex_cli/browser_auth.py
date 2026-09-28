"""Issue bounded browser sessions to the paired worker, without user credentials."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from datetime import timedelta
from functools import partial
from typing import Any

from aiohttp import web

from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import get_url, get_supervisor_network_url
from homeassistant.helpers.storage import Store
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

SESSION_SECONDS = 180
STORAGE_KEY = f"{DOMAIN}.browser_identity"


def browser_resources(hass: HomeAssistant) -> dict[str, list[str]]:
    """Describe static routes, never arbitrary GET views or filesystem paths."""
    files: set[str] = set()
    directories: set[str] = set()
    if hass.http is not None:
        for resource in hass.http.app.router.resources():
            if isinstance(resource, web.StaticResource):
                directories.add(resource.canonical)
                continue
            for route in resource:
                handler = route.handler
                # HA registers individual static files as partials of these
                # serving functions, moved to http.server in newer Core releases.
                # A .js-looking URL alone proves nothing.
                if (route.method == "GET" and isinstance(handler, partial)
                    and getattr(handler.func, "__module__", "") in {
                        "homeassistant.components.http", "homeassistant.components.http.server",
                    }
                    and getattr(handler.func, "__name__", "") in {"_serve_file", "_serve_file_with_cache_headers"}):
                    files.add(resource.canonical)
    extra_urls = set()
    for key in ("frontend_extra_module_url", "frontend_extra_js_url_es5"):
        extra_urls.update(getattr(hass.data.get(key), "urls", ()))
    result = {}
    remaining = 32000
    for key, values in (("files", files), ("directories", directories), ("extra_urls", extra_urls)):
        selected = []
        for value in sorted(value for value in values if isinstance(value, str))[:256]:
            size = len(json.dumps(value).encode("utf-8"))
            if value != "/" and 0 < len(value.encode("utf-8")) <= 1024 and size <= remaining:
                selected.append(value)
                remaining -= size
        result[key] = selected
    return result


class BrowserSessions:
    """Keep renewal credentials in Core and revoke every issued session."""

    def __init__(self, hass: HomeAssistant, worker_token: str) -> None:
        self.hass = hass
        self.worker_token = worker_token
        self.user = None
        self.sessions: dict[str, tuple[Any, asyncio.TimerHandle]] = {}
        self.lock = asyncio.Lock()
        self.closed = False

    async def setup(self) -> None:
        store = Store(self.hass, 1, STORAGE_KEY)
        stored = await store.async_load() or {}
        user = await self.hass.auth.async_get_user(stored.get("user_id", ""))
        # Never adopt or change an existing human account.
        if user is not None and not user.system_generated:
            raise ValueError("Browser identity is not a system user")
        if user is None:
            user = await self.hass.auth.async_create_system_user(
                "Dashboard verification", group_ids=[GROUP_ID_READ_ONLY], local_only=True
            )
            await store.async_save({"user_id": user.id})
        await self.hass.auth.async_update_user(
            user, group_ids=[GROUP_ID_READ_ONLY], local_only=True, is_active=True
        )
        # Remove credentials left behind by an interrupted Core process.
        for token in list(user.refresh_tokens.values()):
            self.hass.auth.async_remove_refresh_token(token)
        self.user = user

    async def issue(self) -> dict[str, Any]:
        async with self.lock:
            if self.closed or self.user is None:
                raise web.HTTPServiceUnavailable()
            if self.sessions:
                raise web.HTTPTooManyRequests(reason="A verification browser is already active")
            # Direct Core connection: the Supervisor API proxy is not a frontend.
            url = get_supervisor_network_url(self.hass) or get_url(
                self.hass, allow_external=False, allow_cloud=False
            )
            resources = browser_resources(self.hass)
            refresh = await self.hass.auth.async_create_refresh_token(
                self.user, access_token_expiration=timedelta(seconds=SESSION_SECONDS)
            )
            if self.closed:
                self.hass.auth.async_remove_refresh_token(refresh)
                raise web.HTTPServiceUnavailable()
            try:
                access = self.hass.auth.async_create_access_token(refresh)
            except Exception:
                self.hass.auth.async_remove_refresh_token(refresh)
                raise
            timer = self.hass.loop.call_later(SESSION_SECONDS, self.revoke, refresh.id)
            self.sessions[refresh.id] = (refresh, timer)
            return {"session_id": refresh.id, "access_token": access,
                    "expires_in": SESSION_SECONDS, "url": url,
                    "resources": resources}

    def revoke(self, session_id: str) -> None:
        if session := self.sessions.pop(session_id, None):
            refresh, timer = session
            timer.cancel()
            self.hass.auth.async_remove_refresh_token(refresh)

    def close(self) -> None:
        self.closed = True
        for session_id in list(self.sessions):
            self.revoke(session_id)


class BrowserSessionView(HomeAssistantView):
    """Require both HA administrator authentication and the paired worker secret."""

    url = "/api/codex_cli/browser_session"
    name = "api:codex_cli:browser_session"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def broker(self, request: web.Request, data: dict[str, Any]) -> BrowserSessions:
        broker = self.hass.data.get(DOMAIN, {}).get("browser_sessions")
        if broker is None or broker.closed:
            raise web.HTTPServiceUnavailable()
        supplied = data.get("worker_token", "")
        if not isinstance(supplied, str) or not request[KEY_HASS_USER].is_admin or not hmac.compare_digest(
            supplied.encode(), broker.worker_token.encode()
        ):
            raise web.HTTPForbidden()
        return broker

    async def post(self, request: web.Request) -> web.Response:
        data = await self.payload(request)
        result = await self.broker(request, data).issue()
        return self.json(result, headers={"Cache-Control": "no-store"})

    async def delete(self, request: web.Request) -> web.Response:
        data = await self.payload(request)
        broker = self.broker(request, data)
        if not isinstance(data, dict) or not isinstance(data.get("session_id"), str):
            raise web.HTTPBadRequest()
        broker.revoke(data["session_id"])
        return self.json({"ok": True})

    async def payload(self, request: web.Request) -> dict[str, Any]:
        # The Supervisor proxy only forwards selected headers, so pairing travels
        # in the authenticated JSON body, never a URL/query string.
        if request.content_length is not None and request.content_length > 4096:
            raise web.HTTPBadRequest()
        content = bytearray()
        async for chunk in request.content.iter_chunked(1024):
            content.extend(chunk)
            if len(content) > 4096:
                raise web.HTTPBadRequest()
        try:
            data = json.loads(content)
        except (ValueError, UnicodeError) as exc:
            raise web.HTTPBadRequest() from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest()
        return data


class DiagnosticView(BrowserSessionView):
    """Allow the paired worker to read bounded Core logs, never other apps' logs."""

    url = "/api/codex_cli/diagnostic_logs"
    name = "api:codex_cli:diagnostic_logs"

    async def post(self, request: web.Request) -> web.Response:
        data = await self.payload(request)
        self.broker(request, data)
        target = data.get("target") if isinstance(data, dict) else None
        if target != "core":
            raise web.HTTPBadRequest()
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise web.HTTPServiceUnavailable()
        async with async_get_clientsession(self.hass).get(
            "http://supervisor/core/logs", params={"lines": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15, allow_redirects=False,
        ) as response:
            if response.status != 200:
                return self.json({"error": f"Log read returned HTTP {response.status}"}, status_code=502)
            content = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                content.extend(chunk)
                if len(content) > 32768:
                    break
        # Final redaction is applied by the worker before persistence or model access.
        return self.json({"text": bytes(content[:32768]).decode("utf-8", "replace"),
                          "truncated": len(content) > 32768}, headers={"Cache-Control": "no-store"})

    async def delete(self, request: web.Request) -> web.Response:
        raise web.HTTPMethodNotAllowed("DELETE", ["POST"])
