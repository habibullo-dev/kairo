"""Tool registry: the set of tools the agent can call, and their API schemas.

Adding a capability to Jarvis is meant to be one file plus one policy line — so
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
        count = 0
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, Tool)
                and obj is not Tool
                and not getattr(obj, "__abstractmethods__", None)
                and obj.__module__ == module.__name__
            ):
                self.register(obj(context))
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
