"""Render agent events to the terminal with rich.

A thin consumer of the loop's event stream: streamed text prints live, tool calls
show as panels, results as a check/cross line. The loop knows nothing about any of
this — swapping in a web UI later means writing a different event consumer.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from kira.core.events import (
    Event,
    SubAgentCompleted,
    SubAgentEvent,
    TextDelta,
    ToolDecision,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)


def _format_args(data: dict) -> str:
    parts = []
    for k, v in data.items():
        s = repr(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _oneline(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class ConsoleRenderer:
    """Callable event sink for ``AgentLoop.run_turn(on_event=...)``."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._mid_text = False

    def reset(self) -> None:
        self._mid_text = False

    def _end_text(self) -> None:
        if self._mid_text:
            self.console.print()
            self._mid_text = False

    def __call__(self, event: Event) -> None:
        if isinstance(event, TextDelta):
            self.console.print(event.text, end="", markup=False, highlight=False, soft_wrap=True)
            self._mid_text = True
        elif isinstance(event, ToolStarted):
            self._end_text()
            self.console.print(
                Panel(
                    f"[bold]{event.name}[/]({_format_args(event.input)})",
                    title="tool",
                    border_style="cyan",
                    expand=False,
                )
            )
        elif isinstance(event, ToolFinished):
            style = "red" if event.is_error else "green"
            mark = "✗" if event.is_error else "✓"
            self.console.print(f"[{style}]{mark} {event.name}[/] [dim]{_oneline(event.preview)}[/]")
        elif isinstance(event, TurnCompleted):
            self._end_text()
            if event.stop_reason == "max_iterations":
                self.console.print("[yellow]Stopped: reached the max tool-iteration guard.[/]")
            elif event.stop_reason == "max_context":
                self.console.print("[yellow]Stopped: conversation exceeds the context window.[/]")
            elif event.stop_reason == "refusal":
                self.console.print("[yellow]The model declined to respond.[/]")
        elif isinstance(event, SubAgentEvent):
            self._render_child(event)
        elif isinstance(event, SubAgentCompleted):
            self._end_text()
            cost = f" · ${event.cost_usd:.4f}" if event.cost_usd is not None else ""
            self.console.print(f'[magenta]⤷ sub-agent "{event.title}" {event.status}{cost}[/]')

    def _render_child(self, env: SubAgentEvent) -> None:
        """Compact, tagged lines for a child's tool activity — never its text (two
        parallel children streaming markdown would be noise; the report arrives as the
        spawn result). Denied attempts are shown so a caught injection is visible."""
        inner = env.inner
        tag = f"[magenta]⤷ {env.title}[/]"
        if isinstance(inner, ToolStarted):
            self._end_text()
            self.console.print(f"{tag} [cyan]{inner.name}[/]([dim]{_format_args(inner.input)}[/])")
        elif isinstance(inner, ToolFinished):
            style = "red" if inner.is_error else "green"
            mark = "✗" if inner.is_error else "✓"
            self.console.print(
                f"{tag} [{style}]{mark} {inner.name}[/] [dim]{_oneline(inner.preview)}[/]"
            )
        elif isinstance(inner, ToolDecision) and inner.resolution == "deny":
            self.console.print(
                f"{tag} [red]✗ {inner.name} denied[/] [dim]({inner.gate_decision})[/]"
            )
        # child TextDelta / allowed ToolDecision / TurnCompleted: intentionally not rendered
