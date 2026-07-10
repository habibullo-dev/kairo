"""Attention routing (Phase 16 Task 4): where an item goes — a minimized push, the digest, or
center-only. The rules are DATA (``AttentionConfig`` + a pure decision function), not code paths
per rule, so the matrix is one table under test.

Safety pins:
* **Minimized, body-free pushes.** An urgent push is composed from open-item COUNTS BY KIND only
  ("Kairo · 3 need you: 2 approvals, 1 proposal") — never an item title, email subject, task body,
  or any payload. An email subject can therefore never leak to Telegram/Kakao.
* **Opt-in egress.** ``urgent_channels`` defaults to empty, so nothing is ever pushed until a
  channel is deliberately enabled (and its notifier configured).
* **Quiet hours + per-project mute NARROW only** — they can suppress a push (fold to digest), never
  widen one.
"""

from __future__ import annotations

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
    quiet_start: int | None,
    quiet_end: int | None,
    muted_projects: list[int],
) -> NotifyDecision:
    """The routing matrix (pure). Urgent ⇒ a minimized push to ``urgent_channels`` — UNLESS the
    project is muted or it's quiet hours, in which case it folds to the digest (suppress, never
    escalate). Normal ⇒ digest. Low ⇒ center-only."""
    if priority == "urgent":
        if project_id is not None and project_id in muted_projects:
            return NotifyDecision((), True, "urgent but project muted → digest")
        if in_quiet_hours(hour, quiet_start, quiet_end):
            return NotifyDecision((), True, "urgent but quiet hours → digest")
        return NotifyDecision(tuple(urgent_channels), False, "urgent → minimized push")
    if priority == "normal":
        return NotifyDecision((), True, "normal → digest")
    return NotifyDecision((), False, "low → center-only")


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
                        self._notices.post(f"Attention push to {channel} failed.", kind="warn")
        return decision
