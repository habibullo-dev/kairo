"""Agent core: the loop and the model-client boundary it runs on."""

from kira.core.agent import AgentLoop, Approver, EventSink, TurnResult
from kira.core.anthropic_client import AnthropicClient, to_model_response
from kira.core.client import (
    FakeClient,
    LLMClient,
    ModelResponse,
    ToolCall,
    text_message,
    tool_use_message,
)
from kira.core.events import (
    Event,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from kira.core.execution import (
    ExecutionContext,
    ProjectExecutionScope,
    bind_execution_context,
    bind_project_scope,
    current_execution_context,
    current_project_scope,
)
from kira.core.prompts import build_system

__all__ = [
    "AgentLoop",
    "AnthropicClient",
    "Approver",
    "Event",
    "EventSink",
    "ExecutionContext",
    "FakeClient",
    "LLMClient",
    "ModelResponse",
    "ProjectExecutionScope",
    "TextDelta",
    "ToolCall",
    "ToolFinished",
    "ToolStarted",
    "TurnCompleted",
    "TurnResult",
    "build_system",
    "bind_execution_context",
    "bind_project_scope",
    "current_execution_context",
    "current_project_scope",
    "text_message",
    "to_model_response",
    "tool_use_message",
]
