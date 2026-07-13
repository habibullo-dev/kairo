"""Durable, proposal-first Telegram Remote Operator.

Natural-language Telegram messages may expose exactly one tool to the utility model:
``remote_propose_work``.  That tool can only persist a local proposal.  It cannot run project
tools, approve itself, or create a scheduler task.  Execution begins only after an expiring,
single-use code is resolved by the allowlisted Telegram controller.

Approved jobs later use the scheduler's existing parked-continuation mechanism.  Every risky
tool request is therefore bound to its original tool id, name, canonical input hash, and saved
model transcript before a second Telegram approval can resume it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Literal

import aiosqlite
from pydantic import BaseModel, Field, model_validator
from tzlocal import get_localzone_name

from jarvis.config import TelegramRemoteOperatorConfig
from jarvis.permissions.gate import Decision
from jarvis.persistence.db import transaction
from jarvis.projects.store import Project, ProjectStore
from jarvis.scheduler.store import ParkedContinuation
from jarvis.scheduler.triggers import validate
from jarvis.tools.base import Permission, Tool, ToolResult

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
    """Proposal/token persistence on Kairo's shared SQLite connection and lock."""

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
                "AND t.status = 'active' AND p.state = 'queued' "
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

    async def mark_failed(self, proposal_id: int, error: str) -> None:
        now = _iso(_utc_now())
        async with self.lock:
            await self.db.execute(
                "UPDATE remote_operator_proposals SET state = 'failed', error = ?, "
                "updated_at = ? WHERE id = ? AND state = 'approved'",
                (error[:500], now, proposal_id),
            )
            await self.db.commit()

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
        description="An existing Kairo project id, slug, or exact name; never a filesystem path.",
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


class RemoteProposalGate:
    """A structural gate for the one proposal-only remote model tool."""

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
        return Decision(Permission.DENY, "remote model has no execution authority")


class RemoteProposalTool(Tool):
    name = "remote_propose_work"
    description = (
        "Prepare one owner-requested Kairo job or reminder for Telegram approval. This only "
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
            raise ValueError("Projects are unavailable. Use local Kairo to configure one.")
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
