"""The unified attention queue read model (Phase 16 Task 2).

One projection over every source that wants the human's judgment — live Gate ASKs (ephemeral, in
``ApprovalManager``), write-intents awaiting approval (Phase 12), pending graph suggestions
(Phase 15), and the durable ``attention_items`` rows (proposals / alerts / reviews that dreaming +
the system produce). It UNIONS them at read time: each item carries ``source`` + ``ref`` pointing AT
the originating row, so the Notification Center renders one list while every action still flows
through that source's EXISTING gated route. The center adds NO authority; only ``attention_items``
rows resolve through the one new metadata route (``/api/attention/{id}/resolve``).

Bodies-free by default: an item's ``detail`` is the source's own already-safe payload (e.g. a
write-intent's rendered preview, a Gate ASK's full input on the private screen) — never a fresh
dump. The minimized push (Task 4) uses only ``title`` + ``counts`` + ``category``.
"""

from __future__ import annotations

from typing import Any

#: Sort order: urgent first, then normal, then low; newest within a band.
_PRIORITY_ORDER = {"urgent": 0, "normal": 1, "low": 2}
_PROJECT_INTELLIGENCE_COUNTS = frozenset(
    {
        "strengths",
        "weaknesses",
        "security_candidates",
        "frontend_backend_gaps",
        "test_reliability_gaps",
        "recommendations",
    }
)


def _item(
    source: str,
    ref: str,
    kind: str,
    title: str,
    *,
    priority: str = "normal",
    project_id: int | None = None,
    created_at: str | None = None,
    trust_class: str | None = None,
    category: str | None = None,
    detail: Any = None,
) -> dict:
    return {
        "source": source,
        "ref": ref,
        "kind": kind,
        "title": title,
        "priority": priority,
        "project_id": project_id,
        "state": "open",
        "created_at": created_at,
        "trust_class": trust_class,
        "category": category,
        "detail": detail,
    }


def _project_intelligence_detail(item: Any, *, project_id: int | None) -> dict | None:
    """Allow one report pointer only inside its exact concrete project queue."""
    if (
        project_id is None
        or item.project_id != project_id
        or item.source != "project_intelligence"
        or item.category != "project_intelligence"
        or not isinstance(item.payload, dict)
    ):
        return None
    report_id = item.payload.get("report_id")
    counts = item.payload.get("counts")
    if (
        type(report_id) is not int
        or report_id <= 0
        or item.source_ref != str(report_id)
        or not isinstance(counts, dict)
        or set(counts) != _PROJECT_INTELLIGENCE_COUNTS
        or any(type(value) is not int or value < 0 for value in counts.values())
    ):
        return None
    return {"report_id": report_id, "counts": dict(counts)}


async def attention_queue(
    *,
    attention: Any,
    intents: Any = None,
    graph: Any = None,
    approvals: Any = None,
    approval_context: Any = None,
    project_id: int | None = None,
    limit: int = 200,
) -> dict:
    """The unified open queue (+ counts by kind). ``project_id=None`` is the global view; a project
    id scopes intents/attention to that project and graph suggestions to that project + global.
    Every store is OPTIONAL (absent surface ⇒ simply not projected) so the center degrades, never
    500s. Resolution is NOT done here — this is a read model."""
    items: list[dict] = []

    # 1. Live Gate ASKs — ephemeral, in-memory; they BLOCK a turn, so they're urgent. Not project-
    #    scoped (a turn's ASK isn't durable). Resolved via /api/approvals/{id}/resolve.
    if approvals is not None:
        pending = (
            approvals.pending_for(approval_context)
            if approval_context is not None
            else approvals.pending()
        )
        for p in pending:
            items.append(
                _item(
                    "gate",
                    p.decision_id,
                    "approval",
                    f"Approve tool: {p.call.name}",
                    priority="urgent",
                    detail=p.to_public(),
                )
            )

    # 2. Write-intents awaiting approval (durable). Resolved via /api/intents/{id}/approve|reject.
    if intents is not None:
        from kira.actions.intents import IntentState

        for i in await intents.list(
            state=IntentState.PREVIEWED, project_id=project_id, limit=limit
        ):
            items.append(
                _item(
                    "intent",
                    str(i.id),
                    "approval",
                    i.summary,
                    priority=i.priority,
                    project_id=i.project_id,
                    created_at=i.created_at,
                    detail={"preview": i.preview, "kind": i.kind},
                )
            )

    # 3. Pending graph suggestions. Resolved via /api/graph/suggestions/{id}/approve|reject.
    if graph is not None:
        from kira.graph.store import ANY_PROJECT

        pid: object = project_id if project_id is not None else ANY_PROJECT
        for s in await graph.list_suggestions(project_id=pid, status="pending"):
            payload = s.payload or {}
            preview = str(payload.get("content") or payload.get("title") or s.kind)[:140]
            items.append(
                _item(
                    "graph_suggestion",
                    str(s.id),
                    "review",
                    f"Suggested {s.kind}: {preview}",
                    project_id=s.project_id,
                    created_at=s.created_at,
                    trust_class=s.trust_class,
                )
            )

    # 4. Durable attention_items (dreaming proposals / system alerts / reviews). The ONLY rows the
    #    new /api/attention/{id}/resolve route touches.
    for a in await attention.list(state="open", project_id=project_id, limit=limit):
        items.append(
            _item(
                "attention",
                str(a.id),
                a.kind.value,
                a.title,
                priority=a.priority.value,
                project_id=a.project_id,
                created_at=a.created_at,
                trust_class=a.trust_class,
                category=a.category,
                detail=_project_intelligence_detail(a, project_id=project_id),
            )
        )

    # Two stable passes: newest-first within each priority band. A live Gate ASK (created_at None)
    # gets a sentinel that sorts it to the top of its (urgent) band.
    items.sort(key=lambda x: x.get("created_at") or "9999", reverse=True)
    items.sort(key=lambda x: _PRIORITY_ORDER.get(x["priority"], 1))
    counts: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    return {"items": items[:limit], "counts": counts, "total": len(items)}
