"""Confirmed dead static assets stay removed rather than silently drifting back."""

from jarvis.ui.server import STATIC_DIR


def test_dead_status_targets_and_unused_static_assets_are_absent() -> None:
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for target in ("composer-model", "composer-mode", "chat-model", "chat-mode"):
        assert target not in app
    assert not (STATIC_DIR / "ui" / "components.js").exists()
    assert not (STATIC_DIR / "assets" / "kairo-mark.svg").exists()
