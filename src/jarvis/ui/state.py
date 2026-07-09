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

#: The output-config effort levels the human may pick, cheapest → most thorough. Fewer output
#: tokens at a lower effort ⇒ lower cost; higher effort ⇒ more reasoning/thoroughness. Matches
#: ``config.limits.effort``'s domain. ``POST /api/effort`` validates against this exact tuple.
EFFORT_LEVELS: tuple[tuple[str, str], ...] = (
    ("low", "Low — cheapest"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "Extra high"),
    ("max", "Max — most thorough"),
)

VALID_EFFORTS: frozenset[str] = frozenset(v for v, _ in EFFORT_LEVELS)


class InteractiveModelState:
    """The current interactive model id AND a per-model effort choice. Mirrors ``ModeState``: a
    tiny, mutable, in-process holder read by the loop through callables (``current`` /
    ``current_effort``, frozen per turn). ``set`` refuses anything outside
    :data:`ALLOWED_MODEL_IDS` (fail-closed — the private-context Anthropic-only pin); ``set_effort``
    refuses anything outside :data:`VALID_EFFORTS`.

    Effort is remembered PER MODEL (the user's ask: "control the effort depending on each model"),
    so switching model restores that model's chosen effort. A model with no explicit choice yet
    uses ``default_effort`` (``config.limits.effort``) ⇒ byte-identical to no selector."""

    def __init__(self, default: str, *, default_effort: str = "high") -> None:
        self._current = default
        self._default_effort = default_effort if default_effort in VALID_EFFORTS else "high"
        self._effort_by_model: dict[str, str] = {}

    def current(self) -> str:
        return self._current

    def current_effort(self) -> str:
        """The effort for the CURRENT model (its per-model choice, else the default)."""
        return self._effort_by_model.get(self._current, self._default_effort)

    def efforts(self) -> dict[str, str]:
        """The effective effort for every allowed model (for the picker to prefill)."""
        return {
            mid: self._effort_by_model.get(mid, self._default_effort) for mid in ALLOWED_MODEL_IDS
        }

    def set(self, model_id: str) -> None:
        if model_id not in ALLOWED_MODEL_IDS:
            raise ValueError(f"model must be one of {sorted(ALLOWED_MODEL_IDS)}")
        self._current = model_id

    def set_effort(self, effort: str, *, model_id: str | None = None) -> None:
        """Set the effort for ``model_id`` (default: the current model). Fail-closed on an unknown
        effort or an out-of-allowlist model."""
        if effort not in VALID_EFFORTS:
            raise ValueError(f"effort must be one of {sorted(VALID_EFFORTS)}")
        target = model_id or self._current
        if target not in ALLOWED_MODEL_IDS:
            raise ValueError(f"model must be one of {sorted(ALLOWED_MODEL_IDS)}")
        self._effort_by_model[target] = effort
