"""Durable, proposal-first Telegram Remote Operator.

Natural-language Telegram messages may expose an inert proposal tool and an independently opt-in,
one-query public live-search wrapper to the utility model. The proposal tool cannot run project
tools, approve itself, or create a scheduler task. Execution begins only after an expiring,
single-use code is resolved by the allowlisted Telegram controller. Live search carries public
reference data only and grants no local authority.

Approved jobs later use the scheduler's existing parked-continuation mechanism.  Every risky
tool request is therefore bound to its original tool id, name, canonical input hash, and saved
model transcript before a second Telegram approval can resume it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

import aiosqlite
from pydantic import BaseModel, Field, model_validator
from tzlocal import get_localzone_name

from kira.config import TelegramRemoteOperatorConfig
from kira.permissions.gate import Decision
from kira.persistence.db import transaction
from kira.projects.store import Project, ProjectStore
from kira.scheduler.service import ScheduleError, TaskService
from kira.scheduler.store import ParkedContinuation, Task
from kira.scheduler.triggers import validate
from kira.tools.base import Permission, Tool, ToolResult

if TYPE_CHECKING:
    from kira.scheduler.runner import BackgroundRunner

_PROPOSAL_COLUMNS = (
    "id, kind, title, instruction, project_id, schedule_kind, schedule_spec, "
    "status_interval_minutes, state, task_id, created_at, expires_at, resolved_at, "
    "updated_at, last_status_at, status_updates_sent, error"
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().upper().encode("ascii", errors="ignore")).hexdigest()


def _proposal_binding(proposal: RemoteProposal) -> str:
    raw = json.dumps(
        {
            "id": proposal.id,
            "kind": proposal.kind,
            "title": proposal.title,
            "instruction": proposal.instruction,
            "project_id": proposal.project_id,
            "schedule_kind": proposal.schedule_kind,
            "schedule_spec": proposal.schedule_spec,
            "status_interval_minutes": proposal.status_interval_minutes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RemoteProposal:
    id: int
    kind: str
    title: str
    instruction: str
    project_id: int | None
    schedule_kind: str
    schedule_spec: str
    status_interval_minutes: int
    state: str
    task_id: int | None
    created_at: str
    expires_at: str
    resolved_at: str | None
    updated_at: str
    last_status_at: str | None
    status_updates_sent: int
    error: str | None


@dataclass(frozen=True)
class RemoteAuthorization:
    proposal: RemoteProposal
    approval_code: str


@dataclass(frozen=True)
class RemoteTokenGrant:
    subject_type: Literal["proposal", "parked_run"]
    subject_id: int
    binding_hash: str
    resolution: Literal["approve", "deny"]


@dataclass(frozen=True)
class RemotePendingTool:
    run_id: int
    task_id: int
    task_title: str
    project_id: int | None
    continuation: ParkedContinuation


def _row_to_proposal(row: tuple) -> RemoteProposal:
    return RemoteProposal(*row)


class RemoteOperatorStore:
    """Proposal/token persistence on Kira's shared SQLite connection and lock."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        self.db = db
        self.lock = lock

    async def create_proposal(
        self,
        *,
        kind: str,
        title: str,
        instruction: str,
        project_id: int | None,
        schedule_kind: str,
        schedule_spec: str,
        status_interval_minutes: int,
        proposal_ttl_minutes: int,
        approval_ttl_minutes: int,
        now: dt.datetime | None = None,
    ) -> RemoteAuthorization:
        moment = now or _utc_now()
        created = _iso(moment)
        expires = _iso(moment + dt.timedelta(minutes=proposal_ttl_minutes))
        async with transaction(self.db, self.lock):
            cursor = await self.db.execute(
                "INSERT INTO remote_operator_proposals "
                "(kind, title, instruction, project_id, schedule_kind, schedule_spec, "
                "status_interval_minutes, state, created_at, expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    kind,
                    title,
                    instruction,
                    project_id,
                    schedule_kind,
                    schedule_spec,
                    status_interval_minutes,
                    created,
                    expires,
                    created,
                ),
            )
            assert cursor.lastrowid is not None
            proposal_id = int(cursor.lastrowid)
            proposal = await self._get_locked(proposal_id)
            assert proposal is not None
            code = await self._issue_token_locked(
                subject_type="proposal",
                subject_id=proposal.id,
                binding_hash=_proposal_binding(proposal),
                ttl_minutes=approval_ttl_minutes,
                now=moment,
            )
        return RemoteAuthorization(proposal=proposal, approval_code=code)

    async def get(self, proposal_id: int) -> RemoteProposal | None:
        cursor = await self.db.execute(
            f"SELECT {_PROPOSAL_COLUMNS} FROM remote_operator_proposals WHERE id = ?",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        return _row_to_proposal(row) if row else None

    async def _get_locked(self, proposal_id: int) -> RemoteProposal | None:
        return await self.get(proposal_id)

    async def list(self, *, limit: int = 20) -> list[RemoteProposal]:
        cap = max(1, min(limit, 100))
        await self.expire_pending()
        rows = await (
            await self.db.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM remote_operator_proposals "
                "ORDER BY id DESC LIMIT ?",
                (cap,),
            )
        ).fetchall()
        return [_row_to_proposal(row) for row in rows]

    async def approved_without_task(self) -> list[RemoteProposal]:
        """Return approvals that were not durably bound to a scheduler task."""
        rows = await (
            await self.db.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM remote_operator_proposals "
                "WHERE state = 'approved' AND task_id IS NULL ORDER BY id"
            )
        ).fetchall()
        return [_row_to_proposal(row) for row in rows]

    async def expire_pending(self, *, now: dt.datetime | None = None) -> int:
        moment = _iso(now or _utc_now())
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE remote_operator_proposals SET state = 'expired', updated_at = ? "
                "WHERE state = 'pending' AND expires_at <= ?",
                (moment, moment),
            )
            await self.db.commit()
        return cursor.rowcount

    async def issue_proposal_token(
        self,
        proposal_id: int,
        *,
        ttl_minutes: int,
        now: dt.datetime | None = None,
    ) -> RemoteAuthorization | None:
        moment = now or _utc_now()
        async with transaction(self.db, self.lock):
            proposal = await self._get_locked(proposal_id)
            if (
                proposal is None
                or proposal.state != "pending"
                or dt.datetime.fromisoformat(proposal.expires_at) <= moment
            ):
                return None
            code = await self._issue_token_locked(
                subject_type="proposal",
                subject_id=proposal.id,
                binding_hash=_proposal_binding(proposal),
                ttl_minutes=ttl_minutes,
                now=moment,
            )
        return RemoteAuthorization(proposal=proposal, approval_code=code)

    async def pending_tools(self) -> list[RemotePendingTool]:
        rows = await (
            await self.db.execute(
                "SELECT r.id, t.id, t.title, t.project_id, r.continuation_json "
                "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
                "JOIN remote_operator_proposals p ON p.task_id = t.id "
                "WHERE r.status = 'running' AND r.approval_state = 'pending' "
                "AND t.status = 'active' AND t.origin = 'remote_operator' "
                "ORDER BY r.id"
            )
        ).fetchall()
        pending: list[RemotePendingTool] = []
        for row in rows:
            if not isinstance(row[4], str):
                continue
            try:
                continuation = ParkedContinuation.from_json(row[4])
            except ValueError:
                continue
            pending.append(
                RemotePendingTool(
                    run_id=int(row[0]),
                    task_id=int(row[1]),
                    task_title=str(row[2]),
                    project_id=row[3],
                    continuation=continuation,
                )
            )
        return pending

    async def pending_tool(self, run_id: int) -> RemotePendingTool | None:
        return next((item for item in await self.pending_tools() if item.run_id == run_id), None)

    async def issue_parked_token(
        self,
        run_id: int,
        *,
        ttl_minutes: int,
        now: dt.datetime | None = None,
    ) -> tuple[RemotePendingTool, str] | None:
        pending = await self.pending_tool(run_id)
        if pending is None:
            return None
        moment = now or _utc_now()
        async with transaction(self.db, self.lock):
            # Re-read inside the write transaction so a completed/replaced continuation cannot
            # inherit a token minted for the earlier exact input.
            current = await self.pending_tool(run_id)
            if current is None or (
                current.continuation.tool_input_hash
                != pending.continuation.tool_input_hash
            ):
                return None
            code = await self._issue_token_locked(
                subject_type="parked_run",
                subject_id=run_id,
                binding_hash=current.continuation.tool_input_hash,
                ttl_minutes=ttl_minutes,
                now=moment,
            )
        return current, code

    async def _issue_token_locked(
        self,
        *,
        subject_type: Literal["proposal", "parked_run"],
        subject_id: int,
        binding_hash: str,
        ttl_minutes: int,
        now: dt.datetime,
    ) -> str:
        created = _iso(now)
        await self.db.execute(
            "UPDATE remote_operator_tokens SET consumed_at = ?, resolution = 'deny' "
            "WHERE subject_type = ? AND subject_id = ? AND consumed_at IS NULL",
            (created, subject_type, subject_id),
        )
        for _attempt in range(5):
            code = secrets.token_hex(6).upper()
            try:
                await self.db.execute(
                    "INSERT INTO remote_operator_tokens "
                    "(token_hash, subject_type, subject_id, binding_hash, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _token_hash(code),
                        subject_type,
                        subject_id,
                        binding_hash,
                        created,
                        _iso(now + dt.timedelta(minutes=ttl_minutes)),
                    ),
                )
                return code
            except aiosqlite.IntegrityError:
                continue
        raise RuntimeError("could not mint a unique remote approval code")

    async def consume_token(
        self,
        code: str,
        *,
        resolution: Literal["approve", "deny"],
        now: dt.datetime | None = None,
    ) -> RemoteTokenGrant | None:
        moment = now or _utc_now()
        now_iso = _iso(moment)
        async with transaction(self.db, self.lock):
            row = await (
                await self.db.execute(
                    "SELECT id, subject_type, subject_id, binding_hash, expires_at "
                    "FROM remote_operator_tokens "
                    "WHERE token_hash = ? AND consumed_at IS NULL",
                    (_token_hash(code),),
                )
            ).fetchone()
            if row is None or dt.datetime.fromisoformat(row[4]) <= moment:
                return None
            subject_type = row[1]
            subject_id = int(row[2])
            binding_hash = row[3]
            if subject_type == "proposal":
                proposal = await self._get_locked(subject_id)
                if (
                    proposal is None
                    or proposal.state != "pending"
                    or dt.datetime.fromisoformat(proposal.expires_at) <= moment
                    or _proposal_binding(proposal) != binding_hash
                ):
                    return None
                state = "approved" if resolution == "approve" else "denied"
                updated = await self.db.execute(
                    "UPDATE remote_operator_proposals SET state = ?, resolved_at = ?, "
                    "updated_at = ? WHERE id = ? AND state = 'pending'",
                    (state, now_iso, now_iso, subject_id),
                )
                if updated.rowcount != 1:
                    return None
            else:
                pending = await self.pending_tool(subject_id)
                if (
                    pending is None
                    or pending.continuation.tool_input_hash != binding_hash
                ):
                    return None
            consumed = await self.db.execute(
                "UPDATE remote_operator_tokens SET consumed_at = ?, resolution = ? "
                "WHERE id = ? AND consumed_at IS NULL",
                (now_iso, resolution, row[0]),
            )
            if consumed.rowcount != 1:
                return None
        return RemoteTokenGrant(
            subject_type=subject_type,
            subject_id=subject_id,
            binding_hash=binding_hash,
            resolution=resolution,
        )

    async def mark_queued(self, proposal_id: int, task_id: int) -> bool:
        now = _iso(_utc_now())
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE remote_operator_proposals SET state = 'queued', task_id = ?, "
                "updated_at = ? WHERE id = ? AND state = 'approved' AND task_id IS NULL",
                (task_id, now, proposal_id),
            )
            await self.db.commit()
        return cursor.rowcount == 1

    async def mark_failed(self, proposal_id: int, error: str) -> bool:
        now = _iso(_utc_now())
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE remote_operator_proposals SET state = 'failed', error = ?, "
                "updated_at = ? WHERE id = ? AND state = 'approved'",
                (error[:500], now, proposal_id),
            )
            await self.db.commit()
        return cursor.rowcount == 1

    async def mark_cancelled(self, proposal_id: int) -> bool:
        now = _iso(_utc_now())
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE remote_operator_proposals SET state = 'cancelled', updated_at = ? "
                "WHERE id = ? AND state IN ('approved', 'queued')",
                (now, proposal_id),
            )
            await self.db.commit()
        return cursor.rowcount == 1

    async def record_status_update(self, proposal_id: int, *, limit: int = 100) -> bool:
        now = _iso(_utc_now())
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE remote_operator_proposals SET last_status_at = ?, "
                "status_updates_sent = status_updates_sent + 1, updated_at = ? "
                "WHERE id = ? AND state = 'queued' AND status_updates_sent < ?",
                (now, now, proposal_id, limit),
            )
            await self.db.commit()
        return cursor.rowcount == 1

    async def proposal_for_task(self, task_id: int) -> RemoteProposal | None:
        row = await (
            await self.db.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM remote_operator_proposals WHERE task_id = ?",
                (task_id,),
            )
        ).fetchone()
        return _row_to_proposal(row) if row else None

    async def active_count(self) -> int:
        row = await (
            await self.db.execute(
                "SELECT COUNT(*) FROM remote_operator_proposals p "
                "LEFT JOIN tasks t ON t.id = p.task_id "
                "WHERE p.state = 'approved' OR (p.state = 'queued' AND t.status = 'active')"
            )
        ).fetchone()
        return int(row[0]) if row is not None else 0


class RemoteProposalParams(BaseModel):
    kind: Literal["job", "reminder"] = Field(
        description="A job performs local project work; a reminder only notifies the owner."
    )
    title: str = Field(min_length=3, max_length=120)
    instruction: str = Field(
        min_length=1,
        max_length=2_000,
        description="The exact self-contained work request or reminder text.",
    )
    project: str | None = Field(
        default=None,
        max_length=120,
        description="An existing Kira project id, slug, or exact name; never a filesystem path.",
    )
    schedule_kind: Literal["immediate", "once", "interval", "cron"] = "immediate"
    schedule_spec: str = Field(
        default="",
        max_length=200,
        description=(
            "Blank for immediate; local ISO datetime for once; seconds for interval; "
            "five-field expression for cron."
        ),
    )
    status_interval_minutes: int | None = Field(
        default=None,
        description="0 means milestone-only; otherwise use an allowed minute cadence.",
    )

    @model_validator(mode="after")
    def _schedule_shape(self) -> RemoteProposalParams:
        self.title = " ".join(self.title.split())
        self.instruction = self.instruction.strip()
        self.project = self.project.strip() if self.project else None
        self.schedule_spec = self.schedule_spec.strip()
        if self.schedule_kind == "immediate" and self.schedule_spec:
            raise ValueError("an immediate proposal must have a blank schedule_spec")
        if self.schedule_kind != "immediate" and not self.schedule_spec:
            raise ValueError("a non-immediate proposal requires schedule_spec")
        return self


class RemoteLiveSearchParams(BaseModel):
    query: str = Field(
        min_length=2,
        max_length=300,
        description=(
            "One concise public-information search query. Include the location and date when "
            "needed; never include secrets, private files, email, or project content."
        ),
    )


class RemoteLiveSearchTool(Tool):
    """One bounded public web search for a fresh allowlisted Telegram message."""

    name = "remote_live_search"
    description = (
        "Search current public information once for this Telegram message, such as weather, "
        "news, public schedules, prices, or other time-sensitive facts. Results are untrusted "
        "reference material. This cannot fetch arbitrary pages or access local/private data."
    )
    Params = RemoteLiveSearchParams
    permission_default = Permission.ALLOW
    egress = True

    def __init__(self, *, source: Tool, max_results: int) -> None:
        super().__init__()
        if source.name != "web_search":
            raise ValueError("remote live search requires the bounded web_search adapter")
        if not 1 <= max_results <= 5:
            raise ValueError("remote live search result cap must be between 1 and 5")
        self.source = source
        self.max_results = max_results
        self._turn_lock = asyncio.Lock()
        self._accepting = False

    def begin_turn(self) -> None:
        self._accepting = True

    async def run(self, params: RemoteLiveSearchParams) -> ToolResult | str:
        async with self._turn_lock:
            if not self._accepting:
                return ToolResult(
                    content="Only one live public search may run per Telegram message.",
                    is_error=True,
                )
            self._accepting = False
        source_params = self.source.Params(
            query=" ".join(params.query.split()),
            max_results=self.max_results,
        )
        return await self.source.run(source_params)


class RemoteProposalGate:
    """Structural allowlist for inert proposals and bounded public live search."""

    def check(
        self,
        tool_name: str,
        tool_input: dict | None = None,
        *,
        tool_default: Permission | None = None,
    ) -> Decision:
        del tool_input, tool_default
        if tool_name == RemoteProposalTool.name:
            return Decision(Permission.ALLOW, "remote proposal creation is preparation only")
        if tool_name == RemoteLiveSearchTool.name:
            return Decision(
                Permission.ALLOW,
                "one owner-requested public-information search is allowed",
            )
        return Decision(Permission.DENY, "remote model has no execution authority")


class RemoteProposalTool(Tool):
    name = "remote_propose_work"
    description = (
        "Prepare one owner-requested Kira job or reminder for Telegram approval. This only "
        "stores a proposal; it never schedules, executes, opens, writes, or approves anything."
    )
    Params = RemoteProposalParams
    permission_default = Permission.ALLOW

    def __init__(
        self,
        *,
        store: RemoteOperatorStore,
        projects: ProjectStore | None,
        config: TelegramRemoteOperatorConfig,
    ) -> None:
        super().__init__()
        self.store = store
        self.projects = projects
        self.config = config
        self._created: list[RemoteAuthorization] = []
        self._turn_lock = asyncio.Lock()
        self._accepting = False

    def begin_turn(self) -> None:
        self._created.clear()
        self._accepting = True

    def drain_created(self) -> list[RemoteAuthorization]:
        created, self._created = self._created, []
        self._accepting = False
        return created

    async def _resolve_project(self, reference: str | None) -> Project | None:
        if reference is None:
            return None
        if self.projects is None:
            raise ValueError("Projects are unavailable. Use Kira locally to configure one.")
        projects = await self.projects.list(status="active")
        normalized = reference.casefold().lstrip("#")
        matches = [
            project
            for project in projects
            if str(project.id) == normalized
            or project.slug.casefold() == normalized
            or project.name.casefold() == normalized
        ]
        if len(matches) != 1:
            raise ValueError("Project alias was not found. Send /projects and use one exact alias.")
        return matches[0]

    async def run(self, params: RemoteProposalParams) -> ToolResult | str:
        async with self._turn_lock:
            if not self._accepting:
                return ToolResult(
                    content="Only one remote proposal may be created per Telegram message.",
                    is_error=True,
                )
            self._accepting = False
        try:
            project = await self._resolve_project(params.project)
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)
        if (
            params.status_interval_minutes is not None
            and params.status_interval_minutes not in self.config.allowed_status_intervals
        ):
            return ToolResult(
                content=(
                    "Unsupported status interval. Allowed minutes: "
                    + ", ".join(map(str, self.config.allowed_status_intervals))
                ),
                is_error=True,
            )
        if params.schedule_kind != "immediate":
            problem = validate(params.schedule_kind, params.schedule_spec, get_localzone_name())
            if problem is not None:
                return ToolResult(content=problem, is_error=True)
        authorization = await self.store.create_proposal(
            kind=params.kind,
            title=params.title,
            instruction=params.instruction,
            project_id=project.id if project is not None else None,
            schedule_kind=params.schedule_kind,
            schedule_spec=params.schedule_spec,
            status_interval_minutes=(
                params.status_interval_minutes
                if params.status_interval_minutes is not None
                else self.config.default_status_interval_minutes
            ),
            proposal_ttl_minutes=self.config.proposal_ttl_minutes,
            approval_ttl_minutes=self.config.approval_ttl_minutes,
        )
        self._created.append(authorization)
        return f"Prepared remote proposal #{authorization.proposal.id}; awaiting owner approval."


def render_proposal(authorization: RemoteAuthorization, project: Project | None) -> str:
    proposal = authorization.proposal
    project_line = project.slug if project is not None else "global"
    schedule = (
        "immediately after approval"
        if proposal.schedule_kind == "immediate"
        else f"{proposal.schedule_kind}: {proposal.schedule_spec}"
    )
    cadence = (
        "milestones only"
        if proposal.status_interval_minutes == 0
        else f"every {proposal.status_interval_minutes} minute(s) while active"
    )
    return (
        f"Remote proposal #{proposal.id}\n"
        f"Type: {proposal.kind}\n"
        f"Project: {project_line}\n"
        f"Schedule: {schedule}\n"
        f"Updates: {cadence}\n\n"
        f"{proposal.title}\n{proposal.instruction}\n\n"
        f"Approve: /approve {authorization.approval_code}\n"
        f"Deny: /deny {authorization.approval_code}\n"
        "This code is single-use and expires shortly. Approval creates only this exact task; "
        "risky tools will request separate approvals."
    )


def render_tool_approval(pending: RemotePendingTool, approval_code: str) -> str:
    call = pending.continuation
    inp = call.tool_input
    if call.tool_name == "run_shell":
        cwd = inp.get("cwd") or "default workspace"
        detail = f"cwd: {cwd}\n$ {str(inp.get('command', '')).strip()[:1_000]}"
    elif call.tool_name == "write_file":
        content = str(inp.get("content", ""))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        detail = (
            f"path: {inp.get('path', '?')}\n"
            f"content: {len(content)} chars · sha256 {digest}…"
        )
    elif call.tool_name in {"read_file", "list_dir"}:
        detail = f"path: {inp.get('path', '?')}"
    elif call.tool_name == "glob_search":
        detail = f"root: {inp.get('root', '.')}\npattern: {inp.get('pattern', '?')}"
    else:
        detail = "input hash: " + call.tool_input_hash[:16] + "…"
    return (
        f"Approval needed for task #{pending.task_id}\n"
        f"{pending.task_title}\n\n"
        f"Tool: {call.tool_name}\n{detail}\n"
        f"Reason: {call.decision_reason}\n"
        f"Exact input hash: {call.tool_input_hash[:16]}…\n\n"
        f"Approve once: /approve {approval_code}\n"
        f"Deny: /deny {approval_code}\n"
        "The code is single-use and bound to this exact saved tool call."
    )


RemoteSender = Callable[[str], Awaitable[None]]
RemoteSleep = Callable[[float], Awaitable[None]]


class RemoteOperatorService:
    """Host-side proposal resolution, scheduling, parked-call approval, and status delivery."""

    def __init__(
        self,
        *,
        store: RemoteOperatorStore,
        config: TelegramRemoteOperatorConfig,
        tasks: TaskService,
        projects: ProjectStore | None,
        runner: BackgroundRunner,
        sender: RemoteSender | None = None,
        sleep: RemoteSleep = asyncio.sleep,
    ) -> None:
        self.store = store
        self.config = config
        self.tasks = tasks
        self.projects = projects
        self.runner = runner
        self.sender = sender
        self.sleep = sleep
        self._background: set[asyncio.Task[None]] = set()
        self._monitors: dict[int, asyncio.Task[None]] = {}

    def set_sender(self, sender: RemoteSender) -> None:
        self.sender = sender

    async def start(self) -> None:
        """Reconcile interrupted approvals and restore monitors after restart."""
        cancelled_orphan = False
        for task in await self.tasks.store.list():
            if task.origin != "remote_operator":
                continue
            if await self.store.proposal_for_task(task.id) is not None:
                continue
            if await self.tasks.cancel(task.id) is not None:
                cancelled_orphan = True
        if cancelled_orphan:
            self.runner.kick()

        for proposal in await self.store.approved_without_task():
            failed = await self.store.mark_failed(
                proposal.id,
                "Kira restarted before the approved proposal was durably bound to a task",
            )
            if failed:
                await self._safe_send(
                    f"Remote proposal #{proposal.id} was interrupted during queueing and was "
                    "closed without running work. Please send the request again."
                )

        for proposal in await self.store.list(limit=100):
            if proposal.state != "queued" or proposal.task_id is None:
                continue
            task = await self.tasks.store.get(proposal.task_id)
            if task is not None and task.status == "active":
                self._start_monitor(proposal)

    async def stop(self) -> None:
        pending = [*self._monitors.values(), *self._background]
        self._monitors.clear()
        self._background.clear()
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _safe_send(self, text: str) -> None:
        if self.sender is None:
            return
        with contextlib.suppress(Exception):
            await self.sender(text[:3_800])

    async def render_authorization(self, authorization: RemoteAuthorization) -> str:
        project = (
            await self.projects.get(authorization.proposal.project_id)
            if self.projects is not None and authorization.proposal.project_id is not None
            else None
        )
        return render_proposal(authorization, project)

    async def projects_text(self) -> str:
        if self.projects is None:
            return "Projects are unavailable on this Kira instance."
        projects = await self.projects.list(status="active")
        if not projects:
            return "No active Kira projects. Create and link a project on the local workstation."
        lines = ["Registered project aliases:"]
        for project in projects[:20]:
            lines.append(f"{project.slug} — {project.name} ({len(project.repos)} linked repo(s))")
        return "\n".join(lines)

    async def jobs_text(self) -> str:
        proposals = await self.store.list(limit=20)
        if not proposals:
            return "No Telegram Remote Operator jobs yet."
        lines = ["Remote Operator jobs:"]
        for proposal in proposals[:10]:
            state = proposal.state
            if proposal.task_id is not None:
                task = await self.tasks.store.get(proposal.task_id)
                if task is not None:
                    state = task.status
                state += f" · task #{proposal.task_id}"
            lines.append(f"#{proposal.id} [{state}] {proposal.title}")
        return "\n".join(lines)

    async def approvals_text(self) -> str:
        blocks: list[str] = []
        for proposal in await self.store.list(limit=20):
            if proposal.state != "pending":
                continue
            authorization = await self.store.issue_proposal_token(
                proposal.id, ttl_minutes=self.config.approval_ttl_minutes
            )
            if authorization is not None:
                blocks.append(await self.render_authorization(authorization))
            if len(blocks) >= 2:
                break
        if len(blocks) < 2:
            for pending in (await self.store.pending_tools())[: 2 - len(blocks)]:
                issued = await self.store.issue_parked_token(
                    pending.run_id, ttl_minutes=self.config.approval_ttl_minutes
                )
                if issued is not None:
                    current, code = issued
                    blocks.append(render_tool_approval(current, code))
        return "\n\n---\n\n".join(blocks) if blocks else "No pending remote approvals."

    async def resolve(self, code: str, *, resolution: Literal["approve", "deny"]) -> str:
        if len(code.strip()) != 12:
            return "Invalid or expired approval code. Send /approvals for fresh pending codes."
        grant = await self.store.consume_token(code, resolution=resolution)
        if grant is None:
            return "Invalid or expired approval code. Send /approvals for fresh pending codes."
        if grant.subject_type == "proposal":
            proposal = await self.store.get(grant.subject_id)
            if proposal is None:
                return "That proposal no longer exists."
            if resolution == "deny":
                return f"Denied remote proposal #{proposal.id}. Nothing was scheduled or run."
            return await self._queue_proposal(proposal)

        pending = await self.store.pending_tool(grant.subject_id)
        if pending is None or pending.continuation.tool_input_hash != grant.binding_hash:
            return "That exact tool request is no longer pending. Nothing was executed."
        self._spawn(self._resume_parked(pending, resolution))
        verb = "approved" if resolution == "approve" else "denied"
        return (
            f"Tool request {verb} for task #{pending.task_id}. "
            "Kira is processing the saved continuation."
        )

    async def _queue_proposal(self, proposal: RemoteProposal) -> str:
        if await self.store.active_count() >= self.config.max_active_jobs:
            await self.store.mark_failed(proposal.id, "remote active-job limit reached")
            return (
                f"Proposal #{proposal.id} was approved but not queued: the remote active-job "
                f"limit ({self.config.max_active_jobs}) is reached."
            )
        timezone = get_localzone_name()
        schedule_kind = proposal.schedule_kind
        schedule_spec = proposal.schedule_spec
        if schedule_kind == "immediate":
            # A short future edge guarantees the origin + proposal mapping commits before the
            # scheduler can observe the task, even if its wake loop is polling concurrently.
            local = (self.tasks.now() + dt.timedelta(seconds=5)).astimezone(ZoneInfo(timezone))
            schedule_kind = "once"
            schedule_spec = local.replace(tzinfo=None).isoformat(timespec="seconds")
        try:
            task = await self.tasks.schedule(
                kind=proposal.kind,
                title=proposal.title,
                payload=proposal.instruction,
                schedule_kind=schedule_kind,
                schedule_spec=schedule_spec,
                created_by="user",
                timezone=timezone,
                project_id=proposal.project_id,
                origin="remote_operator",
                source_session_id=None,
            )
        except (ScheduleError, ValueError) as exc:
            await self.store.mark_failed(proposal.id, str(exc))
            return f"Proposal #{proposal.id} was approved but could not be queued: {exc}"
        if not await self.store.mark_queued(proposal.id, task.id):
            await self.tasks.cancel(task.id)
            await self.store.mark_failed(proposal.id, "could not bind the scheduled task")
            return "Kira could not bind the approved proposal safely; the task was cancelled."
        self.runner.kick()
        queued = await self.store.get(proposal.id)
        if queued is not None:
            self._start_monitor(queued)
        return (
            f"Approved and queued remote proposal #{proposal.id} as task #{task.id}. "
            "Kira will send milestones and request separate approval for risky tools."
        )

    async def _resume_parked(
        self, pending: RemotePendingTool, resolution: Literal["approve", "deny"]
    ) -> None:
        action = "approve" if resolution == "approve" else "reject"
        ok = await self.runner.resume_parked(pending.run_id, action)
        if not ok:
            await self._safe_send(
                f"Task #{pending.task_id} could not consume that parked approval safely. "
                "Nothing was replayed; send /approvals to inspect current state."
            )

    async def cancel(self, proposal_id_text: str) -> str:
        try:
            proposal_id = int(proposal_id_text.lstrip("#"))
        except ValueError:
            return "Usage: /cancel <remote-job-id>"
        proposal = await self.store.get(proposal_id)
        if proposal is None or proposal.task_id is None or proposal.state != "queued":
            return f"No active remote job #{proposal_id} to cancel."
        task = await self.tasks.cancel(proposal.task_id)
        if task is None:
            return f"Remote job #{proposal_id} is no longer active."
        await self.store.mark_cancelled(proposal_id)
        monitor = self._monitors.pop(proposal_id, None)
        if monitor is not None:
            monitor.cancel()
        return f"Cancelled remote job #{proposal_id} (task #{proposal.task_id})."

    async def handle_task_event(self, line: str, task: Task) -> None:
        if task.origin != "remote_operator":
            return
        proposal = await self.store.proposal_for_task(task.id)
        if "waiting for your approval" in line:
            pending = next(
                (item for item in await self.store.pending_tools() if item.task_id == task.id),
                None,
            )
            if pending is not None:
                issued = await self.store.issue_parked_token(
                    pending.run_id, ttl_minutes=self.config.approval_ttl_minutes
                )
                if issued is not None:
                    current, code = issued
                    await self._safe_send(render_tool_approval(current, code))
                    return
        prefix = f"Remote job #{proposal.id}: " if proposal is not None else "Remote job: "
        await self._safe_send(prefix + line)

    def dispatch_task_event(self, line: str, task: Task) -> None:
        """Schedule best-effort Telegram delivery from the runner's synchronous callback."""
        self._spawn(self.handle_task_event(line, task))

    def _start_monitor(self, proposal: RemoteProposal) -> None:
        if (
            proposal.task_id is None
            or proposal.status_interval_minutes <= 0
            or proposal.id in self._monitors
        ):
            return
        monitor = asyncio.create_task(self._monitor(proposal.id, proposal.task_id))
        self._monitors[proposal.id] = monitor
        monitor.add_done_callback(lambda _task: self._monitors.pop(proposal.id, None))

    async def _monitor(self, proposal_id: int, task_id: int) -> None:
        proposal = await self.store.get(proposal_id)
        if proposal is None:
            return
        interval = proposal.status_interval_minutes * 60
        while interval > 0:
            await self.sleep(interval)
            task = await self.tasks.store.get(task_id)
            if task is None or task.status != "active":
                return
            if not await self.store.record_status_update(proposal_id):
                return
            runs = await self.tasks.store.runs_for(task_id, limit=1)
            state = "waiting to run"
            if runs and runs[0].status == "running":
                state = (
                    "waiting for tool approval"
                    if runs[0].approval_state == "pending"
                    else "running"
                )
            await self._safe_send(
                f"Remote job #{proposal_id} is still {state}: {proposal.title}"
            )
