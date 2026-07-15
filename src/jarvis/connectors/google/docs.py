"""Google Docs adapter (Phase 12): create + batchUpdate (edit-in-place) + read.

Thin transport wrappers over the Docs API (``docs.googleapis.com/v1``), which accepts the narrow
``drive.file`` scope for documents the app creates/opens — so Kira needs NEITHER the broad
``documents`` scope NOR full ``drive``. Like the calendar writer, these build the request from
primitive params, never take a model-supplied URL, and are exercised only with a fake transport
until the Milestone-2 tool wiring adds the scope.

``create_document`` only sets the title (that is all the Docs create endpoint does); initial body
text is a subsequent ``batch_update`` with an append request — the executor composes the two, so a
partial failure (titled-but-empty doc) is a journaled, trashable outcome rather than a silent
half-write. The request builders (:func:`append_text_request`, :func:`replace_all_text_request`)
map Kira's DocAppend/DocReplace ops to Docs API requests.
"""

from __future__ import annotations

from urllib.parse import quote

from jarvis.connectors.google.client import GoogleClient

_DOCS_API = "https://docs.googleapis.com/v1"


def _doc_url(document_id: str) -> str:
    return f"{_DOCS_API}/documents/{quote(document_id, safe='')}"


async def create_document(client: GoogleClient, *, title: str) -> dict:
    """Create an empty document with ``title``; returns the created resource (``documentId``)."""
    return await client.post_json(f"{_DOCS_API}/documents", json_body={"title": title})


async def batch_update(client: GoogleClient, document_id: str, requests: list[dict]) -> dict:
    """Apply a list of Docs API requests atomically (edit-in-place); returns the batchUpdate
    reply (``documentId``, ``replies``, ``writeControl``). The ``:batchUpdate`` suffix is part of
    the path and is intentionally not URL-escaped."""
    return await client.post_json(
        f"{_doc_url(document_id)}:batchUpdate", json_body={"requests": requests}
    )


async def get_document(client: GoogleClient, document_id: str) -> dict:
    """Fetch a document resource — used to capture rollback state before an edit."""
    return await client.get_json(_doc_url(document_id))


def append_text_request(text: str) -> dict:
    """A Docs request that appends ``text`` at the end of the body."""
    return {"insertText": {"text": text, "endOfSegmentLocation": {}}}


def replace_all_text_request(find: str, replace: str, *, match_case: bool = False) -> dict:
    """A Docs request that replaces every occurrence of ``find`` with ``replace``."""
    return {
        "replaceAllText": {
            "containsText": {"text": find, "matchCase": match_case},
            "replaceText": replace,
        }
    }
