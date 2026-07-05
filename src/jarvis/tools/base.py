"""Tool contract: what a tool *is* to the model vs. what it *does*.

The model only ever sees a tool's JSON schema (name + description + input schema);
it never runs code. Your :class:`ToolExecutor` owns the actual side effect. That
boundary — data to the model, code behind the executor — is where safety lives.

A tool subclasses :class:`Tool`, declares three class attributes (``name``,
``description``, ``Params``), a ``permission_default``, and implements
``async run(params)``. The pydantic ``Params`` model generates the JSON schema
sent to the API *and* validates the model's tool input before ``run`` sees it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel


class Permission(StrEnum):
    """A tool call's disposition. Lives here because a tool's *default* is intrinsic
    tool metadata; the PermissionGate (task 5) consumes this same enum."""

    ALLOW = "allow"  # run without asking
    ASK = "ask"  # prompt the human
    DENY = "deny"  # never run


class ToolResult(BaseModel):
    """What a tool hands back. ``is_error`` becomes the ``is_error`` flag on the
    ``tool_result`` block, so the *model* sees failures and can self-correct."""

    content: str
    is_error: bool = False


class Tool(ABC):
    """Base class for all tools.

    Subclasses set ``name`` / ``description`` / ``Params`` (a ``BaseModel``) and
    implement :meth:`run`. ``__init_subclass__`` enforces the three attributes on
    concrete subclasses so a misdeclared tool fails at import, not at call time.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    Params: ClassVar[type[BaseModel]]
    permission_default: ClassVar[Permission] = Permission.ASK

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return  # still abstract — an intermediate base, not a real tool
        for attr in ("name", "description", "Params"):
            if not getattr(cls, attr, None):
                raise TypeError(f"{cls.__name__} must define class attribute '{attr}'")

    def input_schema(self) -> dict:
        """JSON schema for this tool's input, derived from ``Params``."""
        return self.Params.model_json_schema()

    def tool_spec(self) -> dict:
        """The tool definition passed to the Anthropic API ``tools`` array."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema(),
        }

    @abstractmethod
    async def run(self, params: BaseModel) -> ToolResult | str:
        """Perform the tool's action. Receives a validated ``Params`` instance.

        Return a :class:`ToolResult` (or a plain ``str``, which the executor wraps
        as a success). Raising is fine — the executor converts exceptions into an
        error ``ToolResult`` so the model, not the process, handles the failure.
        """
        raise NotImplementedError
