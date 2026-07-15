"""Expected-output contracts for scheduled jobs stay bounded and deterministic."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest

from kira.config import SchedulerConfig
from kira.scheduler.runner import BackgroundRunner, JobOutcome
from kira.scheduler.service import TaskService
from kira.scheduler.store import TaskStore
from kira.scheduler.verification import VerificationContract, verify_final_text

UTC = dt.UTC
START = dt.datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> dt.datetime:
        return self.at

    def advance(self, **kwargs: float) -> None:
        self.at += dt.timedelta(**kwargs)


async def _service(tmp_path: Path) -> tuple[TaskService, Clock, TaskStore]:
    from kira.persistence.db import connect

    clock = Clock()
    store = TaskStore(await connect(tmp_path / "tasks.db"))
    return TaskService(store, SchedulerConfig(), now=clock), clock, store


async def _task(service: TaskService, contract: VerificationContract):
    return await service.schedule(
        kind="job",
        title="report",
        payload="write the report",
        schedule_kind="interval",
        schedule_spec="3600",
        timezone="UTC",
        created_by="user",
        verification=contract,
    )


def test_contract_is_literal_bounded_and_does_not_echo_terms_in_results() -> None:
    contract = VerificationContract.contains_all(["STATUS: complete", "FILES-CHANGED"])
    assert verify_final_text(contract, "files-changed\nStatus: Complete").status == "passed"
    failed = verify_final_text(contract, "STATUS: complete")
    assert failed.status == "failed"
    assert failed.summary == "required-output check missing 1 of 2 phrase(s)"
    assert "FILES-CHANGED" not in failed.summary

    assert VerificationContract.from_json(contract.to_json()) == contract
    with pytest.raises(ValueError, match="unknown|invalid"):
        VerificationContract.from_json('{"v":1,"kind":"contains_all","terms":["x"],"extra":1}')
    with pytest.raises(ValueError, match="at most 8"):
        VerificationContract.contains_all([str(i) for i in range(9)])


async def test_job_verification_pass_is_durable_and_visible(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    try:
        contract = VerificationContract.contains_all(["STATUS: complete", "FILES-CHANGED"])
        task = await _task(service, contract)
        notices: list[str] = []

        async def run_job(_task):
            return JobOutcome(session_id=None, text="STATUS: complete\nFILES-CHANGED: report.md")

        runner = BackgroundRunner(
            service, notify=notices.append, run_job=run_job, turn_lock=asyncio.Lock()
        )
        clock.advance(hours=1, minutes=1)
        assert await runner.check_due() == 1
        (run,) = await store.runs_for(task.id)
        assert run.status == "ok"
        assert run.verification_status == "passed"
        assert run.verification_summary == "required-output check matched 2 phrase(s)"
        assert any("✓" in notice for notice in notices)
    finally:
        await store.db.close()


async def test_job_verification_failure_is_terminal_and_never_retry_safe(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    try:
        task = await _task(service, VerificationContract.contains_all(["RESULT: safe", "AUDIT-ID"]))

        async def run_job(_task):
            return JobOutcome(session_id=None, text="RESULT: safe", retry_safe=True)

        runner = BackgroundRunner(
            service, notify=lambda _line: None, run_job=run_job, turn_lock=asyncio.Lock()
        )
        clock.advance(hours=1, minutes=1)
        await runner.check_due()
        (run,) = await store.runs_for(task.id)
        assert run.status == "error"
        assert run.result_text == "RESULT: safe"
        assert run.verification_status == "failed"
        assert run.verification_summary == "required-output check missing 1 of 2 phrase(s)"
        assert run.error == "verification failed: required-output check missing 1 of 2 phrase(s)"
        # A missing final phrase must never authorize a duplicate side effect.
        updated = await store.get(task.id)
        assert updated is not None
        assert updated.status == "failed" and updated.next_run_at is None
        assert updated.consecutive_failures == 1
    finally:
        await store.db.close()


async def test_missed_verified_job_records_that_no_check_ran(tmp_path: Path) -> None:
    service, clock, store = await _service(tmp_path)
    try:
        task = await _task(service, VerificationContract.contains_all(["STATUS: complete"]))
        runner = BackgroundRunner(
            service,
            notify=lambda _line: None,
            run_job=lambda _task: (_ for _ in ()).throw(AssertionError("must not run")),
            turn_lock=asyncio.Lock(),
        )
        clock.advance(hours=5)
        await runner.check_due()
        (run,) = await store.runs_for(task.id)
        assert run.status == "missed"
        assert run.verification_status == "not_run"
        assert run.verification_summary == (
            "required-output check did not run because this occurrence was missed"
        )
    finally:
        await store.db.close()
