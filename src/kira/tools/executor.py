"""Tool executor: the guarded boundary between the model and real side effects.

Three invariants, each a classic agent bug when violated:

* **Validation** — the model's raw input is validated against the tool's
  ``Params`` before ``run``; a bad shape becomes an error result, not a crash.
* **Errors are results** — timeouts and exceptions are captured and returned as
  ``is_error`` results so the *model* sees and recovers from them.
* **Truncation** — output is capped so one giant read can't silently blow the
  context window and evict the rest of the conversation.

Audit logging of tool calls lives in the agent loop (which owns the ``trace_id``),
keeping this executor a pure, easily-tested unit.
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from kira.tools.base import DEFAULT_TIMEOUT, Tool, ToolResult


class ToolExecutor:
    def __init__(self, *, timeout: float = 60.0, max_result_chars: int = 24_000) -> None:
        self.timeout = timeout
        self.max_result_chars = max_result_chars

    async def execute(self, tool: Tool, raw_input: dict | None) -> ToolResult:
        try:
            params = tool.Params(**(raw_input or {}))
        except ValidationError as exc:
            return ToolResult(content=f"Invalid input for '{tool.name}': {exc}", is_error=True)

        timeout = self._timeout_for(tool)
        try:
            if timeout is None:
                # The tool owns its own deadline (e.g. spawn_agent enforces
                # sub_agents.timeout_seconds itself). A global wait_for here would
                # cut a legitimately long run short and turn a clean, recordable
                # timeout into an anonymous executor kill.
                result = await tool.run(params)
            else:
                result = await asyncio.wait_for(tool.run(params), timeout=timeout)
        except TimeoutError:
            # Only reachable when timeout is a float (wait_for path), so it prints.
            return ToolResult(content=f"'{tool.name}' timed out after {timeout:g}s.", is_error=True)
        except Exception as exc:  # noqa: BLE001 - tool failures are model feedback, not crashes
            return ToolResult(
                content=f"'{tool.name}' failed: {type(exc).__name__}: {exc}", is_error=True
            )

        if isinstance(result, str):
            result = ToolResult(content=result)
        return self._truncate(result)

    def _timeout_for(self, tool: Tool) -> float | None:
        """Effective execution timeout for ``tool``: the ``DEFAULT_TIMEOUT`` sentinel
        resolves to the executor default; a float is used as-is; ``None`` means no
        executor timeout (the tool enforces its own)."""
        override = getattr(tool, "timeout_override", DEFAULT_TIMEOUT)
        if override is DEFAULT_TIMEOUT:
            return self.timeout
        return override

    def _truncate(self, result: ToolResult) -> ToolResult:
        if len(result.content) <= self.max_result_chars:
            return result
        dropped = len(result.content) - self.max_result_chars
        body = result.content[: self.max_result_chars]
        return ToolResult(
            content=f"{body}\n\n[... truncated {dropped} chars to protect the context window ...]",
            is_error=result.is_error,
        )
