"""The Auto/Manual router (Phase 15.6): ties the routing-mode state, the classifier, the manual
model/effort, and provider availability into one ``route(user_text) -> RouteDecision`` used by the
interactive loop at turn start.

Safety: MANUAL returns the human-pinned model (Anthropic allowlist today). AUTO classifies via
Gemini Flash-Lite and maps through :func:`~jarvis.routing.policy.resolve_route` (private_ok +
availability enforced there). If the router model is unavailable OR the classifier fails, AUTO
falls back to the trusted SAFE default (Sonnet) — fail-closed, never a cheap/non-private route.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from jarvis.models.providers import provider_spec
from jarvis.routing.classifier import Classifier
from jarvis.routing.policy import (
    FAILSAFE,
    SAFE_DEFAULT,
    RouteDecision,
    RoutingMode,
    provider_for_model,
    resolve_route,
)

#: The provider whose availability gates AUTO (classifier + the cheap SIMPLE tier both run on it).
_ROUTER_PROVIDER = "gemini"


class RoutingState:
    """The interactive routing policy holder (AUTO|MANUAL), mutable + in-process like ``ModeState``.
    Default AUTO — the cost-aware daily experience. SEPARATE axis from the permission Mode."""

    def __init__(self, mode: RoutingMode = RoutingMode.AUTO) -> None:
        self._mode = mode

    def mode(self) -> RoutingMode:
        return self._mode

    def set(self, mode: RoutingMode) -> None:
        self._mode = mode


class Router:
    """Resolves one turn's model. Collaborators are injected as callables so the router stays
    decoupled from the UI state and is fully unit-testable keyless."""

    def __init__(
        self,
        *,
        state: RoutingState,
        manual_model: Callable[[], str],
        manual_effort: Callable[[], str | None],
        classifier: Classifier | None,
        is_available: Callable[[str], bool],
    ) -> None:
        self._state = state
        self._manual_model = manual_model
        self._manual_effort = manual_effort
        self._classifier = classifier
        self._is_available = is_available

    async def route(
        self,
        user_text: str | None,
        *,
        before_classifier: Callable[[str, dict], Awaitable[None]] | None = None,
        after_classifier: Callable[[str, object], Awaitable[None]] | None = None,
    ) -> RouteDecision:
        if self._state.mode() is RoutingMode.MANUAL:
            model = self._manual_model()
            provider = provider_for_model(model)
            spec = provider_spec(provider)
            return RouteDecision(
                provider=provider,
                model=model,
                effort=self._manual_effort(),
                tier="manual",
                mode="manual",
                sensitivity="unknown",
                reason=f"manual: {model}",
                tools_enabled=bool(spec and spec.tool_capable),
            )
        # AUTO — fail-closed to the trusted SAFE default if the router model can't run.
        if self._classifier is None or not self._is_available(_ROUTER_PROVIDER):
            return RouteDecision(
                provider=SAFE_DEFAULT.provider,
                model=SAFE_DEFAULT.model,
                effort=None,
                tier=SAFE_DEFAULT.key,
                mode="auto",
                sensitivity="unknown",
                reason="auto: router model unavailable → safe default (Sonnet 5)",
            )
        classification = await self._classifier.classify(
            user_text,
            before_call=(
                (lambda request: before_classifier(_ROUTER_PROVIDER, request))
                if before_classifier is not None
                else None
            ),
            after_call=(
                (lambda response: after_classifier(_ROUTER_PROVIDER, response))
                if after_classifier is not None
                else None
            ),
        )
        if classification is None:
            classification = FAILSAFE  # private/hard ⇒ escalate to the trusted tier
        return resolve_route(classification, is_available=self._is_available)
