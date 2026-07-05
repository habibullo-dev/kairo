"""System-prompt assembly.

Minimal for the MVP: a stable identity + operating instructions. This is the seam
where phase 2 injects recalled long-term memories and task 8 adds environment
context (cwd, date). Kept as a function so those additions compose cleanly.
"""

from __future__ import annotations

DEFAULT_IDENTITY = """\
You are Jarvis, a precise, capable agentic assistant running on the user's machine.

Operating principles:
- Use tools when they let you act or verify; don't guess when you can check.
- After acting, briefly say what you did and what you found.
- If a tool returns an error, read it and adapt — try a different approach or ask.
- If a tool call is denied, do not retry it; explain and offer an alternative.
- Be concise and lead with the outcome."""


def build_system(*, extra: str | None = None) -> str:
    """Assemble the system prompt. ``extra`` appends context (memories, cwd, date)
    added by later phases."""
    if extra:
        return f"{DEFAULT_IDENTITY}\n\n{extra}"
    return DEFAULT_IDENTITY
