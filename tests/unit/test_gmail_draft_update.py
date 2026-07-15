"""Gmail draft edit-in-place (Phase 12 Task 6) — MockTransport, no network.

update_draft edits an existing draft (users.drafts.update, PUT) within the gmail.compose scope.
The load-bearing pin: it targets /drafts/{id} and NEVER a send path — Kira remains drafts-only.
"""

from __future__ import annotations

import base64
import datetime as _dt
import email
import json
from contextlib import asynccontextmanager
from email import policy
from pathlib import Path

import httpx

from jarvis.connectors.google import gmail
from jarvis.connectors.google.client import GoogleClient
from jarvis.connectors.oauth import OAuthProvider
from jarvis.connectors.tokens import TokenState, TokenStore

FIXED = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.UTC)
_P = OAuthProvider(
    name="google", auth_url="", token_url="https://oauth2.googleapis.com/token", scopes=("s",)
)


def _fresh() -> TokenState:
    return TokenState(
        provider="google",
        access_token="tok",
        refresh_token="rt",
        expires_at=(FIXED + _dt.timedelta(hours=1)).isoformat(),
        obtained_at=FIXED.isoformat(),
        scopes=["s"],
    )


@asynccontextmanager
async def _client(handler, tmp_path: Path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = TokenStore(
        tmp_path / "g.json", provider=_P, client_id="c", client_secret="s", http=http,
        now=lambda: FIXED,
    )
    store.save(_fresh())
    try:
        yield GoogleClient(store, http=http)
    finally:
        await http.aclose()


async def test_update_draft_puts_to_draft_and_never_sends(tmp_path: Path) -> None:
    box: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        box["method"] = req.method
        box["path"] = req.url.path
        box["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "draft-1"})

    async with _client(handler, tmp_path) as gc:
        out = await gmail.update_draft(
            gc, "draft-1", to="bob@example.com", subject="Re: hi (edited)", body="Updated body"
        )
    assert out == "draft-1"
    assert box["method"] == "PUT"
    assert box["path"].endswith("/drafts/draft-1")  # drafts.update, NOT /drafts/draft-1/send
    assert "send" not in box["path"]
    assert box["body"]["id"] == "draft-1"
    raw = box["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    parsed = email.message_from_bytes(decoded, policy=policy.default)
    assert parsed["To"] == "bob@example.com" and parsed["Subject"] == "Re: hi (edited)"
    assert "Updated body" in parsed.get_content()


async def test_update_draft_threads_when_given_thread_id(tmp_path: Path) -> None:
    box: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        box["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "draft-1"})

    async with _client(handler, tmp_path) as gc:
        await gmail.update_draft(
            gc, "draft-1", to="a@b.com", subject="S", body="B", thread_id="thread-9"
        )
    assert box["body"]["message"]["threadId"] == "thread-9"
