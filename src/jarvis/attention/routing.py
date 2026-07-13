"""Attention routing (Phase 16 Task 4): where an item goes — a minimized push, the digest, or
center-only. The rules are DATA (``AttentionConfig`` + a pure decision function), not code paths
per rule, so the matrix is one table under test.

Safety pins:
* **Minimized, body-free pushes.** A push is composed from open-item COUNTS BY KIND only
  ("Kairo · 3 need you: 2 approvals, 1 proposal") — never an item title, email subject, task body,
  or any payload. An email subject can therefore never leak to Telegram/Kakao.
* **Opt-in egress.** Every priority's channel list defaults to empty, so nothing is ever pushed
  until that priority is deliberately enabled (and its notifier configured).
* **Quiet hours + per-project mute NARROW only** — they can suppress a push (fold to digest), never
  widen one.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

from jarvis.connectors.base import ConnectorError
from jarvis.observability import get_logger, log_egress

_log = get_logger("jarvis.attention")


@dataclass(frozen=True)
class NotifyDecision:
    """Where one attention item routes. ``channels`` are the notifier names to push a minimized
    nudge to; ``to_digest`` folds it into the next digest instead; neither ⇒ center-only."""

    channels: tuple[str, ...]
    to_digest: bool
    reason: str


def in_quiet_hours(hour: int, start: int | None, end: int | None) -> bool:
    """Is ``hour`` (local 0-23) inside the quiet window [start, end)? Handles a window that wraps
    midnight (e.g. 22→7). No window configured ⇒ never quiet."""
    if start is None or end is None:
        return False
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def route_notification(
    *,
    priority: str,
    project_id: int | None,
    hour: int,
    urgent_channels: list[str],
    normal_channels: list[str],
    low_channels: list[str],
    quiet_start: int | None,
    quiet_end: int | None,
    muted_projects: list[int],
) -> NotifyDecision:
    """The pure routing matrix. A configured priority sends a minimized push unless quiet hours
    or a project mute suppress it. Unconfigured priorities retain the legacy digest/center
    destination, so enabling Telegram for one level cannot widen another by accident."""
    channels, fallback = {
        "urgent": (urgent_channels, False),
        "normal": (normal_channels, True),
        "low": (low_channels, False),
    }.get(priority, ([], False))
    if project_id is not None and project_id in muted_projects:
        return NotifyDecision((), True, f"{priority} but project muted → digest")
    if in_quiet_hours(hour, quiet_start, quiet_end):
        return NotifyDecision((), True, f"{priority} but quiet hours → digest")
    if channels:
        return NotifyDecision(tuple(channels), fallback, f"{priority} → minimized push")
    return NotifyDecision((), fallback, f"{priority} → {'digest' if fallback else 'center-only'}")


def minimized_push(counts: dict[str, int], *, cap: int = 280) -> str:
    """The ONLY text that goes off-box for an urgent push: counts by kind, no titles/bodies. E.g.
    ``"Kairo · 3 need you: 2 approvals, 1 proposal"``. An email subject / task body can never
    appear here — the push is derived purely from how MANY items of each kind are open."""
    total = sum(counts.values())
    if total <= 0:
        return "Kairo · nothing waiting"
    parts = [f"{n} {kind}{'s' if n != 1 else ''}" for kind, n in sorted(counts.items()) if n > 0]
    need = "needs" if total == 1 else "need"
    return f"Kairo · {total} {need} you: {', '.join(parts)}"[:cap]


class NotificationRouter:
    """Applies the routing matrix and, for an urgent item that routes to a channel, sends the
    MINIMIZED (count-only) push through the existing notifiers. It never composes item content —
    only :func:`minimized_push` output crosses the wire. UI/center is always the record; a push is
    a best-effort nudge (a failed send warns, never raises)."""

    def __init__(self, config: Any, connectors: Any, *, notices: Any = None) -> None:
        self._cfg = config.attention
        self._connectors = connectors
        self._notices = notices

    async def notify(
        self, *, priority: str, project_id: int | None, open_counts: dict[str, int], hour: int
    ) -> NotifyDecision:
        """Route one item. Returns the decision (for the caller/tests); performs the minimized push
        as a side effect when the decision has channels."""
        decision = route_notification(
            priority=priority,
            project_id=project_id,
            hour=hour,
            urgent_channels=self._cfg.urgent_channels,
            normal_channels=self._cfg.normal_channels,
            low_channels=self._cfg.low_channels,
            quiet_start=self._cfg.quiet_hours_start,
            quiet_end=self._cfg.quiet_hours_end,
            muted_projects=self._cfg.muted_projects,
        )
        if decision.channels:
            text = minimized_push(open_counts)  # counts only — body-free by construction
            for channel in decision.channels:
                notifier = self._connectors.notifier(channel) if self._connectors else None
                if notifier is None:
                    continue
                try:
                    await notifier.send(text)
                    log_egress(category="attention_push", destination_type=channel)
                except ConnectorError:
                    _log.warning("attention_push_failed", channel=channel)
                    if self._notices is not None:
                        self._notices.post(
                            f"Attention push to {channel} failed.",
                            kind="warn",
                            project_id=project_id,
                        )
        return decision


async def notify_open_attention_item(
    router: NotificationRouter | None, store: Any, item_id: int, *, now: _dt.datetime | None = None
) -> NotifyDecision | None:
    """Best-effort, post-commit notification for one durable attention row.

    The helper deliberately re-reads the row and aggregates counts from the store rather than
    accepting a title or payload.  A producer can call it only after its durable transaction
    commits; failed delivery therefore never rolls back the source state or loses the item.
    """
    if router is None:
        return None
    item = await store.get(item_id)
    if item is None or str(item.state) != "open":
        return None
    moment = now or _dt.datetime.now().astimezone()
    counts = await store.open_counts(project_id=item.project_id)
    return await router.notify(
        priority=str(item.priority),
        project_id=item.project_id,
        open_counts=counts,
        hour=moment.hour,
    )
