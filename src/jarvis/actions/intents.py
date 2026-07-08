"""WriteIntent: the two-phase state machine + its SQLite store (schema v10).

A write intent is the operational record of ONE proposed outward write. Its life is a small,
strictly-validated state machine::

    draft ──▶ previewed ──▶ approved ──▶ executed ──▶ undone
      │           │                        │
      └── rejected┘                        └──▶ failed

The safety properties live in this file, pinned by tests:

* **Faithful execution.** ``request`` (the resolved payload) is written once at ``create_draft``
  and there is NO method to change it afterwards — the preview is rendered from it and the
  executor runs it, so what the human approves is exactly what is sent. The model approves or
  rejects a *stored* intent; it never re-supplies the payload at execute time.
* **No double-write.** ``idempotency_key`` is UNIQUE, so a retried ``create_draft`` returns the
  existing intent rather than a second row, and ``mark_executed`` on an already-executed intent
  is a no-op that returns the recorded result — a replayed execute cannot fire the write twice.
* **Only legal transitions.** :func:`IntentStore._transition` refuses any move not in
  :data:`ALLOWED_TRANSITIONS` (e.g. execute-before-approve, approve-after-reject) with
  :class:`InvalidTransition`.

Plain SQL on the shared connection + write lock, like the other stores. ``request`` / ``preview``
/ ``result`` are opaque JSON dicts (the store does not interpret them — the tools and the
preview builder own their shapes).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass
from enum import StrEnum

import aiosqlite


class IntentState(StrEnum):
    """The lifecycle of a write intent. Values match the ``write_intents.state`` CHECK."""

    DRAFT = "draft"
    PREVIEWED = "previewed"
    APPROVED = "approved"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"
    UNDONE = "undone"


class IntentKind(StrEnum):
    """The outward-write verb. One value per (provider, operation) Kairo can perform. New
    verbs are added here AND allowed by the tool layer — the store treats ``kind`` as opaque."""

    CALENDAR_CREATE = "calendar_create"
    CALENDAR_UPDATE = "calendar_update"
    CALENDAR_CANCEL = "calendar_cancel"
    DOC_CREATE = "doc_create"
    DOC_UPDATE = "doc_update"
    GMAIL_DRAFT_CREATE = "gmail_draft_create"
    GMAIL_DRAFT_UPDATE = "gmail_draft_update"


#: The ONLY permitted state moves. Anything not listed is refused by the store. Terminal states
#: (executed→undone aside) have no outgoing edges: a failed/rejected/undone intent is done, and a
#: fresh attempt is a NEW intent with a new idempotency key. ``previewed → previewed`` is allowed
#: so a preview can be re-rendered (e.g. the remote state changed) before approval.
ALLOWED_TRANSITIONS: dict[IntentState, frozenset[IntentState]] = {
    IntentState.DRAFT: frozenset({IntentState.PREVIEWED, IntentState.REJECTED}),
    IntentState.PREVIEWED: frozenset(
        {IntentState.PREVIEWED, IntentState.APPROVED, IntentState.REJECTED}
    ),
    IntentState.APPROVED: frozenset({IntentState.EXECUTED, IntentState.FAILED}),
    IntentState.EXECUTED: frozenset({IntentState.UNDONE}),
    IntentState.FAILED: frozenset(),
    IntentState.REJECTED: frozenset(),
    IntentState.UNDONE: frozenset(),
}


class InvalidTransition(ValueError):
    """Raised when a caller asks for a state move outside :data:`ALLOWED_TRANSITIONS`."""

    def __init__(self, intent_id: int, current: IntentState, requested: IntentState) -> None:
        self.intent_id = intent_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"intent {intent_id}: cannot move {current.value} → {requested.value} "
            f"(allowed: {sorted(s.value for s in ALLOWED_TRANSITIONS[current])})"
        )


@dataclass(frozen=True)
class WriteIntent:
    """One row of ``write_intents``. ``request`` / ``preview`` / ``result`` are the decoded JSON
    columns; ``preview`` and ``result`` are None until the intent reaches those states."""

    id: int
    idempotency_key: str
    provider: str
    kind: str
    state: IntentState
    project_id: int | None
    source: str
    priority: str
    session_id: int | None
    trace_id: str | None
    summary: str
    request: dict
    preview: dict | None
    result: dict | None
    error: str | None
    created_at: str
    previewed_at: str | None
    decided_at: str | None
    executed_at: str | None
    undone_at: str | None
    updated_at: str


_COLUMNS = (
    "id, idempotency_key, provider, kind, state, project_id, source, priority, session_id, "
    "trace_id, summary, request_json, preview_json, result_json, error, created_at, "
    "previewed_at, decided_at, executed_at, undone_at, updated_at"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _loads(blob: str | None) -> dict | None:
    return json.loads(blob) if blob else None


def _row_to_intent(row: tuple) -> WriteIntent:
    return WriteIntent(
        id=row[0],
        idempotency_key=row[1],
        provider=row[2],
        kind=row[3],
        state=IntentState(row[4]),
        project_id=row[5],
        source=row[6],
        priority=row[7],
        session_id=row[8],
        trace_id=row[9],
        summary=row[10],
        request=json.loads(row[11]),
        preview=_loads(row[12]),
        result=_loads(row[13]),
        error=row[14],
        created_at=row[15],
        previewed_at=row[16],
        decided_at=row[17],
        executed_at=row[18],
        undone_at=row[19],
        updated_at=row[20],
    )


class IntentStore:
    """SQLite persistence + state-machine enforcement for write intents (schema v10)."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def create_draft(
        self,
        *,
        idempotency_key: str,
        provider: str,
        kind: IntentKind | str,
        request: dict,
        summary: str,
        source: str,
        project_id: int | None = None,
        session_id: int | None = None,
        trace_id: str | None = None,
        priority: str = "normal",
    ) -> int:
        """Insert a DRAFT intent and return its id. Idempotent by ``idempotency_key``: a retry
        with the same key returns the existing intent's id (no second row, no double-write) —
        even if that intent has since moved on. ``request`` is persisted as-is and is never
        mutated afterwards, which is what makes the later preview and execution faithful."""
        kind_value = kind.value if isinstance(kind, IntentKind) else kind
        now = _now()
        async with self.lock:
            existing = await self._get_by_key_locked(idempotency_key)
            if existing is not None:
                return existing.id
            cursor = await self.db.execute(
                "INSERT INTO write_intents (idempotency_key, provider, kind, state, project_id, "
                "source, priority, session_id, trace_id, summary, request_json, created_at, "
                "updated_at) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    provider,
                    kind_value,
                    project_id,
                    source,
                    priority,
                    session_id,
                    trace_id,
                    summary,
                    json.dumps(request),
                    now,
                    now,
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get(self, intent_id: int) -> WriteIntent | None:
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM write_intents WHERE id = ?", (intent_id,)
        )
        row = await cursor.fetchone()
        return _row_to_intent(row) if row else None

    async def get_by_key(self, idempotency_key: str) -> WriteIntent | None:
        return await self._get_by_key_locked(idempotency_key)

    async def _get_by_key_locked(self, idempotency_key: str) -> WriteIntent | None:
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM write_intents WHERE idempotency_key = ?", (idempotency_key,)
        )
        row = await cursor.fetchone()
        return _row_to_intent(row) if row else None

    async def list(
        self,
        *,
        state: IntentState | str | None = None,
        project_id: int | None = None,
        limit: int = 100,
    ) -> list[WriteIntent]:
        """Intents, newest first. ``state`` filters to one lifecycle state (e.g. the pending
        approval queue = ``previewed``); ``project_id`` scopes to one project."""
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value if isinstance(state, IntentState) else state)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, limit))
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM write_intents {where} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        return [_row_to_intent(r) for r in await cursor.fetchall()]

    async def _transition(
        self,
        intent_id: int,
        to_state: IntentState,
        *,
        idempotent_states: frozenset[IntentState] = frozenset(),
        **fields: object,
    ) -> WriteIntent:
        """Move ``intent_id`` to ``to_state`` (validated against :data:`ALLOWED_TRANSITIONS`),
        setting the given columns + ``updated_at`` atomically under the write lock. Raises
        :class:`InvalidTransition` on an illegal move, ``KeyError`` if the intent is missing.

        If the intent is already in one of ``idempotent_states``, this is a no-op that returns the
        intent unchanged — the read-check-write is all inside the one lock, so even a *concurrent*
        replay of e.g. ``mark_executed`` can never fire the transition twice (the first wins; the
        second sees the terminal state and returns the recorded row instead of raising)."""
        async with self.lock:
            current = await self.get(intent_id)
            if current is None:
                raise KeyError(f"no write intent with id {intent_id}")
            if current.state in idempotent_states:
                return current
            if to_state not in ALLOWED_TRANSITIONS[current.state]:
                raise InvalidTransition(intent_id, current.state, to_state)
            sets = ["state = ?"]
            params: list[object] = [to_state.value]
            for col, value in fields.items():
                sets.append(f"{col} = ?")
                params.append(value)
            sets.append("updated_at = ?")
            params.append(_now())
            params.append(intent_id)
            await self.db.execute(
                f"UPDATE write_intents SET {', '.join(sets)} WHERE id = ?", tuple(params)
            )
            await self.db.commit()
        updated = await self.get(intent_id)
        assert updated is not None
        return updated

    async def mark_previewed(self, intent_id: int, *, preview: dict) -> WriteIntent:
        """Attach the rendered preview and move to PREVIEWED (re-render allowed from PREVIEWED)."""
        return await self._transition(
            intent_id,
            IntentState.PREVIEWED,
            preview_json=json.dumps(preview),
            previewed_at=_now(),
        )

    async def approve(self, intent_id: int) -> WriteIntent:
        """Human approval: PREVIEWED → APPROVED. An intent must be previewed before it can be
        approved (no approve-a-draft) — the enforcement of "no write without a faithful preview"."""
        return await self._transition(intent_id, IntentState.APPROVED, decided_at=_now())

    async def reject(self, intent_id: int) -> WriteIntent:
        """Reject a pending intent (DRAFT or PREVIEWED → REJECTED). Terminal."""
        return await self._transition(intent_id, IntentState.REJECTED, decided_at=_now())

    async def mark_executed(self, intent_id: int, *, result: dict) -> WriteIntent:
        """Record a successful write: APPROVED → EXECUTED. Idempotent — if the intent is ALREADY
        executed, this returns the recorded intent unchanged, without overwriting its result (the
        double-write guard: a retried execute never fires a second remote write)."""
        return await self._transition(
            intent_id,
            IntentState.EXECUTED,
            idempotent_states=frozenset({IntentState.EXECUTED}),
            result_json=json.dumps(result),
            executed_at=_now(),
        )

    async def mark_failed(self, intent_id: int, *, error: str) -> WriteIntent:
        """Record a failed execution: APPROVED → FAILED. ``error`` is a friendly message, never a
        provider response body. Terminal — a fresh attempt is a new intent."""
        return await self._transition(intent_id, IntentState.FAILED, error=error)

    async def mark_undone(self, intent_id: int) -> WriteIntent:
        """Record that an executed write was reversed: EXECUTED → UNDONE."""
        return await self._transition(intent_id, IntentState.UNDONE, undone_at=_now())
