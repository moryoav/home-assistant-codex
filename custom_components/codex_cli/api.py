"""API client for the Codex CLI Worker add-on."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientError, ClientResponseError, ClientSession


class CodexCliApiError(Exception):
    """Raised when the Codex CLI Worker API fails."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CodexCliAuthError(CodexCliApiError):
    """Raised when the Codex CLI Worker rejects authentication."""


class CodexCliApiClient:
    """Small async client for the local Codex CLI Worker."""

    def __init__(self, session: ClientSession, base_url: str, api_token: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token

    @property
    def base_url(self) -> str:
        """Return the configured worker URL."""
        return self._base_url

    async def health(self) -> dict[str, Any]:
        """Fetch unauthenticated worker health."""
        return await self._request("GET", "/health", auth=False)

    async def status(self) -> dict[str, Any]:
        """Fetch authenticated worker status."""
        return await self._request("GET", "/status")

    async def start_login(self, force: bool = False) -> dict[str, Any]:
        """Start the Codex device-code login flow."""
        return await self._request("POST", "/auth/start", json={"force": force})

    async def login_status(self) -> dict[str, Any]:
        """Fetch Codex login flow status."""
        return await self._request("GET", "/auth/status")

    async def logout(self) -> dict[str, Any]:
        """Remove saved Codex CLI credentials from the worker."""
        return await self._request("POST", "/auth/logout")

    async def list_tasks(self, **filters: Any) -> dict[str, Any]:
        """List known tasks."""
        query = urlencode({key: str(value).lower() if isinstance(value, bool) else value for key, value in filters.items()})
        return await self._request("GET", "/tasks" + ("?" + query if query else ""))

    async def start_task(self, prompt: str) -> dict[str, Any]:
        """Start a Codex task."""
        return await self._request("POST", "/tasks", json={"prompt": prompt})

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch one task."""
        return await self._request("GET", f"/tasks/{task_id}")

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel a task."""
        return await self._request("POST", f"/tasks/{task_id}/cancel")

    async def reply_task(self, task_id: str, reply: str) -> dict[str, Any]:
        """Reply to a waiting Codex task."""
        return await self._request("POST", f"/tasks/{task_id}/reply", json={"reply": reply})

    async def continue_task(self, task_id: str, message: str) -> dict[str, Any]:
        """Continue a saved conversation."""
        return await self._request("POST", f"/tasks/{task_id}/continue", json={"message": message})

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {}
        if auth and self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=json,
                timeout=30,
            ) as response:
                if response.status >= 400 and response.status not in (401, 403):
                    try:
                        error = await response.json(content_type=None)
                    except (ValueError, ClientError):
                        error = {}
                    detail = error.get("error") if isinstance(error, dict) else None
                    raise CodexCliApiError(str(detail or f"Worker returned HTTP {response.status}"), status=response.status)
                response.raise_for_status()
                data = await response.json(content_type=None)
        except ClientResponseError as exc:
            if exc.status in (401, 403):
                raise CodexCliAuthError(
                    "Worker authentication failed",
                    status=exc.status,
                ) from exc
            raise CodexCliApiError(
                f"Worker returned HTTP {exc.status}",
                status=exc.status,
            ) from exc
        except ClientError as exc:
            raise CodexCliApiError(f"Worker request failed: {exc}") from exc
        except TimeoutError as exc:
            raise CodexCliApiError("Worker request timed out") from exc
        except ValueError as exc:
            raise CodexCliApiError("Worker returned invalid JSON") from exc

        if isinstance(data, dict):
            return data
        raise CodexCliApiError("Worker returned a non-object JSON response")
