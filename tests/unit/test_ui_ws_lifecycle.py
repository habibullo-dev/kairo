"""Client WebSocket liveness must survive reconnects without multiplying timers."""

from __future__ import annotations

from kira.ui.server import STATIC_DIR

APP = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_reconnect_replaces_and_clears_the_single_heartbeat_timer() -> None:
    assert "let heartbeatTimer = null;" in APP
    assert "function clearHeartbeat()" in APP
    assert "clearInterval(heartbeatTimer);" in APP
    # Reconnect clears a previous interval before creating a socket; close clears the active one.
    assert "function connect() {\n  clearHeartbeat();" in APP
    assert "socket.onclose = (event) => {" in APP
    assert "if (event.code === 1008)" in APP
    assert 'fetch("/auth/session"' in APP
    assert "if (ws !== socket) return;\n    clearHeartbeat();\n    ws = null;" in APP
    # The fresh socket is the only one permitted to heartbeat after reconnecting.
    assert "startHeartbeat(socket);" in APP
    assert "if (ws === socket && socket.readyState === 1)" in APP
