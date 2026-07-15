"""SubAgentService: build and run one scoped child AgentLoop turn (Phase 6, Task 4).

Exercises the runner end-to-end against FakeClient (and a few purpose-built fake
clients for timeout / cancellation / concurrency): events forwarded to the parent
sink, child transcript persisted as kind='subagent', agent_runs row completed with
both trace ids, framed report, scope validation, depth-1 refusal, per-turn spawn cap,
timeout, cancellation, and semaphore concurrency. Keyless.
"""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path

from kira.agents import AgentRunStore, SubAgentService
from kira.agents.service import _IN_SUBAGENT
from kira.config import load_config
from kira.core.client import ToolCall, text_message, tool_use_message
from kira.core.events import SubAgentCompleted, SubAgentEvent
from kira.core.execution import (
    ExecutionContext,
    bind_execution_context,
    current_project_scope,
)
from kira.observability import bind_trace, clear_trace, get_trace_id
from kira.permissions import PermissionGate, Policy
from kira.persistence import SessionStore
from kira.persistence.db import connect
from kira.projects import ProjectStore
from kira.tools import ToolContext, ToolExecutor, ToolRegistry

# --- fake clients ------------------------------------------------------------


class _SleepClient:
    """Sleeps on each create, then returns a final text message (drives timeout)."""

    def __init__(self, delay: float, text: str = "done") -> None:
        self.delay = delay
        self.text = text

    async def create(self, **_kw: object):
        await asyncio.sleep(self.delay)
        return text_message(self.text)


class _HangClient:
    """Blocks forever on create (drives cancellation)."""

    async def create(self, **_kw: object):
        await asyncio.Event().wait()
        return text_message("unreachable")


class _ConcurrencyProbe:
    """Tracks the peak number of concurrent create() calls (drives the semaphore test)."""

    def __init__(self, delay: float = 0.03) -> None:
        self.delay = delay
        self.current = 0
        self.peak = 0

    async def create(self, **_kw: object):
        self.current += 1
        self.peak = max(self.peak, self.current)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.current -= 1
        return text_message("done")


class _ProjectScopeProbe:
    def __init__(self) -> None:
        self.seen: list[int | None] = []

    async def create(self, **_kw: object):
        scope = current_project_scope()
        self.seen.append(scope.project_id if scope is not None else None)
        return text_message("done")


# --- harness -----------------------------------------------------------------


async def _service(
    tmp_path: Path, client: object, *, sub_agents: dict | None = None, make_approver=None
) -> tuple[SubAgentService, AgentRunStore, SessionStore, object]:
    cfg = load_config(root=tmp_path, env_file=None)
    if sub_agents:
        cfg = cfg.model_copy(update={"sub_agents": cfg.sub_agents.model_copy(update=sub_agents)})
    db = await connect(tmp_path / "db.db")
    lock = asyncio.Lock()
    sessions = SessionStore(db, lock)
    runs = AgentRunStore(db, lock)
    registry = ToolRegistry()
    registry.discover("kira.tools.builtin", ToolContext(config=cfg))
    svc = SubAgentService(
        session_store=sessions,
        run_store=runs,
        client=client,  # type: ignore[arg-type]
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        make_approver=make_approver,
    )
    svc.bind(registry=registry)
    return svc, runs, sessions, db


# --- happy path --------------------------------------------------------------


async def test_happy_path_forwards_events_persists_and_frames(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("the answer is 42", encoding="utf-8")
    client = FakeClientTwoStep()
    svc, runs, sessions, db = await _service(tmp_path, client)
    events: list[object] = []
    svc.emit = events.append
    svc.bound_session_id = await sessions.create_session(title="parent")
    try:
        result = await svc.spawn(title="research", prompt="read notes", tools=["read_file"])
        # framed report returned to the parent
        assert not result.is_error
        assert '[sub-agent "research" — ok;' in result.content
        assert "begin sub-agent report" in result.content
        assert "the answer is 42" in result.content  # child's final text is the body

        # events forwarded: child activity wrapped, plus a completion with usage
        wrapped = [e for e in events if isinstance(e, SubAgentEvent)]
        assert wrapped and all(e.agent_id and e.title == "research" for e in wrapped)
        inner_types = {type(e.inner).__name__ for e in wrapped}
        assert "ToolDecision" in inner_types  # the attempts tap is forwarded
        completed = [e for e in events if isinstance(e, SubAgentCompleted)]
        assert len(completed) == 1 and completed[0].status == "ok"

        # audit row completed, both trace ids + parent session recorded
        run = (await runs.list())[0]
        assert run.status == "ok"
        assert run.tools_scope == ["read_file"]
        assert run.child_session_id is not None
        assert run.parent_session_id == svc.bound_session_id
        assert run.iterations >= 1

        # child transcript persisted as a subagent session
        child_msgs = await sessions.load_messages(run.child_session_id)
        assert child_msgs and child_msgs[0]["role"] == "user"
    finally:
        await db.close()


async def test_spawn_inherits_live_workspace_project_provenance(tmp_path: Path) -> None:
    svc, runs, sessions, db = await _service(tmp_path, _SleepClient(0, text="done"))
    try:
        project_id = await ProjectStore(db, runs.lock).create(name="Scoped")
        parent_session_id = await sessions.create_session(title="parent", project_id=project_id)
        with bind_execution_context(
            ExecutionContext(session_id=parent_session_id, project_id=project_id)
        ):
            result = await svc.spawn(title="research", prompt="inspect", tools=["read_file"])

        assert not result.is_error
        run = (await runs.list())[0]
        assert run.project_id == project_id
        child = await sessions.get_meta(run.child_session_id)
        assert child is not None and child.project_id == project_id
    finally:
        await db.close()


async def test_spawn_binds_explicit_project_scope_for_child_tools(tmp_path: Path) -> None:
    client = _ProjectScopeProbe()
    svc, runs, _sessions, db = await _service(tmp_path, client)
    try:
        project_id = await ProjectStore(db, runs.lock).create(name="Scoped child")
        result = await svc.spawn(
            title="scoped",
            prompt="inspect",
            tools=["read_file"],
            project_id=project_id,
        )
        assert not result.is_error
        assert client.seen == [project_id]
        assert current_project_scope() is None
    finally:
        await db.close()


# A two-step child: call read_file, then report. Defined as a class so each test gets
# fresh scripted responses (FakeClient consumes them).
class FakeClientTwoStep:
    def __init__(self) -> None:
        self._responses = [
            tool_use_message([ToolCall("t1", "read_file", {"path": "notes.txt"})]),
            text_message("Read the file; the answer is 42."),
        ]

    async def create(self, **_kw: object):
        return self._responses.pop(0)


# --- scope validation --------------------------------------------------------


async def test_scope_rejects_unspawnable_tools(tmp_path: Path) -> None:
    svc, runs, _sessions, db = await _service(tmp_path, _SleepClient(0))
    try:
        r = await svc.spawn(title="x", prompt="p", tools=["remember"])
        assert r.is_error and "can't be delegated" in r.content
        empty = await svc.spawn(title="x", prompt="p", tools=[])
        assert empty.is_error and "at least one tool" in empty.content
        # no run rows created for pre-flight rejections
        assert await runs.list() == []
    finally:
        await db.close()


# --- depth 1 -----------------------------------------------------------------


async def test_depth_one_refusal(tmp_path: Path) -> None:
    svc, runs, _sessions, db = await _service(tmp_path, _SleepClient(0))
    try:
        token = _IN_SUBAGENT.set(True)  # simulate running inside a child
        try:
            r = await svc.spawn(title="nested", prompt="p", tools=["read_file"])
        finally:
            _IN_SUBAGENT.reset(token)
        assert r.is_error and "depth-1" in r.content
        assert await runs.list() == []
    finally:
        await db.close()


# --- timeout -----------------------------------------------------------------


async def test_timeout_records_and_reports(tmp_path: Path) -> None:
    svc, runs, _sessions, db = await _service(
        tmp_path, _SleepClient(0.5), sub_agents={"timeout_seconds": 0.02}
    )
    try:
        r = await svc.spawn(title="slow", prompt="p", tools=["read_file"])
        assert r.is_error
        assert "timed out" in r.content
        run = (await runs.list())[0]
        assert run.status == "timeout"
        assert run.error and "timed out" in run.error
    finally:
        await db.close()


# --- cancellation ------------------------------------------------------------


async def test_cancellation_records_and_reraises(tmp_path: Path) -> None:
    svc, runs, _sessions, db = await _service(tmp_path, _HangClient())
    try:
        task = asyncio.create_task(svc.spawn(title="hangs", prompt="p", tools=["read_file"]))
        await asyncio.sleep(0.05)  # let it reach the hanging child run (past begin_run)
        task.cancel()
        try:
            await task
            raise AssertionError("spawn should have re-raised CancelledError")
        except asyncio.CancelledError:
            pass
        run = (await runs.list())[0]
        assert run.status == "cancelled"  # recorded despite the cancel (shielded write)
    finally:
        await db.close()


# --- max_iterations ----------------------------------------------------------


async def test_max_iterations_is_error(tmp_path: Path) -> None:
    # A child that always calls a tool hits its (tight) iteration bound.
    class _Looper:
        async def create(self, **_kw: object):
            return tool_use_message([ToolCall("t", "list_dir", {"path": "."})])

    svc, runs, _sessions, db = await _service(tmp_path, _Looper(), sub_agents={"max_iterations": 2})
    try:
        r = await svc.spawn(title="loop", prompt="p", tools=["list_dir"])
        assert r.is_error
        run = (await runs.list())[0]
        assert run.status == "error"
        assert run.error and "max_iterations" in run.error
        assert run.iterations == 2  # the tight child bound, not the parent's 25
    finally:
        await db.close()


# --- trace-id isolation ------------------------------------------------------


async def test_trace_ids_captured_and_parent_context_uncontaminated(tmp_path: Path) -> None:
    svc, runs, sessions, db = await _service(tmp_path, _SleepClient(0, text="ok"))
    try:
        parent_tid = bind_trace("parent-turn-trace")
        # Run spawn in a COPIED context, as asyncio.gather does for each parallel tool.
        ctx = contextvars.copy_context()
        task = asyncio.create_task(
            svc.spawn(title="t", prompt="p", tools=["read_file"]), context=ctx
        )
        await task
        # The parent's own context is untouched: its trace id survives the delegation.
        assert get_trace_id() == parent_tid
        run = (await runs.list())[0]
        assert run.parent_trace_id == parent_tid
        assert run.child_trace_id and run.child_trace_id != parent_tid  # child bound its own
    finally:
        clear_trace()
        await db.close()


# --- semaphore concurrency ---------------------------------------------------


async def test_semaphore_caps_concurrency(tmp_path: Path) -> None:
    probe = _ConcurrencyProbe(delay=0.05)
    svc, _runs, _sessions, db = await _service(tmp_path, probe, sub_agents={"max_parallel": 2})
    try:
        await asyncio.gather(
            *(svc.spawn(title=f"c{i}", prompt="p", tools=["read_file"]) for i in range(3))
        )
        assert probe.peak == 2  # 3 children, cap 2 -> never more than 2 run at once
    finally:
        await db.close()


# --- per-turn spawn cap ------------------------------------------------------


async def _spawn_isolated(svc: SubAgentService, **kwargs: object):
    """Run one spawn in a COPIED context, exactly as the parent loop's gather does for
    each parallel tool call — so the child's own bind_trace() stays in its copy and does
    not leak the parent turn's trace id (the D4 isolation the real system relies on)."""
    ctx = contextvars.copy_context()
    return await asyncio.create_task(svc.spawn(**kwargs), context=ctx)  # type: ignore[arg-type]


async def test_spawn_cap_per_turn_trips_and_resets(tmp_path: Path) -> None:
    svc, runs, _sessions, db = await _service(
        tmp_path, _SleepClient(0, text="ok"), sub_agents={"max_spawn_calls_per_turn": 2}
    )
    try:
        bind_trace("turn-1")
        r1 = await _spawn_isolated(svc, title="a", prompt="p", tools=["read_file"])
        r2 = await _spawn_isolated(svc, title="b", prompt="p", tools=["read_file"])
        assert not r1.is_error and not r2.is_error
        assert svc.at_spawn_cap("turn-1") is True
        r3 = await _spawn_isolated(svc, title="c", prompt="p", tools=["read_file"])
        assert r3.is_error and "spawn cap" in r3.content

        # a new turn (trace) resets the counter
        bind_trace("turn-2")
        assert svc.at_spawn_cap("turn-2") is False
        r4 = await _spawn_isolated(svc, title="d", prompt="p", tools=["read_file"])
        assert not r4.is_error
        # exactly 3 children actually ran (2 in turn-1 + 1 in turn-2); the capped one didn't
        assert len(await runs.list()) == 3
    finally:
        clear_trace()
        await db.close()
