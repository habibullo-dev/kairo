"""ContextManager: keep the message list sent to the API under the token budget.

The model is stateless, so every call replays the whole conversation — which grows
without bound. This produces a *view* of the messages to send, while the caller
keeps the full uncompacted history (that's the source of truth for persistence and
reflection; a lossy summary must never overwrite it).

Two mechanisms, in order:

1. **Cut at a turn boundary.** When the estimated size crosses the threshold, drop
   the oldest turns and keep a recent tail. The cut is *token-weighted* (keep the
   largest tail that fits a target) and always lands on a **real user turn** — a
   ``role=user`` message with no ``tool_result`` block — so a ``tool_use`` is never
   split from its ``tool_result`` and thinking blocks drop only as whole messages.
   (Dropping whole messages is API-legal; *editing* a replayed assistant block is
   not.) In Task 7 the dropped prefix is represented by a system-prompt summary.

2. **Elide inside the live turn.** A single turn can exceed the budget on its own
   (many tool iterations), and there's no boundary to cut at. Then we shrink the
   *bodies* of the oldest ``tool_result`` blocks in the view — those are unsigned
   user-role content, safe to edit — oldest first. If even that can't fit, the view
   reports ``overflow`` and the loop ends the turn rather than send a doomed request.

Token accounting needs no extra API call: estimate every current message by
chars/4 (which *over*-counts replayed thinking that the server strips, erring
early/safe), floored by the last real ``input_tokens`` we observed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from jarvis.core.client import LLMClient
from jarvis.observability.cost import Usage
from jarvis.observability.ledger import cost_scope

_SUMMARY_SYSTEM = """\
You are compacting a long assistant/user conversation so it fits the context \
window. Produce a dense, faithful summary that preserves everything needed to \
continue the work:
- decisions made and their rationale
- facts and values established (file paths, names, numbers, tool results)
- open threads and what the user still wants
- the user's intent and any constraints they set
Write compact prose or bullets. Invent nothing. If given a PRIOR SUMMARY, extend \
it with the new messages rather than repeating it."""


@dataclass
class CompactionView:
    """A per-request view of the messages, plus what was done to produce it."""

    messages: list[dict]
    cut: int  # the view is derived from full_messages[cut:]
    elided: int  # count of tool_result bodies shrunk to fit
    overflow: bool  # True => even elision couldn't fit; the loop should stop


def _has_tool_result(content: object) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _is_real_user_turn(message: dict) -> bool:
    """A user message that *starts* a turn — not a tool-result carrier."""
    return message.get("role") == "user" and not _has_tool_result(message.get("content"))


def _render_for_summary(messages: list[dict]) -> str:
    """Flatten messages to plain text for the summarizer (thinking blocks dropped)."""
    lines: list[str] = []
    for m in messages:
        role = str(m.get("role", "?")).upper()
        content = m.get("content")
        if isinstance(content, str):
            lines.append(f"{role}: {content}")
            continue
        for b in content if isinstance(content, list) else []:
            t = b.get("type")
            if t == "text":
                lines.append(f"{role}: {b.get('text', '')}")
            elif t == "tool_use":
                args = json.dumps(b.get("input", {}), ensure_ascii=False)[:200]
                lines.append(f"{role} called {b.get('name')}({args})")
            elif t == "tool_result":
                body = b.get("content")
                body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                lines.append(f"TOOL RESULT: {body[:500]}")
    return "\n".join(lines)


def _message_chars(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False))


class ContextManager:
    def __init__(
        self,
        *,
        context_token_budget: int = 180_000,
        compaction_threshold: float = 0.7,
        keep_fraction: float = 0.5,
        summarizer: LLMClient | None = None,
        utility_model: str = "claude-sonnet-5",
    ) -> None:
        self.budget = context_token_budget
        self.threshold = compaction_threshold
        self.keep_fraction = keep_fraction
        self.summarizer = summarizer  # None => drop the prefix without a summary
        self.utility_model = utility_model
        self._observed_input = 0  # last real input-token count (a floor for the estimate)
        self._summary: str | None = None  # covers full_messages[:_covered_cut]
        self._covered_cut = 0

    def observe(self, usage: Usage) -> None:
        """Record the last response's context cost (all input-side token buckets)."""
        self._observed_input = (
            usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
        )

    # --- summary state (persisted on the session, restored on --resume) ----

    def state(self) -> tuple[str | None, int]:
        return self._summary, self._covered_cut

    def restore(self, summary: str | None, cut: int | None) -> None:
        self._summary = summary
        self._covered_cut = cut or 0

    # --- estimation --------------------------------------------------------

    def _estimate(self, messages: list[dict]) -> int:
        return sum(_message_chars(m) for m in messages) // 4

    def _current_size(self, messages: list[dict]) -> int:
        # Floor the chars estimate by the last measured input count: the real
        # tokenizer said the context was at least that big, and it only grew.
        return max(self._estimate(messages), self._observed_input)

    def should_compact(self, messages: list[dict]) -> bool:
        return self._current_size(messages) > self.threshold * self.budget

    # --- view --------------------------------------------------------------

    def view(self, messages: list[dict], *, cut: int | None = None) -> CompactionView:
        """The message list to send to the API (never mutates ``messages``).

        ``cut`` may be supplied to *freeze* the drop point for a whole turn (so the
        summary and cut stay stable across iterations); when omitted, the cut is
        computed from the current messages (used by tests and the no-freeze path)."""
        if not messages:
            return CompactionView(messages, cut=0, elided=0, overflow=False)
        if cut is None:
            if not self.should_compact(messages):
                return CompactionView(messages, cut=0, elided=0, overflow=False)
            cut = self._find_cut(messages)
        cut = min(cut, len(messages))
        tail, elided, overflow = self._elide_to_fit(messages[cut:])
        return CompactionView(tail, cut=cut, elided=elided, overflow=overflow)

    async def summary_for(self, messages: list[dict]) -> tuple[int, str | None]:
        """Decide this turn's frozen (cut, summary). Call once per turn.

        The summary covers everything before the cut and is built *incrementally* —
        only the newly-dropped messages are folded into the prior summary, so we
        re-summarize a slice, not the whole prefix, each time the cut advances."""
        if not self.should_compact(messages):
            return 0, None
        cut = self._find_cut(messages)
        if cut > self._covered_cut:
            self._summary = await self._summarize(self._summary, messages[self._covered_cut : cut])
            self._covered_cut = cut
        # Return the covered cut: the view drops exactly what the summary represents,
        # so no message is ever both summarized *and* shown.
        return self._covered_cut, self._summary

    async def _summarize(self, prior: str | None, new_messages: list[dict]) -> str | None:
        if self.summarizer is None or not new_messages:
            return prior
        parts = []
        if prior:
            parts.append(f"PRIOR SUMMARY:\n{prior}\n")
        parts.append("NEW MESSAGES TO FOLD IN:\n" + _render_for_summary(new_messages))
        with cost_scope(purpose="compaction"):
            response = await self.summarizer.create(
                model=self.utility_model,
                system=_SUMMARY_SYSTEM,
                messages=[{"role": "user", "content": "\n".join(parts)}],
                tools=[],
                max_tokens=2000,
            )
        return response.text or prior

    def _find_cut(self, messages: list[dict]) -> int:
        """Index of the first kept message: the earliest real-user boundary whose
        suffix fits the keep target (largest tail that fits). If none fits, the last
        boundary (smallest tail) — elision then handles the overflow."""
        target = self.keep_fraction * self.budget
        boundaries = [i for i, m in enumerate(messages) if _is_real_user_turn(m)]
        if not boundaries:
            return 0
        for b in boundaries:
            if self._estimate(messages[b:]) <= target:
                return b
        return boundaries[-1]

    def _elide_to_fit(self, tail: list[dict]) -> tuple[list[dict], int, bool]:
        """Shrink oldest tool_result bodies until the tail fits, or report overflow.

        Only tool_result content is touched (unsigned user-role data); tool_use ids
        and block structure are preserved, and the input list is never mutated."""
        if self._estimate(tail) <= self.budget:
            return tail, 0, False
        out = list(tail)
        elided = 0
        for i, m in enumerate(out):
            if m.get("role") != "user" or not isinstance(m.get("content"), list):
                continue
            new_blocks, changed = [], False
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    body = b.get("content")
                    n = len(body) if isinstance(body, str) else len(json.dumps(body))
                    new_blocks.append({**b, "content": f"[elided: {n} chars]"})
                    changed = True
                    elided += 1
                else:
                    new_blocks.append(b)
            if changed:
                out[i] = {**m, "content": new_blocks}
                if self._estimate(out) <= self.budget:
                    break
        return out, elided, self._estimate(out) > self.budget
