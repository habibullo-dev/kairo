"""Drafts-only, forever (Phase 9 clarification): no Gmail send surface may exist in src/.

Kira may create drafts (users.drafts.create via gmail.compose) but must never send. This grep
pin fails if any Gmail send endpoint, scope, or path appears anywhere in the source tree — a
standing guard against a future task quietly adding one.
"""

from __future__ import annotations

import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "jarvis"

#: Gmail-specific send surfaces. Deliberately narrow so it can't false-positive on unrelated
#: "send" identifiers (send_notification, send_telegram_message, ws.send_json, ...).
_FORBIDDEN = (
    "messages/send",  # POST users/me/messages/send
    "drafts/send",  # POST users/me/drafts/send
    "gmail.send",  # the send OAuth scope
    "messages.send",
    "drafts.send",
)


def test_no_gmail_send_surface_in_src() -> None:
    hits: list[str] = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN:
            if needle in text:
                hits.append(f"{path.relative_to(_SRC)}: contains {needle!r}")
    assert not hits, "Gmail send surface must not exist (drafts-only):\n" + "\n".join(hits)


def test_gmail_compose_is_the_only_gmail_write_scope() -> None:
    # The compose scope (drafts.create/update) is allowed; the send scope is not.
    from jarvis.connectors.google import GOOGLE_SCOPES

    assert any(s.endswith("gmail.compose") for s in GOOGLE_SCOPES)
    assert not any(s.endswith("gmail.send") for s in GOOGLE_SCOPES)
    assert not any(s.endswith("gmail.modify") for s in GOOGLE_SCOPES)


def test_gmail_adapter_surface_has_no_send() -> None:
    # Re-asserted as the draft write surface grows (Phase 12 adds update_draft): the gmail
    # adapter exposes ONLY draft writes, and nothing whose name suggests a send.
    from jarvis.connectors.google import gmail

    public = [n for n in dir(gmail) if not n.startswith("_")]
    assert "create_draft" in public and "update_draft" in public
    assert not any("send" in n.lower() for n in public)
