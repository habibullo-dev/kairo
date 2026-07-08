"""Google Drive adapter: read (search + fetch_text) + trash (the undo for a create).

Google Docs are exported as text/plain; plain-text files are fetched via ``alt=media``;
anything else returns a short metadata note rather than binary. Text is capped before it
reaches the model. ``trash_file`` (Phase 12) is the reversible undo for a document/file Kairo
created — it needs only the narrow ``drive.file`` scope and is exercised with a fake transport
until the Milestone-2 tool wiring adds the scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from jarvis.connectors.google.client import GoogleClient

_API = "https://www.googleapis.com/drive/v3"
_MAX_TEXT_CHARS = 200_000
_MAX_RESULTS = 25
_GDOC = "application/vnd.google-apps.document"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str
    web_view_link: str


async def search(client: GoogleClient, *, query: str, max_results: int = 10) -> list[DriveFile]:
    cap = max(1, min(max_results, _MAX_RESULTS))
    data = await client.get_json(
        f"{_API}/files",
        params={
            "q": query,
            "pageSize": cap,
            "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
        },
    )
    return [
        DriveFile(
            id=f.get("id", ""),
            name=f.get("name", ""),
            mime_type=f.get("mimeType", ""),
            modified_time=f.get("modifiedTime", ""),
            web_view_link=f.get("webViewLink", ""),
        )
        for f in (data.get("files") or [])[:cap]
    ]


async def fetch_text(client: GoogleClient, file_id: str) -> str:
    meta = await client.get_json(f"{_API}/files/{file_id}", params={"fields": "id,name,mimeType"})
    mime = meta.get("mimeType", "")
    if mime == _GDOC:
        text = await client.get_text(
            f"{_API}/files/{file_id}/export", params={"mimeType": "text/plain"}
        )
        return text[:_MAX_TEXT_CHARS]
    if mime.startswith("text/"):
        text = await client.get_text(f"{_API}/files/{file_id}", params={"alt": "media"})
        return text[:_MAX_TEXT_CHARS]
    return f"(binary file '{meta.get('name', '')}' of type {mime or 'unknown'}; not exported)"


async def trash_file(client: GoogleClient, file_id: str) -> dict:
    """Move a file to Trash (Drive ``files.update`` with ``trashed=true``) — the reversible undo
    for a Kairo-created document/file. Needs only ``drive.file`` for a file the app created."""
    return await client.patch_json(
        f"{_API}/files/{quote(file_id, safe='')}", json_body={"trashed": True}
    )
