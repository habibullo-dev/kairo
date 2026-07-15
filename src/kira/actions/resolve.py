"""Attendee / contact resolution (Phase 12 Task 3, roadmap R5).

Before any calendar invite or email draft is created, every attendee must be an unambiguous email
address. There is deliberately **no contacts API in scope** (deferred), so resolution is
conservative: a syntactically-valid address (optionally in ``Name <addr>`` form) resolves; a bare
name — or anything that isn't clearly an address — is *ambiguous* and must be clarified by the
user. The hard rule (pinned): an unresolved attendee can NEVER reach the preview/draft stage — the
drafting path calls :func:`require_resolved`, which refuses (raising :class:`AttendeesUnresolved`)
until every entry is a real address, so no intent row is ever created for a guessed recipient.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_ANGLE = re.compile(r"<([^>]+)>")
# Pragmatic address check (not full RFC 5322): one @, non-empty local, a dotted domain, no spaces.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _extract(raw: str) -> str:
    """The address out of ``Name <addr>`` (or the trimmed string if there's no angle form)."""
    s = raw.strip()
    m = _ANGLE.search(s)
    return m.group(1).strip() if m else s


def is_email(raw: str) -> bool:
    """True if ``raw`` is (or wraps, as ``Name <addr>``) a syntactically-valid address."""
    s = _extract(raw)
    return bool(_EMAIL.match(s)) and ".." not in s


def normalize_email(raw: str) -> str:
    """Canonical form: extracted address with a lowercased domain (local-part case preserved)."""
    local, _, domain = _extract(raw).partition("@")
    return f"{local}@{domain.lower()}"


@dataclass(frozen=True)
class AttendeeResolution:
    """The outcome of classifying a list of attendee strings.

    ``resolved`` are canonical, de-duplicated addresses; ``ambiguous`` are the entries that need
    the user to say who they mean. ``ok`` is True only when nothing is ambiguous."""

    resolved: tuple[str, ...]
    ambiguous: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.ambiguous


class AttendeesUnresolved(ValueError):
    """Raised when a draft/invite is attempted with one or more ambiguous attendees. Carries the
    ambiguous entries so the caller can ASK the user (and create no intent until they answer)."""

    def __init__(self, ambiguous: tuple[str, ...]) -> None:
        self.ambiguous = ambiguous
        super().__init__(clarification_prompt(ambiguous))


def resolve_attendees(raw: Iterable[str]) -> AttendeeResolution:
    """Classify each entry as a resolved address or ambiguous. Order-preserving; de-duplicates
    resolved addresses (case-insensitively) and ambiguous entries."""
    resolved: list[str] = []
    ambiguous: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        text = entry.strip()
        if not text:
            continue
        if is_email(entry):
            norm = normalize_email(entry)
            if norm.lower() not in seen:
                seen.add(norm.lower())
                resolved.append(norm)
        elif text not in ambiguous:
            ambiguous.append(text)
    return AttendeeResolution(resolved=tuple(resolved), ambiguous=tuple(ambiguous))


def require_resolved(raw: Iterable[str]) -> tuple[str, ...]:
    """Return the canonical addresses, or raise :class:`AttendeesUnresolved` if ANY entry is
    ambiguous. This is the single sanctioned gate the drafting path calls, so an ambiguous
    attendee can never reach a preview or a stored intent."""
    resolution = resolve_attendees(raw)
    if not resolution.ok:
        raise AttendeesUnresolved(resolution.ambiguous)
    return resolution.resolved


def clarification_prompt(ambiguous: tuple[str, ...]) -> str:
    """A friendly ASK the turn shows the user when an attendee couldn't be resolved."""
    names = ", ".join(f"'{a}'" for a in ambiguous)
    return (
        f"I couldn't turn {names} into a specific email address. Who exactly should I include? "
        "Please give the full email address(es) — I won't create the event or draft until every "
        "attendee is a real address."
    )
