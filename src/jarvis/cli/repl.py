"""Terminal REPL — the MVP interface.

Wires config -> client -> registry -> gate -> loop and drives it turn by turn.
Deliberately thin: it renders events, prompts for approvals, tracks session totals,
and lets Ctrl+C cancel a turn without killing the session. It knows nothing about
the loop's internals.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console

from jarvis.cli.render import ConsoleRenderer
from jarvis.config import Config
from jarvis.core import AgentLoop, AnthropicClient
from jarvis.core.client import LLMClient, ToolCall
from jarvis.observability import cost_of, get_logger
from jarvis.observability.cost import Usage
from jarvis.permissions import PermissionGate, load_policy
from jarvis.permissions.gate import Decision
from jarvis.tools import Permission, ToolContext, ToolExecutor, ToolRegistry


class Repl:
    def __init__(
        self,
        config: Config,
        *,
        client: LLMClient | None = None,
        console: Console | None = None,
    ) -> None:
        self.config = config
        self.console = console or Console()
        self.log = get_logger("jarvis.repl")

        self.registry = ToolRegistry()
        self.registry.discover("jarvis.tools.builtin", ToolContext(config=config))
        self.executor = ToolExecutor(
            timeout=config.limits.tool_timeout_seconds,
            max_result_chars=config.limits.max_tool_result_chars,
        )
        policy_path = config.root / "config" / "permissions.yaml"
        self.gate = PermissionGate(load_policy(policy_path), config.root, source_path=policy_path)
        self.client = client or AnthropicClient.from_config(config)
        self.loop = AgentLoop(
            client=self.client,
            registry=self.registry,
            executor=self.executor,
            gate=self.gate,
            config=config,
            approver=self._approve,
        )

        self.messages: list[dict] = []
        self.usage = Usage()
        self.renderer = ConsoleRenderer(self.console)

    # --- approval ----------------------------------------------------------

    async def _approve(self, call: ToolCall, decision: Decision) -> Permission:
        self.console.print(f"\n[yellow]Approve[/] [bold]{call.name}[/]?  [dim]{decision.reason}[/]")
        answer = (await asyncio.to_thread(input, "  [y]es / [N]o / [a]lways: ")).strip().lower()
        if answer in ("a", "always"):
            self._persist_always(call)
            return Permission.ALLOW
        if answer in ("y", "yes"):
            return Permission.ALLOW
        return Permission.DENY

    def _persist_always(self, call: ToolCall) -> None:
        """Persist an 'always allow' choice as narrowly as the tool allows."""
        if call.name == "run_shell":
            command = str(call.input.get("command", "")).strip()
            if command:
                self.gate.persist_shell_rule(command, Permission.ALLOW)
        elif call.name == "write_file":
            path = call.input.get("path")
            if path:
                self.gate.persist_write_dir(str(Path(path).parent))
        else:
            self.gate.persist_allow(call.name)

    # --- loop --------------------------------------------------------------

    async def run(self) -> None:
        # PromptSession is created here (not in __init__) because it opens the
        # terminal — deferring it keeps Repl constructible in tests without a TTY.
        session: PromptSession = PromptSession(history=InMemoryHistory())
        self.console.print(
            "[bold cyan]Jarvis[/] — ask me anything. Type [bold]exit[/] or press Ctrl-D to quit.\n"
        )
        while True:
            try:
                user_input = await session.prompt_async("you › ")
            except (EOFError, KeyboardInterrupt):
                self.console.print("\nBye.")
                return
            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                self.console.print("Bye.")
                return
            self.messages.append({"role": "user", "content": user_input})
            await self.run_turn()

    async def run_turn(self) -> None:
        self.renderer.reset()
        self.console.print("[bold green]jarvis ›[/] ", end="")
        task = asyncio.create_task(self.loop.run_turn(self.messages, on_event=self.renderer))
        try:
            result = await task
        except KeyboardInterrupt:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
                await task
            self.console.print("\n[yellow]Turn cancelled.[/]\n")
            return
        self.messages = result.messages
        self.usage = self.usage + result.usage
        self._print_status()

    def _print_status(self) -> None:
        cost = cost_of(self.config.models.main, self.usage)
        tokens = self.usage.input_tokens + self.usage.output_tokens
        self.console.print(f"[dim]session: {tokens:,} tokens · ${cost:.4f}[/]\n")
