"""Model/provider registry (Phase 10 Task 6): roles → model routes, multi-provider.

Roles (planner/coder/reviewer/…) are resolved to a :class:`ModelRoute` (provider + model +
effort) by the :class:`ModelRegistry`; the :class:`ClientFactory` turns a route into an
:class:`~kira.core.client.LLMClient`, caching instances and failing CLOSED when a
provider's key is missing (never a silent downgrade). OpenAI is text-only this phase —
analysis roles (synthesis/review/judge) can run on it; write-capable executors stay
Anthropic. Keys are never exposed; the Hub reports provider presence booleans only.
"""

from __future__ import annotations

from kira.models.factory import ClientFactory
from kira.models.openai_client import OpenAIChatClient, UnsupportedToolUseError
from kira.models.registry import ModelRegistry, RouteError
from kira.models.roles import ROLES, ModelRoute, default_route

__all__ = [
    "ROLES",
    "ClientFactory",
    "ModelRegistry",
    "ModelRoute",
    "OpenAIChatClient",
    "RouteError",
    "UnsupportedToolUseError",
    "default_route",
]
