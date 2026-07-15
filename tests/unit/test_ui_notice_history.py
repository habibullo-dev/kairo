"""The Notifications screen exposes the durable, read-only notice feed."""

from kira.ui.server import STATIC_DIR


def test_gate_reads_and_safely_renders_notice_history() -> None:
    source = (STATIC_DIR / "screens" / "gate.js").read_text(encoding="utf-8")

    assert 'id="gate-notices"' in source
    assert 'api.get("/api/notices")' in source
    assert "fillNoticeHistory" in source
    assert "noticeHistory.textContent" in source
    assert "document.createElement" in source
    assert "/api/notices" not in source.split("function fillNoticeHistory", 1)[0]


def test_client_notice_buffer_rejects_stale_workspace_frames() -> None:
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # Gate merges the REST tail with live WebSocket activity. Clear the prior scope on a workspace
    # transition and accept a live row only when its server-owned project provenance matches.
    assert "state.notices = [];" in app
    assert "notice.project_id !== state.context.project_id" in app


def test_notice_history_merges_valid_rows_in_chronological_order() -> None:
    source = (STATIC_DIR / "screens" / "gate.js").read_text(encoding="utf-8")

    assert '["durable", data.notices]' in source
    assert '["live", api.state?.notices]' in source
    assert "if (!Array.isArray(rows)) continue;" in source
    assert 'if (!notice || typeof notice !== "object") return;' in source
    assert 'seq:${String(notice.seq)}:${String(notice.at || "")}' in source
    assert "Date.parse(" in source
    assert "bSeq - aSeq" in source
    history = source.split("async function fillNoticeHistory", 1)[1].split(
        "await fillNoticeHistory", 1
    )[0]
    assert "innerHTML" not in history
