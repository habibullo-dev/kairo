"""AttentionStore: the ONE attention queue + its lifecycle state machine (schema v15, Phase 16).

An ``attention_item`` is a durable record of one thing wanting the human's judgment — a live Gate
ASK, a write-intent (Phase 12), a graph suggestion (Phase 15), a dreaming proposal, or a system
alert. This store UNIFIES those sources: ``source`` + ``source_ref`` point AT the originating row;
the queue never duplicates their authority (approve/reject still hit the existing gated routes). The
Notification Center (Task 3) is a view over this table — one attention surface, never two.

Modeled on :class:`~jarvis.actions.intents.IntentStore`: plain SQL on the shared connection + write
lock; JSON columns the store does not interpret; idempotent creation by a UNIQUE ``dedupe_key``; a
strictly-validated :data:`ALLOWED_TRANSITIONS` state machine (illegal moves raise). Safety pins
(Phase 16): ``payload_json`` is NEVER auto-injected into any model context (self-injection
quarantine); dreaming rows default to the untrusted ``model_generated`` trust class; the resolve
route only flips metadata state (done/dismiss/snooze) — it grants no new authority.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass
from enum import StrEnum

import aiosqlite


class AttentionKind(StrEnum):
    """What KIND of attention this is. Matches the ``attention_items.kind`` CHECK."""

    APPROVAL = "approval"  # something awaiting a yes/no on an existing gated route
    REVIEW = "review"  # something to look at (a suggestion, a report)
    PROPOSAL = "proposal"  # a dreaming/agent proposal (accept = a human on a gated route)
    ALERT = "alert"  # a system notice (budget halt, degraded ledger, …)


class AttentionState(StrEnum):
    """The lifecycle of an attention item. Matches the ``attention_items.state`` CHECK."""

    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"
    SNOOZED = "snoozed"
    EXPIRED = "expired"


class AttentionPriority(StrEnum):
    """Routing priority. URGENT ⇒ a minimized push (title/count/category only); NORMAL ⇒ digest;
    LOW ⇒ center-only. Defaults bias to digest (notification fatigue is a product failure)."""

    URGENT = "urgent"
    NORMAL = "normal"
    LOW = "low"


#: Dreaming/agent-generated content is untrusted by default (Phase 16 pin). The vocabulary matches
#: graph_suggestions so the two queues classify trust identically.
TRUST_CLASSES: frozenset[str] = frozenset(
    {"trusted_local", "reviewed", "untrusted_external", "model_generated"}
)

#: The ONLY permitted state moves. OPEN and SNOOZED are the live states; done/dismissed/expired are
#: terminal (a recurrence is a NEW item with a new dedupe_key). A snoozed item wakes back to OPEN.
ALLOWED_TRANSITIONS: dict[AttentionState, frozenset[AttentionState]] = {
    AttentionState.OPEN: frozenset(
        {
            AttentionState.DONE,
            AttentionState.DISMISSED,
            AttentionState.SNOOZED,
            AttentionState.EXPIRED,
        }
    ),
    AttentionState.SNOOZED: frozenset(
        {
            AttentionState.OPEN,
            AttentionState.DONE,
            AttentionState.DISMISSED,
            AttentionState.EXPIRED,
        }
    ),
    AttentionState.DONE: frozenset(),
    AttentionState.DISMISSED: frozenset(),
    AttentionState.EXPIRED: frozenset(),
}


class InvalidTransition(ValueError):
    """Raised on a state move outside :data:`ALLOWED_TRANSITIONS`."""

    def __init__(self, item_id: int, current: AttentionState, requested: AttentionState) -> None:
        self.item_id = item_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"attention item {item_id}: cannot move {current.value} → {requested.value} "
            f"(allowed: {sorted(s.value for s in ALLOWED_TRANSITIONS[current])})"
        )


@dataclass(frozen=True)
class AttentionItem:
    """One row of ``attention_items``. ``payload`` / ``evidence`` are the decoded JSON columns —
    detail for the center, NEVER fed back into a model context automatically (quarantine pin)."""

    id: int
    kind: AttentionKind
    source: str
    source_ref: str | None
    project_id: int | None
    priority: AttentionPriority
    state: AttentionState
    trust_class: str
    title: str
    category: str | None
    payload: dict
    evidence: list
    dedupe_key: str | None
    created_at: str
    updated_at: str
    resolved_at: str | None
    snooze_until: str | None


_COLUMNS = (
    "id, kind, source, source_ref, project_id, priority, state, trust_class, title, category, "
    "payload_json, evidence_json, dedupe_key, created_at, updated_at, resolved_at, snooze_until"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _row(row: tuple) -> AttentionItem:
    return AttentionItem(
        id=row[0],
        kind=AttentionKind(row[1]),
        source=row[2],
        source_ref=row[3],
        project_id=row[4],
        priority=AttentionPriority(row[5]),
        state=AttentionState(row[6]),
        trust_class=row[7],
        title=row[8],
        category=row[9],
        payload=json.loads(row[10]) if row[10] else {},
        evidence=json.loads(row[11]) if row[11] else [],
        dedupe_key=row[12],
        created_at=row[13],
        updated_at=row[14],
        resolved_at=row[15],
        snooze_until=row[16],
    )


class AttentionStore:
    """SQLite persistence + state-machine enforcement for the attention queue (schema v15)."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def create(
        self,
        *,
        kind: AttentionKind | str,
        source: str,
        title: str,
        source_ref: str | None = None,
        project_id: int | None = None,
        priority: AttentionPriority | str = AttentionPriority.NORMAL,
        trust_class: str = "model_generated",
        category: str | None = None,
        payload: dict | None = None,
        evidence: list | None = None,
        dedupe_key: str | None = None,
    ) -> int:
        """Insert an OPEN attention item and return its id. Idempotent by ``dedupe_key``: a retry
        with the same key returns the existing item's id (no duplicate row) — so a producer re-run
        (e.g. tonight's nightly-review) does not re-nag. ``trust_class`` defaults to untrusted
        ``model_generated``; an unknown trust class raises ``ValueError``."""
        item_id, _ = await self.create_if_new(
            kind=kind,
            source=source,
            title=title,
            source_ref=source_ref,
            project_id=project_id,
            priority=priority,
            trust_class=trust_class,
            category=category,
            payload=payload,
            evidence=evidence,
            dedupe_key=dedupe_key,
        )
        return item_id

    async def create_if_new(
        self,
        *,
        kind: AttentionKind | str,
        source: str,
        title: str,
        source_ref: str | None = None,
        project_id: int | None = None,
        priority: AttentionPriority | str = AttentionPriority.NORMAL,
        trust_class: str = "model_generated",
        category: str | None = None,
        payload: dict | None = None,
        evidence: list | None = None,
        dedupe_key: str | None = None,
    ) -> tuple[int, bool]:
        """Create an open row and report whether this call inserted it.

        This is the post-commit notification seam for idempotent producers.  A caller can nudge
        only when the durable row was actually inserted, avoiding a second external notification
        when a retry resolves to an existing ``dedupe_key``.  ``create`` retains its simpler
        id-only API for ordinary producers.
        """
        if trust_class not in TRUST_CLASSES:
            raise ValueError(f"trust_class must be one of {sorted(TRUST_CLASSES)}")
        now = _now()
        async with self.lock:
            if dedupe_key is not None:
                existing = await self._get_by_key_locked(dedupe_key)
                if existing is not None:
                    return existing.id, False
            item_id = await self.create_in_transaction(
                kind=kind,
                source=source,
                title=title,
                source_ref=source_ref,
                project_id=project_id,
                priority=priority,
                trust_class=trust_class,
                category=category,
                payload=payload,
                evidence=evidence,
                dedupe_key=dedupe_key,
                now=now,
            )
            await self.db.commit()
        return item_id, True

    async def create_in_transaction(
        self,
        *,
        kind: AttentionKind | str,
        source: str,
        title: str,
        source_ref: str | None = None,
        project_id: int | None = None,
        priority: AttentionPriority | str = AttentionPriority.NORMAL,
        trust_class: str = "model_generated",
        category: str | None = None,
        payload: dict | None = None,
        evidence: list | None = None,
        dedupe_key: str | None = None,
        now: str | None = None,
    ) -> int:
        """Create an item without committing; caller owns this store's shared transaction.

        This intentionally narrow seam lets another durable state transition and its attention
        alert commit together. It does not grant authority or interpret payload/evidence bodies.
        """
        if trust_class not in TRUST_CLASSES:
            raise ValueError(f"trust_class must be one of {sorted(TRUST_CLASSES)}")
        kind_v = kind.value if isinstance(kind, AttentionKind) else kind
        prio_v = priority.value if isinstance(priority, AttentionPriority) else priority
        timestamp = now or _now()
        if dedupe_key is not None:
            existing = await self._get_by_key_locked(dedupe_key)
            if existing is not None:
                return existing.id
        cursor = await self.db.execute(
            "INSERT INTO attention_items (kind, source, source_ref, project_id, priority, "
            "state, trust_class, title, category, payload_json, evidence_json, dedupe_key, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind_v,
                source,
                source_ref,
                project_id,
                prio_v,
                trust_class,
                title,
                category,
                json.dumps(payload or {}),
                json.dumps(evidence or []),
                dedupe_key,
                timestamp,
                timestamp,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def create_if_new_in_transaction(self, **kwargs: object) -> tuple[int, bool]:
        """Transaction-owned idempotent insert with a notification-safe created flag."""
        dedupe_key = kwargs.get("dedupe_key")
        if isinstance(dedupe_key, str):
            existing = await self._get_by_key_locked(dedupe_key)
            if existing is not None:
                return existing.id, False
        item_id = await self.create_in_transaction(**kwargs)  # type: ignore[arg-type]
        return item_id, True

    async def get(self, item_id: int) -> AttentionItem | None:
        cur = await self.db.execute(
            f"SELECT {_COLUMNS} FROM attention_items WHERE id = ?", (item_id,)
        )
        row = await cur.fetchone()
        return _row(row) if row else None

    async def _get_by_key_locked(self, dedupe_key: str) -> AttentionItem | None:
        cur = await self.db.execute(
            f"SELECT {_COLUMNS} FROM attention_items WHERE dedupe_key = ?", (dedupe_key,)
        )
        row = await cur.fetchone()
        return _row(row) if row else None

    async def list(
        self,
        *,
        state: AttentionState | str | None = None,
        project_id: int | None = None,
        kind: AttentionKind | str | None = None,
        priority: AttentionPriority | str | None = None,
        limit: int = 100,
    ) -> list[AttentionItem]:
        """Items, newest first, filtered by any of state / project / kind / priority. The open
        queue = ``state=OPEN``; project scoping keeps cross-project items out (isolation pin)."""
        clauses: list[str] = []
        params: list[object] = []
        for col, val, enum in (
            ("state", state, AttentionState),
            ("kind", kind, AttentionKind),
            ("priority", priority, AttentionPriority),
        ):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val.value if isinstance(val, enum) else val)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, limit))
        cur = await self.db.execute(
            f"SELECT {_COLUMNS} FROM attention_items {where} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        return [_row(r) for r in await cur.fetchall()]

    async def open_counts(self, *, project_id: int | None = None) -> dict[str, int]:
        """Open-item counts by kind (for the minimized push: title + COUNT + category, never a
        body). Scoped to a project when given."""
        where = "WHERE state = 'open'"
        params: list[object] = []
        if project_id is not None:
            where += " AND project_id = ?"
            params.append(project_id)
        cur = await self.db.execute(
            f"SELECT kind, COUNT(*) FROM attention_items {where} GROUP BY kind", tuple(params)
        )
        return {row[0]: int(row[1]) for row in await cur.fetchall()}

    async def _transition(
        self, item_id: int, to_state: AttentionState, **fields: object
    ) -> AttentionItem:
        """Move ``item_id`` to ``to_state`` (validated against :data:`ALLOWED_TRANSITIONS`), setting
        the given columns + ``updated_at`` atomically under the write lock. Raises
        :class:`InvalidTransition` on an illegal move, ``KeyError`` if the item is missing."""
        async with self.lock:
            current = await self.get(item_id)
            if current is None:
                raise KeyError(f"no attention item with id {item_id}")
            if to_state not in ALLOWED_TRANSITIONS[current.state]:
                raise InvalidTransition(item_id, current.state, to_state)
            sets = ["state = ?"]
            params: list[object] = [to_state.value]
            for col, value in fields.items():
                sets.append(f"{col} = ?")
                params.append(value)
            sets.append("updated_at = ?")
            params.append(_now())
            params.append(item_id)
            await self.db.execute(
                f"UPDATE attention_items SET {', '.join(sets)} WHERE id = ?", tuple(params)
            )
            await self.db.commit()
        updated = await self.get(item_id)
        assert updated is not None
        return updated

    async def mark_done(self, item_id: int) -> AttentionItem:
        """The human handled it (OPEN/SNOOZED → DONE)."""
        return await self._transition(item_id, AttentionState.DONE, resolved_at=_now())

    async def dismiss(self, item_id: int) -> AttentionItem:
        """The human dismissed it without acting (OPEN/SNOOZED → DISMISSED)."""
        return await self._transition(item_id, AttentionState.DISMISSED, resolved_at=_now())

    async def snooze(self, item_id: int, *, until: str) -> AttentionItem:
        """Hide until ``until`` (ISO). OPEN → SNOOZED."""
        return await self._transition(item_id, AttentionState.SNOOZED, snooze_until=until)

    async def reopen(self, item_id: int) -> AttentionItem:
        """A snooze elapsed (SNOOZED → OPEN); clears the snooze marker."""
        return await self._transition(item_id, AttentionState.OPEN, snooze_until=None)

    async def expire(self, item_id: int) -> AttentionItem:
        """A stale item aged out (OPEN/SNOOZED → EXPIRED) — a sweep concern, never an action."""
        return await self._transition(item_id, AttentionState.EXPIRED, resolved_at=_now())

    #: The resolve-route actions (Task 2's POST /api/attention/{id}/resolve) → the store method.
    _RESOLVE = {"done": mark_done, "dismiss": dismiss, "expire": expire}

    async def resolve(
        self, item_id: int, action: str, *, until: str | None = None
    ) -> AttentionItem:
        """Dispatch a metadata-only resolve action (done | dismiss | snooze | expire). This grants
        NO new authority — it only flips the item's queue state. Raises ``ValueError`` on an unknown
        action, ``ValueError`` if snooze is missing ``until``."""
        if action == "snooze":
            if not until:
                raise ValueError("snooze requires 'until'")
            return await self.snooze(item_id, until=until)
        handler = self._RESOLVE.get(action)
        if handler is None:
            raise ValueError(f"unknown resolve action: {action!r}")
        return await handler(self, item_id)
