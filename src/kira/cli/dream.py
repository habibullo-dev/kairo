"""`kira dream run <job>` — run ONE dreaming job ATTENDED (Phase 16 Task 9).

This is the human-in-the-loop way to exercise a dreaming job before Checkpoint K: it collects,
runs the tool-less summarize under the budget, and creates the proposal in the attention queue —
then prints what it produced. It NEVER schedules anything and NEVER performs an action; the output
is a proposal you review in the Notification Center. Scheduling (the unattended path) is Task 10,
after Checkpoint K.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from kira.attention import JOBS, dream_run
from kira.attention.store import AttentionStore
from kira.persistence.database_identity import (
    DatabaseIdentityError,
    migrate_live_database,
)
from kira.persistence.db import connect
from kira.persistence.instance_lock import (
    InstanceAlreadyRunning,
    InstanceLock,
    ResetBarrier,
    ResetMaintenanceBusy,
)
from kira.persistence.reset_recovery import (
    ResetRecoveryError,
    interrupted_reset_diagnostic,
    recover_interrupted_reset,
)
from kira.scheduler.store import TaskStore

if TYPE_CHECKING:
    from kira.config import Config


async def _run(config: Config, database: Path, job_name: str) -> int:
    from rich.console import Console

    from kira.attention import NotificationRouter
    from kira.cli.repl import _build_cost_ledger, _utility_client
    from kira.connectors.factory import build_connectors
    from kira.persistence.sessions import SessionStore

    console = Console()
    db = await connect(database)
    try:
        store = SessionStore(db)  # shared connection + write lock
        ledger = _build_cost_ledger(config, db, store.lock)
        summarizer = _utility_client(config, ledger=ledger)  # thinking-off; model chosen per call
        now = _dt.datetime.now().astimezone()
        cap = config.attention.dreaming_budget_usd
        console.print(
            f"[dim]dreaming {job_name} — budget ${cap:.2f}, model policy Haiku/Sonnet, "
            f"proposal-only (no actions).[/]"
        )
        res = await dream_run(
            job_name,
            config=config,
            attention=AttentionStore(db, store.lock),
            summarizer=summarizer,
            tasks=TaskStore(db, store.lock),
            ledger=ledger,
            now=now,
            notification_router=NotificationRouter(config, build_connectors(config)),
        )
        if res.halted:
            console.print(f"[yellow]halted[/] — {res.reason or 'budget cap'} (an alert was filed).")
            return 0
        console.print(
            f"[green]proposal #{res.proposal_id}[/] filed in Notifications "
            f"(cost ${res.cost_usd or 0:.4f}):"
        )
        console.print(res.summary, markup=False)
        return 0
    finally:
        await db.close()


def dream_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="kira dream", description="Proposal-only dreaming jobs.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Run ONE dreaming job attended (NOT scheduled).")
    r.add_argument("job", choices=sorted(JOBS), help="which dreaming job to run once")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        from kira.config import ConfigError, load_config

        try:
            config = load_config()
            if interrupted_reset_diagnostic(config) is None:
                config.require("anthropic")
            with (
                ResetBarrier(config.data_dir) as barrier,
                InstanceLock(config.data_dir) as lock,
            ):
                recover_interrupted_reset(config, barrier, lock)
                config.require("anthropic")
                config.ensure_dirs()
                database = migrate_live_database(lock)
                barrier.release()
                return asyncio.run(_run(config, database, args.job))
        except ConfigError as exc:
            print(f"Dream configuration error: {exc}", file=sys.stderr)
            return 1
        except (
            InstanceAlreadyRunning,
            ResetMaintenanceBusy,
            ResetRecoveryError,
            DatabaseIdentityError,
        ) as exc:
            print(f"Dream command blocked: {exc}", file=sys.stderr)
            return 1
    return 2
