"""Agent core: the loop and the model-client boundary it runs on."""

from jarvis.core.agent import AgentLoop, Approver, EventSink, TurnResult
from jarvis.core.client import (
    FakeClient,
    LLMClient,
    ModelResponse,
    ToolCall,
    text_message,
    tool_use_message,
)
from jarvis.core.events import (
    Event,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from jarvis.core.prompts import build_system

__all__ = [
    "AgentLoop",
    "Approver",
    "Event",
    "EventSink",
    "FakeClient",
    "LLMClient",
    "ModelResponse",
    "TextDelta",
    "ToolCall",
    "ToolFinished",
    "ToolStarted",
    "TurnCompleted",
    "TurnResult",
    "build_system",
    "text_message",
    "tool_use_message",
]
