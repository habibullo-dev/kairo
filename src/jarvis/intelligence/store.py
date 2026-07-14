"""SQLite stores for snapshot-bound project analysis jobs and reports (schema v30).

Imported projects are untrusted and model analysis is expensive, so the durable identity is
``(project_id, snapshot_hash, profile_version)``.  Retrying a browser finalize or restarting the
host therefore resolves to the same job instead of silently buying a second analysis.  Reports
are never deleted; publishing a newer one only marks the older project reports stale.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from jarvis.intelligence.report import ProjectReportDraft


class AnalysisJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PUBLISHED = "published"
    DISCARDED = "discarded"
    FAILED = "failed"


_TERMINAL = {
    AnalysisJobState.PUBLISHED,
    AnalysisJobState.DISCARDED,
    AnalysisJobState.FAILED,
}


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _load_dict(text: str | None) -> dict:
    value = _load(text, {})
    return value if isinstance(value, dict) else {}


def _load_list(text: str | None) -> list:
    value = _load(text, [])
    return value if isinstance(value, list) else []


def _identity(snapshot_hash: str, profile_version: str) -> tuple[str, str]:
    snapshot = snapshot_hash.strip().lower()
    profile = profile_version.strip()
    if not snapshot or len(snapshot) > 128:
        raise ValueError("snapshot_hash must be 1-128 characters")
    if not profile or len(profile) > 64:
        raise ValueError("profile_version must be 1-64 characters")
    return snapshot, profile


@dataclass(frozen=True)
class AnalysisJob:
    id: int
    project_id: int
    snapshot_hash: str
    profile_version: str
    state: AnalysisJobState
    orchestration_run_id: int | None
    attempts: int
    last_error: str | None
    graph_watermark: int
    coverage: dict
    created_at: str
    updated_at: str


_JOB_COLUMNS = (
    "id, project_id, snapshot_hash, profile_version, state, orchestration_run_id, attempts, "
    "last_error, graph_watermark, coverage_json, created_at, updated_at"
)


def _job(row: tuple) -> AnalysisJob:
    return AnalysisJob(
        id=row[0],
        project_id=row[1],
        snapshot_hash=row[2],
        profile_version=row[3],
        state=AnalysisJobState(row[4]),
        orchestration_run_id=row[5],
        attempts=row[6],
        last_error=row[7],
        graph_watermark=row[8],
        coverage=_load_dict(row[9]),
        created_at=row[10],
        updated_at=row[11],
    )


class AnalysisJobStore:
    """Idempotent queue state over ``analysis_jobs``.

    ``claim`` is the sole queued→running transition and increments ``attempts`` atomically.
    Model work happens outside this store's lock; the coordinator returns only for small durable
    transitions so it cannot hold SQLite open during a provider call.
    """

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def enqueue(
        self,
        *,
        project_id: int,
        snapshot_hash: str,
        profile_version: str,
        graph_watermark: int = 0,
        coverage: dict | None = None,
    ) -> tuple[AnalysisJob, bool]:
        snapshot, profile = _identity(snapshot_hash, profile_version)
        if graph_watermark < 0:
            raise ValueError("graph_watermark must be non-negative")
        now = _now()
        async with self.lock:
            cur = await self.db.execute(
                "INSERT OR IGNORE INTO analysis_jobs "
                "(project_id, snapshot_hash, profile_version, graph_watermark, coverage_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    snapshot,
                    profile,
                    graph_watermark,
                    _dump(coverage or {}),
                    now,
                    now,
                ),
            )
            await self.db.commit()
            row = await (
                await self.db.execute(
                    f"SELECT {_JOB_COLUMNS} FROM analysis_jobs "
                    "WHERE project_id=? AND snapshot_hash=? AND profile_version=?",
                    (project_id, snapshot, profile),
                )
            ).fetchone()
        assert row is not None
        return _job(row), cur.rowcount > 0

    async def get(self, job_id: int) -> AnalysisJob | None:
        row = await (
            await self.db.execute(
                f"SELECT {_JOB_COLUMNS} FROM analysis_jobs WHERE id=?", (job_id,)
            )
        ).fetchone()
        return _job(row) if row else None

    async def list(
        self,
        *,
        state: AnalysisJobState | str | None = None,
        project_id: int | None = None,
        limit: int = 100,
    ) -> list[AnalysisJob]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state=?")
            params.append(state.value if isinstance(state, AnalysisJobState) else state)
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1_000)))
        rows = await (
            await self.db.execute(
                f"SELECT {_JOB_COLUMNS} FROM analysis_jobs {where} ORDER BY id LIMIT ?",
                tuple(params),
            )
        ).fetchall()
        return [_job(row) for row in rows]

    async def latest(self, project_id: int) -> AnalysisJob | None:
        """Newest durable assessment identity for one project."""
        row = await (
            await self.db.execute(
                f"SELECT {_JOB_COLUMNS} FROM analysis_jobs "
                "WHERE project_id=? ORDER BY id DESC LIMIT 1",
                (project_id,),
            )
        ).fetchone()
        return _job(row) if row else None

    async def claim(self, job_id: int) -> AnalysisJob | None:
        now = _now()
        async with self.lock:
            cur = await self.db.execute(
                "UPDATE analysis_jobs SET state='running', attempts=attempts+1, last_error=NULL, "
                "updated_at=? WHERE id=? AND state='queued'",
                (now, job_id),
            )
            await self.db.commit()
            if cur.rowcount <= 0:
                return None
            row = await (
                await self.db.execute(
                    f"SELECT {_JOB_COLUMNS} FROM analysis_jobs WHERE id=?", (job_id,)
                )
            ).fetchone()
        assert row is not None
        return _job(row)

    async def attach_run(self, job_id: int, run_id: int) -> bool:
        async with self.lock:
            cur = await self.db.execute(
                "UPDATE analysis_jobs SET orchestration_run_id=?, updated_at=? "
                "WHERE id=? AND state='running' AND orchestration_run_id IS NULL",
                (run_id, _now(), job_id),
            )
            await self.db.commit()
        return cur.rowcount > 0

    async def attach_run_in_transaction(self, expected: AnalysisJob, run_id: int) -> bool:
        """CAS-bind a newly inserted orchestration run without acquiring or committing.

        The caller owns ``transaction(self.db, self.lock)``.  Matching the claimed attempt keeps
        an old worker from attaching work to a later retry of the same durable job.
        """
        if (
            expected.state is not AnalysisJobState.RUNNING
            or expected.orchestration_run_id is not None
        ):
            return False
        cur = await self.db.execute(
            "UPDATE analysis_jobs SET orchestration_run_id=?, updated_at=? "
            "WHERE id=? AND project_id=? AND snapshot_hash=? AND profile_version=? "
            "AND state='running' AND attempts=? AND orchestration_run_id IS NULL",
            (
                run_id,
                _now(),
                expected.id,
                expected.project_id,
                expected.snapshot_hash,
                expected.profile_version,
                expected.attempts,
            ),
        )
        return cur.rowcount == 1

    async def requeue(self, job_id: int, *, error: str | None = None) -> bool:
        async with self.lock:
            cur = await self.db.execute(
                "UPDATE analysis_jobs SET state='queued', orchestration_run_id=NULL, "
                "last_error=?, updated_at=? WHERE id=? AND state='running'",
                ((error or "")[:500] or None, _now(), job_id),
            )
            await self.db.commit()
        return cur.rowcount > 0

    async def finish(
        self,
        job_id: int,
        state: AnalysisJobState,
        *,
        error: str | None = None,
        coverage: dict | None = None,
    ) -> bool:
        if state not in _TERMINAL:
            raise ValueError("finish state must be published, discarded, or failed")
        sets = ["state=?", "last_error=?", "updated_at=?"]
        params: list[object] = [state.value, (error or "")[:500] or None, _now()]
        if coverage is not None:
            sets.append("coverage_json=?")
            params.append(_dump(coverage))
        params.append(job_id)
        async with self.lock:
            cur = await self.db.execute(
                f"UPDATE analysis_jobs SET {', '.join(sets)} "
                "WHERE id=? AND state='running'",
                tuple(params),
            )
            await self.db.commit()
        return cur.rowcount > 0

    async def transition_in_transaction(
        self,
        expected: AnalysisJob,
        state: AnalysisJobState,
        *,
        error: str | None = None,
        coverage: dict | None = None,
    ) -> bool:
        """Full-claim CAS terminal transition; caller owns the shared transaction."""
        if expected.state is not AnalysisJobState.RUNNING or state not in _TERMINAL:
            return False
        sets = ["state=?", "last_error=?", "updated_at=?"]
        params: list[object] = [state.value, (error or "")[:500] or None, _now()]
        if coverage is not None:
            sets.append("coverage_json=?")
            params.append(_dump(coverage))
        params.extend(
            (
                expected.id,
                expected.project_id,
                expected.snapshot_hash,
                expected.profile_version,
                expected.attempts,
                expected.orchestration_run_id,
            )
        )
        cur = await self.db.execute(
            f"UPDATE analysis_jobs SET {', '.join(sets)} WHERE id=? AND project_id=? "
            "AND snapshot_hash=? AND profile_version=? AND state='running' AND attempts=? "
            "AND orchestration_run_id IS ?",
            tuple(params),
        )
        return cur.rowcount == 1


@dataclass(frozen=True)
class ProjectReport:
    id: int
    project_id: int
    snapshot_hash: str
    profile_version: str
    orchestration_run_id: int | None
    status: str
    trust_class: str
    summary: str
    coverage: dict
    strengths: list
    weaknesses: list
    security_candidates: list
    fe_be_gaps: list
    test_gaps: list
    recommendations: list
    evidence: list
    created_at: str


_REPORT_COLUMNS = (
    "id, project_id, snapshot_hash, profile_version, orchestration_run_id, status, trust_class, "
    "summary, coverage_json, strengths_json, weaknesses_json, security_candidates_json, "
    "fe_be_gaps_json, test_gaps_json, recommendations_json, evidence_json, created_at"
)


def _report(row: tuple) -> ProjectReport:
    return ProjectReport(
        id=row[0],
        project_id=row[1],
        snapshot_hash=row[2],
        profile_version=row[3],
        orchestration_run_id=row[4],
        status=row[5],
        trust_class=row[6],
        summary=row[7],
        coverage=_load_dict(row[8]),
        strengths=_load_list(row[9]),
        weaknesses=_load_list(row[10]),
        security_candidates=_load_list(row[11]),
        fe_be_gaps=_load_list(row[12]),
        test_gaps=_load_list(row[13]),
        recommendations=_load_list(row[14]),
        evidence=_load_list(row[15]),
        created_at=row[16],
    )


class ProjectReportStore:
    """Append-only report history; older project reports become stale on publish."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.lock = lock or asyncio.Lock()

    async def create(
        self,
        *,
        project_id: int,
        snapshot_hash: str,
        profile_version: str,
        summary: str,
        orchestration_run_id: int | None = None,
        coverage: dict | None = None,
        strengths: list | None = None,
        weaknesses: list | None = None,
        security_candidates: list | None = None,
        fe_be_gaps: list | None = None,
        test_gaps: list | None = None,
        recommendations: list | None = None,
        evidence: list | None = None,
    ) -> tuple[ProjectReport, bool]:
        snapshot, profile = _identity(snapshot_hash, profile_version)
        bounded_summary = summary.strip()[:4_000]
        if not bounded_summary:
            raise ValueError("summary is required")
        now = _now()
        async with self.lock:
            cur = await self.db.execute(
                "INSERT OR IGNORE INTO project_reports "
                "(project_id, snapshot_hash, profile_version, orchestration_run_id, summary, "
                "coverage_json, strengths_json, weaknesses_json, security_candidates_json, "
                "fe_be_gaps_json, test_gaps_json, recommendations_json, evidence_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    snapshot,
                    profile,
                    orchestration_run_id,
                    bounded_summary,
                    _dump(coverage or {}),
                    _dump(strengths or []),
                    _dump(weaknesses or []),
                    _dump(security_candidates or []),
                    _dump(fe_be_gaps or []),
                    _dump(test_gaps or []),
                    _dump(recommendations or []),
                    _dump(evidence or []),
                    now,
                ),
            )
            # A retry of an older, already-stale snapshot is a true no-op.  Demote the prior
            # current report only after this call actually inserted a new snapshot; doing it
            # before INSERT OR IGNORE could leave every report stale on an old-snapshot retry.
            if cur.rowcount > 0:
                await self.db.execute(
                    "UPDATE project_reports SET status='stale' "
                    "WHERE project_id=? AND status='current' AND "
                    "NOT (snapshot_hash=? AND profile_version=?)",
                    (project_id, snapshot, profile),
                )
            await self.db.commit()
            row = await (
                await self.db.execute(
                    f"SELECT {_REPORT_COLUMNS} FROM project_reports "
                    "WHERE project_id=? AND snapshot_hash=? AND profile_version=?",
                    (project_id, snapshot, profile),
                )
            ).fetchone()
        assert row is not None
        return _report(row), cur.rowcount > 0

    async def create_draft_in_transaction(
        self,
        *,
        job: AnalysisJob,
        orchestration_run_id: int,
        draft: ProjectReportDraft,
    ) -> tuple[ProjectReport, bool]:
        """Insert/recover a validated draft without locking or committing."""
        summary = str(draft.summary).strip()[:4_000]
        if not summary:
            raise ValueError("summary is required")
        now = _now()
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO project_reports "
            "(project_id, snapshot_hash, profile_version, orchestration_run_id, summary, "
            "coverage_json, strengths_json, weaknesses_json, security_candidates_json, "
            "fe_be_gaps_json, test_gaps_json, recommendations_json, evidence_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.project_id,
                job.snapshot_hash,
                job.profile_version,
                orchestration_run_id,
                summary,
                _dump(draft.coverage),
                _dump(draft.strengths),
                _dump(draft.weaknesses),
                _dump(draft.security_candidates),
                _dump(draft.fe_be_gaps),
                _dump(draft.test_gaps),
                _dump(draft.recommendations),
                _dump(draft.evidence),
                now,
            ),
        )
        if cur.rowcount > 0:
            await self.db.execute(
                "UPDATE project_reports SET status='stale' "
                "WHERE project_id=? AND status='current' AND "
                "NOT (snapshot_hash=? AND profile_version=?)",
                (job.project_id, job.snapshot_hash, job.profile_version),
            )
        row = await (
            await self.db.execute(
                f"SELECT {_REPORT_COLUMNS} FROM project_reports "
                "WHERE project_id=? AND snapshot_hash=? AND profile_version=?",
                (job.project_id, job.snapshot_hash, job.profile_version),
            )
        ).fetchone()
        assert row is not None
        return _report(row), cur.rowcount > 0

    async def activate_in_transaction(self, report: ProjectReport) -> ProjectReport:
        """Make a proven-current report current again after a project content reversion."""
        await self.db.execute(
            "UPDATE project_reports SET status=CASE WHEN id=? THEN 'current' ELSE 'stale' END "
            "WHERE project_id=? AND (status='current' OR id=?)",
            (report.id, report.project_id, report.id),
        )
        row = await (
            await self.db.execute(
                f"SELECT {_REPORT_COLUMNS} FROM project_reports WHERE id=?", (report.id,)
            )
        ).fetchone()
        assert row is not None
        return _report(row)

    async def get(self, report_id: int) -> ProjectReport | None:
        row = await (
            await self.db.execute(
                f"SELECT {_REPORT_COLUMNS} FROM project_reports WHERE id=?", (report_id,)
            )
        ).fetchone()
        return _report(row) if row else None

    async def latest(self, project_id: int, *, current_only: bool = True) -> ProjectReport | None:
        state = "AND status='current'" if current_only else ""
        row = await (
            await self.db.execute(
                f"SELECT {_REPORT_COLUMNS} FROM project_reports "
                f"WHERE project_id=? {state} ORDER BY id DESC LIMIT 1",
                (project_id,),
            )
        ).fetchone()
        return _report(row) if row else None

    async def list(self, *, project_id: int, limit: int = 100) -> list[ProjectReport]:
        rows = await (
            await self.db.execute(
                f"SELECT {_REPORT_COLUMNS} FROM project_reports WHERE project_id=? "
                "ORDER BY id DESC LIMIT ?",
                (project_id, max(1, min(limit, 1_000))),
            )
        ).fetchall()
        return [_report(row) for row in rows]
