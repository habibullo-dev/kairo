"""Suggestion pipeline (Phase 15 Task 4) — proposes memories/entities/edges into the QUARANTINED
``graph_suggestions`` table, where they are invisible to search/retrieval/export until a human
approves (Task 5). There is deliberately NO auto-approve path here or anywhere.

Two safety rules are enforced in code (pinned by tests), not left to the model:

* **Trust is the WORST of the evidence.** A proposal derived from any untrusted-external material
  (a web/email/doc excerpt) becomes an ``untrusted_external`` suggestion, so the review UI frames it
  — one untrusted evidence item taints the whole proposal. The model cannot upgrade trust.
* **Evidence is POINTERS only.** A suggestion stores ``{kind,id}`` references to its source
  material, never the raw body — the same bodies-free discipline as the read models.

Runs ONLY when explicitly invoked (``jarvis graph suggest``); it is not wired to a scheduler this
phase. Material is bounded (local run summaries) to cap the extractor's cost; the extractor is
injectable so the pipeline + safety are testable without a live model.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jarvis.graph.store import GraphStore

# best -> worst; the suggestion takes the worst trust present in its evidence.
_TRUST_ORDER = {"trusted_local": 0, "reviewed": 1, "model_generated": 2, "untrusted_external": 3}
_SENS_ORDER = {None: 0, "low": 1, "medium": 2, "high": 3, "private": 4}


@dataclass
class Material:
    """One bounded, local piece of context the extractor reads. ``trust_class`` is the material's
    provenance — it flows into any suggestion derived from it (worst wins)."""

    kind: str
    ref_id: str
    text: str
    trust_class: str
    sensitivity: str | None = None


# extract(materials) -> [{"kind": "memory"|"node"|"edge", "payload": {...}, "evidence": [idx, ...]}]
Extractor = Callable[[list[Material]], Awaitable[list[dict]]]


def _worst_trust(trusts: list[str]) -> str:
    return max(trusts, key=lambda t: _TRUST_ORDER.get(t, 3)) if trusts else "model_generated"


def _worst_sensitivity(sens: list[str | None]) -> str | None:
    return max(sens, key=lambda s: _SENS_ORDER.get(s, 0)) if sens else None


async def gather_material(store: GraphStore, project_id: int, *, limit: int = 20) -> list[Material]:
    """Bounded local material for a project: the most recent orchestration-run synthesis summaries
    (model-generated). Kept small to cap extractor cost. (Extendable to chats/digests/wiki.)"""
    limit = max(1, min(100, int(limit)))
    rows = await (await store.db.execute(
        "SELECT id, synthesis_summary FROM orchestration_runs WHERE project_id=? "
        "AND synthesis_summary IS NOT NULL AND synthesis_summary != '' ORDER BY id DESC LIMIT ?",
        (project_id, limit),
    )).fetchall()
    return [Material("run", str(rid), summary, "model_generated") for rid, summary in rows]


async def suggest(
    store: GraphStore,
    materials: list[Material],
    extract: Extractor,
    *,
    project_id: int,
    extractor_model: str,
    est_cost_usd: float | None = None,
) -> list[int]:
    """Run the extractor over ``materials`` and write each proposal as a QUARANTINED suggestion.
    Trust = worst evidence; sensitivity = worst evidence; evidence = pointers only. Returns the new
    suggestion ids. Nothing is ever auto-approved — these sit in ``pending`` for human review."""
    proposals = await extract(materials)
    ids: list[int] = []
    for p in proposals:
        kind = p.get("kind")
        if kind not in ("memory", "node", "edge"):
            continue  # ignore anything off-vocabulary the model returns
        idxs = [i for i in (p.get("evidence") or [])
                if isinstance(i, int) and 0 <= i < len(materials)]
        cited = [materials[i] for i in idxs]
        trust = _worst_trust([m.trust_class for m in cited])
        sens = _worst_sensitivity([m.sensitivity for m in cited])
        pointers = [{"kind": m.kind, "id": m.ref_id} for m in cited]  # POINTERS, never bodies
        ids.append(await store.add_suggestion(
            kind=kind, payload=p.get("payload") or {}, trust_class=trust, project_id=project_id,
            evidence=pointers, sensitivity=sens, extractor_model=extractor_model,
            est_cost_usd=est_cost_usd,
        ))
    return ids


_SYSTEM = (
    "You extract durable knowledge worth remembering from a project's own run summaries. Return "
    'ONLY a JSON array; each item is {"kind":"memory","payload":{"type":"fact","content":"…"},'
    '"evidence":[<material index>]}. Propose only stable, useful facts, and cite the material '
    "index each came from. No prose, no code fences — just the JSON array (or [] if empty)."
)


def utility_extractor(client: object, model: str, *, max_tokens: int = 1024) -> Extractor:
    """Production extractor: one utility-model call over the numbered materials -> proposals.
    The client is expected to be the ledgered utility client (so the call is accounted). Parsing is
    tolerant — a malformed response yields no proposals rather than an error (they'd be quarantined
    anyway)."""

    async def _extract(materials: list[Material]) -> list[dict]:
        if not materials:
            return []
        numbered = "\n\n".join(f"[material {i}] ({m.kind}:{m.ref_id})\n{m.text}"
                               for i, m in enumerate(materials))
        resp = await client.create(  # type: ignore[attr-defined]
            model=model, system=_SYSTEM,
            messages=[{"role": "user", "content": numbered}], tools=[], max_tokens=max_tokens,
        )
        text = getattr(resp, "text", "") or ""
        try:
            data = json.loads(text[text.index("["): text.rindex("]") + 1])
        except (ValueError, TypeError):
            return []
        return [p for p in data if isinstance(p, dict)]

    return _extract
