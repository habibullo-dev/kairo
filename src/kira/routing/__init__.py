"""Cost-aware Auto routing (Phase 15.6).

The interactive default is AUTO: Gemini 2.5 Flash-Lite classifies each message (intent/difficulty/
sensitivity) and the pure :mod:`policy` layer maps it to a model — cheap Gemini Flash for simple
daily work, Sonnet 5 for judgment/private/important, Opus 4.8 / Fable 5 for deep/high-risk. The
private_ok hard gate + availability fallback are enforced in code (the classifier is only an
optimization); the cheap non-private workers are never Auto chat tiers. MANUAL pins a model.
"""

from __future__ import annotations

from kira.routing.classifier import Classifier
from kira.routing.policy import (
    ALL_TIERS,
    FAILSAFE,
    SAFE_DEFAULT,
    Classification,
    RouteDecision,
    RoutingMode,
    Tier,
    choose_tier,
    coerce_classification,
    provider_for_model,
    resolve_route,
)
from kira.routing.router import Router, RoutingState

__all__ = [
    "ALL_TIERS",
    "FAILSAFE",
    "SAFE_DEFAULT",
    "Classification",
    "Classifier",
    "RouteDecision",
    "Router",
    "RoutingMode",
    "RoutingState",
    "Tier",
    "choose_tier",
    "coerce_classification",
    "provider_for_model",
    "resolve_route",
]
