"""Role → route resolution + validation (Phase 10 Task 6).

Resolution precedence (lowest → highest): code ``DEFAULT_ROUTES`` ← ``settings.yaml``
``model_routes`` ← per-project ``settings_json["model_routes"]`` ← per-run overrides. Each
layer is a partial ``{role: {provider, model, effort?, text_only?}}`` map; a later layer
overrides only the fields it names. Every resolved route is validated: known provider, and
no text-only route on a tool-capable role (a write-capable executor must be able to drive
tools — pre-mortem constraint).
"""

from __future__ import annotations

from jarvis.models.roles import (
    DEFAULT_ROUTES,
    PROVIDERS,
    ROLES,
    TOOL_CAPABLE_ROLES,
    ModelRoute,
)


class RouteError(ValueError):
    """An invalid model route (unknown role/provider, or text-only on a tool role)."""


def _merge(base: ModelRoute, override: dict) -> ModelRoute:
    """Apply a partial override dict onto a route (only named fields change)."""
    return ModelRoute(
        provider=override.get("provider", base.provider),
        model=override.get("model", base.model),
        effort=override.get("effort", base.effort),
        text_only=bool(override.get("text_only", base.text_only)),
    )


def validate_route(role: str, route: ModelRoute) -> None:
    """Raise :class:`RouteError` if the route is invalid for the role."""
    if route.provider not in PROVIDERS:
        raise RouteError(f"role {role!r}: unknown provider {route.provider!r}")
    if not route.model:
        raise RouteError(f"role {role!r}: empty model")
    if route.text_only and role in TOOL_CAPABLE_ROLES:
        raise RouteError(
            f"role {role!r} must drive tools; a text-only route ({route.provider}) is not allowed"
        )


class ModelRegistry:
    """Resolves a role to its :class:`ModelRoute` across the override layers.

    ``settings_routes`` is the config-level map (``settings.yaml``); ``project_routes`` and
    ``run_routes`` are supplied per-resolution. All are partial ``{role: {...}}`` dicts."""

    def __init__(self, settings_routes: dict | None = None) -> None:
        self.settings_routes = settings_routes or {}

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
        return route
