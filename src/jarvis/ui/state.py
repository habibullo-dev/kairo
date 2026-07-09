"""Interactive UI runtime state (Phase 15.5).

``InteractiveModelState`` is the ``ModeState``-shaped holder for the model the INTERACTIVE
conversation runs on. The agent loop reads it via an injected callable (frozen per turn), so a
switch applies from the next turn — never mid-flight. It is deliberately Anthropic-ONLY: the main
chat carries private context (memory, project state), and Phase 10C pins ``private_ok=True`` to
anthropic. Switching here never touches the ``ModelRegistry`` routes (planner/judge/utility keep
their authority pins); it only chooses which Anthropic model answers the human.
"""

from __future__ import annotations

#: The switchable interactive models — all Anthropic (private_ok). ``POST /api/model`` validates
#: against this exact set; anything else (an external provider, an unknown id) is refused. Labels
#: are the human-facing names shown on the composer chip.
INTERACTIVE_MODELS: tuple[tuple[str, str], ...] = (
    ("claude-fable-5", "Fable 5"),
    ("claude-opus-4-8", "Opus 4.8"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5"),
)

#: External providers surfaced (visible-but-disabled) in the model picker, with the honest reason
#: they can't drive the main chat. Availability/state is resolved from the provider catalog.
EXTERNAL_CHAT_PROVIDERS: tuple[str, ...] = ("openai", "gemini", "qwen", "deepseek", "zai")

ALLOWED_MODEL_IDS: frozenset[str] = frozenset(mid for mid, _ in INTERACTIVE_MODELS)


class InteractiveModelState:
    """The current interactive model id. Mirrors ``ModeState``: a tiny, mutable, in-process
    holder read by the loop through a callable. ``set`` refuses anything outside
    :data:`ALLOWED_MODEL_IDS` (fail-closed — the private-context Anthropic-only pin)."""

    def __init__(self, default: str) -> None:
        self._current = default

    def current(self) -> str:
        return self._current

    def set(self, model_id: str) -> None:
        if model_id not in ALLOWED_MODEL_IDS:
            raise ValueError(f"model must be one of {sorted(ALLOWED_MODEL_IDS)}")
        self._current = model_id
