"""OrchestrationStore — the ``orchestration_runs`` audit record (schema v7).

One row per orchestration run, opened ``running`` before any stage executes (so a crash
leaves an orphan the startup sweep marks ``aborted``, mirroring ``agent_runs``/``task_runs``).
Metadata + short summaries only — never a verbatim prompt or child report (A2). The engine
advances ``stage``/``verdict``/costs as it goes; child sub-agent rows link back via
``agent_runs.orchestration_run_id``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass

import aiosqlite

_COLUMNS = (
    "id, project_id, workflow, title, config_json, context_manifest_json, status, stage, "
    "verdict, synthesis_summary, estimated_cost_usd, actual_cost_usd, budget_usd, session_id, "
    "trace_id, started_at, finished_at, created_at, skills_manifest_json, verdict_rationale, "
    "synthesis_findings_json, action_items_json, resume_state, resume_checkpoint_json"
)

#: Terminal statuses (CHECK-enforced in the schema).
TERMINAL = frozenset(
    {"ok", "rejected", "revise", "error", "cancelled", "aborted", "budget_stopped"}
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _safe_resume_checkpoint(value: object) -> bool:
    """Accept only the engine's bounded, inert pre-execution checkpoint shape.

    This store is a privacy boundary, not merely a convenience JSON column.  It rejects extra
    keys so an accidental caller cannot turn recovery metadata into a prompt/report archive.
    """
    if not isinstance(value, dict) or set(value) != {"v", "kind", "summary", "findings"}:
        return False
    if value.get("v") != 1 or value.get("kind") != "post_synthesis_pre_execution":
        return False
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary or len(summary) > 2000 or "\x00" in summary:
        return False
    findings = value.get("findings")
    if not isinstance(findings, list) or len(findings) > 8:
        return False
    for item in findings:
        if not isinstance(item, dict) or set(item) != {"member", "title", "finding"}:
            return False
        if not all(isinstance(item.get(key), str) for key in ("member", "title", "finding")):
            return False
        if (
            not item["member"]
            or not item["title"]
            or not item["finding"]
            or len(item["member"]) > 80
            or len(item["title"]) > 160
            or len(item["finding"]) > 600
            or any("\x00" in item[key] for key in ("member", "title", "finding"))
        ):
            return False
    return True


@dataclass(frozen=True)
class OrchestrationRun:
    id: int
    project_id: int
    workflow: str
    title: str
    config: dict
    context_manifest: list
    status: str
    stage: str | None
    verdict: str | None
    synthesis_summary: str | None
    estimated_cost_usd: float | None
    actual_cost_usd: float | None
    budget_usd: float | None
    session_id: int | None
    trace_id: str | None
    started_at: str
    finished_at: str | None
    created_at: str
    skills_manifest: list
    verdict_rationale: str | None
    synthesis_findings: list
    action_items: list
    resume_state: str
    resume_checkpoint: dict


def _row_to_run(row: tuple) -> OrchestrationRun:
    return OrchestrationRun(
        id=row[0],
        project_id=row[1],
        workflow=row[2],
        title=row[3],
        config=json.loads(row[4]) if row[4] else {},
        context_manifest=json.loads(row[5]) if row[5] else [],
        status=row[6],
        stage=row[7],
        verdict=row[8],
        synthesis_summary=row[9],
        estimated_cost_usd=row[10],
        actual_cost_usd=row[11],
        budget_usd=row[12],
        session_id=row[13],
        trace_id=row[14],
        started_at=row[15],
        finished_at=row[16],
        created_at=row[17],
        skills_manifest=json.loads(row[18]) if row[18] else [],
        verdict_rationale=row[19],
        synthesis_findings=json.loads(row[20]) if row[20] else [],
        action_items=json.loads(row[21]) if row[21] else [],
        resume_state=row[22] or "none",
        resume_checkpoint=json.loads(row[23]) if row[23] else {},
    )


class OrchestrationStore:
    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def begin_run(
        self,
        *,
        project_id: int,
        workflow: str,
        title: str,
        config: dict,
        context_manifest: list,
        estimated_cost_usd: float | None,
        budget_usd: float | None,
        session_id: int | None = None,
        trace_id: str | None = None,
        skills_manifest: list[dict] | None = None,
    ) -> int:
        """Open a ``running`` run row (title is already sanitized by the caller — never raw
        user/email text). config/manifest are metadata + hashes only, no bodies."""
        now = _now()
        async with self.lock:
            cursor = await self.db.execute(
                "INSERT INTO orchestration_runs "
                "(project_id, workflow, title, config_json, context_manifest_json, status, "
                "estimated_cost_usd, budget_usd, session_id, trace_id, started_at, created_at, "
                "skills_manifest_json) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    workflow,
                    title,
                    json.dumps(config),
                    json.dumps(context_manifest),
                    estimated_cost_usd,
                    budget_usd,
                    session_id,
                    trace_id,
                    now,
                    now,
                    json.dumps(skills_manifest or []),
                ),
            )
            await self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def set_stage(self, run_id: int, stage: str) -> None:
        async with self.lock:
            await self.db.execute(
                "UPDATE orchestration_runs SET stage = ? WHERE id = ?", (stage, run_id)
            )
            await self.db.commit()

    async def set_resume_checkpoint(self, run_id: int, checkpoint: dict) -> None:
        """Save the only resumable point: completed synthesis before any writer enters.

        ``checkpoint`` is assembled by the engine from already-bounded head output.  It contains
        no task/context body or child report; the resumed caller must prove an exact fresh
        context manifest before the engine will claim it.
        """
        if not _safe_resume_checkpoint(checkpoint):
            raise ValueError("invalid orchestration resume checkpoint")
        async with self.lock:
            await self.db.execute(
                "UPDATE orchestration_runs SET resume_state = 'ready', "
                "resume_checkpoint_json = ? WHERE id = ? AND status = 'running'",
                (json.dumps(checkpoint), run_id),
            )
            await self.db.commit()

    async def clear_resume_checkpoint(self, run_id: int) -> None:
        """Make a run non-resumable before execution/review/verdict or terminal completion."""
        async with self.lock:
            await self.db.execute(
                "UPDATE orchestration_runs SET resume_state = 'none', "
                "resume_checkpoint_json = '{}' WHERE id = ?",
                (run_id,),
            )
            await self.db.commit()

    async def claim_resume_checkpoint(self, run_id: int) -> dict | None:
        """Atomically claim a crashed post-synthesis checkpoint exactly once.

        The claim clears the checkpoint *before* a writer can run.  A second crash therefore
        asks for a new deliberate run instead of risking a duplicate write.
        """
        async with self.lock:
            cursor = await self.db.execute(
                "SELECT resume_checkpoint_json FROM orchestration_runs "
                "WHERE id = ? AND status = 'aborted' AND resume_state = 'ready'",
                (run_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            try:
                checkpoint = json.loads(row[0]) if row[0] else {}
            except (TypeError, json.JSONDecodeError):
                return None
            if not _safe_resume_checkpoint(checkpoint):
                return None
            result = await self.db.execute(
                "UPDATE orchestration_runs SET status = 'running', stage = 'execution', "
                "finished_at = NULL, resume_state = 'none', resume_checkpoint_json = '{}' "
                "WHERE id = ? AND status = 'aborted' AND resume_state = 'ready'",
                (run_id,),
            )
            await self.db.commit()
        return checkpoint if result.rowcount == 1 else None

    async def complete_run(
        self,
        run_id: int,
        *,
        status: str,
        verdict: str | None = None,
        synthesis_summary: str | None = None,
        verdict_rationale: str | None = None,
        synthesis_findings: list[dict] | None = None,
        action_items: list[dict] | None = None,
        actual_cost_usd: float | None = None,
    ) -> None:
        """Write the terminal state and bounded head-generated result metadata.

        Raw child reports and prompts remain outside this record. ``synthesis_findings`` and
        ``action_items`` are small, inert head syntheses—not child transcripts or scheduler work.
        """
        async with self.lock:
            await self.db.execute(
                "UPDATE orchestration_runs SET status = ?, verdict = ?, synthesis_summary = ?, "
                "verdict_rationale = ?, synthesis_findings_json = ?, action_items_json = ?, "
                "actual_cost_usd = ?, resume_state = 'none', resume_checkpoint_json = '{}', "
                "finished_at = ? WHERE id = ?",
                (
                    status,
                    verdict,
                    synthesis_summary,
                    verdict_rationale,
                    json.dumps(synthesis_findings or []),
                    json.dumps(action_items or []),
                    actual_cost_usd,
                    _now(),
                    run_id,
                ),
            )
            await self.db.commit()

    async def get(self, run_id: int) -> OrchestrationRun | None:
        cursor = await self.db.execute(
            f"SELECT {_COLUMNS} FROM orchestration_runs WHERE id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        return _row_to_run(row) if row else None

    async def list(
        self, *, project_id: int | None = None, limit: int = 50
    ) -> list[OrchestrationRun]:
        if project_id is not None:
            cursor = await self.db.execute(
                f"SELECT {_COLUMNS} FROM orchestration_runs WHERE project_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            )
        else:
            cursor = await self.db.execute(
                f"SELECT {_COLUMNS} FROM orchestration_runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [_row_to_run(r) for r in await cursor.fetchall()]

    async def sweep_orphans(self) -> list[str]:
        """Mark any still-``running`` runs ``aborted`` (a crash left them open).

        A ``ready`` row keeps its bodies-free post-synthesis checkpoint, making one explicit,
        manifest-verified continuation possible.  Every other interrupted stage stays aborted
        only: it may have entered execution and must never be replayed.
        """
        cursor = await self.db.execute(
            "SELECT id, workflow FROM orchestration_runs WHERE status = 'running' ORDER BY id"
        )
        orphans = await cursor.fetchall()
        if not orphans:
            return []
        note = "interrupted before completion (process exited mid-run) — marked aborted"
        now = _now()
        async with self.lock:
            await self.db.execute(
                "UPDATE orchestration_runs SET status = 'aborted', finished_at = ? "
                "WHERE status = 'running'",
                (now,),
            )
            await self.db.commit()
        return [f'orchestration run #{rid} "{wf}" {note}' for rid, wf in orphans]
