"""Terminal REPL — the MVP interface.

Wires config -> client -> registry -> gate -> loop and drives it turn by turn.
Deliberately thin: it renders events, prompts for approvals, tracks session totals,
and lets Ctrl+C cancel a turn without killing the session. It knows nothing about
the loop's internals.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from jarvis.actions import ConnectorWriteJournal, IntentStore
from jarvis.agents import AgentRunStore, SubAgentService
from jarvis.attention import AttentionStore
from jarvis.cli.jobs import JobRunner
from jarvis.cli.render import ConsoleRenderer
from jarvis.config import Config
from jarvis.connectors.factory import build_connectors
from jarvis.core import AgentLoop, AnthropicClient, Approver
from jarvis.core.client import LLMClient, ToolCall
from jarvis.core.context import ContextManager
from jarvis.core.events import SubAgentCompleted
from jarvis.core.prompts import build_system
from jarvis.digest import DigestBuilder, DigestStore, ensure_digest_task
from jarvis.graph import GraphStore
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory import MemoryService, MemoryStore, VoyageEmbedder, reflect
from jarvis.observability import cost_of, get_logger
from jarvis.observability.budget import BudgetService
from jarvis.observability.cost import Usage, load_pricing
from jarvis.observability.ledger import CostLedger, LedgeredClient, ServiceLedger
from jarvis.orchestration import OrchestrationStore
from jarvis.permissions import (
    NEVER_GRANTABLE,
    NEVER_PERSIST,
    PermissionGate,
    SubAgentGate,
    load_policy,
    persist_always,
)
from jarvis.permissions.gate import Decision
from jarvis.permissions.modes import Mode, ModeState
from jarvis.persistence import SessionStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.scheduler.runner import BackgroundRunner, JobOutcome
from jarvis.scheduler.service import TaskService
from jarvis.scheduler.store import Task, TaskStore
from jarvis.tools import Permission, ToolContext, ToolExecutor, ToolRegistry
from jarvis.voice import (
    PushToTalkListener,
    TerminalScreenApprover,
    VoiceApprover,
    VoiceRenderer,
    VoiceSession,
    build_capture,
    build_stt,
    build_tts,
)


def _expand_and_stat(target: str) -> tuple[str, bool]:
    """Expand ``~`` and stat a path (sync; run via to_thread from the async REPL). Returns
    the expanded path string and whether it is a directory."""
    path = Path(target).expanduser()
    return str(path), path.is_dir()


def _call_summary(call: ToolCall) -> str:
    """A one-line preview of what a tool call will do — shown before approval so
    the human consents to the actual action, not just the tool name."""
    inp = call.input or {}
    if call.name == "run_shell":
        return f"$ {str(inp.get('command', '')).strip()[:200]}"
    if call.name == "write_file":
        return f"write -> {inp.get('path', '?')} ({len(str(inp.get('content', '')))} chars)"
    if call.name == "web_fetch":
        return f"fetch -> {inp.get('url', '?')}"
    if call.name == "web_search":
        return f"search -> {str(inp.get('query', '')).strip()[:200]!r}"
    if call.name == "remember":
        # full content, NOT truncated — the human must see exactly what gets stored
        return f"remember [{inp.get('type', 'fact')}]: {inp.get('content', '')}"
    if call.name == "forget":
        return f"forget memory #{inp.get('memory_id', '?')}"
    if call.name == "schedule_task":
        # full payload, NOT truncated + computed fire time — the human consents to
        # the actual future action, and can catch a wrong-timezone datetime.
        return (
            f'schedule {inp.get("kind", "?")} "{inp.get("title", "")}" '
            f"[{_schedule_preview(inp)}]:\n    {inp.get('payload', '')}"
        )
    if call.name == "cancel_task":
        return f"cancel task #{inp.get('task_id', '?')}"
    if call.name == "spawn_agent":
        # full payload, NOT truncated + the tool scope — the human consents to the
        # actual delegated task and the authority the child will run with.
        tools = inp.get("tools") or []
        return (
            f'spawn sub-agent "{inp.get("title", "")}" [tools: {tools}]:\n    '
            f"{inp.get('prompt', '')}"
        )
    if call.name == "ingest_source":
        # the gate's reason line shows the resolved path; here name the target + kind
        if inp.get("path"):
            return f"ingest file -> {inp['path']}"
        if inp.get("url"):
            return f"ingest url -> {inp['url']}"
        return f"ingest note ({len(str(inp.get('text', '')))} chars)"
    if call.name == "write_wiki_page":
        cites = inp.get("source_ids") or []
        preview = str(inp.get("content", "")).strip().splitlines()[:10]
        return f"wiki page -> {inp.get('page', '?')} (cites {cites}):\n    " + "\n    ".join(
            preview
        )
    return ""


def _schedule_preview(inp: dict) -> str:
    """Human summary of a schedule_task's timing, with the computed first fire in
    local time — so a wrong-timezone datetime is visible at the approval prompt.
    Defensive: never raises (it's only a preview)."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from tzlocal import get_localzone_name

    from jarvis.scheduler.triggers import compute_next, validate

    if inp.get("once_at") is not None:
        kind, spec = "once", str(inp["once_at"])
    elif inp.get("cron") is not None:
        kind, spec = "cron", str(inp["cron"])
    elif inp.get("every_seconds") is not None:
        kind, spec = "interval", str(inp["every_seconds"])
    else:
        return "no schedule given"
    try:
        tz = get_localzone_name()
        if validate(kind, spec, tz) is not None:
            return f"{kind} {spec} — invalid"
        fire = compute_next(kind, spec, tz, after=datetime.now(UTC))
        if fire is None:
            return f"{kind} {spec} — in the past"
        local = fire.astimezone(ZoneInfo(tz))
        return f"{kind} {spec}; first fire {local:%Y-%m-%d %H:%M %Z}"
    except Exception:
        return f"{kind} {spec}"


def _summary_is_long(summary: str) -> bool:
    """A call summary big enough to page rather than dump inline (e.g. a large
    spawn_agent prompt): ~18+ lines or ~1500+ chars."""
    return summary.count("\n") >= 18 or len(summary) >= 1500


def _short_summary(summary: str, *, lines: int = 12) -> str:
    """The first ``lines`` lines of a long summary plus a hint to view the full text."""
    shown = summary.splitlines()[:lines]
    remaining = summary.count("\n") + 1 - len(shown)
    tail = f"\n    … {remaining} more line(s) — type 'v' to view the full text" if remaining else ""
    return "\n".join(shown) + tail


def _utility_client(config: Config, *, ledger: CostLedger | None = None) -> LLMClient:
    """The utility model with thinking OFF — used for dedup adjudication, compaction
    summaries, reflection, and the digest (forced/tool-less calls require thinking off).

    Wrapped in a :class:`LedgeredClient` when a cost ledger is supplied (Phase 10), so every
    compaction/reflection/dedup/digest completion is recorded (the call sites set the purpose
    via cost_scope)."""
    client: LLMClient = AnthropicClient(
        api_key=config.secrets.anthropic_api_key,
        effort=config.limits.effort,
        max_retries=config.limits.max_retries,
        thinking=False,
        context_reuse=config.context_reuse.enabled,
    )
    if ledger is not None:
        client = LedgeredClient(
            client, ledger=ledger, provider="anthropic", effort=config.limits.effort
        )
    return client


def _build_cost_ledger(config: Config, db, lock) -> CostLedger:
    """The Phase 10 cost ledger over the shared connection + lock, with versioned pricing."""
    return CostLedger(db, lock, load_pricing(config.root / "config" / "pricing.yaml"))


def _build_memory(
    config: Config, db, console: Console, utility: AnthropicClient, lock
) -> MemoryService | None:
    """Construct the memory service, or return None (disabled / no key) with a note.

    Shares ``db`` *and the write lock* with the SessionStore — a second connection
    to one SQLite file would deadlock on the first concurrent write, and a second
    lock would let a memory write land inside a session-save transaction."""
    if not config.memory.enabled:
        return None
    if not config.secrets.voyage_api_key:
        console.print("[dim]Long-term memory off: set VOYAGE_API_KEY in .env to enable it.[/]")
        get_logger("jarvis.memory").warning("memory_disabled", reason="no_voyage_key")
        return None
    return MemoryService(
        store=MemoryStore(db, lock),
        embedder=VoyageEmbedder.from_config(config),
        config=config.memory,
        utility_client=utility,
        utility_model=config.models.utility,
    )


def _build_context_manager(config: Config, utility: AnthropicClient) -> ContextManager:
    """Compaction is always on live (independent of memory); summaries use the
    utility model."""
    return ContextManager(
        context_token_budget=config.limits.context_token_budget,
        compaction_threshold=config.limits.compaction_threshold,
        summarizer=utility,
        utility_model=config.models.utility,
    )


async def _reflect_session(
    store: SessionStore,
    memory: MemoryService,
    utility: AnthropicClient,
    model: str,
    session_id: int,
    console: Console,
    *,
    announce: bool,
) -> None:
    """Reflect one session into long-term memory, then mark it reflected — always,
    even on skip/failure, so it never blocks exit and never retries forever."""
    try:
        transcript = await store.load_messages(session_id)
        if len(transcript) < 2:  # nothing substantive to reflect on
            return
        if announce:
            console.print("[dim]reflecting…[/]")
        # Attribute the extracted memories to the session's project (Phase 10) — a project
        # session's memories are scoped to it, never leaked into global recall.
        meta = await store.get_meta(session_id)
        results = await reflect(
            transcript=transcript,
            session_id=session_id,
            service=memory,
            client=utility,
            model=model,
            project_id=meta.project_id if meta is not None else None,
        )
        if announce:
            saved = sum(1 for r in results if r.action in ("inserted", "superseded"))
            console.print(f"[dim]reflected: {saved} memories saved.[/]")
    except (KeyboardInterrupt, asyncio.CancelledError):
        if announce:
            console.print("[dim]reflection skipped.[/]")
    except Exception as exc:  # noqa: BLE001 - reflection must never break exit
        get_logger("jarvis.repl").warning("reflection_failed", error=str(exc))
    finally:
        with contextlib.suppress(Exception):
            await store.mark_reflected(session_id)


class Repl:
    def __init__(
        self,
        config: Config,
        *,
        client: LLMClient | None = None,
        console: Console | None = None,
        store: SessionStore | None = None,
        session_id: int | None = None,
        memory: MemoryService | None = None,
        context_manager: ContextManager | None = None,
        tasks: TaskService | None = None,
        knowledge: KnowledgeService | None = None,
        run_store: AgentRunStore | None = None,
        make_context_manager: Callable[[], ContextManager] | None = None,
        runner: BackgroundRunner | None = None,
        turn_lock: asyncio.Lock | None = None,
        cost_ledger: CostLedger | None = None,
        artifacts: object | None = None,
    ) -> None:
        self.config = config
        self.console = console or Console()
        self.log = get_logger("jarvis.repl")
        self.store = store
        self.session_id = session_id
        self.memory = memory
        self.context_manager = context_manager
        self.tasks = tasks
        self.knowledge = knowledge
        self.runner = runner
        # One lock serializes every model turn — interactive AND background — and
        # everything that writes to the terminal (see BackgroundRunner). Shared with
        # the runner when there is one; a private lock keeps a bare Repl usable.
        self.turn_lock = turn_lock or asyncio.Lock()

        self.executor = ToolExecutor(
            timeout=config.limits.tool_timeout_seconds,
            max_result_chars=config.limits.max_tool_result_chars,
        )
        policy_path = config.root / "config" / "permissions.yaml"
        self.gate = PermissionGate(load_policy(policy_path), config.root, source_path=policy_path)
        self.client = client or AnthropicClient.from_config(config)
        # Phase 10 cost ledger: wrap the main client so every interactive turn AND sub-agent
        # child (they share this client) is recorded. None in bare/test Repls (unwrapped).
        self.cost_ledger = cost_ledger
        if cost_ledger is not None:
            self.client = LedgeredClient(
                self.client, ledger=cost_ledger, provider="anthropic", effort=config.limits.effort
            )

        # Sub-agent delegation (Phase 6). Built before tool discovery so spawn_agent
        # registers, then bound to the discovered registry. Needs a session store (to
        # persist child transcripts) and a run store (audit) — absent in bare test Repls.
        self.agents: SubAgentService | None = None
        if run_store is not None and store is not None and config.sub_agents.enabled:
            self.agents = SubAgentService(
                session_store=store,
                run_store=run_store,
                client=self.client,
                executor=self.executor,
                gate=self.gate,
                config=config,
                make_context_manager=make_context_manager,
                make_approver=self._make_subagent_approver,
            )

        # External connectors (Phase 9): Google client + notifiers, or None. Built before
        # discovery so the connector tools register (and gate on) the specific pieces present.
        self.connectors = build_connectors(config)

        # Project workspaces (Phase 10): the active-project scope, injected into the loop as
        # a callable so switching applies next turn. Needs a store (bare test Repls have none
        # ⇒ no projects ⇒ global scope, byte-identical to Phase 9).
        self.projects: ProjectService | None = None
        if store is not None:
            self.projects = ProjectService(ProjectStore(store.db, store.lock))

        # Run modes (Phase 10): Plan/Approval/Auto, surface state shared into the UI loop.
        # Voice stays pinned to Approval (its loop gets mode=None). Background runs never see
        # a mode (they keep the UnattendedGate), so Auto can't leak into unattended jobs.
        self.modes = ModeState(Mode(config.modes.default))

        # Budgets (Phase 10): cost rollups + limit checks over the ledger. Needs the ledger's
        # table (same db); None in bare/test Repls without a store.
        self.budgets: BudgetService | None = None
        # Phase 10B: the service-call ledger the local adapters write to (metadata only).
        self.service_ledger: ServiceLedger | None = None
        # Phase 12: the outward-write intent store the connector WRITE tools propose into, and
        # the metadata-only journal the execute route writes on each executed write.
        self.intents: IntentStore | None = None
        self.write_journal: ConnectorWriteJournal | None = None
        # Phase 13: the artifact store a producing tool (e.g. generate_image) registers into.
        # Threaded in from the UI build (where the Library renders it); None in bare/terminal Repls.
        self.artifacts = artifacts
        if store is not None:
            self.budgets = BudgetService(store.db, store.lock, config.budgets)
            self.service_ledger = ServiceLedger(store.db, store.lock)
            self.intents = IntentStore(store.db, store.lock)
            self.write_journal = ConnectorWriteJournal(store.db, store.lock)

        self.registry = ToolRegistry()
        tool_ctx = ToolContext(
            config=config,
            memory=memory,
            tasks=tasks,
            knowledge=knowledge,
            agents=self.agents,
            connectors=self.connectors,
            project=self.projects.current if self.projects is not None else None,
            service_ledger=self.service_ledger,
            intents=self.intents,
            artifacts=self.artifacts,
        )
        self.registry.discover("jarvis.tools.builtin", tool_ctx)
        # Phase 10B: register the local service adapters (semgrep/gitleaks/playwright_inspect).
        # Each is_available()-gates on the ServiceRegistry (flag ∧ creds ∧ pricing), so a
        # disabled service's tool simply never registers — no adapter is live unless enabled.
        self.registry.discover("jarvis.services", tool_ctx)
        if self.agents is not None:
            self.agents.bind(registry=self.registry)

        self.loop = AgentLoop(
            client=self.client,
            registry=self.registry,
            executor=self.executor,
            gate=self.gate,
            config=config,
            approver=self._approve,
            system=build_system(
                memory_enabled=memory is not None,
                tasks_enabled=tasks is not None,
                knowledge_enabled=knowledge is not None,
                delegation_enabled=self.agents is not None,
                connectors_enabled=self.connectors is not None,
            ),
            context_manager=context_manager,
            memory=memory,
            project=self.projects.current if self.projects is not None else None,
            mode=self.modes.current,
            # With scheduling on, the model needs the current date to resolve
            # relative times ("tomorrow 9am") — it has no clock otherwise.
            add_time_context=tasks is not None,
        )

        self.messages: list[dict] = []
        self.usage = Usage()
        self.child_cost = 0.0  # cumulative sub-agent spend this session (via SubAgentCompleted)
        self._approval_lock = asyncio.Lock()  # serializes parallel children's human prompts
        self.renderer = ConsoleRenderer(self.console)
        if self.agents is not None:
            # Child events flow through here: rendered, and child cost accumulated.
            self.agents.emit = self._agent_event
            self.agents.bound_session_id = session_id

    # --- approval ----------------------------------------------------------

    async def _approve(self, call: ToolCall, decision: Decision) -> Permission:
        self.console.print(f"\n[yellow]Approve[/] [bold]{call.name}[/]?  [dim]{decision.reason}[/]")
        summary = _call_summary(call)
        long = bool(summary) and _summary_is_long(summary)
        if summary:
            # A long payload (e.g. a big spawn_agent prompt) is shown truncated with a
            # 'v' option to page the full text; the full payload is always available
            # before consent, never approved sight-unseen.
            self.console.print(f"  [dim]{_short_summary(summary) if long else summary}[/]")
        # A non-persistable decision (egress after a private read this turn, Phase 9) never
        # offers "always" — a wider standing grant must not be minted from a tainted turn.
        opts = ["[y]es", "[N]o"]
        if decision.persistable:
            opts.append("[a]lways")
        if long:
            opts.append("[v]iew full")
        prompt = "  " + " / ".join(opts) + ": "
        while True:
            answer = (await asyncio.to_thread(input, prompt)).strip().lower()
            if long and answer in ("v", "view"):
                self.console.print(summary, markup=False)  # full, untruncated payload
                continue
            break
        if decision.persistable and answer in ("a", "always"):
            self._persist_always(call)
            return Permission.ALLOW
        if answer in ("y", "yes"):
            return Permission.ALLOW
        return Permission.DENY

    # The narrow-persist rule now lives in permissions/approvals.py so the REPL and the
    # workstation UI share one implementation (ADR-0008 §3). Kept as an alias + a thin
    # delegate for back-compat with existing tests and readers.
    _NEVER_PERSIST = NEVER_PERSIST

    def _persist_always(self, call: ToolCall) -> None:
        """Persist an 'always allow' choice as narrowly as the tool allows (delegates to
        the shared :func:`persist_always`)."""
        persist_always(self.gate, self.config, call, log=self.log)

    # --- sub-agent approval (Phase 6) --------------------------------------

    def _make_subagent_approver(self, gate: SubAgentGate, agent_id: str, title: str) -> Approver:
        """Build the approver for one child run. A child's ASK is forwarded to the human
        (labeled as the sub-agent's) — the interactive safety story holds for delegated
        actions too. 'a-for-this-run' records a *pattern* grant on the child's gate (a
        host / a directory, never a blanket tool-level allow for run_shell/write_file),
        never persisted. Parallel children serialize on the approval lock, since two
        concurrent input() prompts would interleave."""

        async def approve(call: ToolCall, decision: Decision) -> Permission:
            async with self._approval_lock:
                return await self._prompt_subagent(call, decision, gate, title)

        return approve

    async def _prompt_subagent(
        self, call: ToolCall, decision: Decision, gate: SubAgentGate, title: str
    ) -> Permission:
        self.console.print(
            f'\n[magenta]sub-agent "{title}"[/] asks: [bold]{call.name}[/]?  '
            f"[dim]{decision.reason}[/]"
        )
        summary = _call_summary(call)
        if summary:
            self.console.print(f"  [dim]{summary}[/]")
        # run_shell / write_file are never grantable — each is approved individually.
        grantable = call.name not in NEVER_GRANTABLE
        prompt = "  [y]es / [N]o / [a]-for-this-run: " if grantable else "  [y]es / [N]o: "
        answer = (await asyncio.to_thread(input, prompt)).strip().lower()
        if grantable and answer in ("a", "always"):
            grant = gate.grant(call.name, call.input)
            if grant is not None:
                self.console.print(f"  [dim]granted {grant.describe()}[/]")
            return Permission.ALLOW
        if answer in ("y", "yes"):
            return Permission.ALLOW
        return Permission.DENY

    def _agent_event(self, event: object) -> None:
        """Sink for a child's forwarded events: render them (child activity lines, no
        child text streaming) and accumulate child cost so the session status line
        reflects delegated spend."""
        self.renderer(event)  # type: ignore[arg-type]
        if isinstance(event, SubAgentCompleted) and event.cost_usd:
            self.child_cost += event.cost_usd

    # --- loop --------------------------------------------------------------

    async def run(self) -> None:
        # PromptSession is created here (not in __init__) because it opens the
        # terminal — deferring it keeps Repl constructible in tests without a TTY.
        session: PromptSession = PromptSession(history=InMemoryHistory())
        self.console.print(
            "[bold cyan]Jarvis[/] — ask me anything. Type [bold]exit[/] or press Ctrl-D to quit.\n"
        )
        # patch_stdout routes a background notification printed while the prompt is
        # idle to *above* the prompt, so it can't corrupt the line being typed.
        with patch_stdout(raw=True):
            while True:
                try:
                    user_input = await session.prompt_async("you › ")
                except (EOFError, KeyboardInterrupt):
                    self.console.print("\nBye.")
                    return
                user_input = user_input.strip()
                if not user_input:
                    continue
                if user_input.lower() in ("exit", "quit"):
                    self.console.print("Bye.")
                    return
                if user_input.lower() == "memories":
                    await self._show_memories()
                    continue
                if user_input.lower() == "tasks" or user_input.lower().startswith("tasks "):
                    await self._show_tasks(user_input[len("tasks") :].strip())
                    continue
                if user_input.lower() == "kb" or user_input.lower().startswith("kb "):
                    await self._kb_command(user_input[len("kb") :].strip())
                    continue
                if user_input.lower() == "agents" or user_input.lower().startswith("agents "):
                    await self._show_agents(user_input[len("agents") :].strip())
                    continue
                if user_input.lower() == "project" or user_input.lower().startswith("project "):
                    await self._project_command(user_input[len("project") :].strip())
                    continue
                self.messages.append({"role": "user", "content": user_input})
                await self.run_turn()
                # A just-scheduled task should fire promptly, not wait out the cap.
                if self.runner is not None:
                    self.runner.kick()

    async def _project_command(self, arg: str) -> None:
        """`project` shows the active project; `project list` lists projects; `project use
        <slug|id>` activates one (starting a fresh conversation, so the session stays bound
        to one project); `project none` returns to global scope; `project new <name>` creates
        one. Switching resets the conversation — a session belongs to one project for life."""
        if self.projects is None:
            self.console.print("[dim]Projects are not enabled.[/]\n")
            return
        store = self.projects.store
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("", "current", "status"):
            cur = self.projects.current()
            where = f"[bold]{cur.name}[/]" if cur.project_id is not None else "[dim]global[/]"
            self.console.print(f"Active project: {where}\n")
            return
        if sub == "list":
            projects = await store.list(status="active")
            if not projects:
                self.console.print("[dim]No projects yet. Create one: project new <name>[/]\n")
                return
            for p in projects:
                marker = "→" if p.id == self.projects.current().project_id else " "
                self.console.print(f"{marker} [bold]{p.slug}[/] #{p.id} — {p.name}")
            self.console.print()
            return
        if sub == "new":
            if not rest:
                self.console.print("[dim]Usage: project new <name>[/]\n")
                return
            pid = await store.create(name=rest)
            await self._switch_project(pid)
            self.console.print(f"Created and switched to [bold]{rest}[/].\n")
            return
        if sub == "none":
            await self._switch_project(None)
            self.console.print("Switched to [dim]global[/] scope.\n")
            return
        if sub == "use":
            target = rest or ""
            project = None
            if target.isdigit():
                project = await store.get(int(target))
            if project is None:
                project = await store.get_by_slug(target)
            if project is None:
                self.console.print(f"[red]No such project:[/] {target}\n")
                return
            await self._switch_project(project.id)
            self.console.print(f"Switched to [bold]{project.name}[/].\n")
            return
        if sub in ("export", "import"):
            await self._project_export_import(sub, rest)
            return
        self.console.print(
            "[dim]Usage: project [list | use <slug|id> | new <name> | none | "
            "export | import <dir>][/]\n"
        )

    async def _project_export_import(self, sub: str, rest: str) -> None:
        """`project export` writes the active project's memories to Markdown under
        data/exports/<slug>/memories/; `project import <dir>` imports a directory into the
        active project (forcing scope + source='import' — a human ritual, never a tool)."""
        from jarvis.projects.export import export_project_memories, import_project_memories

        if self.memory is None:
            self.console.print("[dim]Memory is not enabled.[/]\n")
            return
        cur = self.projects.current()
        if cur.project_id is None:
            self.console.print("[dim]Select a project first (project use <slug>).[/]\n")
            return
        project = await self.projects.store.get(cur.project_id)
        base = self.config.data_dir / "exports" / project.slug / "memories"
        if sub == "export":
            n = await export_project_memories(self.memory.store, cur.project_id, base)
            self.console.print(f"Exported {n} memories to [bold]{base}[/].\n")
            return
        src = Path(rest.strip()) if rest.strip() else base
        report = await import_project_memories(self.memory, cur.project_id, src)
        self.console.print(
            f"Imported into [bold]{project.name}[/]: {report.created} new, "
            f"{report.duplicate} duplicate, {len(report.skipped)} skipped.\n"
        )

    async def _switch_project(self, project_id: int | None) -> None:
        """Activate a project and start a fresh conversation — a session is bound to one
        project for its life, so a switch never re-tags the current transcript."""
        await self.projects.activate(project_id)
        if self.messages:
            await self._persist()  # save the outgoing conversation before clearing
        self.messages = []
        if self.store is not None:
            self.session_id = await self.store.create_session(project_id=project_id)
            if self.agents is not None:
                self.agents.bound_session_id = self.session_id

    async def _show_tasks(self, arg: str) -> None:
        """`tasks` lists active tasks; `tasks all` includes finished; `tasks <id>`
        shows one task's run history — so a surprising task is always traceable."""
        if self.tasks is None:
            self.console.print("[dim]Scheduling is not enabled.[/]\n")
            return
        if arg.isdigit():
            await self._show_task_runs(int(arg))
            return
        include_finished = arg.lower() == "all"
        items = await self.tasks.store.list(include_finished=include_finished)
        if not items:
            self.console.print("[dim]No tasks.[/]\n")
            return
        for t in items:
            line = self.tasks.describe(t)
            self.console.print(f"[bold]#{t.id}[/] {line} [dim]· by {t.created_by}[/]")
            if t.last_error:
                self.console.print(f"    [red]last error:[/] {t.last_error}")
        self.console.print()

    async def _show_task_runs(self, task_id: int) -> None:
        task = await self.tasks.store.get(task_id)
        if task is None:
            self.console.print(f"[dim]No task #{task_id}.[/]\n")
            return
        desc = self.tasks.describe(task)
        self.console.print(f"[bold]#{task.id}[/] {desc} [dim]· {task.status}[/]")
        runs = await self.tasks.store.runs_for(task_id)
        if not runs:
            self.console.print("    [dim]no runs yet[/]\n")
            return
        for r in runs:
            cost = f" · ${r.cost_usd:.4f}" if r.cost_usd is not None else ""
            denied = f" · {r.denied_count} denied" if r.denied_count else ""
            sess = f" · session {r.session_id}" if r.session_id is not None else ""
            self.console.print(f"    [dim]{r.scheduled_for}[/] {r.status}{cost}{denied}{sess}")
            if r.result_text:
                preview = r.result_text.strip().splitlines()[0][:200]
                self.console.print(f"      {preview}")
            if r.error:
                self.console.print(f"      [red]{r.error}[/]")
        self.console.print()

    async def _kb_command(self, arg: str) -> None:
        """`kb` (stats) / `kb lint` / `kb rebuild` / `kb review` — knowledge-base
        maintenance. rebuild and review are humans-only (never model tools)."""
        if self.knowledge is None:
            self.console.print("[dim]Knowledge base is not enabled.[/]\n")
            return
        sub = arg.lower()
        if sub == "":
            s = await self.knowledge.stats()
            unrev = f" ([yellow]{s['unreviewed']} unreviewed[/])" if s["unreviewed"] else ""
            self.console.print(
                f"[bold]Knowledge base[/] · {s['sources']} sources{unrev} · {s['chunks']} chunks"
            )
            self.console.print(f"[dim]{self.knowledge.knowledge_dir}[/]\n")
        elif sub == "lint":
            self.console.print((await self.knowledge.lint()).render() + "\n")
        elif sub == "rebuild":
            answer = (await asyncio.to_thread(input, "Rebuild the whole index? [y/N]: ")).strip()
            if answer.lower() in ("y", "yes"):
                counts = await self.knowledge.rebuild_index()
                self.console.print(
                    f"[dim]rebuilt: {counts['sources']} sources, {counts['pages']} pages.[/]\n"
                )
            else:
                self.console.print("[dim]cancelled.[/]\n")
        elif sub == "review":
            await self._kb_review()
        elif sub.startswith("ingest"):
            await self._kb_ingest(arg[len("ingest") :].strip())  # keep original case (paths)
        else:
            self.console.print(
                f"[dim]unknown kb command: {arg!r} (try: ingest / lint / rebuild / review)[/]\n"
            )

    async def _kb_ingest(self, spec: str) -> None:
        """`kb ingest <path|url> [--no-recursive]` — bulk-ingest a folder (Obsidian vault,
        Downloads, docs), a single file, or a URL. Human-initiated ⇒ lands reviewed."""
        recursive = True
        tokens: list[str] = []
        for part in spec.split():
            if part in ("--recursive", "-r"):
                recursive = True
            elif part == "--no-recursive":
                recursive = False
            else:
                tokens.append(part)
        target = " ".join(tokens).strip()
        if not target:
            self.console.print("[dim]usage: kb ingest <path|url> [--no-recursive][/]\n")
            return
        if target.startswith(("http://", "https://")):
            await self._kb_ingest_one(url=target)
            return
        # Resolve + stat off the event loop (filesystem I/O; keeps ASYNC-correctness).
        path_str, is_dir = await asyncio.to_thread(_expand_and_stat, target)
        if is_dir:
            report = await self.knowledge.ingest_folder(path_str, recursive=recursive)
            self.console.print(
                f"[bold]Ingested[/] {len(report.ingested)} · {len(report.duplicates)} dup · "
                f"{len(report.skipped)} skipped · {len(report.failed)} failed"
            )
            for pth, reason in report.skipped[:10]:
                self.console.print(f"[dim]  skip {pth}: {reason}[/]", markup=False)
            for pth, err in report.failed[:10]:
                self.console.print(f"[dim]  fail {pth}: {err}[/]", markup=False)
            self.console.print()
        else:
            await self._kb_ingest_one(path=path_str)

    async def _kb_ingest_one(self, **kw: str) -> None:
        try:
            result = await self.knowledge.ingest(created_by="user", **kw)
        except Exception as exc:
            self.console.print(f"[red]ingest failed:[/] {exc}\n", markup=False)
            return
        self.console.print(
            f"[dim]{result.action}: source #{result.source_id} ({result.review_status})[/]\n"
        )

    async def _kb_review(self) -> None:
        """Walk the quarantine queue: unattended-ingested sources a human must approve
        before they're searchable (ADR-0004)."""
        pending = await self.knowledge.unreviewed_sources()
        if not pending:
            self.console.print("[dim]No sources awaiting review.[/]\n")
            return
        for source in pending:
            self.console.print(
                f"[bold]#{source.id}[/] [cyan]{source.kind}[/] {source.origin} "
                f"[dim]· by {source.created_by} · {source.created_at[:10]}[/]"
            )
            if source.title:
                self.console.print(f"    {source.title}")
            answer = (
                (await asyncio.to_thread(input, "  [a]pprove / [r]eject / [s]kip: "))
                .strip()
                .lower()
            )
            if answer in ("a", "approve"):
                await self.knowledge.approve_source(source.id)
                self.console.print("  [green]approved[/]")
            elif answer in ("r", "reject"):
                await self.knowledge.reject_source(source.id)
                self.console.print("  [red]rejected[/]")
            else:
                self.console.print("  [dim]skipped[/]")
        self.console.print()

    async def _show_agents(self, arg: str) -> None:
        """`agents` lists recent sub-agent runs; `agents <id>` shows one run's detail
        (verbatim prompt, scope, both trace ids) — a surprising sub-agent is traceable."""
        if self.agents is None:
            self.console.print("[dim]Delegation is not enabled.[/]\n")
            return
        store = self.agents.run_store
        if arg.isdigit():
            await self._show_agent_detail(store, int(arg))
            return
        runs = await store.list(limit=20)
        if not runs:
            self.console.print("[dim]No sub-agent runs yet.[/]\n")
            return
        for r in runs:
            cost = f" · ${r.cost_usd:.4f}" if r.cost_usd is not None else ""
            denied = f" · {r.denied_count} denied" if r.denied_count else ""
            self.console.print(
                f"[bold]#{r.id}[/] {r.title} "
                f"[dim]· {r.status}{cost}{denied} · {len(r.tools_scope)} tool(s)[/]"
            )
        self.console.print()

    async def _show_agent_detail(self, store: AgentRunStore, run_id: int) -> None:
        run = await store.get(run_id)
        if run is None:
            self.console.print(f"[dim]No sub-agent run #{run_id}.[/]\n")
            return
        cost = f" · ${run.cost_usd:.4f}" if run.cost_usd is not None else ""
        self.console.print(f"[bold]#{run.id}[/] {run.title} [dim]· {run.status}{cost}[/]")
        self.console.print(f"    [dim]scope:[/] {run.tools_scope}")
        self.console.print(
            f"    [dim]iterations {run.iterations} · {run.denied_count} denied · "
            f"child session {run.child_session_id}[/]"
        )
        self.console.print(
            f"    [dim]trace: parent {run.parent_trace_id} → child {run.child_trace_id}[/]"
        )
        self.console.print(f"    prompt: {run.prompt}", markup=False)
        if run.result_text:
            head = run.result_text.strip().splitlines()[0][:200]
            self.console.print(f"    [dim]result:[/] {head}")
        if run.error:
            self.console.print(f"    [red]{run.error}[/]")
        self.console.print()

    async def _show_memories(self) -> None:
        """`memories` command: list what Jarvis knows, with provenance (why it
        believes each) — so a surprising memory is always traceable."""
        if self.memory is None:
            self.console.print("[dim]Long-term memory is not enabled.[/]\n")
            return
        mems = await self.memory.store.all_live()
        if not mems:
            self.console.print("[dim]No memories yet.[/]\n")
            return
        for m in mems:
            c = m.provenance.confidence
            conf = "" if c is None else f" · conf {c:.2f}"
            header = f"[bold]#{m.id}[/] [cyan]{m.type}[/] [dim]{m.source}{conf}[/]"
            self.console.print(f"{header} {m.content}")
            if m.provenance.evidence_summary:
                self.console.print(f"    [dim]why: {m.provenance.evidence_summary}[/]")
        self.console.print()

    async def run_turn(self) -> None:
        # A background job may hold the turn lock; tell the user their message is
        # queued rather than leaving them staring at a frozen prompt.
        if self.turn_lock.locked() and self.runner is not None and self.runner.in_flight:
            self.console.print(
                f'[dim]background task "{self.runner.in_flight}" running — '
                "your message is queued…[/]"
            )
        async with self.turn_lock:
            self.renderer.reset()
            self.console.print("[bold green]jarvis ›[/] ", end="")
            task = asyncio.create_task(self.loop.run_turn(self.messages, on_event=self.renderer))
            try:
                result = await task
            except KeyboardInterrupt:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
                    await task
                self.console.print("\n[yellow]Turn cancelled.[/]\n")
                return
            self.messages = result.messages
            self.usage = self.usage + result.usage
            await self._persist()
            self._print_status()

    async def _persist(self) -> None:
        if self.store is None or self.session_id is None:
            return
        try:
            await self.store.save_messages(self.session_id, self.messages)
            if self.context_manager is not None:
                summary, cut = self.context_manager.state()
                await self.store.save_compaction(self.session_id, summary, cut)
        except Exception as exc:  # noqa: BLE001 - a save failure must not kill the session
            self.log.warning("persist_failed", error=str(exc))

    def _print_status(self) -> None:
        cost = cost_of(self.config.models.main, self.usage)
        tokens = self.usage.input_tokens + self.usage.output_tokens
        child = f" · sub-agents ${self.child_cost:.4f}" if self.child_cost else ""
        self.console.print(f"[dim]session: {tokens:,} tokens · ${cost:.4f}{child}[/]\n")


def _build_scheduler(
    config: Config, db, store: SessionStore, session_id: int
) -> TaskService | None:
    """Construct the TaskService on the shared connection + lock, or None if the
    scheduler is disabled. ``bound_session_id`` makes tool-created tasks carry the
    interactive session as provenance."""
    if not config.scheduler.enabled:
        return None
    service = TaskService(TaskStore(db, store.lock), config.scheduler)
    service.bound_session_id = session_id
    return service


def _build_knowledge(
    config: Config, db, lock, console: Console, memory: MemoryService | None, *, artifacts=None
) -> KnowledgeService | None:
    """Construct the KnowledgeService on the shared connection + lock, or None if the
    KB is disabled / has no embedder. Reuses the memory service's embedder when present
    (one embedding space, one client), else builds a Voyage embedder, else degrades."""
    if not config.knowledge.enabled:
        return None
    embedder = memory.embedder if memory is not None else None
    if embedder is None:
        if not config.secrets.voyage_api_key:
            console.print("[dim]Knowledge base off: set VOYAGE_API_KEY in .env to enable it.[/]")
            get_logger("jarvis.knowledge").warning("knowledge_disabled", reason="no_voyage_key")
            return None
        embedder = VoyageEmbedder.from_config(config)
    service = KnowledgeService(
        KnowledgeStore(db, lock),
        embedder,
        config.knowledge,
        knowledge_dir=config.knowledge_dir,
        root=config.root,
        artifacts=artifacts,  # Phase 11: index written wiki pages as artifacts
    )
    service.ensure_dirs()
    return service


async def run_repl(config: Config, *, resume: bool = False, console: Console | None = None) -> None:
    """Open the database, resume or start a session, wire memory + scheduling, run the REPL."""
    console = console or Console()
    # One shared connection + write lock: SessionStore, MemoryStore and TaskStore all
    # use them (a second connection would deadlock; a second lock would let writes
    # interleave inside each other's transactions).
    db = await connect(config.data_dir / "jarvis.db")
    store = SessionStore(db)
    memory: MemoryService | None = None
    utility: AnthropicClient | None = None
    session_id: int | None = None
    reflect_on = False
    runner: BackgroundRunner | None = None
    try:
        ledger = _build_cost_ledger(config, db, store.lock)
        utility = _utility_client(config, ledger=ledger)
        memory = _build_memory(config, db, console, utility, store.lock)
        context_manager = _build_context_manager(config, utility)
        reflect_on = memory is not None and config.memory.reflection

        session_id = await store.latest_session_id() if resume else None
        history = await store.load_messages(session_id) if session_id else []
        if session_id is None:
            session_id = await store.create_session()
        elif resume:
            # restore the frozen compaction summary + cut so we don't re-summarize
            summary, cut = await store.load_compaction(session_id)
            context_manager.restore(summary, cut)

        # Catch-up: reflect on any past session that never got reflected (e.g. a
        # crash/kill skipped its on-exit reflection). Quiet; never blocks startup.
        if reflect_on:
            for stale_id in await store.unreflected_session_ids(exclude=session_id):
                await _reflect_session(
                    store, memory, utility, config.models.utility, stale_id, console, announce=False
                )

        tasks = _build_scheduler(config, db, store, session_id)
        knowledge = _build_knowledge(config, db, store.lock, console, memory)
        # Sub-agent delegation (Phase 6): shares the connection + write lock. Children
        # get a fresh context manager each (long research still compacts).
        run_store = AgentRunStore(db, store.lock) if config.sub_agents.enabled else None
        repl = Repl(
            config,
            console=console,
            store=store,
            session_id=session_id,
            memory=memory,
            context_manager=context_manager,
            tasks=tasks,
            knowledge=knowledge,
            run_store=run_store,
            make_context_manager=lambda: _build_context_manager(config, utility),
            cost_ledger=ledger,
        )
        repl.messages = history
        if resume and history:
            console.print(f"[dim]Resumed session {session_id} ({len(history)} messages).[/]\n")

        # Recover any sub-agent runs a crash left 'running' (mirrors the scheduler sweep).
        if run_store is not None:
            for note in await run_store.sweep_orphans():
                console.print(f"[yellow]{note}[/]", markup=False)

        if tasks is not None:
            digest_store = DigestStore(db, store.lock)
            runner = _build_runner(
                config,
                repl,
                store,
                tasks,
                memory,
                knowledge,
                utility,
                console,
                digest_store=digest_store,
            )
            await ensure_digest_task(tasks, config)
            await _scheduler_startup(tasks, runner, console)
            runner.start()

        await repl.run()
    finally:
        # Stop the wake loop before reflecting/closing. stop() awaits any in-flight
        # fire, so a background run completes and is recorded (never a torn write).
        if runner is not None:
            if runner.in_flight:
                console.print(f'finishing task "{runner.in_flight}" before exit…', markup=False)
            await runner.stop()
        # Reflect the current session on exit — but only if it has unreflected content
        # (a resume-and-read with no new turns is already reflected; don't redo it).
        if (
            reflect_on
            and memory is not None
            and utility is not None
            and session_id is not None
            and await store.needs_reflection(session_id)
        ):
            await _reflect_session(
                store, memory, utility, config.models.utility, session_id, console, announce=True
            )
        await db.close()


def _build_runner(
    config: Config,
    repl: Repl,
    store: SessionStore,
    tasks: TaskService,
    memory: MemoryService | None,
    knowledge: KnowledgeService | None,
    utility: AnthropicClient,
    console: Console,
    board: object | None = None,
    digest_store: object | None = None,
    artifacts: object | None = None,
) -> BackgroundRunner:
    """Wire the BackgroundRunner: it fires due tasks, and delegates job execution to
    a JobRunner built from the REPL's already-composed collaborators (same registry,
    executor, gate, client). Both share the REPL's turn lock, so a background run and
    an interactive turn never overlap. ``board`` (a NoticeBoard, UI only) fans notify lines
    out to the browser as well as the console (Phase 9 Task 5). ``digest_store`` enables the
    Daily Digest fire path (Phase 9 Task 7)."""
    job_runner = JobRunner(
        session_store=store,
        client=repl.client,
        registry=repl.registry,
        executor=repl.executor,
        gate=repl.gate,
        config=config,
        memory=memory,
        knowledge=knowledge,
        make_context_manager=lambda: _build_context_manager(config, utility),
    )

    def notify(line: str) -> None:
        # markup off: task titles/payloads are data, not rich markup.
        console.print(line, markup=False)
        if board is not None:  # UI: also surface the line as a browser notice
            board.post(line, kind="task")

    run_digest = None
    if digest_store is not None:

        async def run_digest(task: Task) -> JobOutcome:
            # A fresh builder per fire; deterministic collectors + one tool-less summarize.
            builder = DigestBuilder(
                config=config,
                utility=utility,
                store=digest_store,
                connectors=repl.connectors,
                tasks=tasks,
                knowledge=knowledge,
                notices=board,
                artifacts=artifacts,
                task_id=task.id,
            )
            outcome = await builder.build_and_deliver()
            return JobOutcome(
                session_id=None,
                text=outcome.text,
                error=outcome.error,
                cost_usd=outcome.cost_usd,
            )

    return BackgroundRunner(
        tasks,
        notify=notify,
        run_job=job_runner.run,
        run_digest=run_digest,
        turn_lock=repl.turn_lock,
    )


async def _scheduler_startup(
    tasks: TaskService, runner: BackgroundRunner, console: Console
) -> None:
    """Recover crash orphans, then fire anything already due (catch-up) before the
    first prompt."""
    for note in await tasks.sweep_stale_runs():
        console.print(f"[yellow]{note}[/]", markup=False)
    handled = await runner.check_due()
    if handled:
        console.print(f"[dim]handled {handled} due task(s) on startup — see `tasks`.[/]")


# --- voice interface (Phase 7) ---------------------------------------------


def build_voice_session(
    config: Config, *, repl: Repl, console: Console
) -> tuple[VoiceSession, PushToTalkListener]:
    """Compose the voice interface from the REPL's already-built collaborators (same
    registry, gate, executor, client, memory, context manager, turn lock) plus a
    voice-specific loop whose approver is the ``VoiceApprover`` → screen escalation.

    The screen is the terminal: the ``TerminalScreenApprover`` reuses the REPL's own
    ``_call_summary`` so a voice-escalated action shows the *exact* preview a typed turn
    would. The renderer wraps the configured TTS (local ``PrintSynthesizer`` or, opted in,
    OpenAI / ElevenLabs) and enforces the TTS-privacy rule; its ``announce_escalation`` is
    what the approver speaks (never the input). One OpenAI key covers both STT and TTS."""
    tts = build_tts(
        config.voice,
        openai_key=config.secrets.openai_api_key,
        elevenlabs_key=config.secrets.elevenlabs_api_key,
        console=console,
    )
    renderer = VoiceRenderer(tts)
    stt = build_stt(config.voice, openai_key=config.secrets.openai_api_key)
    screen = TerminalScreenApprover(console, _call_summary)
    approver = VoiceApprover(screen, on_escalate=renderer.announce_escalation)
    loop = AgentLoop(
        client=repl.client,
        registry=repl.registry,
        executor=repl.executor,
        gate=repl.gate,
        config=config,
        approver=approver,
        system=build_system(
            memory_enabled=repl.memory is not None,
            tasks_enabled=repl.tasks is not None,
            knowledge_enabled=repl.knowledge is not None,
            delegation_enabled=repl.agents is not None,
            voice=True,
        ),
        context_manager=repl.context_manager,
        memory=repl.memory,
        project=repl.projects.current if repl.projects is not None else None,
        add_time_context=repl.tasks is not None,
    )
    session = VoiceSession(
        loop=loop,
        stt=stt,
        output=renderer,
        turn_lock=repl.turn_lock,
        # A3: voice announces the active project at turn start and inherits the process's
        # last-activated project (GLOBAL when none). It never *sets* a project — project
        # writes need on-screen selection first (voice prepares, screen commits).
        project=repl.projects.current if repl.projects is not None else None,
    )
    listener = PushToTalkListener(build_capture(config.voice), session)
    return session, listener


async def run_voice(config: Config, *, console: Console | None = None) -> None:
    """Open the database, wire the same services the REPL uses, and run a push-to-talk
    voice loop. Read-only by default; risky actions escalate to a typed on-screen
    confirmation (never voice-only)."""
    console = console or Console()
    if not config.voice.enabled:
        console.print(
            "[dim]Voice is not enabled. Set voice.enabled: true in settings.yaml "
            "(and install the voice extra: uv sync --extra voice).[/]"
        )
        return
    db = await connect(config.data_dir / "jarvis.db")
    store = SessionStore(db)
    try:
        ledger = _build_cost_ledger(config, db, store.lock)
        utility = _utility_client(config, ledger=ledger)
        memory = _build_memory(config, db, console, utility, store.lock)
        context_manager = _build_context_manager(config, utility)
        session_id = await store.create_session()
        tasks = _build_scheduler(config, db, store, session_id)
        knowledge = _build_knowledge(config, db, store.lock, console, memory)
        run_store = AgentRunStore(db, store.lock) if config.sub_agents.enabled else None
        repl = Repl(
            config,
            console=console,
            store=store,
            session_id=session_id,
            memory=memory,
            context_manager=context_manager,
            tasks=tasks,
            knowledge=knowledge,
            run_store=run_store,
            make_context_manager=lambda: _build_context_manager(config, utility),
            cost_ledger=ledger,
        )
        _session, listener = build_voice_session(config, repl=repl, console=console)
        console.print(
            "[bold cyan]Jarvis voice[/] — press Enter to talk, Ctrl-C to quit. "
            "Risky actions confirm on screen.\n"
        )
        while True:  # push-to-talk: Enter arms one utterance (live; needs a mic)
            await asyncio.to_thread(input, "")
            with contextlib.suppress(KeyboardInterrupt):
                await listener.listen_once()
    finally:
        await db.close()


# --- workstation UI (Phase 8) ----------------------------------------------


def build_ui_app(config: Config, *, repl: Repl, auth=None, artifacts=None):
    """Compose the workstation app from the REPL's already-built collaborators, with the UI
    approver seams swapped in (ADR-0008): the turn loop's approver is the ``UIApprover`` (Gate
    queue), sub-agent ASKs escalate to the UI screen, and the shared turn lock serializes UI
    turns against background jobs. One gate (the REPL's) is shared by the loop, the approver's
    narrow-persist, and the policy read model. Mirrors ``build_voice_session`` — one
    composition, injected approvers. Returns the FastAPI app (with an ``AuthManager`` on
    ``app.state.auth`` whose ``launch_token`` the host prints once)."""
    from jarvis.ui import AuthManager, UiServices, UiSession, make_ui_subagent_approver
    from jarvis.ui.server import create_app
    from jarvis.ui.state import InteractiveModelState

    app = create_app(config, auth=auth or AuthManager(), gate=repl.gate)
    # Phase 15.5: the interactive model selector. The loop reads it via model_override (frozen per
    # turn); a switch is Anthropic-only (private-context pin) and never touches the ModelRegistry
    # routes. Default = config.models.main ⇒ byte-identical until the human picks another model.
    model_state = InteractiveModelState(config.models.main, default_effort=config.limits.effort)
    # Phase 15.6: cost-aware Auto routing (interactive loop ONLY). Default policy = AUTO — Gemini
    # 2.5 Flash-Lite classifies each message and the router picks Gemini Flash (simple) / Sonnet 5
    # (judgment/private) / Opus·Fable (deep), with the private_ok hard gate + fail-closed fallback
    # to Sonnet enforced in jarvis.routing. MANUAL pins model_state. A ledgered client per routable
    # provider (anthropic = repl.client; gemini only when available) keeps ledger attribution
    # correct. REPL / sub-agents / evals get NO router ⇒ byte-identical (self.client + config main).
    from jarvis.models.factory import ClientFactory
    from jarvis.models.providers import ProviderRegistry
    from jarvis.models.roles import ModelRoute
    from jarvis.routing import Classifier, Router, RoutingState

    provider_registry = ProviderRegistry.from_config(config)
    routing_state = RoutingState()  # default AUTO — the cost-aware daily experience
    clients_by_provider: dict[str, object] = {"anthropic": repl.client}
    classifier = None
    if provider_registry.route_allowed("gemini"):
        _gem_client = ClientFactory(config).for_route(
            ModelRoute("gemini", "gemini-2.5-flash", text_only=True)
        )
        if repl.cost_ledger is not None:
            _gem_client = LedgeredClient(
                _gem_client, ledger=repl.cost_ledger, provider="gemini", effort=config.limits.effort
            )
        clients_by_provider["gemini"] = _gem_client
        classifier = Classifier(_gem_client, "gemini-2.5-flash-lite")
    router = Router(
        state=routing_state,
        manual_model=model_state.current,
        manual_effort=model_state.current_effort,
        classifier=classifier,
        is_available=provider_registry.route_allowed,
    )
    app.state.routing = routing_state
    app.state.last_route = None

    def _client_selector(decision):  # RouteDecision → the ledgered client for its provider
        return clients_by_provider.get(decision.provider)

    def _on_route(decision):  # surface the per-turn pick for the UI (Task 6 reads last_route)
        app.state.last_route = decision

    if repl.agents is not None:
        # A child's ASK escalates to the UI screen (the Gate), not the terminal prompt.
        app_approvals = app.state.approvals
        repl.agents.make_approver = lambda gate, aid, title: make_ui_subagent_approver(
            app_approvals, gate, aid, title
        )
    loop = AgentLoop(
        client=repl.client,
        registry=repl.registry,
        executor=repl.executor,
        gate=repl.gate,
        config=config,
        approver=app.state.ui_approver,
        context_manager=repl.context_manager,
        memory=repl.memory,
        project=repl.projects.current if repl.projects is not None else None,
        mode=repl.modes.current,
        model_override=model_state.current,
        effort_override=model_state.current_effort,
        router=router,
        client_selector=_client_selector,
        on_route=_on_route,
        add_time_context=repl.tasks is not None,
        system=build_system(
            memory_enabled=repl.memory is not None,
            tasks_enabled=repl.tasks is not None,
            knowledge_enabled=repl.knowledge is not None,
            delegation_enabled=repl.agents is not None,
        ),
    )
    active_pid = repl.projects.current().project_id if repl.projects is not None else None
    app.state.session = UiSession(
        loop=loop,
        connections=app.state.connections,
        turn_lock=repl.turn_lock,
        ring_buffer_events=config.ui.ring_buffer_events,
        # Phase 10: the UI conversation persists as a real interactive session (its own row,
        # distinct from the REPL's), reusing the REPL's store + shared context manager, and
        # is tagged with the active project so it stays scoped for its life.
        sessions=repl.store,
        context_manager=repl.context_manager,
        project_id=active_pid,
    )
    app.state.projects = repl.projects
    app.state.modes = repl.modes
    app.state.interactive_models = model_state
    run_store = repl.agents.run_store if repl.agents is not None else None
    # Phase 10B: the orchestration store (Studio history/detail read models) exists whenever the
    # DB does; the engine + controller are wired only when delegation (spawn) is available.
    orch_store = (
        OrchestrationStore(repl.store.db, repl.store.lock) if repl.store is not None else None
    )
    from jarvis.persistence.saved_views import SavedViewStore

    views_store = (
        SavedViewStore(repl.store.db, repl.store.lock) if repl.store is not None else None
    )
    app.state.services = UiServices(
        memory=repl.memory,
        tasks=repl.tasks,
        knowledge=repl.knowledge,
        run_store=run_store,
        connectors=repl.connectors,  # Phase 9: Hub + Daily connector status
        sessions=repl.store,  # Phase 10: chats list / search / pin / resume
        projects=repl.projects,  # Phase 10: Projects screen read model
        ledger=repl.cost_ledger,  # Phase 10: Costs + A5 ledger-degraded status
        budgets=repl.budgets,  # Phase 10: Costs screen rollups + limits
        orchestration=orch_store,  # Phase 10B: Studio runs
        artifacts=artifacts,  # Phase 11: Artifacts Library + global search + content route
        views=views_store,  # Phase 11: saved views / smart collections
        intents=repl.intents,  # Phase 12: the outward-write approval queue
        write_journal=repl.write_journal,  # Phase 12: the metadata-only write journal
        graph=GraphStore(repl.store.db, repl.store.lock) if repl.store is not None else None,
        embedder=repl.memory.embedder if repl.memory is not None else None,  # Phase 15 search
        attention=(  # Phase 16: the ONE attention queue (proposals/alerts/reviews)
            AttentionStore(repl.store.db, repl.store.lock) if repl.store is not None else None
        ),
    )
    if repl.agents is not None and orch_store is not None:
        app.state.orchestrator = _build_orchestrator(
            config, repl=repl, app=app, store=orch_store, artifacts=artifacts
        )
    if config.voice.enabled:
        app.state.voice = _build_ui_voice(config, repl=repl, app=app, artifacts=artifacts)
    return app


def _build_orchestrator(config: Config, *, repl: Repl, app, store, artifacts=None):
    """Compose the OrchestrationEngine (Task 13/14) + its UI controller (Task 15). The head
    synthesis/verdict run on a thinking-off Fable client (forced-schema calls need thinking off,
    the utility-client precedent); members run on their per-role model via the shared client.
    The engine gets the ModelRegistry + PricingTable + budget config so the pre-fan-out
    worst-case reservation is live."""
    from jarvis.models import ModelRegistry
    from jarvis.models.providers import ProviderRegistry
    from jarvis.observability.cost import load_pricing
    from jarvis.orchestration import OrchestrationEngine
    from jarvis.ui.orchestration import OrchestrationController

    # One pricing table feeds both provider availability (fail-closed: enabled ∧ key ∧ priced)
    # and the reservation math. ModelRegistry gets the ProviderRegistry so route resolution
    # rejects a disabled/missing-key/unpriced provider (Phase 10C) rather than downgrading.
    pricing = load_pricing(config.root / "config" / "pricing.yaml")
    provider_registry = ProviderRegistry.from_config(config, pricing)
    model_registry = ModelRegistry(config.models.routes, provider_registry=provider_registry)
    engine = OrchestrationEngine(
        spawn=repl.agents.spawn,
        store=store,
        head_client=_utility_client(config, ledger=repl.cost_ledger),  # thinking-off, ledgered
        head_model=model_registry.route("planner").model,  # Fable by default
        turn_lock=repl.turn_lock,  # execution stage serializes against interactive turns
        max_rounds=config.budgets.max_rounds,
        budget=repl.budgets,  # between-stage hard stop + project-monthly gate
        registry=model_registry,  # per-role member models + reservation pricing
        pricing=pricing,
        budgets=config.budgets,  # reservation caps + confirm threshold
        est_iterations=config.sub_agents.max_iterations,
        artifacts=artifacts,  # Phase 11: index each finished run as a DB-backed artifact
    )
    return OrchestrationController(
        engine=engine, connections=app.state.connections, projects=repl.projects
    )


def _build_ui_voice(config: Config, *, repl: Repl, app, artifacts=None):
    """Wire the UI's voice surface: a push-to-talk listener whose risky actions escalate to
    the UI screen (``app.state.ui_screen``, fail-closed), and meeting capture → an unreviewed
    KB source. Reuses the Phase-7 pieces with the workstation as the screen."""
    from jarvis.ui import UiVoice, UiVoiceRenderer
    from jarvis.voice import (
        MeetingCapture,
        PushToTalkListener,
        VoiceApprover,
        VoiceSession,
        build_capture,
        build_playback,
        build_stt,
        build_tts,
    )

    # UiVoice first, so its read-only note_state hook can wire the state machines below.
    voice = UiVoice(
        connections=app.state.connections,
        stt_name=config.voice.stt_provider,
        tts_name=config.voice.tts_provider,
    )
    tts = build_tts(
        config.voice,
        openai_key=config.secrets.openai_api_key,
        elevenlabs_key=config.secrets.elevenlabs_api_key,
    )
    voice.tts = tts  # Phase 15.5: browser TTS playback synthesizes the SAFE caption via this
    # The calm renderer, mirrored to the browser: heard transcript + the SAFE spoken caption
    # (post-privacy). Optional playback plays ONLY those synthesized-from-safe bytes; mid-turn
    # events stay unvoiced/unmirrored — one attention surface.
    renderer = UiVoiceRenderer(tts, app.state.connections, play=build_playback(config.voice))
    stt = build_stt(config.voice, openai_key=config.secrets.openai_api_key)
    # The screen is the workstation (not the terminal): fail-closed, modal-bound.
    approver = VoiceApprover(app.state.ui_screen, on_escalate=renderer.announce_escalation)
    loop = AgentLoop(
        client=repl.client,
        registry=repl.registry,
        executor=repl.executor,
        gate=repl.gate,
        config=config,
        approver=approver,
        context_manager=repl.context_manager,
        memory=repl.memory,
        add_time_context=repl.tasks is not None,
        system=build_system(
            memory_enabled=repl.memory is not None,
            tasks_enabled=repl.tasks is not None,
            knowledge_enabled=repl.knowledge is not None,
            delegation_enabled=repl.agents is not None,
            voice=True,
        ),
    )
    capture = build_capture(config.voice)
    # on_state → note_state streams the read-only state pill (listening/transcribing/thinking/
    # speaking/idle) to the browser — never any content.
    voice_session = VoiceSession(
        loop=loop, stt=stt, output=renderer, turn_lock=repl.turn_lock, on_state=voice.note_state
    )
    voice.listener = PushToTalkListener(capture, voice_session, on_state=voice.note_state)
    voice.capture = capture
    if repl.knowledge is not None:
        voice.meeting = MeetingCapture(
            repl.knowledge, stt, artifacts=artifacts, on_state=voice.note_state
        )
    return voice


async def run_ui(config: Config, *, console: Console | None = None) -> None:
    """Open the database, compose the same services the REPL uses, and serve the workstation
    UI on loopback. Prints the tokened URL once (the token drops from the URL after login).
    Shuts down with REPL parity: the background runner finishes any in-flight job (never a
    torn write) then stops, and the session is reflected on exit."""
    console = console or Console()
    # Make the server self-sufficient for output encoding: force UTF-8 on the console (Windows
    # defaults to cp1252, which crashes on an emoji/em-dash in a log line) and route structured
    # logs to the UTF-8 file. The `jarvis --ui` entry does this too; doing it here means run_ui is
    # safe however it's launched — a Unicode char in a tool input/message can never kill a turn.
    import contextlib as _ctx
    import sys as _sys

    for _stream in (_sys.stdout, _sys.stderr):
        with _ctx.suppress(Exception):
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    with _ctx.suppress(Exception):
        from jarvis.observability import configure_logging
        configure_logging(config.logs_dir)
    if not config.ui.enabled:
        console.print(
            "[dim]The workstation UI is not enabled. Set ui.enabled: true in settings.yaml "
            "(and install the ui extra: uv sync --extra ui).[/]"
        )
        return
    try:
        import uvicorn
    except ModuleNotFoundError:
        console.print("[red]The workstation UI needs the ui extra:[/] uv sync --extra ui")
        return

    db = await connect(config.data_dir / "jarvis.db")
    store = SessionStore(db)
    runner: BackgroundRunner | None = None
    session_id: int | None = None
    utility = memory = None
    try:
        from jarvis.persistence.artifacts import ArtifactStore

        # Phase 11: one ArtifactStore on the shared connection feeds the producer hooks
        # (digest/orchestration/wiki/meeting) + the Artifacts Library / search / content route.
        # managed_roots confine local artifacts to data/artifacts + the wiki/eval subtrees.
        artifacts = ArtifactStore(
            db,
            store.lock,
            data_dir=config.data_dir,
            managed_roots={
                "artifacts": config.data_dir / "artifacts",
                "wiki": config.knowledge_dir / "wiki",  # write_page pages
                "markdown": config.knowledge_dir / "markdown",  # meeting-note markdown
                "evals": config.data_dir / "evals",  # eval reports (T4 lazy hook)
            },
        )
        ledger = _build_cost_ledger(config, db, store.lock)
        utility = _utility_client(config, ledger=ledger)
        memory = _build_memory(config, db, console, utility, store.lock)
        context_manager = _build_context_manager(config, utility)
        session_id = await store.create_session()
        tasks = _build_scheduler(config, db, store, session_id)
        knowledge = _build_knowledge(config, db, store.lock, console, memory, artifacts=artifacts)
        run_store = AgentRunStore(db, store.lock) if config.sub_agents.enabled else None
        repl = Repl(
            config,
            console=console,
            store=store,
            session_id=session_id,
            memory=memory,
            context_manager=context_manager,
            tasks=tasks,
            knowledge=knowledge,
            run_store=run_store,
            make_context_manager=lambda: _build_context_manager(config, utility),
            cost_ledger=ledger,
            artifacts=artifacts,  # Phase 13: lets generate_image register its PNG as an artifact
        )
        from jarvis.ui import AuthManager

        auth = AuthManager()
        app = build_ui_app(config, repl=repl, auth=auth, artifacts=artifacts)

        # Wire the real Playwright driver for the inspect-only browser-QA tool + the screenshot
        # harness, if the `browser` extra is installed. Absent ⇒ the tool keeps its degrading
        # stub (cleanly errors when invoked); never a startup crash.
        from jarvis.services.playwright_driver import install_if_available

        if install_if_available(screenshot_dir=config.data_dir / "screenshots"):
            console.print("[dim]playwright: inspect-only browser QA enabled[/]")

        # Recover any orchestration runs a crash left 'running' (backstop; mirrors the sub-agent
        # / scheduler sweeps). Orchestration runs are only created here in the UI path.
        if app.state.services.orchestration is not None:
            for note in await app.state.services.orchestration.sweep_orphans():
                console.print(f"[yellow]{note}[/]", markup=False)

        # Background job/reminder lines fan out to the browser as notices (Phase 9 Task 5).
        from jarvis.ui.notices import NoticeBoard

        board = NoticeBoard(broadcast=app.state.connections.broadcast)
        app.state.notices = board

        if tasks is not None:  # background runner shares the turn lock (no interleaving)
            digest_store = DigestStore(db, store.lock)
            app.state.digests = digest_store
            app.state.services.digests = digest_store  # Daily's Briefing reads the latest digest

            async def _run_digest_now():
                # "Run digest now" from the UI — a fresh builder, no scheduler task.
                builder = DigestBuilder(
                    config=config,
                    utility=utility,
                    store=digest_store,
                    connectors=repl.connectors,
                    tasks=tasks,
                    knowledge=knowledge,
                    notices=board,
                    artifacts=artifacts,
                )
                return await builder.build_and_deliver()

            app.state.run_digest_now = _run_digest_now
            runner = _build_runner(
                config,
                repl,
                store,
                tasks,
                memory,
                knowledge,
                utility,
                console,
                board=board,
                digest_store=digest_store,
                artifacts=artifacts,
            )
            app.state.runner = runner
            await ensure_digest_task(tasks, config)
            await _scheduler_startup(tasks, runner, console)
            runner.start()

        url = f"http://{config.ui.host}:{config.ui.port}/?token={auth.launch_token}"
        console.print(
            f"\n[bold cyan]Kairo Workstation[/] — open this once "
            f"(the token drops from the URL after login):\n  [underline]{url}[/]\n"
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host=config.ui.host, port=config.ui.port, log_level="warning")
        )
        await server.serve()  # blocks until Ctrl-C / shutdown signal
    finally:
        if runner is not None:
            if runner.in_flight:
                console.print(f'finishing task "{runner.in_flight}" before exit…', markup=False)
            await runner.stop()
        if (
            memory is not None
            and utility is not None
            and session_id is not None
            and await store.needs_reflection(session_id)
        ):
            await _reflect_session(
                store, memory, utility, config.models.utility, session_id, console, announce=True
            )
        await db.close()
