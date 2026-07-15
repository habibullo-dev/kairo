"""UI turn engine + event stream + emergency stop (Phase 8, Task 4).

Keyless via FakeClient: a turn streams every event (incl. a *denied* tool — the same
denied-visible tap evals rely on) to a bounded ring buffer and to live clients; cancel is
Ctrl-C parity; the emergency stop maps only to existing brakes (turn cancel + runner stop),
adding no capability.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from kira.config import load_config
from kira.core import AgentLoop, FakeClient, build_system, text_message, tool_use_message
from kira.core.client import ToolCall
from kira.core.events import (
    SubAgentCompleted,
    SubAgentEvent,
    TextDelta,
    ToolDecision,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from kira.core.execution import ExecutionContext
from kira.observability.cost import Usage
from kira.permissions import PermissionGate, Policy
from kira.tools import Permission, ToolContext, ToolExecutor, ToolRegistry
from kira.ui.auth import SESSION_COOKIE, AuthManager
from kira.ui.connections import ConnectionManager
from kira.ui.server import create_app
from kira.ui.session import EVENT_SCHEMA_VERSION, UiSession, serialize_event

# --- serialize_event: every type -> versioned JSON --------------------------


def test_serialize_all_event_types() -> None:
    assert serialize_event(TextDelta("hi")) == {
        "schema_version": EVENT_SCHEMA_VERSION,
        "type": "text_delta",
        "text": "hi",
    }
    dec = serialize_event(ToolDecision("run_shell", {"command": "x"}, "ask", "deny"))
    assert dec["type"] == "tool_decision" and dec["resolution"] == "deny"
    st = serialize_event(ToolStarted("t1", "read_file", {"path": "a"}))
    assert st["type"] == "tool_started" and st["id"] == "t1"
    fin = serialize_event(ToolFinished("t1", "read_file", is_error=False, preview="ok"))
    assert fin["type"] == "tool_finished" and fin["is_error"] is False
    tc = serialize_event(TurnCompleted("done", "end_turn"))
    assert tc["type"] == "turn_completed" and tc["stop_reason"] == "end_turn"
    # Sub-agent activity is visible, but a child payload must never reach the parent browser.
    sae = serialize_event(
        SubAgentEvent("a1", "reader", ToolStarted("c1", "read_file", {"path": "SECRET"}))
    )
    assert sae["type"] == "subagent_event" and sae["inner"]["type"] == "tool_started"
    assert sae["inner"] == {
        "schema_version": EVENT_SCHEMA_VERSION,
        "type": "tool_started",
        "name": "read_file",
    }
    drafting = serialize_event(SubAgentEvent("a1", "reader", TextDelta("SECRET CHILD TEXT")))
    assert drafting["inner"] == {"schema_version": EVENT_SCHEMA_VERSION, "type": "text_delta"}
    finished = serialize_event(
        SubAgentEvent("a1", "reader", ToolFinished("c1", "read_file", True, "SECRET PREVIEW"))
    )
    assert finished["inner"] == {
        "schema_version": EVENT_SCHEMA_VERSION,
        "type": "tool_finished",
        "name": "read_file",
        "is_error": True,
    }
    sac = serialize_event(
        SubAgentCompleted("a1", "reader", "ok", Usage(input_tokens=10, output_tokens=5), 0.01)
    )
    assert sac["type"] == "subagent_completed" and sac["usage"]["input_tokens"] == 10


# --- turn engine ------------------------------------------------------------


def _loop(tmp_path: Path, client, approver) -> AgentLoop:
    cfg = load_config(root=tmp_path, env_file=None)
    reg = ToolRegistry()
    reg.discover("kira.tools.builtin", ToolContext(config=cfg))
    return AgentLoop(
        client=client,
        registry=reg,
        executor=ToolExecutor(),
        gate=PermissionGate(Policy(), tmp_path),
        config=cfg,
        approver=approver,
        system=build_system(),
    )


async def _deny(_call, _decision) -> Permission:
    return Permission.DENY


async def _drain(session: UiSession) -> None:
    if session._pushes:
        await asyncio.gather(*list(session._pushes))


async def test_turn_streams_events_to_ring_and_clients(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    cm = ConnectionManager(clock=lambda: 0.0)

    class _WS:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_json(self, m: dict) -> None:
            self.sent.append(m)

    conn = cm.register(_WS(), owner_session="test-auth")
    context = ExecutionContext(session_id=101, project_id=None)
    cm.bind_workspace(
        conn,
        owner_session="test-auth",
        workspace_id="w" * 24,
        context=context,
    )
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "read_file", {"path": "notes.txt"})]),
            text_message("The file says hello."),
        ]
    )
    session = UiSession(loop=_loop(tmp_path, client, _deny), connections=cm)
    session.session_id = context.session_id
    result = await session.handle_text("read notes.txt")
    await _drain(session)
    assert result.text == "The file says hello."
    kinds = [e["type"] for e in session.ring]
    assert "tool_decision" in kinds and "tool_started" in kinds and "tool_finished" in kinds
    pushed = [m for m in conn.ws.sent if m.get("kind") == "event"]
    assert pushed and all(m["session_id"] == 101 and m["project_id"] is None for m in pushed)
    assert session.messages  # conversation accumulated


async def test_denied_tool_is_visible_in_the_stream(tmp_path: Path) -> None:
    # The load-bearing observability property: a tool the gate/approver DENIED still appears
    # (ToolDecision, resolution=deny) — the Trace/Gate never hide a refused call.
    cm = ConnectionManager(clock=lambda: 0.0)
    client = FakeClient(
        [
            tool_use_message([ToolCall("t1", "run_shell", {"command": "rm -rf /"})]),
            text_message("I did not run that."),
        ]
    )
    session = UiSession(loop=_loop(tmp_path, client, _deny), connections=cm)
    await session.handle_text("delete everything")
    decisions = [e for e in session.ring if e["type"] == "tool_decision"]
    assert decisions and decisions[0]["resolution"] == "deny"
    assert not any(e["type"] == "tool_started" for e in session.ring)  # never executed


async def test_ring_buffer_is_bounded(tmp_path: Path) -> None:
    cm = ConnectionManager(clock=lambda: 0.0)
    session = UiSession(
        loop=_loop(tmp_path, FakeClient([]), _deny), connections=cm, ring_buffer_events=3
    )
    for i in range(10):
        session._emit(TextDelta(f"chunk-{i}"))
    await _drain(session)
    assert len(session.ring) == 3  # oldest dropped
    assert session.ring[-1]["text"] == "chunk-9"


async def test_submit_is_one_turn_at_a_time_and_cancel(tmp_path: Path) -> None:
    cm = ConnectionManager(clock=lambda: 0.0)
    # A client that blocks forever on the first create() so the turn stays in flight.
    gate_event = asyncio.Event()

    class _BlockingClient(FakeClient):
        async def create(self, **kw):  # type: ignore[override]
            await gate_event.wait()  # never set ⇒ the turn hangs until cancelled
            return await super().create(**kw)

    session = UiSession(
        loop=_loop(tmp_path, _BlockingClient([text_message("x")]), _deny), connections=cm
    )
    assert session.submit("first") is True
    await asyncio.sleep(0)
    assert session.busy is True
    assert session.submit("second") is False  # one interactive turn at a time
    assert session.cancel() is True
    await asyncio.gather(session._current, return_exceptions=True)
    assert session.busy is False


# --- routes: turn + emergency stop wiring -----------------------------------


class _FakeRunner:
    def __init__(self) -> None:
        self._running = True
        self.in_flight = None
        self.starts = 0
        self.stops = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def stop(self) -> None:
        self._running = False
        self.stops += 1

    def start(self) -> None:
        self._running = True
        self.starts += 1


def _client(tmp_path: Path, *, session=None, runner=None):
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(config, auth=auth, session=session, runner=runner)
    return TestClient(app, base_url="http://127.0.0.1"), app, auth


def _hdr(auth: AuthManager, **extra) -> dict[str, str]:
    return {
        "cookie": f"{SESSION_COOKIE}={auth.mint_session()}",
        "origin": "http://127.0.0.1",
        **extra,
    }


def test_turn_route_503_without_session(tmp_path: Path) -> None:
    client, _app_, auth = _client(tmp_path)
    r = client.post("/api/turn", json={"text": "hi"}, headers=_hdr(auth))
    assert r.status_code == 503


def test_turn_route_400_on_empty(tmp_path: Path) -> None:
    cm = ConnectionManager(clock=lambda: 0.0)
    session = UiSession(loop=_loop(tmp_path, FakeClient([]), _deny), connections=cm)
    client, _app_, auth = _client(tmp_path, session=session)
    r = client.post("/api/turn", json={"text": "   "}, headers=_hdr(auth))
    assert r.status_code == 400


def test_cancel_route_without_session(tmp_path: Path) -> None:
    client, _app_, auth = _client(tmp_path)
    r = client.post("/api/turn/cancel", headers=_hdr(auth))
    assert r.status_code == 200 and r.json()["cancelled"] is False


def test_runner_status_pause_resume(tmp_path: Path) -> None:
    runner = _FakeRunner()
    client, _app_, auth = _client(tmp_path, runner=runner)
    status = client.get("/api/runner", headers={"cookie": _hdr(auth)["cookie"]}).json()
    assert status["runner_available"] is True
    assert status["runner_running"] is True
    assert status["background_busy"] is False
    assert status["global_turn_busy"] is False
    # pause → BackgroundRunner.stop() (finish-in-flight-then-stop)
    paused = client.post("/api/runner/pause", headers=_hdr(auth)).json()
    assert paused["runner_running"] is False and runner.stops == 1
    assert paused["cancelled_turns"] == 0
    # resume → start()
    resumed = client.post("/api/runner/resume", headers=_hdr(auth)).json()
    assert resumed["runner_running"] is True and runner.starts == 1


def test_runner_routes_require_session(tmp_path: Path) -> None:
    client, _app_, _auth = _client(tmp_path, runner=_FakeRunner())
    assert client.get("/api/runner").status_code == 401
    paused = client.post("/api/runner/pause", headers={"origin": "http://127.0.0.1"})
    assert paused.status_code == 401  # no session ⇒ refused (origin ok, session missing)
