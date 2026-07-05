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

from jarvis.observability.cost import Usage


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
    ) -> None:
        self.budget = context_token_budget
        self.threshold = compaction_threshold
        self.keep_fraction = keep_fraction
        self._observed_input = 0  # last real input-token count (a floor for the estimate)

    def observe(self, usage: Usage) -> None:
        """Record the last response's context cost (all input-side token buckets)."""
        self._observed_input = (
            usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
        )

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

    def view(self, messages: list[dict]) -> CompactionView:
        """The message list to send to the API (never mutates ``messages``)."""
        if not messages or not self.should_compact(messages):
            return CompactionView(messages, cut=0, elided=0, overflow=False)
        cut = self._find_cut(messages)
        tail, elided, overflow = self._elide_to_fit(messages[cut:])
        return CompactionView(tail, cut=cut, elided=elided, overflow=overflow)

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
