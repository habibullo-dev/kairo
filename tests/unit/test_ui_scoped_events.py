"""Two-live-socket regression coverage for Phase 16.5 event delivery.

Every test socket is simultaneously live and uses the same local auth cookie.  Isolation is
therefore not an artifact of separate browser logins: the exact ExecutionContext is the delivery
selector for turn-adjacent voice, Gate, and orchestration activity.
"""

from __future__ import annotations

import asyncio

from jarvis.core.client import ToolCall
from jarvis.core.execution import ExecutionContext, bind_execution_context
from jarvis.orchestration.context import ContextBundle
from jarvis.permissions.gate import Decision
from jarvis.projects.context import ProjectContext
from jarvis.tools import Permission
from jarvis.ui.approver import ApprovalManager
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.orchestration import OrchestrationController
from jarvis.ui.voice import UiVoice, UiVoiceRenderer
from jarvis.voice import FakeSynthesizer

_A = ExecutionContext(session_id=101, project_id=1)
_B = ExecutionContext(session_id=202, project_id=2)
_ASK = Decision(Permission.ASK, "needs explicit approval")


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


def _socket(
    connections: ConnectionManager, socket: _Socket, *, context: ExecutionContext, workspace: str
):
    conn = connections.register(socket, owner_session="same-local-browser")
    connections.bind_workspace(
        conn,
        owner_session="same-local-browser",
        workspace_id=workspace,
        context=context,
    )
    return conn


class _Engine:
    """A metadata-only lifecycle emitter; no model/network work in this regression."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def check_provider_context(self, _team, _context: ContextBundle) -> None:
        return None

    def estimate(self, _team, _workflow, _context, *, budget_usd=None):
        return None

    async def run(self, **kwargs) -> int:
        self.calls.append(kwargs)
        sink = kwargs["on_event"]
        await sink({"kind": "orchestration_started", "run_id": 77, "team": "backend"})
        await sink({"kind": "orchestration_stage", "run_id": 77, "stage": "council"})
        await sink({"kind": "orchestration_completed", "run_id": 77, "status": "ok"})
        return 77


async def test_two_live_sockets_never_cross_voice_gate_or_orchestration_events() -> None:
    connections = ConnectionManager(clock=lambda: 0.0)
    socket_a, socket_b = _Socket(), _Socket()
    conn_a = _socket(connections, socket_a, context=_A, workspace="a" * 24)
    conn_b = _socket(connections, socket_b, context=_B, workspace="b" * 24)

    # Safe captions + fixed voice state are source-context scoped, even though their callbacks
    # happen before/after the voice turn lock in the real session.
    renderer = UiVoiceRenderer(FakeSynthesizer(), connections)
    voice = UiVoice(connections=connections)
    with bind_execution_context(_A):
        await renderer.on_heard("hello from project A")
        voice.note_state("thinking")
    await asyncio.sleep(0)
    a_voice = [m for m in socket_a.sent if m.get("kind") in {"voice", "voice_state"}]
    assert len(a_voice) == 2
    assert all((m["session_id"], m["project_id"]) == (101, 1) for m in a_voice)
    assert socket_b.sent == []

    # An approval's full payload, nonce, and resolve path all require A's exact live context.
    approvals = ApprovalManager(connections)
    with bind_execution_context(_A):
        approval_task = asyncio.create_task(
            approvals.request(
                ToolCall("call-a", "write_file", {"path": "A-only.txt", "content": "private"}),
                _ASK,
                kind="turn",
                title=None,
                on_always=lambda: None,
            )
        )
    await asyncio.sleep(0)
    (pending,) = approvals.pending_for(_A)
    assert any(m.get("type") == "approval" for m in socket_a.sent)
    assert not any(m.get("type") == "approval" for m in socket_b.sent)
    assert await approvals.mint_nonce(pending.decision_id, conn_b) is None
    nonce = await approvals.mint_nonce(pending.decision_id, conn_a)
    assert nonce is not None
    assert not approvals.resolve(pending.decision_id, nonce, "approve", context=_B)[0]
    assert approvals.resolve(pending.decision_id, nonce, "approve", context=_A)[0]
    assert await approval_task is Permission.ALLOW

    # The orchestration controller captures the launch context once.  Its run lifecycle reaches
    # the initiating socket only, and every envelope carries both durable ids for consumers.
    engine = _Engine()
    controller = OrchestrationController(engine=engine, connections=connections, projects=None)
    body, status = await controller.start(
        team_id="backend",
        workflow_id="implement",
        task="scope this run",
        execution_context=_A,
        project=ProjectContext(project_id=1, name="A", repos=(), system_extra=""),
    )
    assert status == 202 and body["started"] is True
    await controller._task
    assert engine.calls[0]["execution_context"] == _A
    a_runs = [m for m in socket_a.sent if str(m.get("kind", "")).startswith("orchestration_")]
    b_runs = [m for m in socket_b.sent if str(m.get("kind", "")).startswith("orchestration_")]
    assert [m["kind"] for m in a_runs] == [
        "orchestration_started",
        "orchestration_stage",
        "orchestration_completed",
    ]
    assert not b_runs
    assert all((m["session_id"], m["project_id"]) == (101, 1) for m in a_runs)


async def test_gate_nonce_dies_when_its_socket_rebinds_to_another_context() -> None:
    connections = ConnectionManager(clock=lambda: 0.0)
    socket = _Socket()
    conn = _socket(connections, socket, context=_A, workspace="a" * 24)
    approvals = ApprovalManager(connections)
    with bind_execution_context(_A):
        task = asyncio.create_task(
            approvals.request(
                ToolCall("call-a", "write_file", {"path": "A-only.txt"}),
                _ASK,
                kind="turn",
                title=None,
                on_always=lambda: None,
            )
        )
    await asyncio.sleep(0)
    (pending,) = approvals.pending_for(_A)
    nonce = await approvals.mint_nonce(pending.decision_id, conn)
    assert nonce is not None
    connections.update_workspace_context(
        owner_session="same-local-browser", workspace_id="a" * 24, context=_B
    )
    ok, message = approvals.resolve(pending.decision_id, nonce, "approve", context=_A)
    assert not ok and "workspace" in message
    approvals.fail(pending.decision_id)
    assert await task is Permission.DENY
