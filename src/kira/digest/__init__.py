"""The Daily Digest (Phase 9): deterministic collectors + one tool-less summarize.

A digest is NOT an agent loop. Deterministic collectors gather today's schedule, unread email
headers, repo state, open tasks, the KB review queue, and eval freshness; exactly ONE tool-less
model call turns them into a calm briefing. Because that call has no tools, injected email text
can colour the wording but can never trigger an action (ADR-0010).

Two safety properties dominate (docs/PLAN-9-daily.md, amendments A3/A4):
* Storage is minimized: only snippets/counts/headers/provenance/status are persisted — never a
  raw email body or a provider error body. A failed collector renders a friendly "needs
  reconnect / unavailable" reason, never "zero results".
* Delivery is UI/DB-first: the digest is stored and posted to the UI before any notifier send;
  notifier delivery is best-effort and its output is treated as an egress payload.
"""

from kira.digest.builder import (
    DigestBuilder,
    DigestItem,
    DigestOutcome,
    Section,
    ensure_digest_task,
)
from kira.digest.store import DigestRecord, DigestStore

__all__ = [
    "DigestBuilder",
    "DigestItem",
    "DigestOutcome",
    "DigestRecord",
    "DigestStore",
    "Section",
    "ensure_digest_task",
]
