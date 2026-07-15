"""The voice approval seam — the safety core of Phase 7 (checkpoint §1.3/§1.7).

A voice turn removes the keyboard, and with it the permission model's authenticated-
approval channel. The rule (ADR-0007): a voice turn resolves an ``ASK`` only by
**escalating to a screen** — a display with the exact action rendered and an
authenticated (physical/session) input path — and **fail-closed** if no such screen can
be positively confirmed. There is exactly one approval path: :class:`VoiceApprover` is
the injected ``Approver``, so this cannot be bypassed by any realtime plumbing.

v1 policy is the checkpoint's safe default: **escalate every ``ASK``** (no voice-resolved
risky actions). Read-only tools are ``ALLOW`` by policy, so the approver is never called
for them — the read-only default falls out of the gate, not this code. A "spoken yes"
cannot approve anything: this approver has no audio input; it consults only the screen.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from kira.tools.base import Permission

if TYPE_CHECKING:
    from rich.console import Console

    from kira.core.client import ToolCall
    from kira.permissions.gate import Decision


@runtime_checkable
class ScreenApprover(Protocol):
    """A confirmation surface with an authenticated input path. ``available()`` must
    *positively* confirm the screen is present, can render the preview, and is attended
    (checkpoint §1.3 — uncertainty resolves to unavailable). ``confirm()`` shows the exact
    action and captures a typed/tapped y/N."""

    def available(self) -> bool: ...

    async def confirm(self, call: ToolCall, decision: Decision) -> Permission: ...


class VoiceApprover:
    """The injected ``Approver`` for a voice turn. Escalates every ``ASK`` to the screen
    and is fail-closed: no positively-available screen ⇒ DENY (the ``HeadlessApprover``
    posture, for voice). Never resolves a risky action by voice, and has no path by which
    a spoken "yes" could approve anything."""

    def __init__(
        self,
        screen: ScreenApprover | None,
        *,
        on_escalate: Callable[[ToolCall, Decision], None] | None = None,
    ) -> None:
        #: The screen to escalate to; None ⇒ there is no screen ⇒ every ASK is denied.
        self.screen = screen
        #: Called before escalating (the renderer speaks a safe "confirm on screen" — it
        #: must NOT voice the sensitive preview; that is the renderer's TTS-privacy job).
        self.on_escalate = on_escalate
        self.escalations = 0
        self.denied = 0  # ASKs denied because no screen was available (fail-closed)

    async def __call__(self, call: ToolCall, decision: Decision) -> Permission:
        self.escalations += 1
        if self.on_escalate is not None:
            # Tolerate a sync or async callback (the renderer's announcement is async;
            # a test double may be sync). It must speak a SAFE line — never the preview.
            maybe = self.on_escalate(call, decision)
            if inspect.isawaitable(maybe):
                await maybe
        # Fail-closed: never assume a screen. Absent OR not positively available ⇒ deny.
        if self.screen is None or not self.screen.available():
            self.denied += 1
            return Permission.DENY
        # The exact action goes to the screen (full detail is fine there — it's private);
        # the screen commits or declines by an authenticated keystroke.
        return await self.screen.confirm(call, decision)


class TerminalScreenApprover:
    """The screen = the terminal. ``available()`` is a positive TTY check (an attended
    terminal); ``confirm()`` shows the *full* action preview on the (private) screen and
    reads a typed y/N. No "always" — a voice-escalated risky action is confirmed
    individually, never persisted (prepare-never-commit)."""

    def __init__(self, console: Console, summary_fn: Callable[[ToolCall], str]) -> None:
        self.console = console
        self.summary_fn = summary_fn  # e.g. cli.repl._call_summary (injected in Task 8)

    def available(self) -> bool:
        try:
            return sys.stdin.isatty()  # a positive check; anything else ⇒ unavailable
        except (OSError, ValueError):
            return False

    async def confirm(self, call: ToolCall, decision: Decision) -> Permission:
        self.console.print(
            f"\n[yellow]Confirm on screen[/] [bold]{call.name}[/]?  [dim]{decision.reason}[/]"
        )
        summary = self.summary_fn(call)
        if summary:
            self.console.print(f"  [dim]{summary}[/]")  # full detail — the screen is private
        answer = (await asyncio.to_thread(input, "  [y]es / [N]o: ")).strip().lower()
        return Permission.ALLOW if answer in ("y", "yes") else Permission.DENY


@dataclass
class ScriptedScreenApprover:
    """A :class:`ScreenApprover` double for tests and eval scenarios. ``is_available``
    models screen presence; ``answers`` are the scripted confirm results — **default DENY
    when exhausted**, so the double is fail-closed too. Records what it was asked to
    confirm, so a test can assert the exact action reached the screen."""

    is_available: bool = True
    answers: list[Permission] = field(default_factory=list)
    confirmed: list[dict] = field(default_factory=list)

    def available(self) -> bool:
        return self.is_available

    async def confirm(self, call: ToolCall, decision: Decision) -> Permission:
        self.confirmed.append({"name": call.name, "input": call.input})
        return self.answers.pop(0) if self.answers else Permission.DENY
