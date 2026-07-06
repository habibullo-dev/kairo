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
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from jarvis.agents.service import SubAgentService
    from jarvis.config import Config
    from jarvis.knowledge.service import KnowledgeService
    from jarvis.memory.service import MemoryService
    from jarvis.scheduler.service import TaskService


@dataclass
class ToolContext:
    """Dependencies a tool may need but shouldn't construct itself.

    Passed to tools at discovery/registration so a tool can reach config/secrets
    (e.g. the Tavily key for web search) or the memory service without reading
    globals or the process environment. Tools that need nothing simply ignore it.
    """

    config: Config | Any = None
    memory: MemoryService | Any = None  # None when long-term memory is disabled/unavailable
    tasks: TaskService | Any = None  # None when the scheduler is disabled
    knowledge: KnowledgeService | Any = None  # None when the knowledge base is disabled
    # Multi-agent delegation (Phase 6). None when disabled.
    agents: SubAgentService | Any = None


class Permission(StrEnum):
    """A tool call's disposition. Lives here because a tool's *default* is intrinsic
    tool metadata; the PermissionGate (task 5) consumes this same enum."""

    ALLOW = "allow"  # run without asking
    ASK = "ask"  # prompt the human
    DENY = "deny"  # never run


class _DefaultTimeout:
    """Sentinel type for :data:`DEFAULT_TIMEOUT`. A distinct singleton lets a tool's
    ``timeout_override`` express *three* states that a plain ``float | None`` cannot:
    use the executor's configured timeout (this sentinel), use a specific number of
    seconds (a float), or use *no* executor timeout at all (``None``)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "DEFAULT_TIMEOUT"


#: A tool's ``timeout_override`` defaults to this: "use the executor's timeout".
#: Set ``timeout_override = None`` on a tool that enforces its own deadline (so the
#: executor's global ``wait_for`` must not cut a legitimately long run short — this
#: is exactly ``spawn_agent``, which owns ``sub_agents.timeout_seconds`` itself).
DEFAULT_TIMEOUT = _DefaultTimeout()


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
    #: Per-tool execution deadline. ``DEFAULT_TIMEOUT`` (the default) means "use the
    #: executor's timeout"; a float overrides it; ``None`` disables the executor
    #: timeout so the tool can own its own (see :data:`DEFAULT_TIMEOUT`).
    timeout_override: ClassVar[float | None | _DefaultTimeout] = DEFAULT_TIMEOUT

    def __init__(self, context: ToolContext | None = None) -> None:
        self.context = context or ToolContext()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return  # still abstract — an intermediate base, not a real tool
        for attr in ("name", "description", "Params"):
            if not getattr(cls, attr, None):
                raise TypeError(f"{cls.__name__} must define class attribute '{attr}'")

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        """Whether this tool should be registered given ``context``.

        Default: always. A tool that depends on an optional collaborator (e.g. the
        memory service) overrides this to return False when it's absent — an
        unusable tool in the schema only wastes the model's attention and invites
        doomed calls. Checked by the registry at discovery time.
        """
        return True

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
