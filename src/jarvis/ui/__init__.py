"""Workstation UI (Phase 8): an authenticated LOCAL peer of the REPL/voice.

It drives the same ``AgentLoop`` through the same seams (events out, injected ``Approver``
in) and adds **no new authority**. Safety floor: docs/PLAN-8-ui.md + ADR-0008 — loopback
only, a per-launch token exchanged for a session, approvals explicit/audited/replay-proof,
and the UI is voice's fail-closed "screen".

FastAPI/uvicorn live behind the optional ``ui`` extra, so this package imports them lazily
(``server`` pulls FastAPI); ``auth``/``connections`` are dependency-free and always import.
"""

from jarvis.ui.auth import SESSION_COOKIE, AuthManager, host_allowed, origin_allowed
from jarvis.ui.connections import Connection, ConnectionManager

__all__ = [
    "SESSION_COOKIE",
    "AuthManager",
    "Connection",
    "ConnectionManager",
    "host_allowed",
    "origin_allowed",
]
