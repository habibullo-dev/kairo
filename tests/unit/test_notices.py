"""NoticeBoard + GET /api/notices (Phase 9 Task 5): background events reach the browser."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.core.execution import ExecutionContext
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.connections import ConnectionManager
from jarvis.ui.notices import NoticeBoard
from jarvis.ui.server import create_app

TOKEN = "tok-GOOD-canary"


# --- NoticeBoard mechanics -------------------------------------------------


def test_ring_bounds_and_tail() -> None:
    board = NoticeBoard(maxlen=3, now=lambda: "t")
    for i in range(5):
        board.post(f"n{i}")
    tail = board.tail(50)
    assert [n["text"] for n in tail] == ["n2", "n3", "n4"]  # oldest dropped
    assert [n["seq"] for n in tail] == [3, 4, 5]  # seq is monotonic, not reset


def test_post_off_loop_does_not_raise_and_skips_broadcast() -> None:
    sent: list[dict] = []

    async def broadcast(msg: dict) -> None:
        sent.append(msg)

    board = NoticeBoard(broadcast=broadcast, now=lambda: "t")
    board.post("hello")  # no running loop ⇒ queued only, never raises
    assert board.tail()[-1]["text"] == "hello"
    assert sent == []


async def test_post_on_loop_broadcasts_once() -> None:
    sent: list[dict] = []

    async def broadcast(msg: dict) -> None:
        sent.append(msg)

    board = NoticeBoard(broadcast=broadcast, now=lambda: "t")
    board.post("job done", kind="task")
    await asyncio.sleep(0)  # let the scheduled broadcast task run
    assert len(sent) == 1
    # Envelope discriminator is "notice"; the notice (with its own kind) is nested, so the
    # notice's kind can't clobber the WS routing key.
    assert sent[0]["kind"] == "notice"
    assert sent[0]["notice"] == {
        "seq": 1,
        "at": "t",
        "kind": "task",
        "text": "job done",
        "project_id": None,
    }


async def test_project_notice_delivery_never_broadcasts_to_a_foreign_workspace() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_json(self, message: dict) -> None:
            self.sent.append(message)

    connections = ConnectionManager(clock=lambda: 0.0)
    socket_a, socket_b = Socket(), Socket()
    a = connections.register(socket_a, owner_session="same-browser")
    b = connections.register(socket_b, owner_session="same-browser")
    connections.bind_workspace(
        a,
        owner_session="same-browser",
        workspace_id="workspace-a",
        context=ExecutionContext(session_id=1, project_id=1),
    )
    connections.bind_workspace(
        b,
        owner_session="same-browser",
        workspace_id="workspace-b",
        context=ExecutionContext(session_id=2, project_id=2),
    )
    board = NoticeBoard(publish=connections.publish_project, now=lambda: "t")
    board.post("project A task body", kind="task", project_id=1)
    await asyncio.sleep(0)
    assert [message["notice"]["text"] for message in socket_a.sent] == ["project A task body"]
    assert socket_b.sent == []
    assert [item["text"] for item in board.tail(project_id=1)] == ["project A task body"]
    assert board.tail(project_id=2) == []


# --- GET /api/notices ------------------------------------------------------


def test_notices_route_returns_tail(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token=TOKEN)
    app = create_app(config, auth=auth)
    board = NoticeBoard(now=lambda: "t")
    board.post("first")
    board.post("second")
    app.state.notices = board

    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/api/notices", headers={"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"})
    assert r.status_code == 200
    texts = [n["text"] for n in r.json()["notices"]]
    assert texts == ["first", "second"]


def test_notices_route_empty_without_board(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token=TOKEN)
    client = TestClient(create_app(config, auth=auth), base_url="http://127.0.0.1")
    r = client.get("/api/notices", headers={"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"})
    assert r.status_code == 200 and r.json() == {"notices": []}


def test_notices_route_requires_session(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    client = TestClient(
        create_app(config, auth=AuthManager(token=TOKEN)), base_url="http://127.0.0.1"
    )
    assert client.get("/api/notices").status_code == 401
