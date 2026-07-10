"""The dreaming cage (Phase 16 Task 5) — the safety substrate BEFORE any content builder.

Dreaming is proposal-only automation: it may READ local, non-private data and produce artifacts +
attention proposals, and NOTHING else. The cage enforces that BY CONSTRUCTION, not by trusting the
job:

* **Positive allowlist.** :data:`DREAMING_TOOLS` is the ONLY set of tools a dreaming loop may hold —
  local, read-only, non-egress, non-private-read. :func:`build_dreaming_registry` builds a registry
  containing exactly that intersection and nothing else.
* **Explicit forbidden set + belt.** :data:`FORBIDDEN_TOOLS` enumerates every egress / write / shell
  / spawn / schedule / delete / connector tool. :func:`assert_caged` proves NONE is reachable and
  that every present tool is ``egress=False`` and ``reads_private=False``. The adversarial eval
  (Task 8) tries each forbidden tool by name against a caged context.
* **Hard budget cap.** :class:`DreamingBudget` tracks spend; on cap-hit the run halts and emits ONE
  alert attention item (:func:`emit_budget_halt_alert`) — never a silent overrun.

Proposal acceptance is ALWAYS a human on an existing gated route; dreaming itself never executes,
schedules, sends, or deletes. Outputs are untrusted (``model_generated``) and are never
auto-injected into a future model context (the attention_items quarantine).
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.attention.store import AttentionKind, AttentionPriority, AttentionStore
from jarvis.tools import ToolContext, ToolRegistry

#: The ONLY tools a dreaming loop may hold — local, read-only, non-egress, non-private. Reviews use
#: deterministic store reads + one summarize (no tools); the open-ended self-improvement builder may
#: read the repo/KB through these. Growing this set is a deliberate, adversarially-tested change.
DREAMING_TOOLS: frozenset[str] = frozenset(
    {"read_file", "list_dir", "glob_search", "query_knowledge_base", "list_tasks"}
)

#: Every tool a dreaming context must NEVER be able to reach (egress / write / shell / spawn /
#: schedule / delete / connector). The adversarial eval probes each by name. Kept explicit (not
#: derived) so a newly-added risky tool is caught by the exact-set check until it's classified.
FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "web_search", "web_fetch", "send_notification",
        "gmail_search", "gmail_read", "gmail_create_draft", "gmail_update_draft",
        "drive_search", "drive_fetch", "drive_create_doc", "drive_update_doc",
        "calendar_list_events", "calendar_create_event", "calendar_update_event",
        "calendar_cancel_event",
        "write_file", "run_shell", "spawn_agent", "schedule_task", "cancel_task",
        "remember", "forget", "ingest_source", "write_wiki_page",
    }
)


class DreamingCageError(RuntimeError):
    """Raised if the cage would admit a forbidden/risky tool — a construction-time failure, never a
    silent leak."""


def build_dreaming_registry(context: ToolContext) -> ToolRegistry:
    """A fresh registry holding EXACTLY the available :data:`DREAMING_TOOLS` — never more. A tool
    that isn't available in ``context`` is simply omitted (fewer, never extra). Each admitted tool
    is belt-checked ``egress=False`` + ``reads_private=False``; :func:`assert_caged` re-verifies."""
    full = ToolRegistry()
    full.discover("jarvis.tools.builtin", context)
    caged = ToolRegistry()
    for name in sorted(DREAMING_TOOLS):
        tool = full.get(name)
        if tool is None:
            continue  # not available here — the cage only ever shrinks, never grows
        if tool.egress or tool.reads_private:
            raise DreamingCageError(f"{name!r} is egress/private — cannot be a dreaming tool")
        caged.register(tool)
    assert_caged(caged)
    return caged


def assert_caged(registry: ToolRegistry) -> None:
    """Prove a registry is a valid dreaming cage: no forbidden tool present, nothing outside the
    allowlist, and every tool non-egress + non-private. Raises :class:`DreamingCageError` otherwise.
    This is the reachability guarantee the adversarial eval leans on."""
    names = set(registry.names())
    leaked = names & FORBIDDEN_TOOLS
    if leaked:
        raise DreamingCageError(f"forbidden tools reachable in dreaming: {sorted(leaked)}")
    extra = names - DREAMING_TOOLS
    if extra:
        raise DreamingCageError(f"non-allowlisted tools in dreaming registry: {sorted(extra)}")
    for name in names:
        tool = registry.get(name)
        if tool is not None and (tool.egress or tool.reads_private):
            raise DreamingCageError(f"{name!r} is egress/private — not allowed in dreaming")


@dataclass
class DreamingBudget:
    """The hard per-run spend cap. ``add`` accumulates each model call's cost; ``over_cap`` flips
    once spend meets the cap. A cap of 0 means dreaming is DISABLED (fail-closed) — ``over_cap`` is
    True from the start, so a run refuses before spending anything."""

    cap_usd: float
    spent_usd: float = 0.0

    def add(self, cost_usd: float | None) -> None:
        if cost_usd:
            self.spent_usd += cost_usd

    @property
    def over_cap(self) -> bool:
        if self.cap_usd <= 0:
            return True  # disabled ⇒ never run
        return self.spent_usd >= self.cap_usd

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)


async def emit_budget_halt_alert(
    store: AttentionStore, *, job: str, spent_usd: float, cap_usd: float
) -> int:
    """Create the ONE alert attention item a budget halt produces (idempotent per job — a re-run
    the same period returns the same row). Title is a plain count/amount — no job payload."""
    return await store.create(
        kind=AttentionKind.ALERT,
        source="dreaming",
        title=f"Dreaming halted: budget cap reached (${spent_usd:.2f} / ${cap_usd:.2f})",
        category="dreaming",
        priority=AttentionPriority.NORMAL,
        trust_class="model_generated",
        dedupe_key=f"dreaming-budget-halt:{job}",
    )
