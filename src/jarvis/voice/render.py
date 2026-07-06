"""The calm, voice-safe renderer (Phase 7, Task 4 — checkpoint §1.6, ADR-0007 §5).

Governing rule: **voice summarizes safely; the screen holds the detail.** This renderer
is the ``VoiceOutput`` the session speaks through, and it enforces the **TTS privacy
rule** — it never voices secrets, tokens, full commands, file/message contents, or the
particulars of a risky action. The room can hear the speaker, so the spoken channel is a
broadcast; sensitive detail stays on the (private) screen (rendered there by the reused
``ConsoleRenderer``), and the enforcement lives *here*, not in the model's discretion.

Two guarantees, strongest first:

1. **The escalation announcement never touches ``call.input``.** When a risky action
   escalates, the renderer speaks a category phrase ("run a command", "send a message")
   plus "review it on screen" — structurally, the command/recipient/token in the tool
   input cannot reach the synthesizer.
2. **The final answer is masked + capped** before speaking — a best-effort backstop that
   redacts obvious secret patterns and trims long output to "…the rest is on screen."

Mid-turn tool/sub-agent events are **not voiced at all** (collapse-by-default) — the
event firehose belongs on the screen.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.core.agent import TurnResult
    from jarvis.core.client import ToolCall
    from jarvis.core.events import Event
    from jarvis.permissions.gate import Decision
    from jarvis.voice.protocols import TTSProvider

# Obvious secret shapes, masked before anything is spoken (a backstop — the primary
# guarantee is that previews/commands never reach on_result/announce at all).
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{6,}|ghp_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9._-]{10,}|[A-Fa-f0-9]{32,})"
)

# A risky tool → a plain category phrase for the spoken escalation (NEVER the input).
_ESCALATION_VERB: dict[str, str] = {
    "run_shell": "run a command",
    "write_file": "write a file",
    "web_fetch": "fetch a page",
    "web_search": "search the web",
    "schedule_task": "schedule a task",
    "cancel_task": "cancel a task",
    "remember": "save something to memory",
    "forget": "forget a memory",
    "ingest_source": "ingest a source",
    "write_wiki_page": "write a wiki page",
    "spawn_agent": "delegate to a sub-agent",
}


def _mask_secrets(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


class VoiceRenderer:
    """A ``VoiceOutput``: speaks a safe summary via a ``TTSProvider``. ``spoken`` records
    every string sent to the synthesizer, so a test can assert exactly what was voiced —
    the substrate for the TTS-privacy pins."""

    def __init__(
        self,
        tts: TTSProvider,
        *,
        play: Callable[[bytes], Awaitable[None]] | None = None,
        max_spoken_chars: int = 600,
    ) -> None:
        self.tts = tts
        self.play = play  # audio playback (Task 6); None in tests
        self.max_spoken_chars = max_spoken_chars
        self.spoken: list[str] = []

    async def _say(self, text: str) -> None:
        safe = _mask_secrets(text).strip()
        if len(safe) > self.max_spoken_chars:
            safe = safe[: self.max_spoken_chars].rstrip() + " … the rest is on screen."
        if not safe:
            return
        self.spoken.append(safe)
        audio = await self.tts.synthesize(safe)
        if self.play is not None:
            await self.play(audio)

    # --- VoiceOutput ---------------------------------------------------------

    async def on_heard(self, text: str) -> None:
        # Echo the user's own words (masked, in case they spoke a secret aloud) so a
        # mishear is caught before acting.
        await self._say(f"I heard: {text}")

    def __call__(self, event: Event) -> None:
        # Mid-turn events are NOT voiced — the tool/sub-agent firehose stays on the screen
        # (collapse-by-default). Intentionally a no-op for speech.
        return None

    async def on_result(self, result: TurnResult) -> None:
        # Speak the model's final answer as a safe, capped summary. Detailed/long output is
        # trimmed with a pointer to the screen; obvious secrets are masked as a backstop.
        if result.text and result.text.strip():
            await self._say(result.text)

    # --- escalation announcement (wired to VoiceApprover.on_escalate) -------

    async def announce_escalation(self, call: ToolCall, decision: Decision) -> None:
        """Speak that a confirmation is needed — the category and *where*, never the
        sensitive particulars. ``call.input`` is deliberately not referenced, so the
        command/recipient/token in it cannot reach the synthesizer."""
        verb = _ESCALATION_VERB.get(call.name, f"use {call.name}")
        await self._say(
            f"I've drafted it, but I can't approve that by voice — review it on screen to {verb}."
        )
