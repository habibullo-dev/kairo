"""Docs write adapter (Phase 12 Task 5) — MockTransport, no network.

Pins the Docs API requests: create sets only the title, batchUpdate posts the requests array to
the :batchUpdate path, get fetches the doc, and the append/replace request builders emit the
correct Docs shapes.
"""

from __future__ import annotations

import datetime as _dt
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from jarvis.connectors.base import ConnectorError
from jarvis.connectors.google import docs, drive
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


def _capture(box: dict, response: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        box["method"] = req.method
        box["path"] = req.url.path
        box["body"] = json.loads(req.content) if req.content else None
        return httpx.Response(200, json=response)

    return handler


async def test_create_document_posts_title(tmp_path: Path) -> None:
    box: dict = {}
    async with _client(_capture(box, {"documentId": "doc-1", "title": "Spec"}), tmp_path) as gc:
        out = await docs.create_document(gc, title="Spec")
    assert box["method"] == "POST"
    assert box["path"].endswith("/documents")
    assert box["body"] == {"title": "Spec"}
    assert out["documentId"] == "doc-1"


async def test_batch_update_posts_requests_to_batchupdate_path(tmp_path: Path) -> None:
    box: dict = {}
    reqs = [docs.append_text_request("Hello"), docs.replace_all_text_request("TODO", "Done")]
    async with _client(_capture(box, {"documentId": "doc-1", "replies": []}), tmp_path) as gc:
        await docs.batch_update(gc, "doc-1", reqs)
    assert box["method"] == "POST"
    assert box["path"].endswith("/documents/doc-1:batchUpdate")
    assert box["body"] == {"requests": reqs}


async def test_get_document(tmp_path: Path) -> None:
    box: dict = {}
    async with _client(_capture(box, {"documentId": "doc-1", "title": "Spec"}), tmp_path) as gc:
        out = await docs.get_document(gc, "doc-1")
    assert box["method"] == "GET" and box["path"].endswith("/documents/doc-1")
    assert out["title"] == "Spec"


def test_append_text_request_shape() -> None:
    assert docs.append_text_request("hi") == {
        "insertText": {"text": "hi", "endOfSegmentLocation": {}}
    }


def test_replace_all_text_request_shape() -> None:
    assert docs.replace_all_text_request("a", "b", match_case=True) == {
        "replaceAllText": {"containsText": {"text": "a", "matchCase": True}, "replaceText": "b"}
    }


async def test_docs_4xx_does_not_leak_body(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "no", "detail": "SECRET-LEAK"})

    async with _client(handler, tmp_path) as gc:
        with pytest.raises(ConnectorError) as exc:
            await docs.create_document(gc, title="x")
    assert "SECRET-LEAK" not in str(exc.value)


# --- drive trash (the undo for a create) -----------------------------------


async def test_trash_file_patches_trashed_true(tmp_path: Path) -> None:
    box: dict = {}
    async with _client(_capture(box, {"id": "doc-1", "trashed": True}), tmp_path) as gc:
        out = await drive.trash_file(gc, "doc-1")
    assert box["method"] == "PATCH"
    assert box["path"].endswith("/files/doc-1")
    assert box["body"] == {"trashed": True}
    assert out["trashed"] is True
