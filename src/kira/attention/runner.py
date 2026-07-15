"""Dreaming run orchestration (Phase 16 Task 7): collect → run one job → proposal.

Each ``dream_run`` is ONE job = ONE chunk (a single deterministic collect + one tool-less
summarize — well under the ~14-minute background ceiling). Scheduling is a separate, deliberate
step (Task 10, AFTER Checkpoint K); nothing here registers a scheduler task. ``kira dream run
<job>`` runs a job ATTENDED for testing. Collectors read only local, durable, reviewed data
(tasks + cost ledger) — never quarantined suggestions, never dreaming's own prior proposals.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from kira.attention.builders import JOBS, DreamingBudget, DreamingResult, run_dreaming_job


def _day_start(now: _dt.datetime) -> str:
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _due_today(next_run_at: str | None, today: _dt.date, tz: Any) -> bool:
    if not next_run_at:
        return False
    try:
        when = _dt.datetime.fromisoformat(next_run_at)
    except ValueError:
        return False
    when = when.astimezone(tz) if when.tzinfo else when
    return when.date() == today


async def collect(
    job_name: str,
    *,
    tasks: Any = None,
    ledger: Any = None,
    now: _dt.datetime,
    project_id: int | None = None,
) -> list[str]:
    """Deterministic material for a job — plain lines, no model. Returns [] when the needed store
    is absent (the job then summarizes 'nothing notable'). Reads reviewed/durable data only."""
    lines: list[str] = []
    today = now.date()
    tz = now.tzinfo
    task_rows = await tasks.list() if tasks is not None else []

    if job_name == "morning_briefing":
        due = [t for t in task_rows if _due_today(t.next_run_at, today, tz)]
        lines = [f"Due today: {t.title} ({t.kind})" for t in due]
    elif job_name == "nightly_review":
        if ledger is not None:
            spend = await ledger.spent(since=_day_start(now))
            lines.append(f"Model spend today: ${spend:.2f}")
        lines += [
            f"Job failing: {t.title} ({t.consecutive_failures} consecutive failures)"
            for t in task_rows
            if t.consecutive_failures
        ]
    elif job_name == "bottleneck":
        lines = [
            f"Recurring failure: {t.title} ({t.consecutive_failures}x)"
            for t in task_rows
            if t.consecutive_failures >= 2
        ]
    elif job_name == "roi_summary":
        if ledger is not None:
            spend = await ledger.spent(since=_day_start(now))
            lines.append(f"Model spend today: ${spend:.2f} (weigh against time saved).")
    elif job_name == "self_improvement":
        # Deterministic seed only; the codebase-reading variant runs in the cage (deferred). For
        # now it reflects on the day's failing jobs as improvement candidates.
        lines = [
            f"Consider automating/fixing: {t.title}"
            for t in task_rows
            if t.consecutive_failures
        ]
    return lines


async def dream_run(
    job_name: str,
    *,
    config: Any,
    attention: Any,
    summarizer: Any,
    tasks: Any = None,
    ledger: Any = None,
    artifacts: Any = None,
    now: _dt.datetime,
    project_id: int | None = None,
    notification_router: Any = None,
) -> DreamingResult:
    """Collect + run ONE dreaming job (one chunk). Budget comes from
    ``config.attention.dreaming_budget_usd`` (0 ⇒ disabled ⇒ the job halts before spending). Dedup
    window = the local date, so a same-day re-run doesn't re-nag. NEVER schedules anything."""
    job = JOBS.get(job_name)
    if job is None:
        raise ValueError(f"unknown dreaming job: {job_name!r}")
    budget = DreamingBudget(cap_usd=config.attention.dreaming_budget_usd)
    collected = await collect(
        job_name, tasks=tasks, ledger=ledger, now=now, project_id=project_id
    )
    return await run_dreaming_job(
        job,
        collected=collected,
        summarizer=summarizer,
        budget=budget,
        attention=attention,
        artifacts=artifacts,
        project_id=project_id,
        window=now.date().isoformat(),
        notification_router=notification_router,
    )
