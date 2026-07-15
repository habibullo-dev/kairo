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

from jarvis.attention import JOBS, dream_run
from jarvis.attention.store import AttentionStore
from jarvis.persistence.db import connect
from jarvis.scheduler.store import TaskStore


async def _run(job_name: str) -> int:
    from rich.console import Console

    from jarvis.attention import NotificationRouter
    from jarvis.cli.repl import _build_cost_ledger, _utility_client
    from jarvis.config import load_config
    from jarvis.connectors.factory import build_connectors
    from jarvis.persistence.sessions import SessionStore

    console = Console()
    config = load_config(require=("anthropic",))
    config.ensure_dirs()
    db = await connect(config.data_dir / "jarvis.db")
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
        console.print(f"[green]proposal #{res.proposal_id}[/] filed in Notifications "
                      f"(cost ${res.cost_usd or 0:.4f}):")
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
        return asyncio.run(_run(args.job))
    return 2
