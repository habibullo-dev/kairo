"""Federated search: one query across the seven FTS domains, returning short scoped snippets.

Flow per domain: :func:`kira.persistence.fts.query_domain` runs the sanitised MATCH and applies
project scope + status/visibility **in SQL** (never in MATCH), returning ranked base-row ids; a
per-domain *hydrator* then fetches a clean display title + a short plain-text snippet + timestamp.

Invariants:
* **Snippets only.** Every result carries a capped, whitespace-collapsed snippet — never a full
  body. Chat content (stored as JSON blocks) is projected to plain prose first, so tool-call /
  thinking scaffolding never leaks into a snippet.
* **Scope is inherited from query_domain**, so a project-B search can never surface project-A rows
  (adversarially pinned). This module adds no scope logic of its own — it only hydrates ids that
  the scoped query already returned.
* **Chats dedupe to one result per session** (many message hits → the best-ranked, keyed by the
  session so navigation opens the chat).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

import aiosqlite

from kira.persistence.fts import ANY_PROJECT, query_domain

_SNIPPET_CHARS = 200
_TITLE_CHARS = 80
_WORD = re.compile(r"[^\w]+", re.UNICODE)

#: Default federated set + display order (chats first, artifacts last).
DEFAULT_DOMAINS: tuple[str, ...] = (
    "chats", "memories", "knowledge", "tasks", "orchestration", "digests", "artifacts",
)


@dataclass(frozen=True)
class SearchResult:
    domain: str
    ref_id: int  # the row to navigate to (a session id for chats, else the base-row id)
    project_id: int | None
    title: str
    snippet: str
    ts: str | None
    score: float
    kind: str | None = None
    sensitivity: str | None = None


def _terms(raw: str) -> list[str]:
    return [t for t in _WORD.split(raw or "") if t]


def _plain_text_from_message_content(raw: str) -> str:
    """messages.content is JSON — a string, or a list of content blocks. Project to plain prose:
    keep 'text' blocks (and bare strings), drop tool_use / thinking / tool_result scaffolding."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw or ""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        parts: list[str] = []
        for block in data:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return " ".join(p for p in parts if p)
    return ""


def _snippet(body: str, terms: list[str], *, limit: int = _SNIPPET_CHARS) -> str:
    """A short, whitespace-collapsed, plain-text window around the first matching term (else the
    head of the body). Never returns more than ``limit`` characters of content."""
    text = " ".join((body or "").split())
    if not text:
        return ""
    low = text.lower()
    pos = -1
    for t in terms:
        p = low.find(t.lower())
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos <= 0:
        return text[:limit] + ("…" if len(text) > limit else "")
    start = max(0, pos - 40)
    end = min(len(text), start + limit)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def _title(text: str) -> str:
    first = " ".join((text or "").split())
    return first[:_TITLE_CHARS] if first else "(untitled)"


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


async def _hydrate_chats(db, ids, scores, terms):
    rows = await (await db.execute(
        f"SELECT m.id, m.content, s.id, s.title, s.project_id, s.updated_at "
        "FROM messages m JOIN sessions s ON s.id = m.session_id "
        f"WHERE m.id IN ({_placeholders(len(ids))})",
        tuple(ids),
    )).fetchall()
    by_msg = {r[0]: r for r in rows}
    out: list[SearchResult] = []
    seen_sessions: set[int] = set()
    lowered = [t.lower() for t in terms]
    for mid in ids:  # ids are best-ranked first
        r = by_msg.get(mid)
        if r is None:
            continue
        session_id = r[2]
        if session_id in seen_sessions:
            continue  # dedupe: one result per chat, keep the best-ranked hit
        body = _plain_text_from_message_content(r[1])
        # messages_fts indexes the raw JSON (incl. tool_use / thinking scaffolding), so a hit
        # can land entirely in content the user never typed. Only surface a chat whose VISIBLE
        # prose actually contains the query — no phantom matches, no blank snippets, and model
        # reasoning / tool arguments never become discoverable via chat search.
        low = body.lower()
        if not any(t in low for t in lowered):
            continue
        seen_sessions.add(session_id)
        out.append(SearchResult(
            domain="chats", ref_id=session_id, project_id=r[4],
            title=_title(r[3] or f"Chat {session_id}"), snippet=_snippet(body, terms),
            ts=r[5], score=scores[mid],
        ))
    return out


async def _hydrate_memories(db, ids, scores, terms):
    rows = {r[0]: r for r in await (await db.execute(
        "SELECT id, content, project_id, created_at FROM memories "
        f"WHERE id IN ({_placeholders(len(ids))})",
        tuple(ids),
    )).fetchall()}
    return [
        SearchResult(domain="memories", ref_id=i, project_id=rows[i][2], title=_title(rows[i][1]),
                     snippet=_snippet(rows[i][1], terms), ts=rows[i][3], score=scores[i])
        for i in ids if i in rows
    ]


async def _hydrate_knowledge(db, ids, scores, terms):
    rows = {r[0]: r for r in await (await db.execute(
        "SELECT c.id, c.text, c.wiki_path, ks.title, ks.origin, ks.project_id, c.created_at "
        "FROM kb_chunks c LEFT JOIN kb_sources ks ON ks.id = c.source_id "
        f"WHERE c.id IN ({_placeholders(len(ids))})",
        tuple(ids),
    )).fetchall()}
    out: list[SearchResult] = []
    for i in ids:
        if i not in rows:
            continue
        _id, text, wiki_path, src_title, origin, project_id, created_at = rows[i]
        title = src_title or wiki_path or origin or "(knowledge)"
        out.append(SearchResult(domain="knowledge", ref_id=i, project_id=project_id,
                                title=_title(title), snippet=_snippet(text, terms), ts=created_at,
                                score=scores[i]))
    return out


async def _hydrate_tasks(db, ids, scores, terms):
    rows = {r[0]: r for r in await (await db.execute(
        "SELECT id, title, payload, project_id, created_at FROM tasks "
        f"WHERE id IN ({_placeholders(len(ids))})",
        tuple(ids),
    )).fetchall()}
    return [
        SearchResult(domain="tasks", ref_id=i, project_id=rows[i][3], title=_title(rows[i][1]),
                     snippet=_snippet(rows[i][2], terms), ts=rows[i][4], score=scores[i])
        for i in ids if i in rows
    ]


async def _hydrate_orchestration(db, ids, scores, terms):
    rows = {r[0]: r for r in await (await db.execute(
        "SELECT id, title, synthesis_summary, project_id, created_at "
        f"FROM orchestration_runs WHERE id IN ({_placeholders(len(ids))})",
        tuple(ids),
    )).fetchall()}
    return [
        SearchResult(domain="orchestration", ref_id=i, project_id=rows[i][3],
                     title=_title(rows[i][1]), snippet=_snippet(rows[i][2] or "", terms),
                     ts=rows[i][4], score=scores[i])
        for i in ids if i in rows
    ]


async def _hydrate_digests(db, ids, scores, terms):
    rows = {r[0]: r for r in await (await db.execute(
        "SELECT id, summary, date_local, generated_at, project_id "
        f"FROM digests WHERE id IN ({_placeholders(len(ids))})",
        tuple(ids),
    )).fetchall()}
    return [
        SearchResult(domain="digests", ref_id=i, project_id=rows[i][4],
                     title=_title(f"Daily digest {rows[i][2]}"),
                     snippet=_snippet(rows[i][1], terms), ts=rows[i][3], score=scores[i])
        for i in ids if i in rows
    ]


async def _hydrate_artifacts(db, ids, scores, terms):
    rows = {r[0]: r for r in await (await db.execute(
        "SELECT id, title, kind, sensitivity, project_id, created_at "
        f"FROM artifacts WHERE id IN ({_placeholders(len(ids))})",
        tuple(ids),
    )).fetchall()}
    return [
        SearchResult(domain="artifacts", ref_id=i, project_id=rows[i][4],
                     title=_title(rows[i][1]), snippet="", ts=rows[i][5], score=scores[i],
                     kind=rows[i][2], sensitivity=rows[i][3])
        for i in ids if i in rows
    ]


_HYDRATORS = {
    "chats": _hydrate_chats,
    "memories": _hydrate_memories,
    "knowledge": _hydrate_knowledge,
    "tasks": _hydrate_tasks,
    "orchestration": _hydrate_orchestration,
    "digests": _hydrate_digests,
    "artifacts": _hydrate_artifacts,
}


async def search(
    db: aiosqlite.Connection,
    raw_query: str | None,
    *,
    project_id: object = ANY_PROJECT,
    include_global: bool = True,
    domains: list[str] | None = None,
    per_domain: int = 8,
    limit: int = 40,
) -> list[dict]:
    """Federated search → JSON-safe result dicts, grouped by domain order then bm25 rank. ``[]``
    for an empty/blank query. Scope/visibility come from query_domain; this only hydrates."""
    terms = _terms(raw_query or "")
    if not terms:
        return []
    selected = [d for d in (domains or DEFAULT_DOMAINS) if d in _HYDRATORS]
    per_domain_hits: list[list[SearchResult]] = []
    for dom in selected:
        hits = await query_domain(
            db, dom, raw_query, project_id=project_id, include_global=include_global,
            limit=per_domain,
        )
        if not hits:
            continue
        ids = [h[0] for h in hits]
        scores = {h[0]: h[1] for h in hits}
        per_domain_hits.append(await _HYDRATORS[dom](db, ids, scores, terms))
    # Round-robin across domains before truncating, so a headline surface (e.g. artifacts) is
    # never starved out of the first `limit` results by an earlier chatty domain.
    merged: list[SearchResult] = []
    rank = 0
    while len(merged) < limit and any(rank < len(d) for d in per_domain_hits):
        for domain_results in per_domain_hits:
            if rank < len(domain_results):
                merged.append(domain_results[rank])
                if len(merged) >= limit:
                    break
        rank += 1
    return [asdict(r) for r in merged]
