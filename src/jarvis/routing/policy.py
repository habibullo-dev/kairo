"""Cost-aware Auto routing policy (Phase 15.6) — the pure decision layer.

A :class:`Classification` (from the Gemini Flash-Lite classifier) maps to a :class:`RouteDecision`
via :func:`resolve_route`. Two invariants are enforced HERE, in pure code — the classifier is only
an optimization, never the security boundary:

* **private_ok hard gate** — the interactive main chat carries private context (memory, history,
  project state), so an Auto route may ONLY land on a ``private_ok`` provider. Every Auto tier is
  private_ok by construction (Gemini/Claude); the gate is belt-and-suspenders, reading the provider
  catalog (the single source of truth). The cheap non-private workers (qwen/deepseek/zai) are NOT
  Auto chat tiers — they are reached only via scoped delegation, where the orchestration engine's
  private-bundle refusal is the belt.
* **fail-closed availability** — a tier whose provider is unavailable (disabled / missing key /
  unpriced) downgrades to the SAFE default (Sonnet on the always-available anthropic core), never a
  silent drop to a cheaper/non-private provider.

Effort is left ``None`` for Auto (the client's configured default); the per-model effort selector
(Phase 15.5) is a MANUAL-mode control.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from jarvis.models.providers import provider_spec


class RoutingMode(StrEnum):
    """The interactive routing policy — deliberately SEPARATE from the permission Mode
    (plan/approval/auto). AUTO = cost-aware per-message routing; MANUAL = a human-pinned model."""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass(frozen=True)
class Tier:
    """One Auto destination: a private_ok (provider, model) with a human-facing label."""

    key: str
    provider: str
    model: str
    label: str


#: Auto tiers — ALL private_ok (safe for the private main chat). Simple daily work → Gemini Flash
#: (cheapest); judgment/private/important → Sonnet 5; deep/high-risk → Opus 4.8; deep *planning* →
#: Fable 5. Opus/Fable are never the simple-chat default (only hard/expert or planning reach them).
SIMPLE = Tier("simple", "gemini", "gemini-2.5-flash", "Gemini 2.5 Flash (cheap daily)")
JUDGMENT = Tier("judgment", "anthropic", "claude-sonnet-5", "Claude Sonnet 5 (judgment/private)")
DEEP = Tier("deep", "anthropic", "claude-opus-4-8", "Claude Opus 4.8 (deep/high-risk)")
PLANNING = Tier("planning", "anthropic", "claude-fable-5", "Fable 5 (deep planning)")

#: The trusted, always-available fallback (anthropic core → route_allowed even before the key is
#: checked; the client build enforces the key fail-closed). Used on classifier failure / uncertainty
#: / an unavailable tier — never fail-open to a cheap or non-private provider.
SAFE_DEFAULT = JUDGMENT

ALL_TIERS: tuple[Tier, ...] = (SIMPLE, JUDGMENT, DEEP, PLANNING)

#: Allowed classifier enum values; anything else coerces to the SAFE value (fail-safe).
DIFFICULTIES: frozenset[str] = frozenset({"trivial", "simple", "moderate", "hard", "expert"})
SENSITIVITIES: frozenset[str] = frozenset({"non_sensitive", "personal", "private"})
CATEGORIES: frozenset[str] = frozenset(
    {"chat", "summary", "coding", "planning", "email", "calendar", "finance", "other"}
)

_SENSITIVE = {"personal", "private"}
_SENSITIVE_CATEGORIES = {"email", "calendar", "finance"}
_HARD = {"hard", "expert"}


@dataclass(frozen=True)
class Classification:
    """The Flash-Lite classifier's read of one user message. Unknown/partial values are coerced to
    the SAFE extreme (difficulty=hard, sensitivity=private) so a degraded parse escalates, never
    downgrades."""

    intent: str
    difficulty: str
    sensitivity: str
    category: str


#: The fail-safe classification used when the classifier errors or returns junk: treat as PRIVATE +
#: hard ⇒ routes to the trusted JUDGMENT tier (Sonnet). Never non_sensitive/simple (that would
#: risk a cheap route for private content).
FAILSAFE = Classification(
    intent="unknown", difficulty="hard", sensitivity="private", category="other"
)


@dataclass(frozen=True)
class RouteDecision:
    """The resolved route for one turn. ``mode`` is the routing mode (auto|manual) recorded in the
    cost ledger; ``tier`` is the Auto tier key (or ``manual``); ``reason`` is a short, safe,
    human-facing explanation shown in the UI + trace."""

    provider: str
    model: str
    effort: str | None
    tier: str
    mode: str
    sensitivity: str
    reason: str


def coerce_classification(data: dict) -> Classification:
    """Build a :class:`Classification` from raw (possibly partial/hostile) classifier JSON. Any
    value outside the known enums coerces to the SAFE extreme so the route escalates, not
    downgrades. Never raises."""

    def pick(value: object, allowed: frozenset[str], safe: str) -> str:
        v = value if isinstance(value, str) else ""
        return v if v in allowed else safe

    intent = data.get("intent")
    return Classification(
        intent=(intent if isinstance(intent, str) else "unknown")[:80],
        difficulty=pick(data.get("difficulty"), DIFFICULTIES, "hard"),
        sensitivity=pick(data.get("sensitivity"), SENSITIVITIES, "private"),
        category=pick(data.get("category"), CATEGORIES, "other"),
    )


def choose_tier(c: Classification) -> Tier:
    """The intended Auto tier for a classification (before the availability/private_ok gate)."""
    if c.category == "planning" and c.difficulty in _HARD:
        return PLANNING  # deep planning / architecture
    if c.difficulty == "expert":
        return DEEP  # deep / high-risk
    if (
        c.sensitivity in _SENSITIVE
        or c.category in _SENSITIVE_CATEGORIES
        or c.difficulty == "hard"
        or c.category == "coding"
    ):
        return JUDGMENT  # judgment-heavy / private / important
    return SIMPLE  # simple everyday non-sensitive work


#: Prefix → provider for attributing a manually-picked model id to its provider (the ledger needs
#: the provider). Interactive manual models are Anthropic today; the wider prefixes are ready for
#: the UI's expanded manual list.
_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic"),
    ("gpt", "openai"),
    ("gemini", "gemini"),
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("glm", "zai"),
)


def provider_for_model(model: str) -> str:
    """The provider that serves ``model`` (by id prefix). Defaults to ``anthropic`` for an unknown
    id (the interactive manual allowlist is Anthropic)."""
    m = (model or "").lower()
    for prefix, provider in _MODEL_PREFIXES:
        if m.startswith(prefix):
            return provider
    return "anthropic"


def _is_private_ok(provider: str) -> bool:
    spec = provider_spec(provider)
    return bool(spec and spec.private_ok)


def resolve_route(c: Classification, *, is_available: Callable[[str], bool]) -> RouteDecision:
    """Map a classification to a safe, available :class:`RouteDecision`. The chosen tier must be
    private_ok (the main chat is private) AND available; otherwise downgrade to the SAFE default."""
    tier = choose_tier(c)
    chosen = tier
    if not _is_private_ok(chosen.provider) or not is_available(chosen.provider):
        chosen = SAFE_DEFAULT
    reason = f"auto: {c.category}/{c.difficulty}/{c.sensitivity} → {chosen.label}"
    if chosen is not tier:
        reason += f" (downgraded from {tier.label} — unavailable)"
    return RouteDecision(
        provider=chosen.provider,
        model=chosen.model,
        effort=None,
        tier=chosen.key,
        mode="auto",
        sensitivity=c.sensitivity,
        reason=reason,
    )
