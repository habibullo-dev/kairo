"""Role → route resolution + validation (Phase 10 Task 6).

Resolution precedence (lowest → highest): code ``DEFAULT_ROUTES`` ← ``settings.yaml``
``model_routes`` ← per-project ``settings_json["model_routes"]`` ← per-run overrides. Each
layer is a partial ``{role: {provider, model, effort?, text_only?}}`` map; a later layer
overrides only the fields it names. Every resolved route is validated: known provider, and
no text-only route on a tool-capable role (a write-capable executor must be able to drive
tools — pre-mortem constraint).
"""

from __future__ import annotations

from jarvis.models.providers import TRUSTED_AUTHORITY_PROVIDERS
from jarvis.models.roles import (
    DEFAULT_ROUTES,
    FINAL_AUTHORITY_ROLES,
    PRIVATE_CONTEXT_ROLES,
    PROVIDERS,
    ROLES,
    TOOL_CAPABLE_ROLES,
    ModelRoute,
)


class RouteError(ValueError):
    """An invalid model route: unknown role/provider, empty model, text-only on a tool role,
    a non-trusted provider on a final-authority/private-context role, or (when a provider
    registry is wired) an unavailable provider (fail-closed — never a silent downgrade)."""


def _merge(base: ModelRoute, override: dict) -> ModelRoute:
    """Apply a partial override dict onto a route (only named fields change)."""
    return ModelRoute(
        provider=override.get("provider", base.provider),
        model=override.get("model", base.model),
        effort=override.get("effort", base.effort),
        text_only=bool(override.get("text_only", base.text_only)),
    )


def validate_route(role: str, route: ModelRoute) -> None:
    """Raise :class:`RouteError` if the route is invalid for the role. Authority is enforced
    here (pure, catalog-static — no config): a final-authority role (planner/judge) or a
    private-context role (utility) can ONLY resolve to a trusted provider (anthropic this
    phase), at EVERY override layer. Giving another provider final authority is a separate,
    explicit design change (Phase 10C non-negotiable #1), never a routing override."""
    if route.provider not in PROVIDERS:
        raise RouteError(f"role {role!r}: unknown provider {route.provider!r}")
    if not route.model:
        raise RouteError(f"role {role!r}: empty model")
    if route.text_only and role in TOOL_CAPABLE_ROLES:
        raise RouteError(
            f"role {role!r} must drive tools; a text-only route ({route.provider}) is not allowed"
        )
    if (
        role in FINAL_AUTHORITY_ROLES or role in PRIVATE_CONTEXT_ROLES
    ) and route.provider not in TRUSTED_AUTHORITY_PROVIDERS:
        kind = "final-authority" if role in FINAL_AUTHORITY_ROLES else "private-context"
        allowed = ", ".join(sorted(TRUSTED_AUTHORITY_PROVIDERS))
        raise RouteError(
            f"role {role!r} is a {kind} role; only trusted providers ({allowed}) may hold it, "
            f"not {route.provider!r} — this is a code-level authority pin, not a routing option"
        )


class ModelRegistry:
    """Resolves a role to its :class:`ModelRoute` across the override layers.

    ``settings_routes`` is the config-level map (``settings.yaml``); ``project_routes`` and
    ``run_routes`` are supplied per-resolution. All are partial ``{role: {...}}`` dicts.

    ``provider_registry`` (optional) makes resolution enforce fail-closed AVAILABILITY: a route
    to a disabled / missing-key / unpriced provider raises :class:`RouteError` with the reason,
    never a silent downgrade. Omitted (pure/tests) ⇒ only authority + shape are validated."""

    def __init__(
        self, settings_routes: dict | None = None, *, provider_registry: object | None = None
    ) -> None:
        self.settings_routes = settings_routes or {}
        self.provider_registry = provider_registry

    def route(
        self,
        role: str,
        *,
        project_routes: dict | None = None,
        run_routes: dict | None = None,
    ) -> ModelRoute:
        if role not in DEFAULT_ROUTES:
            raise RouteError(f"unknown role {role!r} (known: {', '.join(ROLES)})")
        route = DEFAULT_ROUTES[role]
        for layer in (self.settings_routes, project_routes or {}, run_routes or {}):
            override = layer.get(role)
            if override:
                route = _merge(route, override)
        validate_route(role, route)
        if self.provider_registry is not None and not self.provider_registry.route_allowed(
            route.provider
        ):
            state = self.provider_registry.state(route.provider).value
            raise RouteError(
                f"role {role!r}: provider {route.provider!r} is not available ({state}) — "
                f"fail-closed, no downgrade to another provider"
            )
        return route
