"""Stable-first prompt layout + stable-prefix hashes (S7.2).

The portable half of context reuse: order the prompt so the *stable, reusable, non-sensitive*
framing leads and the *volatile* per-turn content trails. Every provider's caching (explicit
breakpoints, automatic prefix caching, or none) keys off a repeated leading prefix, so this
ordering helps even providers we cache nothing for.

Stable prefix (in order): system safety contract (+ playbooks/skills), tool schemas, team
profiles, service-catalog summaries, stable per-project instructions. Volatile tail (in order):
latest user turn, current time, pending approvals, memory recall, search snippets, connector
data, web/email/calendar content.

Pure + deterministic: :func:`assemble` returns the ordered sections, the split text, and the five
named stable-prefix hashes plus their composite ``stable_prefix_hash`` — a change to any stable
section deterministically busts the composite (so a stale cache is never reused). Hashing is
content-only; it neither stores nor sends anything. Cache is NOT memory.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class SectionKind(StrEnum):
    """A labeled prompt section. The stable kinds form the cacheable prefix (in this order);
    the volatile kinds are never a cache anchor (in this order)."""

    # --- stable prefix (cacheable), in canonical order ---
    SYSTEM_CONTRACT = "system_contract"  # safety contract + Kairo playbooks/skills
    TOOL_SCHEMAS = "tool_schemas"
    TEAM_PROFILES = "team_profiles"
    SERVICE_CATALOG = "service_catalog"
    PROJECT_POLICY = "project_policy"  # stable per-project instructions
    # --- volatile tail (never cached), in canonical order ---
    USER_TURN = "user_turn"
    CURRENT_TIME = "current_time"
    PENDING_APPROVALS = "pending_approvals"
    MEMORY_RECALL = "memory_recall"
    SEARCH_SNIPPETS = "search_snippets"
    CONNECTOR_DATA = "connector_data"
    WEB_EMAIL_CALENDAR = "web_email_calendar"


STABLE_ORDER: tuple[SectionKind, ...] = (
    SectionKind.SYSTEM_CONTRACT,
    SectionKind.TOOL_SCHEMAS,
    SectionKind.TEAM_PROFILES,
    SectionKind.SERVICE_CATALOG,
    SectionKind.PROJECT_POLICY,
)
VOLATILE_ORDER: tuple[SectionKind, ...] = (
    SectionKind.USER_TURN,
    SectionKind.CURRENT_TIME,
    SectionKind.PENDING_APPROVALS,
    SectionKind.MEMORY_RECALL,
    SectionKind.SEARCH_SNIPPETS,
    SectionKind.CONNECTOR_DATA,
    SectionKind.WEB_EMAIL_CALENDAR,
)
#: The five named stable-prefix component hashes, and which section kind each covers.
_HASH_KIND = {
    "system_contract_hash": SectionKind.SYSTEM_CONTRACT,
    "tool_schema_hash": SectionKind.TOOL_SCHEMAS,
    "team_profile_hash": SectionKind.TEAM_PROFILES,
    "service_catalog_hash": SectionKind.SERVICE_CATALOG,
    "project_policy_hash": SectionKind.PROJECT_POLICY,
}
_HASH_ORDER = (
    "system_contract_hash",
    "tool_schema_hash",
    "team_profile_hash",
    "service_catalog_hash",
    "project_policy_hash",
)


@dataclass(frozen=True)
class PromptSection:
    """One labeled piece of the prompt. ``sensitive`` marks content derived from private/project
    sources — a stable section is normally NON-sensitive; if one is flagged, the policy layer
    refuses to cache the prefix unless the private-content gate explicitly allows it."""

    kind: SectionKind
    text: str
    sensitive: bool = False


@dataclass(frozen=True)
class AssembledPrompt:
    sections: tuple[PromptSection, ...]  # ordered stable-first, volatile-last
    stable_text: str
    volatile_text: str
    hashes: dict[str, str]  # the five named component hashes + composite stable_prefix_hash
    stable_is_sensitive: bool  # any stable section flagged sensitive (gates private caching)

    @property
    def stable_prefix_hash(self) -> str:
        return self.hashes["stable_prefix_hash"]


def _digest(text: str) -> str:
    """A short, stable content fingerprint (never a secret — a hash of prompt framing)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def assemble(sections: list[PromptSection]) -> AssembledPrompt:
    """Order ``sections`` stable-first/volatile-last and compute the stable-prefix hashes.
    Multiple sections of one kind keep their given relative order (a stable sort)."""
    stable = [s for s in sections if s.kind in STABLE_ORDER]
    volatile = [s for s in sections if s.kind in VOLATILE_ORDER]
    stable.sort(key=lambda s: STABLE_ORDER.index(s.kind))  # stable sort preserves within-kind order
    volatile.sort(key=lambda s: VOLATILE_ORDER.index(s.kind))

    def kind_text(kind: SectionKind) -> str:
        return "\n".join(s.text for s in stable if s.kind is kind)

    hashes = {name: _digest(kind_text(kind)) for name, kind in _HASH_KIND.items()}
    hashes["stable_prefix_hash"] = _digest("|".join(hashes[name] for name in _HASH_ORDER))
    return AssembledPrompt(
        sections=tuple(stable + volatile),
        stable_text="\n".join(s.text for s in stable),
        volatile_text="\n".join(s.text for s in volatile),
        hashes=hashes,
        stable_is_sensitive=any(s.sensitive for s in stable),
    )
