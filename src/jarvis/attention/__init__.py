"""Attention + automation (Phase 16).

The ONE attention queue (:class:`AttentionStore` over ``attention_items``) that unifies approvals,
reviews, proposals, and alerts from every source into a single surface — the Notification Center.
Proposal-only dreaming (later tasks) writes here and nowhere risky.
"""

from __future__ import annotations

from jarvis.attention.dreaming import (
    DREAMING_TOOLS,
    FORBIDDEN_TOOLS,
    DreamingBudget,
    DreamingCageError,
    assert_caged,
    build_dreaming_registry,
    emit_budget_halt_alert,
)
from jarvis.attention.routing import (
    NotificationRouter,
    NotifyDecision,
    in_quiet_hours,
    minimized_push,
    route_notification,
)
from jarvis.attention.store import (
    ALLOWED_TRANSITIONS,
    TRUST_CLASSES,
    AttentionItem,
    AttentionKind,
    AttentionPriority,
    AttentionState,
    AttentionStore,
    InvalidTransition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "TRUST_CLASSES",
    "AttentionItem",
    "AttentionKind",
    "AttentionPriority",
    "AttentionState",
    "AttentionStore",
    "InvalidTransition",
    "NotificationRouter",
    "NotifyDecision",
    "in_quiet_hours",
    "minimized_push",
    "route_notification",
    "DREAMING_TOOLS",
    "FORBIDDEN_TOOLS",
    "DreamingBudget",
    "DreamingCageError",
    "assert_caged",
    "build_dreaming_registry",
    "emit_budget_halt_alert",
]
