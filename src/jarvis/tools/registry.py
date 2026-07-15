"""Tool registry: the set of tools the agent can call, and their API schemas.

Adding a capability to Kira is meant to be one file plus one policy line — so
the registry auto-discovers concrete :class:`Tool` subclasses under a package
(``jarvis.tools.builtin`` by default). ``specs()`` produces the ``tools`` array
for the Anthropic request.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType

from jarvis.tools.base import Tool, ToolContext


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self) -> list[dict]:
        """Tool definitions for the Anthropic ``tools`` request field."""
        return [tool.tool_spec() for tool in self._tools.values()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def register_from_module(self, module: ModuleType, context: ToolContext | None = None) -> int:
        """Register every concrete Tool subclass *defined in* ``module``.

        The ``__module__`` check means an imported base class (or a tool imported
        for re-export) isn't double-registered — only classes authored in this
        module count. ``context`` is injected into each tool's constructor.
        """
        ctx = context or ToolContext()
        count = 0
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, Tool)
                and obj is not Tool
                and not getattr(obj, "__abstractmethods__", None)
                and obj.__module__ == module.__name__
                and obj.is_available(ctx)
            ):
                self.register(obj(ctx))
                count += 1
        return count

    def discover(
        self, package: str = "jarvis.tools.builtin", context: ToolContext | None = None
    ) -> int:
        """Import ``package`` and all its submodules, registering the tools found
        (with ``context`` injected). Returns the number registered."""
        pkg = importlib.import_module(package)
        count = self.register_from_module(pkg, context)
        for info in pkgutil.iter_modules(getattr(pkg, "__path__", []), prefix=f"{pkg.__name__}."):
            count += self.register_from_module(importlib.import_module(info.name), context)
        return count


class ScopedRegistry:
    """A read-only, filtered *view* over a :class:`ToolRegistry` exposing only an
    allowed set of tool names — a sub-agent's registry (Phase 6, see
    docs/PLAN-6-multi-agent.md).

    Deliberately **composition, not a subclass**: it structurally cannot expose the
    parent's full tool set, its mutation methods (``register``), or a tool the spawn
    didn't scope in. It provides exactly the read surface an
    :class:`~jarvis.core.agent.AgentLoop` uses (``specs`` / ``get``) plus
    ``names`` / ``__contains__`` / ``__len__``. Scope is UX here; the
    ``SubAgentGate`` enforces the same scope again at call time (defense in depth),
    and an out-of-scope name that reaches the loop still emits ``ToolDecision`` via
    the loop's unknown-tool path, so even out-of-scope *attempts* stay observable.
    """

    def __init__(self, base: ToolRegistry, allowed: frozenset[str]) -> None:
        self._base = base
        # Intersect with what actually exists: a scoped name with no backing tool is
        # simply not exposed (the gate denies it too — scope is enforced twice).
        self._allowed = frozenset(name for name in allowed if name in base)

    def get(self, name: str) -> Tool | None:
        return self._base.get(name) if name in self._allowed else None

    def names(self) -> list[str]:
        return [name for name in self._base.names() if name in self._allowed]

    def specs(self) -> list[dict]:
        """Tool definitions for the Anthropic ``tools`` field — the scoped subset only."""
        specs: list[dict] = []
        for name in self.names():
            tool = self._base.get(name)
            if tool is not None:
                specs.append(tool.tool_spec())
        return specs

    def __contains__(self, name: object) -> bool:
        return name in self._allowed

    def __len__(self) -> int:
        return len(self._allowed)
