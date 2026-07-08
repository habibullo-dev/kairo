"""GoogleClient: the authenticated HTTP seam under the calendar/gmail/drive adapters.

Holds a :class:`TokenStore` and attaches a bearer token to each request. On a 401 it forces a
single token refresh and retries exactly once (a token can be revoked/rotated before its
recorded expiry); a second 401 is a hard :class:`ConnectorAuthError` (reconnect) — never a
loop. 403/429/5xx become a friendly :class:`ConnectorError` that never carries the provider's
response body. The adapters pass fixed endpoint URLs (module constants) — this client, like the
adapters, never takes a model-supplied URL.
"""

from __future__ import annotations

from typing import Any

import httpx

from jarvis.connectors.base import ConnectorAuthError, ConnectorError
from jarvis.connectors.tokens import TokenStore
from jarvis.observability import get_logger

_log = get_logger("jarvis.connectors.google")


class GoogleClient:
    def __init__(
        self, tokens: TokenStore, *, http: Any = None, timeout_seconds: float = 20.0
    ) -> None:
        self._tokens = tokens
        self._http = http
        self._timeout = timeout_seconds

    def status(self) -> dict[str, Any]:
        return self._tokens.status()

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        resp = await self._request("GET", url, params=params)
        return resp.json()

    async def post_json(self, url: str, *, json_body: dict, params: dict | None = None) -> dict:
        resp = await self._request("POST", url, params=params, json_body=json_body)
        return resp.json()

    async def patch_json(self, url: str, *, json_body: dict, params: dict | None = None) -> dict:
        resp = await self._request("PATCH", url, params=params, json_body=json_body)
        return resp.json()

    async def delete(self, url: str, *, params: dict | None = None) -> None:
        """DELETE (e.g. cancel a calendar event). A 2xx/204 carries no body, so nothing is
        parsed — the 401→refresh→retry-once and typed-4xx handling still apply via ``_request``."""
        await self._request("DELETE", url, params=params)

    async def get_text(self, url: str, *, params: dict | None = None) -> str:
        resp = await self._request("GET", url, params=params)
        return resp.text

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        token = await self._tokens.access_token()
        resp = await self._send(method, url, token, params, json_body)
        if resp.status_code == 401:
            # Token invalid before its recorded expiry — force one refresh and retry once.
            token = await self._tokens.force_refresh()
            resp = await self._send(method, url, token, params, json_body)
        return self._checked(resp)

    async def _send(
        self,
        method: str,
        url: str,
        token: str,
        params: dict | None,
        json_body: dict | None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"}
        if self._http is not None:
            return await self._http.request(
                method, url, params=params, json=json_body, headers=headers
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, url, params=params, json=json_body, headers=headers)

    def _checked(self, resp: httpx.Response) -> httpx.Response:
        if resp.status_code == 401:
            raise ConnectorAuthError("google")  # still unauthorized after a refresh + retry
        if resp.status_code >= 400:
            # Log the status for diagnosis; never surface the provider's body (it can echo
            # addresses/content). The friendly message is all the caller/UI sees.
            _log.warning("google_api_error", status=resp.status_code)
            raise ConnectorError(
                "google",
                user_message=f"Google API request failed (HTTP {resp.status_code}).",
            )
        return resp
