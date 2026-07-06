"""Live smoke-eval runner.

Runs each scenario in tests/evals/scenarios/*.yaml against the *real* API, N times
(default 3 — agents are stochastic, so a single pass hides flakiness), in an
isolated temp working directory. A scenario passes only if all N runs pass.

Not a pytest test: it hits the network and costs money. Run explicitly:

    uv run python tests/evals/runner.py            # all scenarios, 3 runs each
    uv run python tests/evals/runner.py --runs 1   # quick single pass
    uv run python tests/evals/runner.py --scenario web_research
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import yaml

from jarvis.cli.jobs import JobRunner
from jarvis.config import ConfigError, load_config
from jarvis.core import AgentLoop, AnthropicClient, ToolCall
from jarvis.core.context import ContextManager
from jarvis.core.events import Event, ToolStarted
from jarvis.core.prompts import build_system
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory import MemoryService, MemoryStore, VoyageEmbedder
from jarvis.observability.cost import cost_of
from jarvis.permissions import PermissionGate, load_policy
from jarvis.permissions.gate import Decision
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.scheduler.runner import BackgroundRunner
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import TaskStore
from jarvis.tools import Permission, ToolContext, ToolExecutor, ToolRegistry

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


async def _seed_tasks(store: TaskStore, specs: list[dict]) -> None:
    """Insert scenario ``background_tasks`` directly (bypassing the past-time guard),
    defaulting to a fire time 30s ago so they're due-within-grace on ``check_due``."""
    due = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)).isoformat()
    for spec in specs:
        await store.add(
            kind=spec["kind"],
            title=spec.get("title", "seeded task"),
            payload=spec["payload"],
            schedule_kind=spec.get("schedule_kind", "once"),
            schedule_spec=spec.get("schedule_spec", due),
            timezone=spec.get("timezone", "UTC"),
            next_run_at=spec.get("next_run_at", due),
            created_by=spec.get("created_by", "user"),
        )


async def _seed_kb_sources(knowledge: KnowledgeService, specs: list[dict]) -> None:
    """Pre-ingest sources for a scenario's ``setup.kb_sources`` (each a dict with one
    of path/url/text, + optional title) before the turns run."""
    for spec in specs:
        await knowledge.ingest(
            path=spec.get("path"),
            url=spec.get("url"),
            text=spec.get("text"),
            title=spec.get("title"),
            created_by=spec.get("created_by", "user"),
        )


async def _seed_wiki_pages(knowledge: KnowledgeService, pages: dict[str, str]) -> None:
    """Pre-write wiki pages for a scenario's ``setup.wiki_pages`` (path -> content),
    through write_page so links are indexed (needed to seed lint defects)."""
    for page, content in pages.items():
        await knowledge.write_page(page, content, created_by="user")


def _query(db_path: Path, sql: str) -> list[tuple]:
    """Read rows from the run's db with a plain sync connection (the async one is
    closed before checks run). Missing file/table => no rows."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute(sql))
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def load_scenarios() -> list[dict]:
    return [
        yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(SCENARIOS_DIR.glob("*.yaml"))
    ]


def make_approver(deny_tools: list[str]):
    async def approver(call: ToolCall, _decision: Decision) -> Permission:
        return Permission.DENY if call.name in deny_tools else Permission.ALLOW

    return approver


def evaluate(checks: list[dict], workdir: Path, answer: str, called: list[str]) -> list[str]:
    """Return a list of failure descriptions (empty == all checks passed)."""
    db_path = workdir / "jarvis.db"
    failures: list[str] = []
    for check in checks:
        kind = check["type"]
        if kind == "task_matches":
            rows = _query(db_path, "SELECT kind, status, payload FROM tasks")
            pat = check.get("payload_pattern")
            hits = [
                r
                for r in rows
                if check.get("kind") in (None, r[0])
                and check.get("status") in (None, r[1])
                and (pat is None or re.search(pat, r[2]))
            ]
            if len(hits) < check.get("min_count", 1):
                failures.append(f"expected a task matching {check} (tasks: {rows})")
            continue
        if kind == "task_run_matches":
            rows = _query(db_path, "SELECT status, result_text, denied_count FROM task_runs")
            hits = [
                r
                for r in rows
                if check.get("status") in (None, r[0])
                and (
                    check.get("result_pattern") is None
                    or (r[1] and re.search(check["result_pattern"], r[1]))
                )
                and (r[2] or 0) >= check.get("min_denied", 0)
            ]
            if not hits:
                failures.append(f"expected a task run matching {check} (runs: {rows})")
            continue
        if kind == "kb_source_matches":
            rows = _query(
                db_path, "SELECT kind, status, origin, title, review_status FROM kb_sources"
            )
            pat = check.get("origin_pattern")
            hits = [
                r
                for r in rows
                if check.get("kind") in (None, r[0])
                and check.get("status") in (None, r[1])
                and check.get("review_status") in (None, r[4])
                and (pat is None or re.search(pat, r[2] or ""))
            ]
            if len(hits) < check.get("min_count", 1):
                failures.append(f"expected a kb source matching {check} (sources: {rows})")
            continue
        if kind == "file_exists":
            if not (workdir / check["path"]).exists():
                failures.append(f"expected file {check['path']} to exist")
        elif kind == "file_absent":
            if (workdir / check["path"]).exists():
                failures.append(f"expected file {check['path']} to be absent")
        elif kind == "file_matches":
            path = workdir / check["path"]
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if not re.search(check["pattern"], text):
                failures.append(f"{check['path']} did not match /{check['pattern']}/")
        elif kind == "answer_matches":
            if not re.search(check["pattern"], answer):
                failures.append(f"answer did not match /{check['pattern']}/")
        elif kind == "tool_called":
            if check["name"] not in called:
                failures.append(f"expected tool {check['name']} to be called (called: {called})")
        elif kind == "tool_not_called":
            if check["name"] in called:
                failures.append(f"expected tool {check['name']} NOT to be called")
        else:
            failures.append(f"unknown check type {kind!r}")
    return failures


def _scenario_turns(scenario: dict) -> list[str]:
    """A scenario is a single ``prompt``, a list of ``turns`` (each a fresh session —
    new history — sharing long-term memory, for cross-session tests), or neither (a
    pure background-task scenario driven only by ``check_due``)."""
    if scenario.get("turns"):
        return scenario["turns"]
    return [scenario["prompt"]] if scenario.get("prompt") else []


async def run_once(config, scenario: dict) -> tuple[list[str], float, str]:
    workdir = Path(tempfile.mkdtemp(prefix="jarvis-eval-"))
    for name, content in scenario.get("setup", {}).get("files", {}).items():
        (workdir / name).write_text(content, encoding="utf-8")

    # Isolate the run by making the workdir the workspace *root*: tools and gate
    # both resolve relative paths against it (the unified resolution), so the
    # agent's files land here, not in the repo. The real permissions.yaml is
    # loaded from the project root before the override.
    policy_path = config.root / "config" / "permissions.yaml"
    run_config = config.model_copy(update={"root": workdir})

    utility = AnthropicClient.from_config(run_config)
    memory = None
    if scenario.get("needs_memory"):
        memory = MemoryService(
            store=MemoryStore(await connect(workdir / "memory.db")),
            embedder=VoyageEmbedder.from_config(run_config),
            config=run_config.memory,
            utility_client=utility,
            utility_model=run_config.models.utility,
        )

    # Scheduler (P3) + knowledge (P4) share the one jarvis.db connection + lock, as in
    # prod. Open it once if either feature is exercised.
    tasks: TaskService | None = None
    knowledge: KnowledgeService | None = None
    session_store: SessionStore | None = None
    if scenario.get("needs_scheduler") or scenario.get("needs_knowledge"):
        session_store = SessionStore(await connect(workdir / "jarvis.db"))
    if scenario.get("needs_scheduler"):
        tasks = TaskService(TaskStore(session_store.db, session_store.lock), run_config.scheduler)
        await _seed_tasks(tasks.store, scenario.get("background_tasks", []))
    if scenario.get("needs_knowledge"):
        embedder = memory.embedder if memory else VoyageEmbedder.from_config(run_config)
        knowledge = KnowledgeService(
            KnowledgeStore(session_store.db, session_store.lock),
            embedder,
            run_config.knowledge,
            knowledge_dir=run_config.knowledge_dir,
            root=workdir,
        )
        knowledge.ensure_dirs()
        await _seed_kb_sources(knowledge, scenario.get("setup", {}).get("kb_sources", []))
        await _seed_wiki_pages(knowledge, scenario.get("setup", {}).get("wiki_pages", {}))

    registry = ToolRegistry()
    registry.discover(
        "jarvis.tools.builtin",
        ToolContext(config=run_config, memory=memory, tasks=tasks, knowledge=knowledge),
    )
    executor = ToolExecutor(
        timeout=run_config.limits.tool_timeout_seconds,
        max_result_chars=run_config.limits.max_tool_result_chars,
    )
    gate = PermissionGate(load_policy(policy_path), workdir)
    loop = AgentLoop(
        client=AnthropicClient.from_config(run_config),
        registry=registry,
        executor=executor,
        gate=gate,
        config=run_config,
        approver=make_approver(scenario.get("deny_tools", [])),
        context_manager=ContextManager(summarizer=utility, utility_model=run_config.models.utility),
        memory=memory,
        add_time_context=tasks is not None,
        system=build_system(
            memory_enabled=memory is not None,
            tasks_enabled=tasks is not None,
            knowledge_enabled=knowledge is not None,
        ),
    )

    called: list[str] = []

    def on_event(event: Event) -> None:
        if isinstance(event, ToolStarted):
            called.append(event.name)

    cost = 0.0
    answer = ""
    cwd = Path.cwd()
    os.chdir(workdir)
    try:
        # Each turn is an independent session (fresh history) sharing `memory`.
        for turn in _scenario_turns(scenario):
            result = await loop.run_turn([{"role": "user", "content": turn}], on_event=on_event)
            cost += cost_of(run_config.models.main, result.usage)
            answer = result.text
        # Fire any due background tasks unattended (seeded jobs, or ones just
        # scheduled by the model) — the real BackgroundRunner + JobRunner path.
        if tasks is not None:
            job_runner = JobRunner(
                session_store=session_store,
                client=AnthropicClient.from_config(run_config),
                registry=registry,
                executor=executor,
                gate=gate,
                config=run_config,
                memory=memory,
                knowledge=knowledge,
                make_context_manager=lambda: ContextManager(
                    summarizer=utility, utility_model=run_config.models.utility
                ),
            )
            runner = BackgroundRunner(
                tasks, notify=lambda _l: None, run_job=job_runner.run, turn_lock=asyncio.Lock()
            )
            await runner.check_due()
    finally:
        os.chdir(cwd)

    if session_store is not None:
        # add background-run cost, then close so the sync checks can read the db
        for (run_cost,) in _query(workdir / "jarvis.db", "SELECT cost_usd FROM task_runs"):
            cost += run_cost or 0.0
        await session_store.close()

    failures = evaluate(scenario.get("checks", []), workdir, answer, called)
    return failures, cost, answer


async def run_scenario(config, scenario: dict, runs: int) -> tuple[bool, float]:
    print(f"\n=== {scenario['name']} ===  {scenario.get('description', '')}")
    all_passed = True
    total_cost = 0.0
    for i in range(runs):
        failures, cost, answer = await run_once(config, scenario)
        total_cost += cost
        if failures:
            all_passed = False
            print(f"  run {i + 1}/{runs}: FAIL  (${cost:.4f})")
            for f in failures:
                print(f"      - {f}")
            print(f"      answer: {answer[:160].strip()!r}")
        else:
            print(f"  run {i + 1}/{runs}: PASS  (${cost:.4f})")
    verdict = "PASS" if all_passed else "FAIL"
    print(f"  => {verdict}  (total ${total_cost:.4f})")
    return all_passed, total_cost


async def run_all(config, runs: int, only: str | None) -> int:
    scenarios = load_scenarios()
    if only:
        scenarios = [s for s in scenarios if s["name"] == only]
        if not scenarios:
            print(f"No scenario named {only!r}.")
            return 2

    passed = 0
    total_cost = 0.0
    for scenario in scenarios:
        ok, cost = await run_scenario(config, scenario, runs)
        passed += int(ok)
        total_cost += cost

    print(f"\nSUMMARY: {passed}/{len(scenarios)} scenarios passed · total ${total_cost:.4f}")
    return 0 if passed == len(scenarios) else 1


def main() -> None:
    _force_utf8()
    parser = argparse.ArgumentParser(description="Run Jarvis smoke evals against the live API.")
    parser.add_argument("--runs", type=int, default=3, help="Runs per scenario (default 3).")
    parser.add_argument("--scenario", help="Run only this scenario by name.")
    args = parser.parse_args()

    # Voyage is only required if a scenario exercises memory or the knowledge base.
    required = ["anthropic", "tavily"]
    scenarios = load_scenarios()
    if any(s.get("needs_memory") or s.get("needs_knowledge") for s in scenarios):
        required.append("voyage")

    try:
        config = load_config(require=tuple(required))
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        sys.exit(1)

    sys.exit(asyncio.run(run_all(config, args.runs, args.scenario)))


if __name__ == "__main__":
    main()
