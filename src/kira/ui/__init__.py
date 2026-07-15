"""Workstation UI (Phase 8): an authenticated LOCAL peer of the REPL/voice.

It drives the same ``AgentLoop`` through the same seams (events out, injected ``Approver``
in) and adds **no new authority**. Safety floor: docs/PLAN-8-ui.md + ADR-0008 — loopback
only, a singleton owner credential with digest-only durable sessions, a per-launch token that
can issue only one setup/recovery grant, approvals explicit/audited/replay-proof, and the UI is
voice's fail-closed "screen".

FastAPI/uvicorn live behind the optional ``ui`` extra, so this package imports them lazily
(``server`` pulls FastAPI); ``auth``/``connections`` are dependency-free and always import.
"""

from kira.ui.approver import (
    ApprovalManager,
    PendingApproval,
    UIApprover,
    UIScreenApprover,
    make_ui_subagent_approver,
)
from kira.ui.auth import SESSION_COOKIE, AuthManager, host_allowed, origin_allowed
from kira.ui.connections import Connection, ConnectionManager
from kira.ui.readmodels import UiServices, hub_status, lab_overview
from kira.ui.session import UiSession, serialize_event
from kira.ui.voice import UiVoice, UiVoiceRenderer

__all__ = [
    "SESSION_COOKIE",
    "ApprovalManager",
    "AuthManager",
    "Connection",
    "ConnectionManager",
    "PendingApproval",
    "UIApprover",
    "UIScreenApprover",
    "UiServices",
    "UiSession",
    "UiVoice",
    "UiVoiceRenderer",
    "host_allowed",
    "hub_status",
    "lab_overview",
    "make_ui_subagent_approver",
    "origin_allowed",
    "serialize_event",
]
