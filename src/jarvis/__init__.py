"""Jarvis — a from-scratch agentic assistant.

The package is organized in layers with a strict dependency direction:
interfaces (cli) -> core (agent loop) -> services (tools, permissions, memory)
-> foundation (persistence, observability, config). See docs/PLAN.md.
"""

__version__ = "0.1.0"
